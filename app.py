from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode
import secrets as random_secrets
import base64
import hashlib
import hmac
import sqlite3
import time
import os

import requests
from zoneinfo import ZoneInfo

import streamlit as st

from tyre_agents.core import (
    ApprovalQueue,
    ContentDraft,
    Destination,
    InMemoryPublisher,
    SAMPLE_REVIEWS,
    generate_content,
    generate_reply,
    publish_approved,
)
from tyre_agents.canva_oauth import create_pkce_request, exchange_code
from tyre_agents.providers import CanvaAdapter, InstagramAdapter, ProviderError, WhatsAppAdapter
from tyre_agents.gemini_text import GeminiTextGenerator


st.set_page_config(page_title="Malay Tyres command centre", page_icon=":material/tire_repair:", layout="wide")

APP_TIMEZONE = ZoneInfo("Asia/Kolkata")
DESTINATION_LABELS = {
    Destination.GOOGLE_BUSINESS_PROFILE: "Google Business Profile",
    Destination.INSTAGRAM_FEED: "Instagram feed",
    Destination.INSTAGRAM_STORY: "Instagram story",
    Destination.WHATSAPP: "WhatsApp",
}
DESTINATION_OPTIONS = list(DESTINATION_LABELS.values())
LABEL_TO_DESTINATION = {label: destination for destination, label in DESTINATION_LABELS.items()}


def now_label() -> str:
    return datetime.now(APP_TIMEZONE).strftime("%d %b %Y, %I:%M %p")


def secret(name: str, default: str = "") -> str:
    environment_value = os.getenv(name)
    if environment_value:
        return environment_value
    try:
        value = st.secrets.get(name)
        return str(value) if value else default
    except Exception:
        return default


# OAuth state is signed so callbacks work even if Streamlit opens a fresh browser session.
OAUTH_DB_PATH = "/tmp/malay_tyres_oauth.sqlite3"

def oauth_db_init() -> None:
    with sqlite3.connect(OAUTH_DB_PATH, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS oauth_tokens (provider TEXT PRIMARY KEY, access_token TEXT NOT NULL, refresh_token TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL DEFAULT 0)")

def _supabase_token_config() -> tuple[str, str]:
    base_url = secret("SUPABASE_URL").rstrip("/")
    service_key = secret("SUPABASE_SERVICE_ROLE_KEY")
    return base_url, service_key

def oauth_token_save(provider: str, access_token: str, refresh_token: str = "", expires_at: float = 0) -> None:
    # Supabase is the durable store on Streamlit Cloud; /tmp SQLite is a local fallback only.
    base_url, service_key = _supabase_token_config()
    if base_url and service_key:
        try:
            response = requests.post(
                f"{base_url}/rest/v1/oauth_tokens",
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}",
                         "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                json={"provider": provider, "access_token": access_token,
                      "refresh_token": refresh_token, "expires_at": float(expires_at or 0)},
                timeout=15,
            )
            response.raise_for_status()
            return
        except Exception:
            pass
    oauth_db_init()
    with sqlite3.connect(OAUTH_DB_PATH, timeout=10) as conn:
        conn.execute("INSERT OR REPLACE INTO oauth_tokens(provider, access_token, refresh_token, expires_at) VALUES (?, ?, ?, ?)", (provider, access_token, refresh_token, float(expires_at or 0)))

def oauth_token_get(provider: str) -> tuple[str, str, float]:
    base_url, service_key = _supabase_token_config()
    if base_url and service_key:
        try:
            response = requests.get(
                f"{base_url}/rest/v1/oauth_tokens",
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
                params={"provider": f"eq.{provider}", "select": "access_token,refresh_token,expires_at", "limit": "1"},
                timeout=15,
            )
            response.raise_for_status()
            rows = response.json()
            if rows:
                row = rows[0]
                return str(row.get("access_token", "")), str(row.get("refresh_token", "")), float(row.get("expires_at", 0) or 0)
        except Exception:
            pass
    try:
        oauth_db_init()
        with sqlite3.connect(OAUTH_DB_PATH, timeout=10) as conn:
            row = conn.execute("SELECT access_token, refresh_token, expires_at FROM oauth_tokens WHERE provider = ?", (provider,)).fetchone()
        return (str(row[0]), str(row[1]), float(row[2])) if row else ("", "", 0.0)
    except Exception:
        return "", "", 0.0

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def _signed_state(provider: str, signing_secret: str) -> str:
    nonce = _b64url(random_secrets.token_bytes(32))
    signature = _b64url(hmac.new(signing_secret.encode("utf-8"), (provider + ":" + nonce).encode("ascii"), hashlib.sha256).digest())
    return nonce + "." + signature

def _valid_signed_state(provider: str, state: str, signing_secret: str) -> bool:
    try:
        nonce, supplied = state.split(".", 1)
    except ValueError:
        return False
    expected = _b64url(hmac.new(signing_secret.encode("utf-8"), (provider + ":" + nonce).encode("ascii"), hashlib.sha256).digest())
    return hmac.compare_digest(supplied, expected)

def _canva_verifier_from_state(state: str, signing_secret: str) -> str:
    if not _valid_signed_state("canva", state, signing_secret):
        return ""
    nonce = state.split(".", 1)[0]
    return _b64url(hmac.new(signing_secret.encode("utf-8"), ("canva-verifier:" + nonce).encode("ascii"), hashlib.sha256).digest())

# Google Business Profile API configuration. Tokens remain in this Streamlit session.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ACCOUNT_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
GOOGLE_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
GOOGLE_REVIEWS_API = "https://mybusiness.googleapis.com/v4"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/business.manage"


def google_redirect_uri() -> str:
    configured = secret("GOOGLE_REDIRECT_URI")
    if configured:
        return configured
    return "https://malay-tyres-ai-nfwrfxd6jkbugixs4e7npm.streamlit.app/"


def google_connected() -> bool:
    return bool(st.session_state.get("google_access_token") or oauth_token_get("google")[0])

def google_state_secret() -> str:
    return secret("GOOGLE_OAUTH_STATE_SECRET") or secret("GOOGLE_CLIENT_SECRET")

def google_authorization_url() -> str | None:
    client_id = secret("GOOGLE_CLIENT_ID")
    client_secret = secret("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    state = _signed_state("google", google_state_secret())
    params = {
        "client_id": client_id,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def google_api_request(method: str, url: str, **kwargs) -> dict:
    stored_access, stored_refresh, stored_expiry = oauth_token_get("google")
    token = st.session_state.get("google_access_token") or stored_access
    expires_at = float(st.session_state.get("google_token_expires_at", 0) or stored_expiry or 0)
    refresh_token = st.session_state.get("google_refresh_token") or stored_refresh or secret("GOOGLE_REFRESH_TOKEN")
    if expires_at and time.time() >= expires_at - 60 and refresh_token:
        refreshed = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": secret("GOOGLE_CLIENT_ID"),
            "client_secret": secret("GOOGLE_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=30)
        if not refreshed.ok:
            raise ProviderError("Google session expired. Reconnect Google Business Profile.")
        data = refreshed.json()
        token = data["access_token"]
        expires_at = time.time() + int(data.get("expires_in", 3600))
        st.session_state.google_access_token = token
        st.session_state.google_token_expires_at = expires_at
        oauth_token_save("google", token, refresh_token, expires_at)
    if not token:
        raise ProviderError("Connect Google Business Profile first.")
    response = requests.request(method, url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=30, **kwargs)
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        raise ProviderError(f"Google API error ({response.status_code}): {str(detail)[:500]}")
    if response.status_code == 204 or not response.text.strip():
        return {}
    return response.json()


def handle_google_oauth_callback() -> None:
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    oauth_error = st.query_params.get("error")
    if not returned_state:
        return
    state_secret = google_state_secret()
    if not _valid_signed_state("google", str(returned_state), state_secret):
        # A callback for Canva or another provider belongs to its own handler.
        return
    if oauth_error:
        st.session_state.google_oauth_error = f"Google authorization was not completed ({oauth_error}). Start again from Connections."
        st.query_params.clear()
        return
    if not code:
        st.session_state.google_oauth_error = "Google returned without an authorization code. Start again from Connections."
        st.query_params.clear()
        return
    try:
        response = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": secret("GOOGLE_CLIENT_ID"),
            "client_secret": secret("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": google_redirect_uri(),
            "grant_type": "authorization_code",
        }, timeout=30)
        if not response.ok:
            try:
                data = response.json()
                detail = data.get("error_description") or data.get("error") or "Google rejected the token exchange."
            except Exception:
                detail = "Google rejected the token exchange. Check the OAuth client and redirect URI in Google Cloud."
            raise ProviderError(f"Google token exchange failed: {str(detail)[:300]}")
        token_data = response.json()
        access_token = str(token_data.get("access_token", ""))
        if not access_token:
            raise ProviderError("Google did not return an access token. Check the OAuth client configuration.")
        refresh_token = str(token_data.get("refresh_token") or oauth_token_get("google")[1] or "")
        expires_at = time.time() + int(token_data.get("expires_in", 3600))
        st.session_state.google_access_token = access_token
        st.session_state.google_refresh_token = refresh_token
        st.session_state.google_token_expires_at = expires_at
        oauth_token_save("google", access_token, refresh_token, expires_at)
        st.session_state.google_oauth_error = ""
        st.session_state.google_location_name = ""
        st.session_state.google_account_name = ""
        st.session_state.google_location_title = ""
        st.session_state.google_oauth_success = True
    except Exception as exc:
        st.session_state.google_oauth_error = str(exc)
    finally:
        try:
            st.query_params.clear()
        except Exception:
            pass


def google_get_reviews() -> list[dict]:
    account_name = st.session_state.get("google_account_name", "")
    location_name = st.session_state.get("google_location_name", "")
    if not account_name or not location_name:
        accounts_data = google_api_request("GET", f"{GOOGLE_ACCOUNT_API}/accounts", params={"pageSize": 20})
        accounts = accounts_data.get("accounts", [])
        if not accounts:
            raise ProviderError("Google sign-in worked, but no Business Profile accounts were returned. Make sure you signed in with an account that manages Malay Tyres.")
        # Prefer a location/account whose title resembles the business; use first available otherwise.
        selected_account = accounts[0]
        for account in accounts:
            if "malay" in account.get("accountName", "").lower():
                selected_account = account
                break
        account_name = selected_account.get("name", "")
        if not account_name:
            raise ProviderError("Google returned an account without a resource name.")
        locations_data = google_api_request("GET", f"{GOOGLE_INFO_API}/{account_name}/locations", params={"pageSize": 100, "readMask": "name,title,storefrontAddress"})
        locations = locations_data.get("locations", [])
        if not locations:
            raise ProviderError("Google sign-in worked, but no business locations were returned for this account.")
        selected_location = locations[0]
        for location in locations:
            title = location.get("title", "").lower()
            if "malay" in title or "ujjain" in title:
                selected_location = location
                break
        location_name = selected_location.get("name", "")
        st.session_state.google_account_name = account_name
        st.session_state.google_location_name = location_name
        st.session_state.google_location_title = selected_location.get("title", "Malay Tyres")
    # The Reviews API requires the full parent resource, including accounts/{id}.
    # Business Information API returns only locations/{id}, so combine it with the account name.
    url = f"{GOOGLE_REVIEWS_API}/{account_name}/{location_name}/reviews"
    all_reviews = []
    page_token = None
    for _ in range(10):
        params = {"pageSize": 50, "orderBy": "updateTime desc"}
        if page_token:
            params["pageToken"] = page_token
        page = google_api_request("GET", url, params=params)
        all_reviews.extend(page.get("reviews", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    st.session_state.google_last_sync = now_label()
    return all_reviews


def google_rating_value(value: str) -> int:
    return {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}.get(str(value).upper(), 5)


def sync_google_reviews() -> int:
    reviews = google_get_reviews()
    from types import SimpleNamespace
    synced = {}
    review_names = {}
    for item in reviews:
        reviewer = item.get("reviewer", {}) or {}
        review_name = item.get("name", "")
        if not review_name:
            continue
        review_id = review_name.split("/")[-1]
        customer_name = reviewer.get("displayName") or "Customer"
        rating = google_rating_value(item.get("starRating", "FIVE"))
        review = SimpleNamespace(
            review_id=review_id,
            customer_name=customer_name,
            rating=rating,
            text=item.get("comment") or "(Rating-only review)",
        )
        prior = st.session_state.get("reviews", {}).get(review_id, {})
        existing_reply = item.get("reviewReply", {}).get("comment", "")
        if existing_reply:
            reply_text = existing_reply
            status = "Published"
        elif prior and prior.get("status") in ("Ready to review", "Approved for publishing"):
            reply_text = prior.get("reply", generate_review_reply(review))
            status = prior.get("status", "Ready to review")
        else:
            reply_text = generate_review_reply(review)
            status = "Ready to review"
        synced[review_id] = {"review": review, "reply": reply_text, "status": status}
        review_names[review_id] = review_name
    st.session_state.reviews = synced
    st.session_state.google_review_names = review_names
    st.session_state.connections["Google Business Profile"] = True
    return len(synced)


def publish_google_review_reply(review_id: str, reply: str) -> None:
    review_name = st.session_state.get("google_review_names", {}).get(review_id)
    if not review_name:
        raise ProviderError("This review has no live Google review ID. Sync Google reviews again.")
    google_api_request("PUT", f"{GOOGLE_REVIEWS_API}/{review_name}/reply", json={"comment": reply})


def canva_access_token() -> str:
    stored_access, stored_refresh, stored_expiry = oauth_token_get("canva")
    token = str(st.session_state.get("canva_access_token") or stored_access or secret("CANVA_ACCESS_TOKEN"))
    refresh_token = str(st.session_state.get("canva_refresh_token") or stored_refresh or secret("CANVA_REFRESH_TOKEN"))
    expires_at = float(st.session_state.get("canva_token_expires_at", 0) or stored_expiry or 0)
    if token and expires_at and time.time() >= expires_at - 60:
        if not refresh_token:
            return ""
        client_id = secret("CANVA_CLIENT_ID")
        client_secret = secret("CANVA_CLIENT_SECRET")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        response = requests.post(
            "https://api.canva.com/rest/v1/oauth/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=30,
        )
        if not response.ok:
            return ""
        data = response.json()
        token = str(data.get("access_token", ""))
        refresh_token = str(data.get("refresh_token") or refresh_token)
        expires_at = time.time() + int(data.get("expires_in", 3600))
        st.session_state.canva_access_token = token
        st.session_state.canva_refresh_token = refresh_token
        st.session_state.canva_token_expires_at = expires_at
        oauth_token_save("canva", token, refresh_token, expires_at)
    return token


def gemini_api_key() -> str:
    return secret("GEMINI_API_KEY")


def generate_review_reply(review) -> str:
    """Write a service-specific reply without ever using the reviewer's name."""
    review_text = str(getattr(review, "text", "") or "").strip()
    rating = int(getattr(review, "rating", 5) or 5)
    lowered = review_text.casefold()

    # Use the language model when configured, with strict grounding in the review.
    api_key = gemini_api_key()
    if api_key and review_text and review_text != "(Rating-only review)":
        try:
            model = secret("GEMINI_MODEL", "gemini-2.5-flash")
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            prompt = (
                "Write one short, polite Google Business Profile owner reply for Malay Tyres, Ujjain. "
                "CRITICAL RULES: Never use, repeat, or infer the reviewer's name. Start exactly with 'Dear Sir/Ma’am,'. "
                "Mention the exact service the reviewer actually mentions (examples: wheel alignment, Teflon coating, "
                "wheel balancing, tyre rotation, tyre inspection, car washing, interior cleaning, headlight cleaning, "
                "rim work, engine work, suspension work, brake work, general mechanical work). Do not mention tyres "
                "or any service that the review does not support. If no service is named, thank them generally without "
                "guessing a service. Match the sentiment and rating; for negative feedback, apologize and invite them "
                "to contact the shop to resolve it without arguing. Keep it natural, 2 sentences maximum, no hashtags.\n\n"
                f"Star rating: {rating}/5\nReview text: {review_text}"
            )
            response = requests.post(
                endpoint, params={"key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {"temperature": 0.25, "maxOutputTokens": 160}},
                timeout=20,
            )
            if response.ok:
                data = response.json()
                candidates = data.get("candidates", [])
                text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])) if candidates else ""
                text = text.strip().strip('"')
                # Defensively strip any accidental name-like greeting or wrong greeting.
                if text:
                    if text.casefold().startswith(("dear sir", "dear ma'am", "dear madam", "hello", "hi ")):
                        text = text.split("\n", 1)[-1].strip() if text.casefold().startswith(("hello", "hi ")) else text
                    if not text.casefold().startswith(("dear sir", "dear ma’am", "dear ma'am")):
                        text = "Dear Sir/Ma’am,\n\n" + text
                    return text
        except Exception:
            pass

    # Reliable fallback: only mention a service when review wording identifies it.
    service_patterns = [
        (("alignment", "wheel align", "wheel-alignment"), "wheel alignment"),
        (("teflon",), "Teflon coating"),
        (("balancing", "wheel balance", "wheel-balancing"), "wheel balancing"),
        (("rotation", "tyre rotate", "tire rotate"), "tyre rotation"),
        (("inspection", "check-up", "checkup"), "vehicle inspection"),
        (("car wash", "washing", "washed my car"), "car washing"),
        (("interior cleaning", "interior clean", "car interior"), "interior cleaning"),
        (("headlight", "head light"), "headlight cleaning"),
        (("rim repair", "rim work", "alloy repair", "rim through"), "rim work"),
        (("suspension",), "suspension work"),
        (("brake",), "brake work"),
        (("engine",), "engine work"),
        (("mechanical", "general work", "repair work"), "mechanical work"),
        (("tyre", "tire"), "tyre service"),
    ]
    service = next((label for terms, label in service_patterns if any(term in lowered for term in terms)), "")
    if rating >= 4:
        if service:
            return f"Dear Sir/Ma’am, thank you for your kind feedback about our {service}. We truly appreciate your trust in Malay Tyres and look forward to serving you again."
        return "Dear Sir/Ma’am, thank you for your valuable feedback and support. We truly appreciate your trust in Malay Tyres and look forward to serving you again."
    if rating == 3:
        if service:
            return f"Dear Sir/Ma’am, thank you for sharing your feedback about our {service}. We’ll use your comments to improve and would appreciate the opportunity to serve you better."
        return "Dear Sir/Ma’am, thank you for your feedback. We’ll use your comments to improve and would appreciate the opportunity to serve you better."
    if service:
        return f"Dear Sir/Ma’am, we’re sorry your experience with our {service} did not meet expectations. Please contact Malay Tyres so we can understand the issue and work to make it right."
    return "Dear Sir/Ma’am, we’re sorry your experience did not meet expectations. Please contact Malay Tyres so we can understand the issue and work to make it right."


def canva_redirect_uri() -> str:
    configured = secret("CANVA_REDIRECT_URI")
    if configured:
        return configured
    return "http://localhost:8501"


def canva_state_secret() -> str:
    return secret("CANVA_OAUTH_STATE_SECRET") or secret("CANVA_CLIENT_SECRET")

def handle_canva_oauth_callback() -> None:
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    oauth_error = st.query_params.get("error")
    if not returned_state:
        return
    state_secret = canva_state_secret()
    verifier = _canva_verifier_from_state(str(returned_state), state_secret)
    if not verifier:
        # Not this provider's callback, so let the Google handler process its own state.
        return
    if oauth_error:
        st.session_state.canva_oauth_error = f"Canva authorization was not completed ({oauth_error}). Start again from Connections."
        st.query_params.clear()
        return
    if not code:
        st.session_state.canva_oauth_error = "Canva returned without an authorization code. Start again from Connections."
        st.query_params.clear()
        return
    try:
        client_id = secret("CANVA_CLIENT_ID")
        client_secret = secret("CANVA_CLIENT_SECRET")
        redirect_uri = canva_redirect_uri()
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        response = requests.post(
            "https://api.canva.com/rest/v1/oauth/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
            timeout=30,
        )
        if not response.ok:
            try:
                detail = response.json().get("error") or "Canva rejected the token exchange."
            except Exception:
                detail = "Canva rejected the token exchange. Check the OAuth client and redirect URI."
            raise ProviderError(f"Canva token exchange failed: {str(detail)[:250]}")
        token_data = response.json()
        access_token = str(token_data.get("access_token", ""))
        if not access_token:
            raise ProviderError("Canva did not return an access token. Check Canva app settings and scopes.")
        refresh_token = str(token_data.get("refresh_token", ""))
        expires_at = time.time() + int(token_data.get("expires_in", 3600))
        st.session_state.canva_access_token = access_token
        st.session_state.canva_refresh_token = refresh_token
        st.session_state.canva_token_expires_at = expires_at
        oauth_token_save("canva", access_token, refresh_token, expires_at)
        st.session_state.canva_oauth_error = ""
    except Exception as exc:
        st.session_state.canva_oauth_error = str(exc)
    finally:
        try:
            st.query_params.clear()
        except Exception:
            pass

def canva_authorization_url() -> str | None:
    client_id = secret("CANVA_CLIENT_ID")
    client_secret = secret("CANVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    state_secret = canva_state_secret()
    state = _signed_state("canva", state_secret)
    nonce = state.split(".", 1)[0]
    verifier = _b64url(hmac.new(state_secret.encode("utf-8"), ("canva-verifier:" + nonce).encode("ascii"), hashlib.sha256).digest())
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    scopes = secret("CANVA_SCOPES", "brandtemplate:content:read brandtemplate:content:write brandtemplate:meta:read design:content:read design:content:write design:meta:read asset:read asset:write profile:read")
    params = {"code_challenge_method": "s256", "response_type": "code", "client_id": client_id, "redirect_uri": canva_redirect_uri(), "scope": scopes, "state": state, "code_challenge": challenge}
    return "https://www.canva.com/api/oauth/authorize?" + urlencode(params)


def canva_ready() -> bool:
    return bool(canva_access_token() and secret("CANVA_BRAND_TEMPLATE_ID"))


def instagram_ready() -> bool:
    return bool(secret("INSTAGRAM_ACCESS_TOKEN") and secret("INSTAGRAM_BUSINESS_ACCOUNT_ID"))


def whatsapp_ready() -> bool:
    return bool(
        secret("WHATSAPP_ACCESS_TOKEN")
        and secret("WHATSAPP_PHONE_NUMBER_ID")
        and secret("WHATSAPP_RECIPIENT_PHONE")
    )


def generate_canva_design(draft: ContentDraft) -> dict:
    field_title = secret("CANVA_TITLE_FIELD", "TITLE")
    field_body = secret("CANVA_BODY_FIELD", "BODY")
    field_cta = secret("CANVA_CTA_FIELD", "CTA")
    adapter = CanvaAdapter(
        access_token=canva_access_token(),
        brand_template_id=secret("CANVA_BRAND_TEMPLATE_ID"),
    )
    return adapter.autofill_export(
        {
            field_title: draft.title,
            field_body: draft.body,
            field_cta: secret("CANVA_CTA_TEXT", "Book your tyre check today"),
        },
        title=draft.title,
    )


def generate_campaign_draft(topic: str, tone: str, draft_id: str, destinations: tuple[Destination, ...]) -> tuple[ContentDraft, str]:
    """Prefer Gemini for copy, with a deterministic fallback for offline trials."""
    if gemini_api_key():
        campaign = GeminiTextGenerator(gemini_api_key()).generate(f"{topic.strip()} (tone: {tone})")
        return ContentDraft(
            draft_id=draft_id,
            title=campaign.title,
            body=campaign.body,
            image_prompt=campaign.image_prompt,
            destinations=destinations,
        ), "Gemini"
    fallback = generate_content(topic, draft_id=draft_id)
    return ContentDraft(
        draft_id=fallback.draft_id,
        title=f"{fallback.title} · {tone}",
        body=fallback.body,
        image_prompt=fallback.image_prompt,
        destinations=destinations,
    ), "offline fallback"


def publish_live(draft: ContentDraft, design: dict) -> tuple[dict, ...]:
    image_url = design.get("image_url") if design else None
    if not image_url:
        raise ProviderError("Generate the Canva design before publishing")
    receipts: list[dict] = []
    for destination in draft.destinations:
        if destination == Destination.INSTAGRAM_FEED:
            if not instagram_ready():
                raise ProviderError("Instagram credentials are not configured")
            receipts.append(
                InstagramAdapter(
                    access_token=secret("INSTAGRAM_ACCESS_TOKEN"),
                    instagram_business_account_id=secret("INSTAGRAM_BUSINESS_ACCOUNT_ID"),
                    api_version=secret("META_GRAPH_API_VERSION", "v21.0"),
                ).publish_image(image_url, draft.body)
            )
        elif destination == Destination.INSTAGRAM_STORY:
            if not instagram_ready():
                raise ProviderError("Instagram credentials are not configured")
            receipts.append(
                InstagramAdapter(
                    access_token=secret("INSTAGRAM_ACCESS_TOKEN"),
                    instagram_business_account_id=secret("INSTAGRAM_BUSINESS_ACCOUNT_ID"),
                    api_version=secret("META_GRAPH_API_VERSION", "v21.0"),
                ).publish_story(image_url)
            )
        elif destination == Destination.WHATSAPP:
            if not whatsapp_ready():
                raise ProviderError("WhatsApp credentials and recipient phone are not configured")
            receipts.append(
                WhatsAppAdapter(
                    access_token=secret("WHATSAPP_ACCESS_TOKEN"),
                    phone_number_id=secret("WHATSAPP_PHONE_NUMBER_ID"),
                    recipient_phone_number=secret("WHATSAPP_RECIPIENT_PHONE"),
                    api_version=secret("META_GRAPH_API_VERSION", "v21.0"),
                ).send_image(image_url, draft.body)
            )
        elif destination == Destination.GOOGLE_BUSINESS_PROFILE:
            raise ProviderError("Google Business Profile content-post publishing is not enabled in this connector yet. Review replies are supported after Google is connected.")
    return tuple(receipts)


def initialize_state() -> None:
    if "reviews" not in st.session_state:
        st.session_state.reviews = {
            review.review_id: {
                "review": review,
                "reply": generate_review_reply(review),
                "status": "Ready to review",
            }
            for review in SAMPLE_REVIEWS
        }
    st.session_state.setdefault("queue", ApprovalQueue())
    st.session_state.setdefault("draft", None)
    st.session_state.setdefault("generated_design", None)
    st.session_state.setdefault("design_error", None)
    st.session_state.setdefault("canva_access_token", "")
    st.session_state.setdefault("canva_refresh_token", "")
    st.session_state.setdefault("canva_oauth_error", "")
    st.session_state.setdefault("canva_last_code", "")
    st.session_state.setdefault("google_review_names", {})
    st.session_state.setdefault("google_oauth_error", "")
    st.session_state.setdefault("publisher", InMemoryPublisher())
    st.session_state.setdefault("publishing_log", [])
    st.session_state.setdefault("activity_log", [
        ("Review agent", "3 new reviews prepared for your approval", "Today"),
        ("Content agent", "Tyre pressure campaign is ready to draft", "Today"),
        ("Safety gate", "Publishing remains locked until you approve", "Today"),
    ])
    st.session_state.setdefault("review_run_at", "Today at 09:00 AM")
    st.session_state.setdefault("connections", {
        "Google Business Profile": False,
        "Gemini / Antigravity": False,
        "Canva": False,
        "Instagram + WhatsApp": False,
    })


def add_activity(agent: str, message: str) -> None:
    st.session_state.activity_log.insert(0, (agent, message, "Just now"))
    st.session_state.activity_log = st.session_state.activity_log[:6]


def badge(label: str, tone: str = "gray") -> None:
    st.badge(label, color=tone)


def render_header() -> None:
    st.title("Malay Tyres command centre")
    st.caption("A human-approved AI workspace for reviews, content and social publishing")

    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown("**AUTOMATION STATUS**")
        badge("Live Google connected" if google_connected() else "Google not connected", "green" if google_connected() else "blue")
        badge("Approval gate on", "green")
        st.caption(f"Last review check · {st.session_state.review_run_at}")


def render_sidebar() -> str:
    with st.sidebar:
        st.markdown("## :material/tire_repair: Malay Tyres")
        st.caption("AI operations workspace")
        view = st.segmented_control(
            "Workspace",
            ["Overview", "Reviews", "Content studio", "Connections"],
            default="Overview",
            key="workspace_view",
        )
        st.divider()
        st.markdown("**Today’s runbook**")
        st.markdown(":material/check_circle: Review new GBP feedback")
        st.markdown(":material/edit_note: Prepare one fresh campaign")
        st.markdown(":material/verified: Wait for your approval")
        st.markdown(":material/send: Publish only to selected channels")
        st.divider()
        st.caption("Nothing is posted by this demo until the final action is approved.")
    return view or "Overview"


def render_metric_row() -> None:
    pending_reviews = sum(item["status"] != "Published" for item in st.session_state.reviews.values())
    draft = st.session_state.draft
    pending_draft = int(draft is not None and not draft.approved)
    published = len(st.session_state.publishing_log)
    cols = st.columns(4)
    cols[0].metric("Reviews to handle", str(pending_reviews), "Daily check")
    cols[1].metric("Content awaiting approval", str(pending_draft), "Human gate")
    cols[2].metric("Published this session", str(published), "Demo receipts")
    ready = int(gemini_api_key() != "") + int(canva_ready()) + int(instagram_ready()) + int(whatsapp_ready())
    cols[3].metric("Connections ready", f"{ready}/4", "Gemini + Canva + Meta")


def render_overview() -> None:
    render_metric_row()
    st.space("small")
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        with st.container(border=True):
            st.subheader("Your agent team")
            agents = [
                ("Review agent", "Checks GBP reviews, writes a distinct polite reply, and waits for approval.", "3 ready", "green"),
                ("Content agent", "Creates a campaign caption and a branded Canva design from your template.", "Draft on demand", "blue"),
                ("Publishing agent", "Routes approved content to exactly the destinations you select.", "Approval locked", "orange"),
            ]
            for name, description, status, tone in agents:
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f"**{name}**")
                    st.caption(description)
                    badge(status, tone)
    with right:
        with st.container(border=True):
            st.subheader("Recent activity")
            for agent, message, when in st.session_state.activity_log:
                st.markdown(f"**{agent}** · {when}")
                st.caption(message)
            st.button("Open review inbox", icon=":material/arrow_forward:", on_click=lambda: st.session_state.update(workspace_view="Reviews"), width="stretch")

    st.space("small")
    with st.container(border=True):
        st.subheader("Daily review check")
        st.write("The review agent is prepared to check Google Business Profile every day at 09:00 Asia/Kolkata.")
        st.caption("This local build shows the workflow. Connect a scheduler and the real GBP API before using it for unattended posting.")
        if st.button("Run review check now", type="primary", icon=":material/refresh:"):
            st.session_state.review_run_at = now_label()
            for item in st.session_state.reviews.values():
                item["reply"] = generate_review_reply(item["review"])
                item["status"] = "Ready to review"
            add_activity("Review agent", "Checked sample GBP reviews and refreshed polite replies")
            st.success("Review check completed. Replies are ready for your approval.", icon=":material/check_circle:")


def render_review_card(review_id: str, item: dict) -> None:
    review = item["review"]
    with st.container(border=True):
        top_left, top_right = st.columns([3, 1], vertical_alignment="center")
        with top_left:
            st.markdown(f"**{review.customer_name}**  ·  {'★' * review.rating}{'☆' * (5 - review.rating)}")
            st.caption(f"Google review ID: {review.review_id}")
        with top_right:
            badge(item["status"], "green" if item["status"] == "Published" else "orange")
        st.write(f'“{review.text}”')
        reply_key = f"reply_{review_id}"
        edited_reply = st.text_area(
            "Suggested reply",
            value=item["reply"],
            key=reply_key,
            height=90,
            help="Edit this reply before sending it to Google Business Profile.",
        )
        buttons = st.container(horizontal=True, vertical_alignment="center")
        with buttons:
            if st.button("Save reply", key=f"save_{review_id}", icon=":material/save:"):
                item["reply"] = edited_reply
                item["status"] = "Approved for publishing"
                add_activity("Review agent", f"Saved a reply for {review.customer_name}")
                st.success("Reply approved and saved. It will not be sent until you click Publish approved reply to Google.")
            if st.button("Publish approved reply to Google", key=f"publish_review_{review_id}", disabled=item["status"] != "Approved for publishing" or not google_connected(), icon=":material/send:"):
                try:
                    publish_google_review_reply(review_id, edited_reply.strip())
                    item["reply"] = edited_reply.strip()
                    item["status"] = "Published"
                    add_activity("Publishing agent", f"Published an approved reply for {review.customer_name} to Google Business Profile")
                    st.success("Approved reply published to Google Business Profile.")
                except Exception as exc:
                    st.error(f"Google reply failed: {exc}")


def render_reviews() -> None:
    st.header("Google Business Profile review inbox")
    st.write("Real Google reviews are loaded here. Replies remain drafts until you save and explicitly publish each one.")
    if google_connected():
        st.success(f"Google Business Profile connected · {st.session_state.get('google_location_title', 'Business location')}")
        if st.button("Sync real Google reviews", type="primary", icon=":material/refresh:"):
            try:
                count = sync_google_reviews()
                add_activity("Google Business Profile", f"Synced {count} real reviews from Google")
                st.success(f"Synced {count} reviews from Google.")
                st.rerun()
            except Exception as exc:
                st.error(f"Review sync failed: {exc}")
    else:
        st.warning("Google is not connected yet. Open Connections and click Connect Google Business Profile.")
    if st.button("Refresh suggested replies", icon=":material/auto_awesome:"):
        for item in st.session_state.reviews.values():
            if item.get("status") != "Published":
                item["reply"] = generate_review_reply(item["review"])
                item["status"] = "Ready to review"
        add_activity("Review agent", "Refreshed suggested replies without publishing")
        st.rerun()
    for review_id, item in st.session_state.reviews.items():
        render_review_card(review_id, item)


def selected_destinations_from_labels(labels: list[str]) -> tuple[Destination, ...]:
    return tuple(LABEL_TO_DESTINATION[label] for label in labels)


def render_draft_preview(draft: ContentDraft) -> None:
    with st.container(border=True):
        st.subheader("Draft preview")
        badge("Approved" if draft.approved else "Awaiting approval", "green" if draft.approved else "orange")
        st.markdown(f"### {draft.title}")
        st.write(draft.body)
        st.markdown("**Canva design brief**")
        st.code(draft.image_prompt, language="text")
        st.markdown("**Publish to**")
        st.write(" · ".join(DESTINATION_LABELS[destination] for destination in draft.destinations) or "No channel selected")
        st.caption("The design brief is retained as an audit trail. Canva creates the branded visual automatically from your configured brand template.")


def render_generated_design(draft: ContentDraft) -> None:
    with st.container(border=True):
        st.subheader("Canva-generated visual")
        generated_design = st.session_state.generated_design
        if generated_design is not None:
            st.image(
                generated_design["image_url"],
                caption="Generated automatically from your Canva brand template",
                alt="Canva-generated Malay Tyres social media creative",
                width="stretch",
            )
            if generated_design.get("design_url"):
                st.link_button("Open and edit in Canva", generated_design["design_url"], icon=":material/edit:", width="stretch")
            st.caption("Canva export URLs expire after 24 hours. Publish approved content during this trial window.")
        elif st.session_state.design_error:
            st.warning(st.session_state.design_error, icon=":material/warning:")
            st.caption("Add CANVA_ACCESS_TOKEN and CANVA_BRAND_TEMPLATE_ID to Streamlit secrets, then submit the brief again.")
        else:
            st.info("Canva will generate the branded visual when you submit a brief.", icon=":material/image:")


def render_content_studio() -> None:
    st.header("Content studio")
    st.write("Create a fresh campaign, prepare its image prompt, choose where it should go, then approve before publishing.")

    with st.container(border=True):
        st.subheader("1. Brief the content agent")
        with st.form("content_brief"):
            topic = st.text_input("Campaign topic", value="tyre pressure checks before a long drive")
            tone = st.selectbox("Tone", ["Friendly and trustworthy", "Educational", "Seasonal offer", "Premium service"])
            destinations = st.multiselect(
                "Destinations",
                DESTINATION_OPTIONS,
                default=["Instagram feed", "WhatsApp"],
                help="Select Instagram story when you want the image formatted for a story placement.",
            )
            submitted = st.form_submit_button("Generate content + Canva design", type="primary", icon=":material/auto_awesome:", width="stretch")
        if submitted:
            draft_id = f"draft-{len(st.session_state.activity_log) + 1}"
            try:
                draft, copy_provider = generate_campaign_draft(
                    topic,
                    tone,
                    draft_id,
                    selected_destinations_from_labels(destinations),
                )
            except ValueError as exc:
                st.warning(f"Gemini copy generation failed: {exc}. Using the offline fallback.", icon=":material/warning:")
                fallback = generate_content(topic, draft_id=draft_id)
                draft = ContentDraft(
                    draft_id=fallback.draft_id,
                    title=f"{fallback.title} · {tone}",
                    body=fallback.body,
                    image_prompt=fallback.image_prompt,
                    destinations=selected_destinations_from_labels(destinations),
                )
                copy_provider = "offline fallback"
            except Exception:
                st.warning("Gemini copy generation was unavailable. Using the offline fallback.", icon=":material/warning:")
                fallback = generate_content(topic, draft_id=draft_id)
                draft = ContentDraft(
                    draft_id=fallback.draft_id,
                    title=f"{fallback.title} · {tone}",
                    body=fallback.body,
                    image_prompt=fallback.image_prompt,
                    destinations=selected_destinations_from_labels(destinations),
                )
                copy_provider = "offline fallback"
            st.session_state.queue.add(draft)
            st.session_state.draft = draft
            st.session_state.generated_design = None
            st.session_state.design_error = None
            add_activity("Content agent", f"Generated copy with {copy_provider} and a Canva design brief for {topic}")
            if canva_ready():
                try:
                    with st.spinner("Creating the branded visual automatically in Canva..."):
                        st.session_state.generated_design = generate_canva_design(draft)
                    add_activity("Canva agent", "Created and exported a branded visual from the Canva template")
                    st.success("Draft and Canva design created automatically. Review both below.", icon=":material/check_circle:")
                except ProviderError as exc:
                    st.session_state.design_error = str(exc)
                    st.warning("The copy was created, but Canva did not return a design.", icon=":material/warning:")
            else:
                st.session_state.design_error = "Canva is not configured yet. Add CANVA_ACCESS_TOKEN and CANVA_BRAND_TEMPLATE_ID to create the design automatically."
                st.warning("The copy was created. Connect Canva to generate the visual automatically.", icon=":material/warning:")

    draft = st.session_state.draft
    if draft is None:
        st.info("No draft yet. Start with a campaign brief above.", icon=":material/edit_note:")
        return

    st.space("small")
    render_draft_preview(draft)
    render_generated_design(draft)
    with st.container(border=True):
        st.subheader("2. Review and approve")
        edited_title = st.text_input("Title", value=draft.title, key="draft_title")
        edited_body = st.text_area("Caption", value=draft.body, key="draft_body", height=120)
        edited_prompt = st.text_area("Image prompt", value=draft.image_prompt, key="draft_prompt", height=110)
        action_col, approval_col = st.columns([1, 1], vertical_alignment="bottom")
        with action_col:
            if st.button("Save edits", icon=":material/save:", width="stretch"):
                updated = ContentDraft(draft.draft_id, edited_title, edited_body, edited_prompt, draft.destinations, draft.approved)
                st.session_state.queue.add(updated)
                st.session_state.draft = updated
                add_activity("Content agent", "Saved edits to the campaign draft")
                st.success("Edits saved.")
        with approval_col:
            if st.button("Approve for publishing", type="primary", disabled=draft.approved, icon=":material/verified:", width="stretch"):
                st.session_state.draft = st.session_state.queue.approve(draft.draft_id)
                add_activity("Safety gate", "Campaign approved; publishing is now available")
                st.success("Approved. Choose Publish now when you are ready.", icon=":material/check_circle:")

    st.space("small")
    with st.container(border=True):
        st.subheader("3. Publish where you say")
        if not draft.approved:
            st.warning("Publishing is locked until you approve the draft above.", icon=":material/lock:")
        else:
            st.success("Approval recorded. This publish action routes only to the selected destinations.", icon=":material/lock_open:")
        if st.button("Publish approved content", type="primary", disabled=not draft.approved, icon=":material/send:", width="stretch"):
            try:
                receipts = publish_live(draft, st.session_state.generated_design)
                st.session_state.publishing_log.extend(receipts)
                add_activity("Publishing agent", f"Routed approved campaign to {len(receipts)} selected channel(s)")
                st.success(f"Publish complete: {len(receipts)} receipt(s) created.", icon=":material/check_circle:")
                for receipt in receipts:
                    st.json(receipt)
            except ProviderError as exc:
                st.error(str(exc), icon=":material/error:")


def render_connections() -> None:
    st.header("Connections and guardrails")
    st.write("Configure Canva, Instagram and WhatsApp once in Streamlit secrets. The content studio then creates the Canva visual and publishes only after approval.")
    st.warning("Canva's public REST API does not expose Magic Media text-to-image generation. This app uses Canva's supported brand-template autofill/export flow, so your Canva brand template should already contain the visual treatment/background.", icon=":material/info:")
    descriptions = {
        "Google Business Profile": "Read new reviews and publish approved replies through Google OAuth.",
        "Gemini": "Generate the campaign copy and image prompt. Gemini image generation is not used.",
        "Canva": "Create and export the branded visual from a Canva brand template.",
        "Instagram": "Publish approved feed posts and stories through the Instagram Graph API.",
        "WhatsApp": "Send approved image messages through the WhatsApp Cloud API.",
    }
    for name, description in descriptions.items():
        with st.container(border=True):
            key_ready = {
                "Google Business Profile": google_connected(),
                "Gemini": bool(gemini_api_key()),
                "Canva": canva_ready(),
                "Instagram": instagram_ready(),
                "WhatsApp": whatsapp_ready(),
            }.get(name, False)
            left, right = st.columns([3, 1], vertical_alignment="center")
            with left:
                st.markdown(f"**{name}**")
                st.caption(description)
            with right:
                badge("Configured" if key_ready else "Needs secrets", "green" if key_ready else "orange")
    st.space("small")
    with st.container(border=True):
        st.subheader("Connect Google Business Profile")
        st.write("Authorize the Google account that manages the Malay Tyres Business Profile. The app will read reviews and publish replies only after you approve each reply.")
        if google_connected():
            st.success(f"Connected to Google · {st.session_state.get('google_location_title') or 'Business Profile'}")
            if st.button("Sync Google reviews now", type="primary", key="google_sync_connections"):
                try:
                    count = sync_google_reviews()
                    add_activity("Google Business Profile", f"Synced {count} real reviews from Google")
                    st.success(f"Successfully loaded {count} Google reviews. Open Reviews to approve replies.")
                except Exception as exc:
                    st.error(f"Google connection is authorized, but review sync failed: {exc}")
            if st.button("Disconnect Google for this session", key="google_disconnect"):
                for key in ("google_access_token", "google_refresh_token", "google_token_expires_at", "google_account_name", "google_location_name", "google_location_title", "google_oauth_state"):
                    st.session_state.pop(key, None)
                with sqlite3.connect(OAUTH_DB_PATH, timeout=10) as conn:
                    conn.execute("DELETE FROM oauth_tokens WHERE provider = ?", ("google",))
                st.rerun()
        elif secret("GOOGLE_CLIENT_ID") and secret("GOOGLE_CLIENT_SECRET"):
            auth_url = google_authorization_url()
            if auth_url:
                st.link_button("Connect Google Business Profile", auth_url, icon=":material/login:", width="stretch")
                st.caption(f"Google OAuth redirect URI must exactly match: {google_redirect_uri()}")
        else:
            st.warning("Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Streamlit Cloud → Settings → Secrets. Never paste the values into chat.")
        if st.session_state.get("google_oauth_error"):
            st.error(st.session_state.google_oauth_error)
        if st.session_state.get("google_oauth_success"):
            st.success("Google sign-in succeeded. Click Sync Google reviews now to load your live reviews.")
            st.session_state.google_oauth_success = False
    st.space("small")
    with st.container(border=True):
        st.subheader("Connect Canva with OAuth")
        st.caption("Use your Canva Client ID and Client secret here through Streamlit secrets. The secret stays on the server; only the Canva authorization page opens in your browser.")
        if canva_access_token():
            st.success("Canva is connected. The app will reuse the saved authorization and refresh it when Canva allows it.", icon=":material/check_circle:")
        elif secret("CANVA_CLIENT_ID") and secret("CANVA_CLIENT_SECRET"):
            authorization_url = canva_authorization_url()
            if authorization_url:
                st.link_button("Authorize Canva", authorization_url, icon=":material/login:", width="stretch")
                st.caption(f"Canva must redirect back to: {canva_redirect_uri()}")
        else:
            st.info("Add CANVA_CLIENT_ID, CANVA_CLIENT_SECRET and CANVA_REDIRECT_URI to Streamlit secrets, then this button will appear.", icon=":material/key:")
        if st.session_state.canva_oauth_error:
            st.error(st.session_state.canva_oauth_error, icon=":material/error:")
    st.space("small")
    with st.container(border=True):
        st.subheader("Connect WhatsApp Business App (Coexistence)")
        st.write(
            "This section is intentionally a setup checklist, not a fake connection button. "
            "The current app can send through a configured Cloud API token, but it does not yet "
            "implement Meta Embedded Signup for keeping the same number in the WhatsApp Business app."
        )
        st.warning(
            "Do not register, migrate, remove, or disconnect your existing phone number using the standard Cloud API setup. "
            "Only continue through an Embedded Signup flow explicitly configured for WhatsApp Business App coexistence.",
            icon=":material/verified_user:",
        )
        meta_app_id = secret("META_APP_ID")
        signup_config_id = secret("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID")
        st.markdown("**Required Meta setup**")
        st.markdown(
            "1. In [Meta for Developers](https://developers.facebook.com/apps/), open your existing app and add/configure WhatsApp.  \n"
            "2. Configure Facebook Login for Business / Embedded Signup using Meta's current supported configuration.  \n"
            "3. Ensure the flow is explicitly set to connect an existing **WhatsApp Business App** number (coexistence), not standard API migration.  \n"
            "4. Configure the required HTTPS callback/webhook backend and subscribe the app to WhatsApp webhook events.  \n"
            "5. Complete Meta's required permissions, review, and provider/tech-provider onboarding steps before using this for customers."
        )
        st.caption("Meta's official Embedded Signup documentation: https://www.postman.com/meta/whatsapp-business-platform/documentation/du6gzjv/embedded-signup")
        c1, c2 = st.columns(2)
        with c1:
            badge("Meta App ID found" if meta_app_id else "META_APP_ID missing", "green" if meta_app_id else "orange")
        with c2:
            badge("Signup Config ID found" if signup_config_id else "Signup Config ID missing", "green" if signup_config_id else "orange")
        st.info(
            "These IDs alone do not connect WhatsApp. A secure backend must complete the signup code exchange, store credentials, "
            "verify webhook requests, and handle incoming messages. This app will not mark WhatsApp as connected until that flow is implemented and tested.",
            icon=":material/info:",
        )
        st.link_button("Open Meta for Developers", "https://developers.facebook.com/apps/", width="stretch")

    st.space("small")
    with st.container(border=True):
        st.subheader("Production checklist")
        st.markdown(":material/check_box_outline_blank: Add GEMINI_API_KEY for AI-written campaign copy")
        st.markdown(":material/check_box_outline_blank: Add Canva access token and brand template ID")
        st.markdown(":material/check_box_outline_blank: Add Instagram Graph API token and business account ID")
        st.markdown(":material/check_box_outline_blank: Add WhatsApp Cloud API token, phone ID and recipient number")
        st.markdown(":material/check_box_outline_blank: Confirm the Canva template field names: TITLE, BODY and CTA")


initialize_state()
handle_google_oauth_callback()
handle_canva_oauth_callback()
view = render_sidebar()
render_header()

if view == "Overview":
    render_overview()
elif view == "Reviews":
    render_reviews()
elif view == "Content studio":
    render_content_studio()
else:
    render_connections()
