"""Small, testable adapter for Gemini image generation."""

from dataclasses import dataclass
from typing import Any, Protocol


DEFAULT_GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
# Kept as an alias for callers that used the first Imagen-based prototype.
DEFAULT_IMAGEN_MODEL = DEFAULT_GEMINI_IMAGE_MODEL


class ImageModels(Protocol):
    def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
        """Generate image content using a Gemini image-capable client."""


class GeminiClient(Protocol):
    models: ImageModels


@dataclass(frozen=True)
class GeneratedImage:
    image_bytes: bytes
    mime_type: str
    model: str


def explain_image_error(error: Exception) -> str:
    """Turn common provider failures into safe, actionable UI text."""
    message = str(error)
    upper_message = message.upper()
    if "429" in message or "RESOURCE_EXHAUSTED" in upper_message or "QUOTA" in upper_message:
        return (
            "Gemini accepted the API key, but this Google project has no available image-generation quota. "
            "Enable billing or use a project with image-model quota, then try again."
        )
    if "404" in message or "NOT_FOUND" in upper_message or "MODEL IS NOT FOUND" in upper_message:
        return (
            "The configured Gemini image model is not available for this API project. "
            "Check the selected model and the Google AI API access for this key."
        )
    if "401" in message or "403" in message or "UNAUTHENTICATED" in upper_message or "PERMISSION_DENIED" in upper_message:
        return "Gemini rejected the API key. Check that the key is active and has access to the Gemini API."
    return "Gemini image generation failed. Check the API key, model access, quota and network connection."


class GeminiImageGenerator:
    """Generate an image through the official ``google-genai`` client.

    A client can be injected for tests or callers that manage client creation.
    When omitted, the client is created lazily, so importing this module never
    performs network work.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = DEFAULT_GEMINI_IMAGE_MODEL,
        client: GeminiClient | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("A Gemini API key is required to generate images.")
        self.model = model
        self._client = client
        self._api_key = api_key

    def generate(self, prompt: str, aspect_ratio: str) -> GeneratedImage:
        if not prompt or not prompt.strip():
            raise ValueError("An image prompt is required.")
        client = self._client
        if client is None:
            from google import genai

            client = genai.Client(api_key=self._api_key)
            self._client = client

        try:
            from google.genai import types

            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
                ),
            )
            candidates = getattr(response, "candidates", None) or []
            parts = getattr(candidates[0].content, "parts", None) if candidates else None
            inline_data = next(
                (getattr(part, "inline_data", None) for part in (parts or []) if getattr(part, "inline_data", None)),
                None,
            )
            image_bytes = getattr(inline_data, "data", None)
            mime_type = getattr(inline_data, "mime_type", None)
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise ValueError("Gemini returned a malformed image response.") from exc

        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ValueError("Gemini returned a malformed image response: image bytes missing.")
        if not isinstance(mime_type, str) or not mime_type:
            raise ValueError("Gemini returned a malformed image response: MIME type missing.")
        return GeneratedImage(image_bytes=image_bytes, mime_type=mime_type, model=self.model)


__all__ = [
    "DEFAULT_GEMINI_IMAGE_MODEL",
    "DEFAULT_IMAGEN_MODEL",
    "GeneratedImage",
    "GeminiImageGenerator",
    "explain_image_error",
]
