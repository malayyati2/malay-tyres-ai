from __future__ import annotations

from datetime import datetime
import os
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


def canva_access_token() -> str:
    return str(st.session_state.get("canva_access_token") or secret("CANVA_ACCESS_TOKEN"))


def canva_redirect_uri() -> str:
    configured = secret("CANVA_REDIRECT_URI")
    if configured:
        return configured
    return "http://localhost:8501"


def handle_canva_oauth_callback() -> None:
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    if not code or not returned_state or st.session_state.get("canva_last_code") == code:
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
    ready = int(canva_ready()) + int(instagram_ready()) + int(whatsapp_ready())
    cols[3].metric("Connections ready", f"{ready}/3", "Canva + Meta")


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
            draft = generate_content(topic, draft_id=draft_id)
            draft = ContentDraft(
                draft_id=draft.draft_id,
                title=f"{draft.title} · {tone}",
                body=draft.body,
                image_prompt=draft.image_prompt,
                destinations=selected_destinations_from_labels(destinations),
            )
            st.session_state.queue.add(draft)
            st.session_state.draft = draft
            st.session_state.generated_design = None
            st.session_state.design_error = None
            add_activity("Content agent", f"Generated copy and a Canva design brief for {topic}")
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
        "Canva": "Create and export the branded visual from a Canva brand template.",
        "Instagram": "Publish approved feed posts and stories through the Instagram Graph API.",
        "WhatsApp": "Send approved image messages through the WhatsApp Cloud API.",
    }
    for name, description in descriptions.items():
        with st.container(border=True):
            key_ready = {
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
