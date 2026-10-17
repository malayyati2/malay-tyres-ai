"""Small, injectable adapters for external publishing/design APIs.

The default HTTP session is created only when an operation is called. Tests can
pass any requests-compatible session, so importing this module never performs
network I/O.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping


class ProviderError(RuntimeError):
    """A safe provider failure; credentials are deliberately omitted."""


def _session(session: Any | None) -> Any:
    if session is not None:
        return session
    import requests
    return requests.Session()


def _json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise ProviderError("Provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError("Provider returned an unexpected response")
    return payload


def _check(response: Any, operation: str) -> dict[str, Any]:
    if not 200 <= int(response.status_code) < 300:
        raise ProviderError(f"{operation} failed (HTTP {response.status_code})")
    return _json(response)


def _job(payload: Mapping[str, Any], operation: str) -> dict[str, Any]:
    job = payload.get("job")
    if not isinstance(job, dict) or not job.get("id"):
        raise ProviderError(f"{operation} response did not include a job ID")
    return job


def _poll_job(session: Any, url: str, headers: Mapping[str, str], operation: str,
              *, attempts: int = 20, interval_seconds: float = 0.25) -> dict[str, Any]:
    for attempt in range(attempts):
        payload = _check(session.get(url, headers=dict(headers)), operation)
        job = payload.get("job")
        if not isinstance(job, dict):
            raise ProviderError(f"{operation} returned an unexpected job response")
        status = job.get("status")
        if status == "success":
            return job
        if status == "failed":
            error = job.get("error")
            if isinstance(error, dict) and error.get("code"):
                raise ProviderError(f"{operation} failed: {error['code']}")
            raise ProviderError(f"{operation} failed")
        if status != "in_progress":
            raise ProviderError(f"{operation} returned unknown job status")
        if attempt < attempts - 1 and interval_seconds:
            time.sleep(interval_seconds)
    raise ProviderError(f"{operation} timed out while waiting for Canva")


@dataclass
class CanvaAdapter:
    access_token: str
    brand_template_id: str
    base_url: str = "https://api.canva.com/rest/v1"
    session: Any | None = None

    def autofill_export(self, text_fields: Mapping[str, str], *, title: str = "Malay Tyres campaign",
                        export_format: str = "png", poll_attempts: int = 20,
                        poll_interval_seconds: float = 0.25) -> dict[str, Any]:
        """Autofill a pre-built Canva brand template and return its export URL.

        Canva's public REST API supports template autofill and export. It does
        not expose Canva Magic Media text-to-image generation as a REST call,
        so this flow assumes the brand template already contains the visual
        treatment/background and the app fills its text fields automatically.
        """
        if not self.access_token or not self.brand_template_id:
            raise ProviderError("Canva access token and brand template ID are required")
        s = _session(self.session)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        root = self.base_url.rstrip("/")
        field_data = {key: {"type": "text", "text": value} for key, value in text_fields.items()}
        fill = _check(
            s.post(
                f"{root}/autofills",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "type": "create_from_brand_template",
                    "brand_template_id": self.brand_template_id,
                    "title": title,
                    "data": field_data,
                },
            ),
            "Canva autofill",
        )
        fill_job = _job(fill, "Canva autofill")
        fill_result = fill_job if fill_job.get("status") == "success" else _poll_job(
            s, f"{root}/autofills/{fill_job['id']}", headers, "Canva autofill",
            attempts=poll_attempts, interval_seconds=poll_interval_seconds,
        )
        design = fill_result.get("result", {}).get("design", {})
        design_id = design.get("id") or fill_result.get("design", {}).get("id")
        if not design_id:
            raise ProviderError("Canva autofill did not return a design ID")
        export = _check(
            s.post(
                f"{root}/exports",
                headers={**headers, "Content-Type": "application/json"},
                json={"design_id": design_id, "format": {"type": export_format}},
            ),
            "Canva export",
        )
        export_job = _job(export, "Canva export")
        export_result = export_job if export_job.get("status") == "success" else _poll_job(
            s, f"{root}/exports/{export_job['id']}", headers, "Canva export",
            attempts=poll_attempts, interval_seconds=poll_interval_seconds,
        )
        urls = export_result.get("urls") or []
        if not urls or not isinstance(urls[0], str):
            raise ProviderError("Canva export did not return a download URL")
        return {
            "provider": "canva",
            "design_id": design_id,
            "design_url": design.get("url"),
            "autofill_id": fill_job["id"],
            "export_id": export_job["id"],
            "image_url": urls[0],
        }


@dataclass
class InstagramAdapter:
    access_token: str
    instagram_business_account_id: str
    api_version: str = "v21.0"
    base_url: str = "https://graph.facebook.com"
    session: Any | None = None

    def publish_image(self, image_url: str, caption: str = "") -> dict[str, Any]:
        if not self.access_token or not self.instagram_business_account_id:
            raise ProviderError("Instagram access token and business account ID are required")
        if not image_url:
            raise ProviderError("Instagram image URL is required")
        s = _session(self.session); root = f"{self.base_url.rstrip('/')}/{self.api_version}"
        params = {"image_url": image_url, "caption": caption, "access_token": self.access_token}
        created = _check(s.post(f"{root}/{self.instagram_business_account_id}/media", params=params), "Instagram media creation")
        container_id = created.get("id")
        if not container_id: raise ProviderError("Instagram response did not include a container ID")
        published = _check(s.post(f"{root}/{self.instagram_business_account_id}/media_publish",
                                  params={"creation_id": container_id, "access_token": self.access_token}), "Instagram publish")
        return {"provider": "instagram", "container_id": container_id, "media_id": published.get("id")}

    def publish_story(self, image_url: str) -> dict[str, Any]:
        """Publish an image as an Instagram story."""
        if not self.access_token or not self.instagram_business_account_id:
            raise ProviderError("Instagram access token and business account ID are required")
        if not image_url:
            raise ProviderError("Instagram story image URL is required")
        s = _session(self.session)
        root = f"{self.base_url.rstrip('/')}/{self.api_version}"
        created = _check(
            s.post(
                f"{root}/{self.instagram_business_account_id}/media",
                params={
                    "image_url": image_url,
                    "media_type": "STORIES",
                    "access_token": self.access_token,
                },
            ),
            "Instagram story creation",
        )
        container_id = created.get("id")
        if not container_id:
            raise ProviderError("Instagram story response did not include a container ID")
        published = _check(
            s.post(
                f"{root}/{self.instagram_business_account_id}/media_publish",
                params={"creation_id": container_id, "access_token": self.access_token},
            ),
            "Instagram story publish",
        )
        return {"provider": "instagram", "placement": "story", "container_id": container_id, "media_id": published.get("id")}


@dataclass
class WhatsAppAdapter:
    access_token: str
    phone_number_id: str
    recipient_phone_number: str
    api_version: str = "v21.0"
    base_url: str = "https://graph.facebook.com"
    session: Any | None = None

    def send_image(self, image_url: str, caption: str = "") -> dict[str, Any]:
        if not self.access_token or not self.phone_number_id or not self.recipient_phone_number:
            raise ProviderError("WhatsApp credentials and recipient phone number are required")
        if not image_url: raise ProviderError("WhatsApp image URL is required")
        s = _session(self.session); root = f"{self.base_url.rstrip('/')}/{self.api_version}"
        payload = {"messaging_product": "whatsapp", "to": self.recipient_phone_number,
                   "type": "image", "image": {"link": image_url, "caption": caption}}
        result = _check(s.post(f"{root}/{self.phone_number_id}/messages", headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}, json=payload), "WhatsApp image send")
        return {"provider": "whatsapp", "message_id": result.get("messages", [{}])[0].get("id"), "response": result}
