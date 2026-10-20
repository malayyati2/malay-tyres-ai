"""Small, injectable adapters for external publishing/design APIs.

The default HTTP session is created only when an operation is called. Tests can
pass any requests-compatible session, so importing this module never performs
network I/O.
"""
from __future__ import annotations

import time
import base64
import json
import uuid
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
        # Canva often returns a safe, actionable error code/message in JSON.
        # Keep it useful without ever including request headers or access tokens.
        detail = ""
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                detail = ": " + " — ".join(str(x) for x in (code, message) if x)
            elif isinstance(error, str):
                detail = ": " + error
        except Exception:
            pass
        raise ProviderError(f"{operation} failed (HTTP {response.status_code}){detail}")
    return _json(response)


def _job(payload: Mapping[str, Any], operation: str) -> dict[str, Any]:
    job = payload.get("job")
    if not isinstance(job, dict) or not job.get("id"):
        raise ProviderError(f"{operation} response did not include a job ID")
    return job


def _poll_job(session: Any, url: str, headers: Mapping[str, str], operation: str,
              *, attempts: int = 60, interval_seconds: float = 1.0) -> dict[str, Any]:
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
            if isinstance(error, dict):
                details = " — ".join(str(error[k]) for k in ("code", "message") if error.get(k))
                if details:
                    raise ProviderError(f"{operation} failed: {details}")
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

    def upload_image_asset(self, image_bytes: bytes, *, name: str = "Malay Tyres AI visual",
                           poll_attempts: int = 60, poll_interval_seconds: float = 1.0) -> str:
        """Upload a freshly generated PNG to Canva and return its asset ID.

        Requires Canva OAuth scopes asset:write and asset:read.
        """
        if not self.access_token:
            raise ProviderError("Canva access token is required to upload the generated image")
        if not image_bytes:
            raise ProviderError("Image generation returned an empty image")
        s = _session(self.session)
        root = self.base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/octet-stream",
            "Asset-Upload-Metadata": json.dumps({"name_base64": base64.b64encode(name[:50].encode("utf-8")).decode("ascii")}),
        }
        payload = _check(s.post(f"{root}/asset-uploads", headers=headers, data=image_bytes), "Canva image upload")
        job = _job(payload, "Canva image upload")
        result = job if job.get("status") == "success" else _poll_job(
            s, f"{root}/asset-uploads/{job['id']}",
            {"Authorization": f"Bearer {self.access_token}"}, "Canva image upload",
            attempts=poll_attempts, interval_seconds=poll_interval_seconds,
        )
        asset = result.get("asset") or result.get("result", {}).get("asset") or {}
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if not asset_id:
            raise ProviderError("Canva image upload completed but did not return an asset ID")
        return str(asset_id)

    def generate_image_asset(self, prompt: str, *, name: str = "Malay Tyres AI visual",
                             aspect_ratio: str = "square", poll_attempts: int = 60,
                             poll_interval_seconds: float = 1.0) -> str:
        """Generate an image with Canva AI and save it directly to the user's Canva assets.

        Canva currently documents this endpoint as a preview API. It consumes Canva AI
        credits and requires the user's access token to have the asset:write scope.
        """
        if not self.access_token:
            raise ProviderError("Canva access token is required for image generation")
        if not prompt or not prompt.strip():
            raise ProviderError("A non-empty image prompt is required")
        allowed_ratios = {"square", "landscape", "portrait", "landscape_3_2", "portrait_2_3", "portrait_4_5"}
        if aspect_ratio not in allowed_ratios:
            raise ProviderError("Unsupported Canva image aspect ratio")
        s = _session(self.session)
        root = self.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        payload = _check(
            s.post(
                f"{root}/image-generations",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "prompt": prompt.strip()[:5000],
                    "idempotency_key": str(uuid.uuid4()),
                    "aspect_ratio": aspect_ratio,
                    "asset_upload": {"type": "upload", "asset_name": name[:255]},
                },
            ),
            "Canva image generation",
        )
        job = _job(payload, "Canva image generation")
        result = job if job.get("status") == "success" else _poll_job(
            s, f"{root}/image-generations/{job['id']}", headers, "Canva image generation",
            attempts=poll_attempts, interval_seconds=poll_interval_seconds,
        )
        generation_result = result.get("result", {})
        image = generation_result.get("image", {}) if isinstance(generation_result, dict) else {}
        asset = image.get("asset", {}) if isinstance(image, dict) else {}
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if not asset_id:
            error = result.get("error", {}) if isinstance(result, dict) else {}
            detail = " — ".join(str(error[k]) for k in ("code", "message") if isinstance(error, dict) and error.get(k))
            raise ProviderError("Canva image generation completed without a saved asset" + (f": {detail}" if detail else ""))
        return str(asset_id)

    def autofill_export(self, text_fields: Mapping[str, str], *, title: str = "Malay Tyres campaign",
                        image_fields: Mapping[str, str] | None = None,
                        export_format: str = "png", poll_attempts: int = 60,
                        poll_interval_seconds: float = 1.0) -> dict[str, Any]:
        """Autofill text and optional uploaded image assets into a Canva Brand Template."""
        if not self.access_token or not self.brand_template_id:
            raise ProviderError("Canva access token and brand template ID are required")
        s = _session(self.session)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        root = self.base_url.rstrip("/")
        field_data = {key: {"type": "text", "text": value} for key, value in text_fields.items()}
        for key, asset_id in (image_fields or {}).items():
            field_data[key] = {"type": "image", "asset_id": asset_id}
        fill = _check(
            s.post(
                f"{root}/autofills",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    # Canva's current REST schema infers create_from_brand_template
                    # from brand_template_id. A legacy `type` key is not required.
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
        fill_payload = fill_result.get("result", {})
        design = fill_payload.get("design", {}) if isinstance(fill_payload, dict) else {}
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
        design_urls = design.get("urls", {}) if isinstance(design.get("urls", {}), dict) else {}
        return {
            "provider": "canva",
            "design_id": design_id,
            "design_url": design_urls.get("edit_url") or design.get("url") or design_urls.get("view_url"),
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
