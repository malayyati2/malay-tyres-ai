"""Pure domain objects for a review/content approval workflow.

Publishing is deliberately represented by an adapter interface and an in-memory
adapter. No function in this module performs a real network post.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol


class Destination(str, Enum):
    GOOGLE_BUSINESS_PROFILE = "google_business_profile"
    INSTAGRAM_FEED = "instagram_feed"
    INSTAGRAM_STORY = "instagram_story"
    WHATSAPP = "whatsapp"


@dataclass(frozen=True)
class Review:
    review_id: str
    customer_name: str
    rating: int
    text: str


@dataclass(frozen=True)
class ContentDraft:
    draft_id: str
    title: str
    body: str
    image_prompt: str
    destinations: tuple[Destination, ...] = ()
    approved: bool = False


class ApprovalQueue:
    """Small deterministic queue; approval returns a new immutable draft."""

    def __init__(self, drafts: tuple[ContentDraft, ...] = ()) -> None:
        self._drafts: dict[str, ContentDraft] = {draft.draft_id: draft for draft in drafts}

    def add(self, draft: ContentDraft) -> None:
        self._drafts[draft.draft_id] = draft

    def get(self, draft_id: str) -> ContentDraft:
        return self._drafts[draft_id]

    def approve(self, draft_id: str) -> ContentDraft:
        approved = replace(self.get(draft_id), approved=True)
        self._drafts[draft_id] = approved
        return approved

    def pending(self) -> tuple[ContentDraft, ...]:
        return tuple(draft for draft in self._drafts.values() if not draft.approved)


def generate_reply(review: Review) -> str:
    """Generate a polite response using rating-aware, deterministic wording."""
    if not 1 <= review.rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    name = review.customer_name.strip() or "there"
    if review.rating >= 4:
        options = (
            f"Thank you, {name}! We are delighted you were happy with your tyre service.",
            f"Thanks so much, {name}, for the kind review. We appreciate your trust in our team!",
        )
    elif review.rating == 3:
        options = (
            f"Thank you for your feedback, {name}. We will use it to keep improving our service.",
            f"Thanks for sharing your experience, {name}. We would welcome the chance to do even better next time.",
        )
    else:
        options = (
            f"We are sorry your experience fell short, {name}. Please contact us so we can put this right.",
            f"Thank you for telling us, {name}. We apologise and would appreciate the opportunity to resolve this.",
        )
    return options[_stable_index(review.review_id, len(options))]


def _stable_index(value: str, size: int) -> int:
    return sum(ord(char) for char in value) % size


def generate_content(topic: str, draft_id: str = "draft-1") -> ContentDraft:
    """Create a channel-neutral promotional draft and image-generation prompt."""
    clean_topic = topic.strip()
    if not clean_topic:
        raise ValueError("topic must not be empty")
    return ContentDraft(
        draft_id=draft_id,
        title=f"Tyre tip: {clean_topic}",
        body=f"Keep your journey safe with our latest advice on {clean_topic}. Visit our tyre team for friendly, expert help.",
        image_prompt=f"A clean, bright professional tyre-service photograph illustrating {clean_topic}; no logos or text",
    )


class Publisher(Protocol):
    def publish(self, draft: ContentDraft, destination: Destination) -> str: ...


@dataclass
class InMemoryPublisher:
    """Test adapter recording intended posts instead of contacting any service."""

    published: list[tuple[str, Destination]] = field(default_factory=list)

    def publish(self, draft: ContentDraft, destination: Destination) -> str:
        self.published.append((draft.draft_id, destination))
        return f"stub:{draft.draft_id}:{destination.value}"


def publish_approved(draft: ContentDraft, publisher: Publisher) -> tuple[str, ...]:
    """Route an approved draft to each selected destination through an adapter."""
    if not draft.approved:
        raise ValueError("only approved drafts may be published")
    return tuple(publisher.publish(draft, destination) for destination in draft.destinations)


SAMPLE_REVIEWS = (
    Review("review-1", "Aisha", 5, "Quick and friendly service."),
    Review("review-2", "Daniel", 3, "Good work, though I waited a little."),
    Review("review-3", "Priya", 2, "The booking was not ready on time."),
)
