import pytest

from tyre_agents.gemini import GeminiImageGenerator, explain_image_error


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.models = FakeModels(response)


class FakeImage:
    data = b"png-data"
    mime_type = "image/png"


class FakeGenerated:
    inline_data = FakeImage()


class FakeContent:
    parts = [FakeGenerated()]


class FakeCandidate:
    content = FakeContent()


class FakeResponse:
    candidates = [FakeCandidate()]


def test_missing_api_key_is_safe_and_explicit():
    with pytest.raises(ValueError, match="API key is required"):
        GeminiImageGenerator("  ")


def test_quota_errors_are_explained_without_exposing_provider_payloads():
    message = explain_image_error(RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded"))

    assert "no available image-generation quota" in message
    assert "Enable billing" in message


def test_extracts_bytes_mime_type_and_model_without_google_network():
    client = FakeClient(FakeResponse())
    result = GeminiImageGenerator("test-key", client=client).generate(
        "a tyre shop", "16:9"
    )

    assert (result.image_bytes, result.mime_type, result.model) == (
        b"png-data",
        "image/png",
        "gemini-2.5-flash-image",
    )
    call = client.models.calls[0]
    assert call["model"] == "gemini-2.5-flash-image"
    assert call["contents"] == "a tyre shop"
    assert call["config"].response_modalities == ["IMAGE"]
    assert call["config"].image_config.aspect_ratio == "16:9"
