from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode
import secrets as random_secrets
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
    return bool(st.session_state.get("google_access_token"))


def google_authorization_url() -> str | None:
    client_id = secret("GOOGLE_CLIENT_ID")
    if not client_id or not secret("GOOGLE_CLIENT_SECRET"):
        return None
    state = random_secrets.token_urlsafe(32)
    st.session_state.google_oauth_state = state
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
    token = st.session_state.get("google_access_token", "")
    expires_at = float(st.session_state.get("google_token_expires_at", 0) or 0)
    refresh_token = st.session_state.get("google_refresh_token") or secret("GOOGLE_REFRESH_TOKEN")
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
        st.session_state.google_access_token = token
        st.session_state.google_token_expires_at = time.time() + int(data.get("expires_in", 3600))
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
    if not code or not returned_state or st.session_state.get("google_last_code") == code:
        return
    expected_state = st.session_state.get("google_oauth_state")
    if not expected_state or returned_state != expected_state:
        # This may be a callback for another provider, such as Canva.
        return
    st.session_state.google_last_code = code
    try:
        response = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": secret("GOOGLE_CLIENT_ID"),
            "client_secret": secret("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": google_redirect_uri(),
            "grant_type": "authorization_code",
        }, timeout=30)
        if not response.ok:
            raise ProviderError("Google token exchange failed. Check the OAuth client and redirect URI in Google Cloud.")
        token_data = response.json()
        st.session_state.google_access_token = token_data["access_token"]
        if token_data.get("refresh_token"):
            st.session_state.google_refresh_token = token_data["refresh_token"]
        st.session_state.google_token_expires_at = time.time() + int(token_data.get("expires_in", 3600))
        st.session_state.google_oauth_error = ""
        st.session_state.google_location_name = ""
        st.session_state.google_account_name = ""
        st.session_state.google_location_title = ""
        st.session_state.google_oauth_success = True
    except Exception as exc:
        st.session_state.google_oauth_error = str(exc)
    finally:
        # Remove OAuth parameters from the visible URL and avoid reprocessing the code.
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
    # Reviews API expects the complete parent resource: accounts/{id}/locations/{id}/reviews.
    url = f"{GOOGLE_REVIEWS_API}/{location_name}/reviews"
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
            reply_text = prior.get("reply", generate_reply(review))
            status = prior.get("status", "Ready to review")
        else:
            reply_text = generate_reply(review)
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
    return str(st.session_state.get("canva_access_token") or secret("CANVA_ACCESS_TOKEN"))


def gemini_api_key() -> str:
    return secret("GEMINI_API_KEY")


def canva_redirect_uri() -> str:
    configured = secret("CANVA_REDIRECT_URI")
    if configured:
        return configured
    return "http://localhost:8501"


def handle_canva_oauth_callback() -> None:
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    if not code or not returned_state or not st.session_state.get("canva_oauth_state") or st.session_state.get("canva_last_code") == code:
        return
    st.session_state.canva_last_code = code
    expected_state = st.session_state.get("canva_oauth_state")
    verifier = st.session_state.get("canva_code_verifier")
    if not expected_state or returned_state != expected_state or not verifier:
        st.session_state.canva_oauth_error = "Canva OAuth state validation failed. Start the connection again."
        return
    try:
        token_response = exchange_code(
            code,
            verifier,
            secret("CANVA_CLIENT_ID"),
            secret("CANVA_CLIENT_SECRET"),
            canva_redirect_uri(),
        )
        st.session_state.canva_access_token = str(token_response["access_token"])
        st.session_state.canva_refresh_token = str(token_response.get("refresh_token", ""))
        st.session_state.canva_oauth_error = ""
    except Exception:
        st.session_state.canva_oauth_error = "Canva OAuth token exchange failed. Check the Client ID, Client secret and redirect URL."


def canva_authorization_url() -> str | None:
    client_id = secret("CANVA_CLIENT_ID")
    if not client_id or not secret("CANVA_CLIENT_SECRET"):
        return None
    request = create_pkce_request(client_id, canva_redirect_uri())
    st.session_state.canva_oauth_state = request.state
    st.session_state.canva_code_verifier = request.code_verifier
    return request.authorization_url


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
                "reply": generate_reply(review),
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
                item["reply"] = generate_reply(item["review"])
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
                item["reply"] = generate_reply(item["review"])
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
            st.success("Canva access token is available for this session.", icon=":material/check_circle:")
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
