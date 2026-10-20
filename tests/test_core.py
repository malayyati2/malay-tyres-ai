import pytest

from tyre_agents.core import (
    ApprovalQueue,
    ContentDraft,
    Destination,
    InMemoryPublisher,
    Review,
    generate_content,
    generate_reply,
    regenerate_reply,
    reply_variants,
    select_salutation,
    publish_approved,
)


def test_reply_variation_is_deterministic_and_varied():
    replies = {generate_reply(Review(f"r{i}", "Sam", 5, "Great")) for i in range(8)}
    assert len(replies) == 2
    assert generate_reply(Review("r1", "Sam", 5, "Great")) == generate_reply(Review("r1", "Sam", 5, "Great"))


def test_replies_do_not_use_names_and_can_be_regenerated():
    review = Review("review-name", "Alexandra", 5, "Excellent service")
    assert select_salutation(review) == "Dear Customer"
    first = generate_reply(review)
    second = regenerate_reply(review, first, salutation="Dear Ma'am")
    assert "Alexandra" not in first
    assert "Alexandra" not in second
    assert second.startswith("Dear Ma'am,")
    assert second != first


def test_only_explicit_salutations_are_accepted():
    assert select_salutation("Dear Sir") == "Dear Sir"
    assert select_salutation("female") == "Dear Customer"
    assert generate_reply(Review("r-sir", "Taylor", 5, "Great", "Dear Sir")).startswith("Dear Sir,")
    assert len(reply_variants(Review("r-variants", "Taylor", 3, "Okay"))) == 2


def test_approval_gates_publishing():
    draft = generate_content("checking tyre pressure")
    queue = ApprovalQueue((draft,))
    publisher = InMemoryPublisher()
    with pytest.raises(ValueError, match="approved"):
        publish_approved(queue.get(draft.draft_id), publisher)
    approved = queue.approve(draft.draft_id)
    assert approved.approved is True
    assert publish_approved(approved, publisher) == ()


def test_destination_routing_uses_selected_channels_only():
    draft = ContentDraft(
        "d1", "Title", "Body", "Prompt",
        (Destination.GOOGLE_BUSINESS_PROFILE, Destination.INSTAGRAM_STORY, Destination.WHATSAPP),
        approved=True,
    )
    publisher = InMemoryPublisher()
    receipts = publish_approved(draft, publisher)
    assert [destination for _, destination in publisher.published] == list(draft.destinations)
    assert receipts == ("stub:d1:google_business_profile", "stub:d1:instagram_story", "stub:d1:whatsapp")
