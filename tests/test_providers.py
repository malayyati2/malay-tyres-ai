import pytest

from tyre_agents.providers import CanvaAdapter, InstagramAdapter, ProviderError, WhatsAppAdapter


class Response:
    def __init__(self, payload, status_code=200): self.payload, self.status_code = payload, status_code
    def json(self): return self.payload


class FakeSession:
    def __init__(self, responses): self.responses, self.calls = iter(responses), []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_canva_upload_autofill_export_routes_and_returns_receipt():
    fake = FakeSession([
        Response({"job": {"id": "fill-1", "status": "success", "result": {"design": {"id": "design-1", "url": "https://canva.test/design/design-1"}}}}),
        Response({"job": {"id": "export-1", "status": "success", "urls": ["https://canva.test/export.png"]}}),
    ])
    receipt = CanvaAdapter("secret", "bt", base_url="https://canva.test/rest/v1", session=fake).autofill_export({"TITLE": "Sale"})
    assert receipt["export_id"] == "export-1"
    assert receipt["image_url"] == "https://canva.test/export.png"
    assert [call[0] for call in fake.calls] == ["https://canva.test/rest/v1/autofills", "https://canva.test/rest/v1/exports"]
    assert fake.calls[0][1]["json"]["data"]["TITLE"] == {"type": "text", "text": "Sale"}
    assert fake.calls[1][1]["json"]["design_id"] == "design-1"


def test_meta_adapters_use_configured_graph_routes():
    insta_fake = FakeSession([Response({"id": "c1"}), Response({"id": "m1"})])
    insta = InstagramAdapter("token", "ig", api_version="v99", base_url="https://meta.test", session=insta_fake)
    assert insta.publish_image("https://img.test/a.jpg", "hello")["media_id"] == "m1"
    assert [x[0] for x in insta_fake.calls] == ["https://meta.test/v99/ig/media", "https://meta.test/v99/ig/media_publish"]
    wa_fake = FakeSession([Response({"messages": [{"id": "w1"}]})])
    receipt = WhatsAppAdapter("token", "phone", "+1555", base_url="https://meta.test", session=wa_fake).send_image("https://img.test/a.jpg")
    assert receipt["message_id"] == "w1"
    assert wa_fake.calls[0][1]["json"]["to"] == "+1555"


def test_errors_are_safe_and_do_not_expose_token():
    fake = FakeSession([Response({"error": "bad"}, 401)])
    with pytest.raises(ProviderError, match="HTTP 401") as error:
        InstagramAdapter("TOP-SECRET", "ig", session=fake).publish_image("url")
    assert "TOP-SECRET" not in str(error.value)


def test_canva_native_image_generation_saves_asset_and_polls_job():
    fake = FakeSession([
        Response({"job": {"id": "image-job-1", "status": "in_progress"}}),
        Response({"job": {"id": "image-job-1", "status": "success", "result": {
            "image": {"asset": {"id": "canva-asset-1", "name": "Malay Tyres visual"}}
        }}}),
    ])
    adapter = CanvaAdapter("secret-token", "template", base_url="https://canva.test/rest/v1", session=fake)

    asset_id = adapter.generate_image_asset(
        "Photorealistic Indian SUV and premium tyre shop",
        name="Malay Tyres visual",
        poll_attempts=2,
        poll_interval_seconds=0,
    )

    assert asset_id == "canva-asset-1"
    assert fake.calls[0][0] == "https://canva.test/rest/v1/image-generations"
    request = fake.calls[0][1]["json"]
    assert request["asset_upload"] == {"type": "upload", "asset_name": "Malay Tyres visual"}
    assert request["prompt"] == "Photorealistic Indian SUV and premium tyre shop"
    assert fake.calls[1][0] == "https://canva.test/rest/v1/image-generations/image-job-1"
