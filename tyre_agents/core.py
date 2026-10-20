"""Pure domain objects for a review/content approval workflow.

Publishing is deliberately represented by an adapter interface and an in-memory
adapter. No function in this module performs a real network post.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal, Protocol


class Destination(str, Enum):
    GOOGLE_BUSINESS_PROFILE = "google_business_profile"
    INSTAGRAM_FEED = "instagram_feed"
    INSTAGRAM_STORY = "instagram_story"
    WHATSAPP = "whatsapp"


Salutation = Literal["Dear Sir", "Dear Ma'am", "Dear Customer"]
SALUTATIONS: tuple[Salutation, ...] = ("Dear Sir", "Dear Ma'am", "Dear Customer")
DEFAULT_SALUTATION: Salutation = "Dear Customer"


@dataclass(frozen=True)
class Review:
    review_id: str
    customer_name: str
    rating: int
    text: str
    salutation: str | None = None


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


def select_salutation(review_or_salutation: Review | str | None = None) -> Salutation:
    """Use only an explicit salutation; never infer gender from a name."""
    requested = review_or_salutation.salutation if isinstance(review_or_salutation, Review) else review_or_salutation
    if requested is None:
        return DEFAULT_SALUTATION
    normalized = " ".join(str(requested).strip().split()).casefold()
    for salutation in SALUTATIONS:
        if normalized == salutation.casefold():
            return salutation
    return DEFAULT_SALUTATION


def reply_variants(review: Review, *, salutation: str | None = None) -> tuple[str, ...]:
    """Create distinct rating-aware replies without using the reviewer name."""
    if not 1 <= review.rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    greeting = select_salutation(review if salutation is None else salutation)
    if review.rating >= 4:
        options = (
            f"{greeting}, thank you for the lovely review. We are delighted our tyre service met your expectations.",
            f"{greeting}, we appreciate you taking the time to share this. Your kind feedback means a lot to our team.",
        )
    elif review.rating == 3:
        options = (
            f"{greeting}, thank you for the balanced feedback. We will use it to keep improving our service.",
            f"{greeting}, we appreciate you sharing your experience. We would welcome the chance to do even better next time.",
        )
    else:
        options = (
            f"{greeting}, we are sorry your experience fell short. Please contact us so we can put this right.",
            f"{greeting}, thank you for bringing this to our attention. We apologise and would value the opportunity to resolve it.",
        )
    return options


def generate_reply(review: Review, variant: int | None = None, *, salutation: str | None = None) -> str:
    options = reply_variants(review, salutation=salutation)
    selected = _stable_index(review.review_id, len(options)) if variant is None else variant % len(options)
    return options[selected]


def regenerate_reply(review: Review, current_reply: str | int | None = None, *, current_variant: int | None = None, salutation: str | None = None) -> str:
    options = reply_variants(review, salutation=salutation)
    if current_variant is not None:
        index = current_variant % len(options)
    elif isinstance(current_reply, int):
        index = current_reply % len(options)
    elif current_reply in options:
        index = options.index(current_reply)
    else:
        index = _stable_index(review.review_id, len(options))
    return options[(index + 1) % len(options)]


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
