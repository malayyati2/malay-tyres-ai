"""Small, testable adapter for Gemini/Imagen image generation."""

from dataclasses import dataclass
from typing import Any, Protocol


DEFAULT_IMAGEN_MODEL = "imagen-4.0-generate-001"


class ImageModels(Protocol):
    def generate_images(self, *, model: str, prompt: str, config: Any) -> Any:
        """Generate images using an Imagen-capable client."""


class GeminiClient(Protocol):
    models: ImageModels


@dataclass(frozen=True)
class GeneratedImage:
    image_bytes: bytes
    mime_type: str
    model: str


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
        model: str = DEFAULT_IMAGEN_MODEL,
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

            response = client.models.generate_images(
                model=self.model,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio,
                    output_mime_type="image/png",
                ),
            )
            generated = getattr(response, "generated_images", None)
            image = generated[0].image if generated else None
            image_bytes = getattr(image, "image_bytes", None)
            mime_type = getattr(image, "mime_type", None)
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise ValueError("Imagen returned a malformed image response.") from exc

        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ValueError("Imagen returned a malformed image response: image bytes missing.")
        if not isinstance(mime_type, str) or not mime_type:
            raise ValueError("Imagen returned a malformed image response: MIME type missing.")
        return GeneratedImage(image_bytes=image_bytes, mime_type=mime_type, model=self.model)


__all__ = ["DEFAULT_IMAGEN_MODEL", "GeneratedImage", "GeminiImageGenerator"]
