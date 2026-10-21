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
import uuid

import requests
from zoneinfo import ZoneInfo

import streamlit as st

from tyre_agents.core import ApprovalQueue, ContentDraft, Destination, InMemoryPublisher, SAMPLE_REVIEWS, generate_content, publish_approved
try:
    from tyre_agents.core import DEFAULT_SALUTATION, SALUTATIONS, regenerate_reply, select_salutation
except ImportError:  # Keeps a running app compatible with older cached deployments during rollout.
    DEFAULT_SALUTATION = "Dear Customer"
    SALUTATIONS = ("Dear Sir", "Dear Ma'am", "Dear Customer")

    def select_salutation(value=None):
        requested = getattr(value, "salutation", value)
        normalized = " ".join(str(requested or "").strip().split()).casefold()
        return next((item for item in SALUTATIONS if item.casefold() == normalized), DEFAULT_SALUTATION)

    def regenerate_reply(review, current_reply=None, *, salutation=None, **_kwargs):
        greeting = select_salutation(salutation)
        base = "Thank you for sharing your feedback. We appreciate the opportunity to serve you better."
        alternate = "We appreciate your review and will continue working to provide excellent service."
        return f"{greeting}, {alternate if current_reply and alternate not in current_reply else base}"
from tyre_agents.canva_oauth import create_pkce_request, exchange_code
from tyre_agents.providers import (
    CanvaAdapter,
    CANVA_TEXT_LIMITS,
    GeminiImageAdapter,
    InstagramAdapter,
    ProviderError,
    WhatsAppAdapter,
    build_canva_text_fields,
)
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
    if "google_stored_access_token" not in st.session_state:
        st.session_state.google_stored_access_token = oauth_token_get("google")[0]
    return bool(st.session_state.get("google_access_token") or st.session_state.google_stored_access_token)

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
        synced[review_id] = {
            "review": review,
            "reply": reply_text,
            "salutation": prior.get("salutation", DEFAULT_SALUTATION) if prior else DEFAULT_SALUTATION,
            "reply_version": prior.get("reply_version", 0) if prior else 0,
            "status": status,
        }
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
    if "canva_stored_token" not in st.session_state:
        stored_access, stored_refresh, stored_expiry = oauth_token_get("canva")
        st.session_state.canva_stored_token = stored_access
        st.session_state.canva_stored_refresh = stored_refresh
        st.session_state.canva_stored_expiry = stored_expiry
    else:
        stored_access = st.session_state.canva_stored_token
        stored_refresh = st.session_state.canva_stored_refresh
        stored_expiry = st.session_state.canva_stored_expiry
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
        st.session_state.canva_stored_token = token
        st.session_state.canva_stored_refresh = refresh_token
        st.session_state.canva_stored_expiry = expires_at
        oauth_token_save("canva", token, refresh_token, expires_at)
    return token


def gemini_api_key() -> str:
    return secret("GEMINI_API_KEY")


def gemini_image_model() -> str:
    return secret("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")


def canva_image_model() -> str:
    return secret("CANVA_IMAGE_MODEL")


def gemini_model() -> str:
    """Use the currently recommended model and transparently replace the retired default."""
    configured = secret("GEMINI_MODEL", "gemini-3.8-flash").strip()
    if not configured or configured == "gemini-2.5-flash":
        return "gemini-3.8-flash"
    return configured


def generate_review_reply(review) -> str:
    """Write a service-specific reply without ever using the reviewer's name."""
    review_text = str(getattr(review, "text", "") or "").strip()
    rating = int(getattr(review, "rating", 5) or 5)
    lowered = review_text.casefold()
    greeting = select_salutation(getattr(review, "salutation", None))
    greeting_for_prompt = greeting.replace("Ma'am", "Ma’am")

    # Use the language model when configured, with strict grounding in the review.
    api_key = gemini_api_key()
    if api_key and review_text and review_text != "(Rating-only review)":
        try:
            model = gemini_model()
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            prompt = (
                "Write one short, polite Google Business Profile owner reply for Malay Tyres, Ujjain. "
                f"CRITICAL RULES: Never use, repeat, or infer the reviewer's name. Start exactly with '{greeting_for_prompt},'. "
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
                    if not text.casefold().startswith(("dear sir", "dear ma’am", "dear ma'am", "dear customer")):
                        text = f"{greeting_for_prompt},\n\n" + text
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
            return f"{greeting_for_prompt}, thank you for your kind feedback about our {service}. We truly appreciate your trust in Malay Tyres and look forward to serving you again."
        return f"{greeting_for_prompt}, thank you for your valuable feedback and support. We truly appreciate your trust in Malay Tyres and look forward to serving you again."
    if rating == 3:
        if service:
            return f"{greeting_for_prompt}, thank you for sharing your feedback about our {service}. We’ll use your comments to improve and would appreciate the opportunity to serve you better."
        return f"{greeting_for_prompt}, thank you for your feedback. We’ll use your comments to improve and would appreciate the opportunity to serve you better."
    if service:
        return f"{greeting_for_prompt}, we’re sorry your experience with our {service} did not meet expectations. Please contact Malay Tyres so we can understand the issue and work to make it right."
    return f"{greeting_for_prompt}, we’re sorry your experience did not meet expectations. Please contact Malay Tyres so we can understand the issue and work to make it right."


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


def canva_diagnostics() -> dict[str, object]:
    """Run read-only Canva checks and report configuration without exposing credentials."""
    results: dict[str, object] = {}
    token = canva_access_token()
    if not token:
        results["OAuth token"] = ("fail", "No usable access token found. Reconnect Canva from this page.")
        results["Brand Template ID"] = ("skip", "Skipped because there is no usable Canva token.")
        results["Template fields"] = ("skip", "Skipped because there is no usable Canva token.")
        return results

    try:
        response = requests.get(
            "https://api.canva.com/rest/v1/users/me",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
        )
        if response.ok:
            results["OAuth token"] = ("pass", "Canva accepted the saved token.")
        else:
            results["OAuth token"] = ("fail", f"Canva rejected the token (HTTP {response.status_code}). Reconnect Canva if needed.")
            results["Brand Template ID"] = ("skip", "Skipped because Canva did not accept the token.")
            results["Template fields"] = ("skip", "Skipped because Canva did not accept the token.")
            return results
    except requests.RequestException as exc:
        results["OAuth token"] = ("fail", f"Could not reach Canva ({type(exc).__name__}). Try again.")
        results["Brand Template ID"] = ("skip", "Skipped because Canva could not be reached.")
        results["Template fields"] = ("skip", "Skipped because Canva could not be reached.")
        return results

    template_id = secret("CANVA_BRAND_TEMPLATE_ID").strip()
    if not template_id:
        results["Brand Template ID"] = ("fail", "CANVA_BRAND_TEMPLATE_ID is missing in Streamlit Secrets.")
        results["Template fields"] = ("skip", "Skipped because no template ID is configured.")
        return results

    root = "https://api.canva.com/rest/v1"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = requests.get(
            f"{root}/brand-templates/{template_id}", headers=headers, timeout=20
        )
        if response.ok:
            payload = response.json()
            template = payload.get("brand_template", {})
            results["Brand Template ID"] = (
                "pass",
                f"Template is accessible: {template.get('title') or 'title not returned'} (ID matches configured value).",
            )
        else:
            results["Brand Template ID"] = (
                "fail",
                f"Canva could not access the configured template (HTTP {response.status_code}). Check the ID, account access, and brandtemplate:meta:read permission.",
            )
            results["Template fields"] = ("skip", "Skipped because the template could not be accessed.")
            return results
        response = requests.get(
            f"{root}/brand-templates/{template_id}/dataset", headers=headers, timeout=20
        )
        if response.ok:
            dataset = response.json().get("dataset", {})
            field_names = sorted(str(name) for name in dataset.keys())
            expected = {
                secret("CANVA_TITLE_FIELD", "TITLE"),
                secret("CANVA_BODY_FIELD", "BODY"),
                secret("CANVA_CTA_FIELD", "CTA"),
                secret("CANVA_IMAGE_FIELD", "IMAGE"),
            }
            found = [name for name in field_names if name in expected]
            missing = sorted(expected - set(field_names))
            summary = "Available fields: " + (", ".join(field_names) if field_names else "none returned")
            if missing:
                summary += ". Configured field names not found: " + ", ".join(missing)
                results["Template fields"] = ("warn", summary)
            else:
                results["Template fields"] = ("pass", summary)
        else:
            results["Template fields"] = (
                "fail",
                f"Canva could not read template fields (HTTP {response.status_code}). Check brandtemplate:content:read permission.",
            )
    except requests.RequestException as exc:
        results["Template fields"] = ("fail", f"Could not complete the template check ({type(exc).__name__}). Try again.")
    except (ValueError, TypeError):
        results["Template fields"] = ("fail", "Canva returned an unexpected template response.")
    return results


def image_generation_config_status() -> tuple[str, str]:
    account_id = secret("CLOUDFLARE_ACCOUNT_ID").strip()
    api_token = secret("CLOUDFLARE_API_TOKEN").strip()
    if canva_access_token() and secret("CANVA_BRAND_TEMPLATE_ID").strip():
        if account_id and api_token:
            return ("pass", "Canva AI is the primary image path and the optional Cloudflare fallback is configured. No image was generated by this check.")
        return ("pass", "Canva AI is the primary image path and uses the existing Canva OAuth connection; Cloudflare credentials are optional. Canva AI generation consumes Canva AI credits and is checked only when you create a campaign.")
    if account_id and api_token:
        return ("pass", "Cloudflare Workers AI credentials are present as a fallback, but a usable Canva token and Brand Template ID are still required to create the final design.")
    return ("fail", "A usable Canva OAuth token and CANVA_BRAND_TEMPLATE_ID are required. Cloudflare is an optional fallback, not a mandatory extra service.")


def run_all_api_diagnostics() -> dict[str, tuple[str, str]]:
    """Perform one-click, read-only checks for configured live integrations."""
    results: dict[str, tuple[str, str]] = {}

    # Google Business Profile: a read-only account-list call validates the OAuth token and API access.
    if not google_connected():
        results["Google Business Profile"] = ("fail", "No saved Google OAuth token. Connect the account that manages Malay Tyres.")
    else:
        try:
            payload = google_api_request("GET", f"{GOOGLE_ACCOUNT_API}/accounts", params={"pageSize": 1})
            accounts = payload.get("accounts", [])
            results["Google Business Profile"] = (
                "pass" if accounts else "warn",
                "Google accepted the token and returned an account." if accounts else
                "Google accepted the request but returned no accounts. Check which Google account was authorized."
            )
        except Exception as exc:
            results["Google Business Profile"] = ("fail", f"Read-only API check failed: {str(exc)[:220]}")

    # Gemini: one tiny, low-token request distinguishes a working key from quota/API errors.
    api_key = gemini_api_key()
    if not api_key:
        results["Gemini campaign copy"] = ("fail", "GEMINI_API_KEY is missing from Streamlit Secrets.")
    else:
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": "Reply with exactly OK."}]}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 5},
                },
                timeout=20,
            )
            if response.ok:
                results["Gemini campaign copy"] = ("pass", "Gemini accepted a small test request.")
            elif response.status_code == 429:
                results["Gemini campaign copy"] = ("fail", "Gemini is reachable, but the API quota/rate limit is exhausted (HTTP 429).")
            elif response.status_code in (401, 403):
                results["Gemini campaign copy"] = ("fail", f"Gemini rejected the key or API permission (HTTP {response.status_code}).")
            else:
                results["Gemini campaign copy"] = ("fail", f"Gemini test request failed (HTTP {response.status_code}).")
        except requests.RequestException as exc:
            results["Gemini campaign copy"] = ("fail", f"Gemini could not be reached ({type(exc).__name__}).")

    # Canva: validate token, template access, and the configured autofill fields.
    try:
        results.update(canva_diagnostics())
    except Exception as exc:
        results["Canva"] = ("fail", f"Canva check could not complete ({type(exc).__name__}).")

    # Instagram: verify token acceptance without creating or publishing a post.
    instagram_token = secret("INSTAGRAM_ACCESS_TOKEN")
    instagram_account = secret("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    if not instagram_token or not instagram_account:
        results["Instagram publishing"] = ("fail", "INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_BUSINESS_ACCOUNT_ID is missing.")
    else:
        try:
            version = secret("META_GRAPH_API_VERSION", "v21.0")
            response = requests.get(
                f"https://graph.facebook.com/{version}/me",
                params={"fields": "id", "access_token": instagram_token},
                timeout=20,
            )
            if response.ok:
                results["Instagram publishing"] = ("pass", "Meta accepted the access token. No post was created.")
            else:
                results["Instagram publishing"] = ("fail", f"Meta rejected the token or permission (HTTP {response.status_code}).")
        except requests.RequestException as exc:
            results["Instagram publishing"] = ("fail", f"Meta could not be reached ({type(exc).__name__}).")

    # WhatsApp: read phone-number metadata only; this does not send a message or alter registration.
    wa_token = secret("WHATSAPP_ACCESS_TOKEN")
    wa_phone_id = secret("WHATSAPP_PHONE_NUMBER_ID")
    if not wa_token or not wa_phone_id:
        results["WhatsApp Cloud API"] = ("fail", "WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is missing.")
    else:
        try:
            version = secret("META_GRAPH_API_VERSION", "v21.0")
            response = requests.get(
                f"https://graph.facebook.com/{version}/{wa_phone_id}",
                headers={"Authorization": f"Bearer {wa_token}"},
                params={"fields": "display_phone_number,verified_name"},
                timeout=20,
            )
            if response.ok:
                results["WhatsApp Cloud API"] = ("pass", "Meta accepted the token and phone-number ID. No message was sent.")
            else:
                results["WhatsApp Cloud API"] = ("fail", f"Meta rejected the token, phone-number ID, or permission (HTTP {response.status_code}).")
        except requests.RequestException as exc:
            results["WhatsApp Cloud API"] = ("fail", f"Meta could not be reached ({type(exc).__name__}).")

    image_status, image_message = image_generation_config_status()
    results["Image generation"] = (
        ("warn", image_message + " This check does not generate an image or spend image-generation credits.")
        if image_status == "pass"
        else ("fail", image_message)
    )

    if secret("SUPABASE_URL") and secret("SUPABASE_SERVICE_ROLE_KEY"):
        results["Persistent OAuth storage"] = ("pass", "Supabase storage settings are present; the check does not read or display stored tokens.")
    else:
        results["Persistent OAuth storage"] = (
            "warn",
            "Supabase storage is not configured. OAuth tokens may rely on /tmp SQLite and can be lost when the app restarts."
        )

    results["Google Business Profile posts"] = (
        "warn",
        "Publishing new Google Business Profile posts is not implemented in this codebase; review sync/replies are separate."
    )
    results["WhatsApp Business App coexistence"] = (
        "warn",
        "Embedded Signup/coexistence is not implemented in this app yet. The Cloud API check above does not migrate or alter your existing WhatsApp number."
    )
    return results


def canva_ready() -> bool:
    return bool(canva_access_token())


def canva_template_ready() -> bool:
    return bool(canva_access_token() and secret("CANVA_BRAND_TEMPLATE_ID"))


def instagram_ready() -> bool:
    return bool(secret("INSTAGRAM_ACCESS_TOKEN") and secret("INSTAGRAM_BUSINESS_ACCOUNT_ID"))


def whatsapp_ready() -> bool:
    return bool(
        secret("WHATSAPP_ACCESS_TOKEN")
        and secret("WHATSAPP_PHONE_NUMBER_ID")
        and secret("WHATSAPP_RECIPIENT_PHONE")
    )


def openai_image_api_key() -> str:
    """Separate image-generation credential; independent of Gemini text quota."""
    return secret("OPENAI_API_KEY")


def generate_fresh_campaign_image(prompt: str) -> bytes:
    """Generate a fresh campaign image through low-cost Cloudflare Workers AI."""
    account_id = secret("CLOUDFLARE_ACCOUNT_ID").strip()
    api_token = secret("CLOUDFLARE_API_TOKEN").strip()
    if not account_id or not api_token:
        raise ProviderError(
            "Cloudflare image generation is selected, but CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_API_TOKEN are not configured in Streamlit Secrets. "
            "Add these two values from Cloudflare Workers AI; OpenAI is not called, so no OpenAI credits are spent."
        )

    variation = random_secrets.randbelow(2_000_000_000) + 1
    image_prompt = (
        "Create ONE photorealistic square 1:1 premium automotive advertising photograph for an Indian tyre and car-care shop. "
        "Show a believable Indian-market car/SUV or a tyre/wheel/workshop scene. Use a new composition, camera angle, subject, "
        "location and lighting; avoid a generic stock-photo look. Premium modern 3D-commercial realism, crisp tyre tread and alloy-wheel detail, "
        "balanced light background with restrained yellow/charcoal accents. No text, no letters, no logos, no watermark, no fake brand badges. "
        "Keep the centre composition clean and leave safe space for Canva overlay text.\n"
        f"Campaign-specific creative direction: {prompt}\nVariation seed: {variation}"
    )[:2048]
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    )
    try:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json={"prompt": image_prompt, "steps": 4, "seed": variation},
            timeout=180,
        )
        if not response.ok:
            detail = ""
            try:
                payload = response.json()
                errors = payload.get("errors", []) if isinstance(payload, dict) else []
                result = payload.get("result", {}) if isinstance(payload, dict) else {}
                detail = "; ".join(
                    str(item.get("message") or item.get("code") or item)
                    for item in errors[:2]
                ) or str(result.get("message") or "")
            except Exception:
                pass
            detail = detail.replace(api_token, "[hidden API token]")
            raise ProviderError(
                f"Cloudflare FLUX image generation failed (HTTP {response.status_code})"
                + (f": {detail[:350]}" if detail else "")
            )
        payload = response.json()
        image_b64 = (payload.get("result") or {}).get("image") if isinstance(payload, dict) else None
        if image_b64:
            try:
                return base64.b64decode(image_b64, validate=True)
            except Exception as exc:
                raise ProviderError("Cloudflare returned image data that could not be decoded.") from exc
        raise ProviderError("Cloudflare returned no image data. Check Workers AI access and account limits.")
    except requests.RequestException as exc:
        raise ProviderError(f"Cloudflare image generation request failed: {type(exc).__name__}") from exc

def _canva_text_fields(draft: ContentDraft) -> dict[str, str]:
    return build_canva_text_fields(
        draft.title,
        draft.body,
        secret("CANVA_CTA_TEXT", "Book your tyre check today"),
        title_field=secret("CANVA_TITLE_FIELD", "TITLE"),
        body_field=secret("CANVA_BODY_FIELD", "BODY"),
        cta_field=secret("CANVA_CTA_FIELD", "CTA"),
    )


def _canva_adapter() -> CanvaAdapter:
    return CanvaAdapter(
        access_token=canva_access_token(),
        brand_template_id=secret("CANVA_BRAND_TEMPLATE_ID"),
    )


def _finish_canva_design(draft: ContentDraft, asset_id: str) -> dict:
    return _canva_adapter().autofill_export(
        _canva_text_fields(draft),
        image_fields={secret("CANVA_IMAGE_FIELD", "IMAGE"): asset_id},
        title=draft.title,
    )


def generate_canva_design(draft: ContentDraft) -> dict:
    field_title = secret("CANVA_TITLE_FIELD", "TITLE")
    field_body = secret("CANVA_BODY_FIELD", "BODY")
    field_cta = secret("CANVA_CTA_FIELD", "CTA")
    field_image = secret("CANVA_IMAGE_FIELD", "IMAGE")
    adapter = _canva_adapter()
    asset_name = f"Malay Tyres {draft.title[:30]} visual"
    try:
        # Prefer Canva-native image generation: it uses the already-connected Canva OAuth
        # account and does not require a separate Cloudflare account/token.
        with st.spinner("Generating a fresh image with Canva AI..."):
            asset_id = adapter.generate_image_asset(draft.image_prompt, name=asset_name)
    except ProviderError as canva_error:
        # Preserve the optional Cloudflare route as a fallback if the user configured it.
        if not (secret("CLOUDFLARE_ACCOUNT_ID").strip() and secret("CLOUDFLARE_API_TOKEN").strip()):
            raise ProviderError(
                f"Canva AI image generation could not complete: {canva_error}. "
                "Canva's image-generation endpoint is a preview API and may need to be enabled for this Canva app; "
                "it also consumes Canva AI credits. If Canva reports image generation is not enabled, the app owner "
                "must enable that capability or configure the optional Cloudflare fallback."
            ) from canva_error
        with st.spinner("Canva AI is unavailable; trying the configured Cloudflare image fallback..."):
            image_bytes = generate_fresh_campaign_image(draft.image_prompt)
        asset_id = adapter.upload_image_asset(image_bytes, name=asset_name)

    return _finish_canva_design(draft, asset_id)


def generate_gemini_design(draft: ContentDraft) -> dict:
    """Generate with Gemini, then optionally render the image through Canva."""
    prompt = (
        "Create a fresh, realistic premium marketing image for Malay Tyres, a local Indian tyre and vehicle-care business. "
        "Show a clean professional tyre-service or safe-road-travel scene with natural lighting and a believable car or SUV. "
        "No readable text, watermark, invented logo, fake phone number, or brand badge. Leave clean space for Canva overlay text. "
        f"Campaign direction: {draft.image_prompt}"
    )
    image = GeminiImageAdapter(gemini_api_key(), model=gemini_image_model()).generate(prompt, aspect_ratio="9:16" if Destination.INSTAGRAM_STORY in draft.destinations else "4:5")
    if not canva_template_ready():
        return {**image, "provider": "gemini", "label": "Gemini image", "publishable": False}
    adapter = _canva_adapter()
    asset_id = adapter.upload_image_asset(image["image_bytes"], name=f"Malay Tyres Gemini {draft.title[:30]}")
    design = _finish_canva_design(draft, asset_id)
    return {
        **design,
        "provider": "gemini",
        "label": "Gemini image in Canva",
        "image_bytes": image["image_bytes"],
        "mime_type": image["mime_type"],
        "publishable": True,
    }


def generate_visual_options(draft: ContentDraft) -> tuple[dict[str, dict], tuple[str, ...]]:
    """Create one image with Gemini and one with Canva for side-by-side review."""
    options: dict[str, dict] = {}
    errors: list[str] = []
    if gemini_api_key():
        try:
            options["Gemini"] = generate_gemini_design(draft)
        except ProviderError as exc:
            errors.append(f"Gemini image: {exc}")
    else:
        errors.append("Gemini image: add GEMINI_API_KEY to Streamlit Secrets")
    if canva_template_ready():
        try:
            options["Canva"] = {**generate_canva_design(draft), "label": "Canva AI image", "publishable": True}
        except ProviderError as exc:
            errors.append(f"Canva image: {exc}")
    else:
        errors.append("Canva image: connect Canva and configure CANVA_BRAND_TEMPLATE_ID")
    return options, tuple(errors)

def generate_campaign_draft(topic: str, tone: str, draft_id: str, destinations: tuple[Destination, ...]) -> tuple[ContentDraft, str]:
    """Prefer Gemini for copy, with a deterministic fallback for offline trials."""
    if gemini_api_key():
        # GeminiTextGenerator reads GEMINI_MODEL from the environment in supported versions.
        # Override the retired default, but respect a different valid model chosen in Streamlit secrets.
        os.environ["GEMINI_MODEL"] = gemini_model()
        generator = GeminiTextGenerator(gemini_api_key(), model=gemini_model())
        prompt = f"{topic.strip()} (tone: {tone})"
        # Retry only transient server-capacity errors. A 429 quota response can
        # represent a daily project/model cap; retrying it wastes requests and time.
        campaign = None
        last_error = None
        for attempt in range(3):
            try:
                campaign = generator.generate(prompt)
                break
            except Exception as exc:
                last_error = exc
                message = f"{type(exc).__name__}: {exc}".casefold()
                quota_exhausted = any(marker in message for marker in (
                    "429", "resource_exhausted", "generate_requests_per_day",
                    "free_tier_requests", "quota exceeded",
                ))
                if quota_exhausted:
                    raise
                temporary = any(marker in message for marker in (
                    "503", "unavailable", "high demand", "500", "502", "504",
                    "temporarily", "internal server error",
                ))
                if not temporary or attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if campaign is None:
            raise last_error or RuntimeError("Gemini did not return a campaign")
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
                "salutation": DEFAULT_SALUTATION,
                "reply_version": 0,
                "status": "Ready to review",
            }
            for review in SAMPLE_REVIEWS
        }
    st.session_state.setdefault("queue", ApprovalQueue())
    st.session_state.setdefault("draft", None)
    st.session_state.setdefault("generated_design", None)
    st.session_state.setdefault("image_candidates", {})
    st.session_state.setdefault("selected_image_provider", None)
    st.session_state.setdefault("image_generation_number", 0)
    st.session_state.setdefault("image_errors", ())
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
    cols[3].metric("Connections ready", f"{ready}/4", "AI images + Canva + Meta")


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
        salutation = st.selectbox(
            "Salutation",
            SALUTATIONS,
            index=SALUTATIONS.index(item.get("salutation", DEFAULT_SALUTATION))
            if item.get("salutation", DEFAULT_SALUTATION) in SALUTATIONS else 0,
            key=f"salutation_{review_id}_{item.get('reply_version', 0)}",
            help="The app never uses the customer name. Choose a salutation explicitly; Dear Customer is the safe default when gender is unknown.",
        )
        item["salutation"] = salutation
        reply_key = f"reply_{review_id}_{item.get('reply_version', 0)}"
        edited_reply = st.text_area(
            "Suggested reply",
            value=item["reply"],
            key=reply_key,
            height=90,
            help="Edit this reply before sending it to Google Business Profile.",
        )
        buttons = st.container(horizontal=True, vertical_alignment="center")
        with buttons:
            if st.button("Regenerate reply", key=f"regenerate_{review_id}", icon=":material/refresh:"):
                item["reply"] = regenerate_reply(review, item["reply"], salutation=salutation)
                item["reply_version"] = item.get("reply_version", 0) + 1
                item["status"] = "Ready to review"
                add_activity("Review agent", f"Regenerated a different reply for review {review_id}")
                st.rerun()
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
    st.write("Real Google reviews are loaded here. Replies never use the customer name, each review has its own wording, and you can regenerate any reply before publishing.")
    st.caption("The app does not reliably infer gender from a name. Choose Dear Sir, Dear Ma'am, or Dear Customer for each reviewer.")
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
        canva_fields = _canva_text_fields(draft)
        st.markdown("**Canva overlay copy**")
        st.caption(
            f"TITLE ≤ {CANVA_TEXT_LIMITS['TITLE']} characters · "
            f"BODY ≤ {CANVA_TEXT_LIMITS['BODY']} · CTA ≤ {CANVA_TEXT_LIMITS['CTA']}"
        )
        for field_name, value in canva_fields.items():
            st.write(f"**{field_name}** ({len(value)} characters): {value}")
        st.caption("The design brief is retained as an audit trail. A new image is generated for each request, uploaded to Canva, then inserted into the IMAGE field while TITLE, BODY and CTA are filled.")


def render_generated_design(draft: ContentDraft) -> None:
    with st.container(border=True):
        st.subheader("Compare Gemini and Canva images")
        candidates = st.session_state.image_candidates
        selected = st.session_state.get("selected_image_provider")
        if candidates:
            st.caption("Both providers create a fresh image for this campaign. Select the version you prefer before approving.")
            columns = st.columns(len(candidates))
            for column, provider in zip(columns, candidates):
                candidate = candidates[provider]
                with column:
                    badge("Selected" if provider == selected else provider, "green" if provider == selected else "blue")
                    if candidate.get("image_url"):
                        st.image(candidate["image_url"], caption=candidate.get("label", provider), alt=f"{provider}-generated Malay Tyres campaign image", width="stretch")
                    elif candidate.get("image_bytes"):
                        st.image(candidate["image_bytes"], caption=candidate.get("label", provider), alt=f"{provider}-generated Malay Tyres campaign image", width="stretch")
                        st.download_button(
                            "Download Gemini image",
                            data=candidate["image_bytes"],
                            file_name=f"{draft.draft_id}-gemini.png",
                            mime=candidate.get("mime_type", "image/png"),
                            key=f"download_{draft.draft_id}_{provider}",
                            width="stretch",
                        )
                    if st.button(
                        "Use this image",
                        key=f"use_{draft.draft_id}_{provider}",
                        type="primary" if provider == selected else "secondary",
                        width="stretch",
                    ):
                        st.session_state.selected_image_provider = provider
                        st.session_state.generated_design = candidate
                        st.rerun()
                    if candidate.get("design_url"):
                        st.link_button("Open in Canva", candidate["design_url"], icon=":material/edit:", width="stretch")
            if selected and not candidates[selected].get("image_url"):
                st.warning("This Gemini option can be previewed and downloaded, but social publishing needs a public Canva image URL. Select the Canva option to publish.", icon=":material/info:")
        elif st.session_state.image_errors:
            for error in st.session_state.image_errors:
                st.warning(error, icon=":material/warning:")
        else:
            st.info("Gemini and Canva will create separate image options when you submit a campaign.", icon=":material/image:")


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
            except Exception as exc:
                raw_error = str(exc) or "No error details returned."
                normalized_error = raw_error.casefold()
                if any(marker in normalized_error for marker in (
                    "generate_requests_per_day", "free_tier_requests", "retrydelay",
                )) or ("429" in normalized_error and "quota" in normalized_error):
                    error_message = (
                        "Gemini's free-tier daily quota is exhausted for the configured model. "
                        "No further Gemini retries will be made for this request. The app is using "
                        "its offline fallback; try Gemini again after the quota reset, or review "
                        "your usage and billing limits in Google AI Studio."
                    )
                elif "429" in normalized_error or "resource_exhausted" in normalized_error:
                    error_message = (
                        "Gemini returned a quota/rate-limit response. The app is using its offline "
                        "fallback instead of retrying and consuming more quota."
                    )
                else:
                    error_message = raw_error
                api_key = gemini_api_key()
                if api_key:
                    error_message = error_message.replace(api_key, "[hidden API key]")
                st.warning(
                    f"Gemini failed ({type(exc).__name__}): {error_message}. Using the offline fallback.",
                    icon=":material/warning:",
                )
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
            st.session_state.image_candidates = {}
            st.session_state.selected_image_provider = None
            st.session_state.image_generation_number = 0
            st.session_state.image_errors = ()
            st.session_state.design_error = None
            add_activity("Content agent", f"Generated copy with {copy_provider} for {topic}")
            with st.spinner("Generating one fresh Gemini image and one fresh Canva image..."):
                options, image_errors = generate_visual_options(draft)
            st.session_state.image_candidates = options
            st.session_state.image_errors = image_errors
            if options:
                st.session_state.selected_image_provider = next(iter(options))
                st.session_state.generated_design = options[st.session_state.selected_image_provider]
                add_activity("Image agents", f"Generated {len(options)} image option(s) for comparison")
                st.success("Caption and image options are ready. Choose the better image below.", icon=":material/check_circle:")
            else:
                st.warning("The caption was created, but no image provider returned a result. Check Connections and try again.", icon=":material/warning:")

    draft = st.session_state.draft
    if draft is None:
        st.info("No draft yet. Start with a campaign brief above.", icon=":material/edit_note:")
        return

    st.space("small")
    render_draft_preview(draft)
    if st.button("Regenerate both images", icon=":material/refresh:", width="stretch"):
        st.session_state.image_generation_number += 1
        with st.spinner("Generating new Gemini and Canva image options..."):
            options, image_errors = generate_visual_options(draft)
        st.session_state.image_candidates = options
        st.session_state.image_errors = image_errors
        st.session_state.selected_image_provider = next(iter(options), None)
        st.session_state.generated_design = options.get(st.session_state.selected_image_provider) if st.session_state.selected_image_provider else None
        add_activity("Image agents", "Regenerated both campaign image options")
        st.rerun()
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
    st.info("This app uses Canva Connect OAuth for template autofill/export and attempts Canva-native AI image generation first; Cloudflare Workers AI is an optional fallback. Canva image generation is a preview API and consumes Canva AI credits. The one-click diagnostic makes read-only checks for Google, Canva, Instagram and WhatsApp and sends one tiny Gemini test request. It does not generate an image, publish a post, or send a message. Secrets are never displayed.", icon=":material/info:")
    descriptions = {
        "Google Business Profile": "Read new reviews and publish approved replies through Google OAuth.",
        "Image generation": "Uses Canva AI with the connected Canva account; Cloudflare Workers AI is an optional fallback.",
        "Canva": "Create and export the branded visual from a Canva brand template.",
        "Instagram": "Publish approved feed posts and stories through the Instagram Graph API.",
        "WhatsApp": "Send approved image messages through the WhatsApp Cloud API.",
    }
    for name, description in descriptions.items():
        with st.container(border=True):
            key_ready = {
                "Google Business Profile": google_connected(),
                "Image generation": bool((canva_access_token() and secret("CANVA_BRAND_TEMPLATE_ID")) or (secret("CLOUDFLARE_ACCOUNT_ID") and secret("CLOUDFLARE_API_TOKEN"))),
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
        if st.button("Run all live API checks", key="canva_safe_diagnostics", icon=":material/health_and_safety:", width="stretch"):
            with st.spinner("Checking Google, Gemini, Canva, Meta and image-generation configuration..."):
                st.session_state["canva_diagnostic_results"] = run_all_api_diagnostics()
        checks = st.session_state.get("canva_diagnostic_results")
        if checks:
            st.markdown("**Diagnostic results**")
            for check_name, (status, message) in checks.items():
                if status == "pass":
                    st.success(f"{check_name}: {message}", icon=":material/check_circle:")
                elif status == "warn":
                    st.warning(f"{check_name}: {message}", icon=":material/warning:")
                elif status == "skip":
                    st.info(f"{check_name}: {message}", icon=":material/info:")
                else:
                    st.error(f"{check_name}: {message}", icon=":material/error:")
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
        # Accept either Streamlit secret name used during setup.
        meta_app_id = secret("META_APP_ID")
        signup_config_id = (
            secret("META_CONFIG_ID")
            or secret("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID")
        )
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
