import json

import pytest

from tyre_agents.gemini_text import CampaignResult, GeminiTextGenerator


class FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return type("Response", (), {"text": json.dumps(self.payload)})()


class FakeClient:
    def __init__(self, payload):
        self.models = FakeModels(payload)


def test_successfully_parses_structured_campaign_response():
    client = FakeClient({"title": "Safer journeys", "body": "Book a tyre check.", "image_prompt": "A tyre shop"})
    generator = GeminiTextGenerator("test-key", client=client)

    result = generator.generate("summer road trips")

    assert result == CampaignResult("Safer journeys", "Book a tyre check.", "A tyre shop")
    assert client.models.calls[0]["model"] == "gemini-2.5-flash"
    assert "test-key" not in client.models.calls[0]["contents"]


def test_missing_api_key_is_rejected_without_client_creation():
    with pytest.raises(ValueError, match="API key"):
        GeminiTextGenerator("  ")
