from __future__ import annotations

from datetime import datetime
import base64
import hashlib
import hmac
import os
import sqlite3
import secrets as py_secrets
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

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


def _canva_store_path() -> str:
    # Shared by Streamlit browser sessions on the same running app instance.
    # Streamlit Cloud may clear /tmp when the app restarts; users can authorize again.
    return "/tmp/malay_tyres_canva_oauth.sqlite3"


def _canva_store_init() -> None:
    with sqlite3.connect(_canva_store_path(), timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS oauth_state (state TEXT PRIMARY KEY, verifier TEXT NOT NULL, created_at INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS oauth_tokens (id INTEGER PRIMARY KEY CHECK (id = 1), access_token TEXT NOT NULL, refresh_token TEXT NOT NULL DEFAULT '')")


def _canva_store_state(state: str, verifier: str) -> None:
    import time
    _canva_store_init()
    with sqlite3.connect(_canva_store_path(), timeout=10) as conn:
        # Remove stale one-time states after 15 minutes.
        conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (int(time.time()) - 900,))
        conn.execute("INSERT OR REPLACE INTO oauth_state(state, verifier, created_at) VALUES (?, ?, ?)", (state, verifier, int(time.time())))


def _canva_get_verifier(state: str) -> str:
    _canva_store_init()
    with sqlite3.connect(_canva_store_path(), timeout=10) as conn:
        row = conn.execute("SELECT verifier FROM oauth_state WHERE state = ?", (state,)).fetchone()
        if row:
            conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
            return str(row[0])
    return ""


def _canva_store_tokens(access_token: str, refresh_token: str = "") -> None:
    _canva_store_init()
    with sqlite3.connect(_canva_store_path(), timeout=10) as conn:
        conn.execute("INSERT OR REPLACE INTO oauth_tokens(id, access_token, refresh_token) VALUES (1, ?, ?)", (access_token, refresh_token))


def canva_access_token() -> str:
    session_token = str(st.session_state.get("canva_access_token") or "")
    if session_token:
        return session_token
    configured_token = secret("CANVA_ACCESS_TOKEN")
    if configured_token:
        return configured_token
    try:
        _canva_store_init()
        with sqlite3.connect(_canva_store_path(), timeout=10) as conn:
            row = conn.execute("SELECT access_token FROM oauth_tokens WHERE id = 1").fetchone()
            return str(row[0]) if row else ""
    except Exception:
        return ""

def gemini_api_key() -> str:
    return secret("GEMINI_API_KEY")


def canva_redirect_uri() -> str:
    configured = secret("CANVA_REDIRECT_URI")
    if configured:
        return configured
    return "http://localhost:8501"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _canva_state_and_verifier(client_secret: str) -> tuple[str, str, str]:
    """Create a signed, stateless OAuth state and a reproducible PKCE verifier.

    This avoids relying on Streamlit session state or local disk, which may not
    survive Canva opening a new tab or Streamlit Cloud routing the callback to
    a different app instance.
    """
    nonce = _b64url(py_secrets.token_bytes(32))
    signature = _b64url(hmac.new(client_secret.encode("utf-8"), ("state:" + nonce).encode("ascii"), hashlib.sha256).digest())
    state = nonce + "." + signature
    verifier = _b64url(hmac.new(client_secret.encode("utf-8"), ("verifier:" + nonce).encode("ascii"), hashlib.sha256).digest())
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return state, verifier, challenge


def _canva_verifier_from_state(state: str, client_secret: str) -> str:
    """Validate OAuth state and reproduce its PKCE verifier without server storage."""
    try:
        nonce, supplied_signature = state.split(".", 1)
    except ValueError:
        return ""
    expected_signature = _b64url(hmac.new(client_secret.encode("utf-8"), ("state:" + nonce).encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return ""
    return _b64url(hmac.new(client_secret.encode("utf-8"), ("verifier:" + nonce).encode("ascii"), hashlib.sha256).digest())


def handle_canva_oauth_callback() -> None:
    """Exchange Canva's callback code using the verifier created for that exact request."""
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    oauth_error = st.query_params.get("error")
    if oauth_error:
        st.session_state.canva_oauth_error = f"Canva authorization was not completed ({oauth_error}). Start again from Connections."
        st.query_params.clear()
        return
    if not code or not returned_state:
        return
    if st.session_state.get("canva_last_code") == code:
        return

    # Validate signed state and reproduce the PKCE verifier without relying on
    # session state, SQLite, /tmp persistence, or a particular Cloud instance.
    client_id = secret("CANVA_CLIENT_ID")
    client_secret = secret("CANVA_CLIENT_SECRET")
    state_secret = secret("CANVA_OAUTH_STATE_SECRET") or client_secret
    verifier = _canva_verifier_from_state(str(returned_state), state_secret)
    if not verifier:
        st.session_state.canva_oauth_error = "Canva OAuth state validation failed. The callback state is invalid or the Canva Client Secret changed during authorization. Return to Connections and click Authorize Canva again."
        st.query_params.clear()
        return

    try:
        redirect_uri = canva_redirect_uri()
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        response = requests.post(
            "https://api.canva.com/rest/v1/oauth/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
        if not response.ok:
            # Do not display request headers, client secret, or token contents.
            detail = "Canva rejected the token exchange. Verify the current Client ID/secret and that the redirect URI matches exactly in Canva and Streamlit."
            try:
                api_error = response.json().get("error")
                if api_error:
                    detail += f" Error: {api_error}."
            except Exception:
                pass
            raise RuntimeError(detail)
        token_response = response.json()
        access_token = token_response.get("access_token")
        if not access_token:
            raise RuntimeError("Canva did not return an access token. Check the OAuth app settings and scopes.")
        refresh_token = str(token_response.get("refresh_token", ""))
        st.session_state.canva_access_token = str(access_token)
        st.session_state.canva_refresh_token = refresh_token
        _canva_store_tokens(str(access_token), refresh_token)
        st.session_state.canva_last_code = code
        st.session_state.canva_oauth_error = ""
        st.session_state.canva_oauth_state = ""
        st.session_state.canva_code_verifier = ""
        st.query_params.clear()
    except Exception as exc:
        message = str(exc)
        st.session_state.canva_oauth_error = message if message.startswith("Canva ") else "Canva token exchange failed. Verify the Client ID, Client secret, redirect URI, and app scopes in Canva Developer Portal."
        st.query_params.clear()


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time comparison for OAuth state values."""
    import hmac
    return hmac.compare_digest(str(left), str(right))


def canva_authorization_url() -> str | None:
    client_id = secret("CANVA_CLIENT_ID")
    if not client_id or not secret("CANVA_CLIENT_SECRET"):
        return None
    # Stateless signed state: safe across new tabs and Streamlit Cloud instances.
    # The same stable key must be used in authorization and callback.
    state_secret = secret("CANVA_OAUTH_STATE_SECRET") or secret("CANVA_CLIENT_SECRET")
    state, verifier, challenge = _canva_state_and_verifier(state_secret)
    st.session_state.canva_oauth_state = state
    st.session_state.canva_code_challenge = challenge
    scopes = secret(
        "CANVA_SCOPES",
        "brandtemplate:content:read brandtemplate:content:write brandtemplate:meta:read design:content:read design:content:write design:meta:read asset:read asset:write profile:read",
    )
    params = {
        "code_challenge_method": "s256",
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": canva_redirect_uri(),
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
    }
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
            receipts.append({"provider": "google_business_profile", "status": "demo_only"})
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
        badge("Demo mode", "blue")
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
                st.success("Reply saved. The real connector can publish it after GBP is connected.")
            if st.button("Mark as published", key=f"publish_review_{review_id}", disabled=item["status"] != "Approved for publishing", icon=":material/send:"):
                item["status"] = "Published"
                add_activity("Publishing agent", f"Demo-published the reply for {review.customer_name} to GBP")
                st.success("Demo receipt created. No external API call was made.")


def render_reviews() -> None:
    st.header("Google Business Profile review inbox")
    st.write("Every review gets its own rating-aware response. Edit it, approve it, and only then publish.")
    st.info("Real GBP access requires OAuth credentials and the Google Business Profile APIs. This workspace currently uses sample reviews and a safe in-memory publisher.", icon=":material/info:")
    if st.button("Refresh all suggested replies", type="primary", icon=":material/auto_awesome:"):
        for item in st.session_state.reviews.values():
            item["reply"] = generate_reply(item["review"])
            item["status"] = "Ready to review"
        add_activity("Review agent", "Generated a different polite reply for each review")
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
        st.subheader("Connect Canva with OAuth")
        st.caption("Use your Canva Client ID and Client secret here through Streamlit secrets. The secret stays on the server; only the Canva authorization page opens in your browser.")
        if canva_access_token() and secret("CANVA_BRAND_TEMPLATE_ID"):
            st.success("Canva is configured: access token and Brand Template ID are available.", icon=":material/check_circle:")
        elif secret("CANVA_CLIENT_ID") and secret("CANVA_CLIENT_SECRET"):
            if canva_access_token() and not secret("CANVA_BRAND_TEMPLATE_ID"):
                st.warning("A Canva access token exists, but CANVA_BRAND_TEMPLATE_ID is missing.")
            elif not canva_access_token():
                st.info("Canva credentials are present. Click Authorize Canva to obtain an access token for this session.")
            authorization_url = canva_authorization_url()
            if authorization_url:
                st.link_button("Authorize Canva", authorization_url, icon=":material/login:", width="stretch")
                st.caption(f"Canva must redirect back to: {canva_redirect_uri()}")
        else:
            st.info("Add CANVA_CLIENT_ID and CANVA_CLIENT_SECRET to Streamlit secrets. Also set CANVA_REDIRECT_URI and CANVA_BRAND_TEMPLATE_ID.", icon=":material/key:")
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
