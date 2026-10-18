"""Core domain logic for the tyre-business content workflow."""

from .core import (
    ApprovalQueue,
    ContentDraft,
    Destination,
    InMemoryPublisher,
    Review,
    generate_content,
    generate_reply,
    publish_approved,
)
from .canva_oauth import PkceRequest, create_pkce_request, exchange_code
from .gemini_text import CampaignResult, GeminiTextGenerator
from .providers import CanvaAdapter, InstagramAdapter, ProviderError, WhatsAppAdapter

__all__ = [
    "ApprovalQueue",
    "ContentDraft",
    "Destination",
    "InMemoryPublisher",
    "Review",
    "generate_content",
    "generate_reply",
    "publish_approved",
    "PkceRequest",
    "create_pkce_request",
    "exchange_code",
    "CampaignResult",
    "GeminiTextGenerator",
    "CanvaAdapter",
    "InstagramAdapter",
    "WhatsAppAdapter",
    "ProviderError",
]
