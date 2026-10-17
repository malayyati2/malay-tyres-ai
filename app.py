from __future__ import annotations

from datetime import datetime
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
    connected = sum(st.session_state.connections.values())
    cols = st.columns(4)
    cols[0].metric("Reviews to handle", str(pending_reviews), "Daily check")
    cols[1].metric("Content awaiting approval", str(pending_draft), "Human gate")
    cols[2].metric("Published this session", str(published), "Demo receipts")
    cols[3].metric("Connections ready", f"{connected}/4", "Add credentials")


def render_overview() -> None:
    render_metric_row()
    st.space("small")
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        with st.container(border=True):
            st.subheader("Your agent team")
            agents = [
                ("Review agent", "Checks GBP reviews, writes a distinct polite reply, and waits for approval.", "3 ready", "green"),
                ("Content agent", "Creates a campaign caption, channel variants and an image prompt for Gemini or Canva.", "Draft on demand", "blue"),
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
        st.markdown("**Image prompt for Gemini / Canva**")
        st.code(draft.image_prompt, language="text")
        st.markdown("**Publish to**")
        st.write(" · ".join(DESTINATION_LABELS[destination] for destination in draft.destinations) or "No channel selected")
        st.caption("The prompt is ready to paste into Gemini or Canva. A production connector can replace this stub with generated media.")


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
            submitted = st.form_submit_button("Generate content + image prompt", type="primary", icon=":material/auto_awesome:", width="stretch")
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
            add_activity("Content agent", f"Generated copy and an image prompt for {topic}")
            st.success("Draft created. Review the copy and image prompt below.", icon=":material/check_circle:")

    draft = st.session_state.draft
    if draft is None:
        st.info("No draft yet. Start with a campaign brief above.", icon=":material/edit_note:")
        return

    st.space("small")
    render_draft_preview(draft)
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
            receipts = publish_approved(draft, st.session_state.publisher)
            st.session_state.publishing_log.extend(receipts)
            add_activity("Publishing agent", f"Routed approved campaign to {len(receipts)} selected channel(s)")
            st.success(f"Demo publish complete: {len(receipts)} receipt(s) created. No external post was sent.", icon=":material/check_circle:")
            for receipt in receipts:
                st.code(receipt, language="text")


def render_connections() -> None:
    st.header("Connections and guardrails")
    st.write("Connect real providers here later. The demo keeps secrets out of the codebase and uses safe adapters until you configure APIs.")
    st.warning("This build does not make live calls to Google, Gemini, Canva, Instagram or WhatsApp. That protects your business while the approval flow is being tested.", icon=":material/shield:")
    descriptions = {
        "Google Business Profile": "Read new reviews and publish approved replies through Google OAuth.",
        "Gemini / Antigravity": "Generate campaign variations and image prompts using your chosen AI provider.",
        "Canva": "Turn the approved image prompt into a branded creative using a Canva connection.",
        "Instagram + WhatsApp": "Publish approved feed, story and WhatsApp assets to selected destinations.",
    }
    for name, description in descriptions.items():
        with st.container(border=True):
            status = st.session_state.connections[name]
            left, right = st.columns([3, 1], vertical_alignment="center")
            with left:
                st.markdown(f"**{name}**")
                st.caption(description)
            with right:
                badge("Connected" if status else "Not connected", "green" if status else "gray")
                if st.button("Disconnect" if status else "Connect demo", key=f"connection_{name}"):
                    st.session_state.connections[name] = not status
                    add_activity("System", f"{'Connected' if not status else 'Disconnected'} {name}")
                    st.rerun()
    st.space("small")
    with st.container(border=True):
        st.subheader("Production checklist")
        st.markdown(":material/check_box_outline_blank: Add OAuth/API credentials with `st.secrets`")
        st.markdown(":material/check_box_outline_blank: Replace the in-memory adapters with provider clients")
        st.markdown(":material/check_box_outline_blank: Add a durable scheduler for the daily review check")
        st.markdown(":material/check_box_outline_blank: Confirm Instagram story image dimensions and WhatsApp template rules")


initialize_state()
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
