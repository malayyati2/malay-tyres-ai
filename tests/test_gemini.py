import pytest

from tyre_agents.gemini import GeminiImageGenerator


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_images(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.models = FakeModels(response)


class FakeImage:
    image_bytes = b"png-data"
    mime_type = "image/png"


class FakeGenerated:
    image = FakeImage()


class FakeResponse:
    generated_images = [FakeGenerated()]


def test_missing_api_key_is_safe_and_explicit():
    with pytest.raises(ValueError, match="API key is required"):
        GeminiImageGenerator("  ")


def test_extracts_bytes_mime_type_and_model_without_google_network():
    client = FakeClient(FakeResponse())
    result = GeminiImageGenerator("test-key", client=client).generate(
        "a tyre shop", "16:9"
    )

    assert (result.image_bytes, result.mime_type, result.model) == (
        b"png-data",
        "image/png",
        "imagen-4.0-generate-001",
    )
    call = client.models.calls[0]
    assert call["model"] == "imagen-4.0-generate-001"
    assert call["prompt"] == "a tyre shop"
    assert call["config"].aspect_ratio == "16:9"
    assert call["config"].number_of_images == 1
    assert call["config"].output_mime_type == "image/png"
