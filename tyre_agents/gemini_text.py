"""Small, testable adapter for Gemini text generation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class CampaignResult:
    """The structured campaign copy returned by Gemini."""

    title: str
    body: str
    image_prompt: str


class GeminiTextGenerator:
    """Generate tyre campaign copy using the official ``google-genai`` client.

    ``client`` is injectable so callers can test this adapter without a network
    connection.  When omitted, the Google client is imported and constructed on
    the first generation request, rather than during application import.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = "gemini-2.5-flash",
        client: Any | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A Gemini API key is required")
        self._api_key = api_key
        self.model = model
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - dependency setup concern
                raise ValueError("The google-genai package is required") from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, topic: str) -> CampaignResult:
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("A campaign topic is required")

        prompt = (
            "Create a concise tyre-business marketing campaign for this topic: "
            f"{topic.strip()}\n"
            "Return JSON only with exactly these string fields: title, body, "
            "image_prompt. Keep the title short, the body suitable for a local "
            "business post, and the image_prompt descriptive."
        )
        try:
            response = self._get_client().models.generate_content(
                model=self.model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            payload = json.loads(response.text)
            if not isinstance(payload, dict):
                raise TypeError
            values = tuple(payload[field] for field in ("title", "body", "image_prompt"))
            if not all(isinstance(value, str) and value.strip() for value in values):
                raise TypeError
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Gemini returned a malformed campaign response") from exc
        return CampaignResult(*(value.strip() for value in values))
