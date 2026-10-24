"""Small, injectable adapters for external publishing/design APIs.

The default HTTP session is created only when an operation is called. Tests can
pass any requests-compatible session, so importing this module never performs
network I/O.
"""
from __future__ import annotations

import time
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping


class ProviderError(RuntimeError):
    """A safe provider failure; credentials are deliberately omitted."""


CANVA_TEXT_LIMITS = {
    "TITLE": 45,
    "BODY": 140,
    "CTA": 20,
}

# These are the Canva image-generation models accepted by the app. The app
# defaults to Craft because the campaign needs realistic marketing visuals.
CANVA_IMAGE_MODELS = {
    "craft_v1_2k",
    "lucid_origin",
    "z_image_turbo",
}

CREATIVE_IMAGE_DIRECTIONS = (
    "low three-quarter front view of a modern Indian SUV parked outside a bright tyre workshop after light rain, with crisp tread detail and soft morning reflections",
    "close-up editorial photograph of a premium alloy wheel and tyre being inspected by a technician in a clean workshop, shallow depth of field and warm side lighting",
    "dynamic road-safety scene with a family SUV preparing for a long highway journey at sunrise, realistic Indian roadside setting and clean open space around the vehicle",
    "wide professional workshop photograph showing a technician checking tyre pressure beside a polished sedan, bright overhead light, natural shadows and a premium service atmosphere",
    "low-angle product-style photograph of a fresh tyre and alloy wheel on a clean charcoal studio floor, subtle yellow accent light, realistic rubber texture and dramatic but natural contrast",
    "overhead lifestyle photograph of a vehicle-care bay with a compact SUV, neatly arranged tools and a technician working carefully, believable detail and documentary realism",
    "side profile photograph of a clean crossover driving on a safe open road with distant Indian landscape, golden-hour light, visible wheel detail and a fresh travel mood",
    "close realistic portrait of a professional tyre technician presenting a checked wheel beside a customer car, friendly service setting, natural expression and uncluttered background",
)

POSTER_LAYOUT_DIRECTIONS = (
    "use a bold diagonal red-and-black composition with the hero vehicle crossing the frame and a clean lower caption panel",
    "use a premium split layout with the realistic workshop photograph on one side and a strong white information panel on the other",
    "use a clean editorial layout with a large hero photograph, a small red service badge, and generous white space for the offer",
    "use a dynamic asymmetric layout with a curved red accent, the vehicle placed off-centre, and a dark footer strip for contact details",
    "use a modern full-bleed photograph with a translucent white text card and restrained red highlights, avoiding the previous poster structure",
    "use a documentary-style layout with a wide hero image, compact service facts, and a strong bottom call-to-action bar",
    "use a high-contrast black, white, and red layout with a close-up wheel detail, a small supporting image area, and a clear contact block",
    "use a bright roadside-travel composition with the hero vehicle in the upper third and a fresh lower section for safety advice and booking details",
)


def fit_canva_text(value: str, limit: int) -> str:
    """Trim Canva overlay copy to a safe character limit, preferring whole words."""
    if limit < 1:
        raise ValueError("Canva text limit must be positive")
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    shortened = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:!?-—")
    return shortened or clean[:limit].rstrip()


def build_canva_text_fields(
    title: str,
    body: str,
    cta: str,
    *,
    title_field: str = "TITLE",
    body_field: str = "BODY",
    cta_field: str = "CTA",
) -> dict[str, str]:
    """Build Canva autofill text with the Malay Tyres overlay limits applied."""
    return {
        title_field: fit_canva_text(title, CANVA_TEXT_LIMITS["TITLE"]),
        body_field: fit_canva_text(body, CANVA_TEXT_LIMITS["BODY"]),
        cta_field: fit_canva_text(cta, CANVA_TEXT_LIMITS["CTA"]),
    }


def fresh_image_prompt(base_prompt: str, variation_id: str | None = None) -> str:
    """Build a materially different, photorealistic prompt for every image request."""
    token = variation_id or uuid.uuid4().hex
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    direction = CREATIVE_IMAGE_DIRECTIONS[int.from_bytes(digest[:4], "big") % len(CREATIVE_IMAGE_DIRECTIONS)]
    return (
        "Create ONE genuinely new, photorealistic premium automotive advertising photograph for Malay Tyres. "
        "Make it look like a real camera photograph, not an illustration, 3D render, collage, or generic stock image. "
        "Do not reuse a previous composition, camera angle, vehicle position, background, lighting setup, or crop. "
        "Show realistic Indian-market vehicles, believable tyre rubber and alloy-wheel detail, natural materials, and accurate perspective. "
        "No readable text, letters, logos, watermark, fake brand badge, phone number, or typography in the generated image. "
        "Leave a clean safe area for the fixed Canva overlay template.\n"
        f"New visual direction for this request: {direction}.\n"
        f"Campaign subject: {str(base_prompt or '').strip()}\n"
        f"Unique generation signature: {token}"
    )


def manual_canva_poster_prompt(
    base_prompt: str,
    variation_id: str | None = None,
    *,
    business_name: str = "Malay Tyres",
    address: str = "74, Dewas Road, Ujjain (M.P.) 456010",
    phone: str = "+91 99260 91313",
) -> str:
    """Create a fresh full-poster prompt for manual Canva AI use.

    This intentionally asks Canva's website to create the whole poster rather
    than asking the app to call Canva's image-generation/autofill APIs.
    """
    token = variation_id or uuid.uuid4().hex
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    visual = CREATIVE_IMAGE_DIRECTIONS[int.from_bytes(digest[:4], "big") % len(CREATIVE_IMAGE_DIRECTIONS)]
    layout = POSTER_LAYOUT_DIRECTIONS[int.from_bytes(digest[4:8], "big") % len(POSTER_LAYOUT_DIRECTIONS)]
    return (
        f"Create a complete, ready-to-publish 4:5 Instagram promotional poster for {business_name}, an Indian tyre and vehicle-care business. "
        "This must be a brand-new poster concept, not a template autofill and not a copy of a previous design. "
        "Generate one photorealistic automotive advertising photograph with a real camera look: accurate vehicle proportions, believable tyre tread and alloy-wheel detail, natural materials, realistic Indian setting, and documentary-quality lighting. "
        f"Visual direction: {visual}. Layout direction: {layout}. Campaign subject: {str(base_prompt or '').strip()}. "
        "Use only a restrained red, black, white, and charcoal brand palette. Include a clean logo area for the Malay Tyres logo, but do not invent or distort a logo. "
        f"Use these exact business details if text is rendered: business name '{business_name}', address '{address}', phone '{phone}'. "
        "Keep all rendered text short, large, high-contrast, correctly spelled, and easy to read on a phone. Do not add invented prices, claims, services, addresses, phone numbers, hashtags, watermarks, or other brands. "
        "Make the composition, crop, camera angle, subject placement, lighting, accent shapes, text hierarchy, and CTA treatment different from previous generations. "
        f"Unique generation signature: {token}. Before publishing, inspect every word and replace any AI-mistyped logo or text in Canva with the real logo and exact business details."
    )


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


DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_IMAGEN_MODEL = DEFAULT_GEMINI_IMAGE_MODEL


class GeneratedImage(dict[str, Any]):
    """Mapping result that also supports attribute access in callers/tests."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _image_part(response: Any) -> tuple[bytes, str]:
    """Extract the first inline image from a google-genai response."""
    parts: list[Any] = []
    for candidate in _value(response, "candidates", []) or []:
        content = _value(candidate, "content")
        parts.extend(_value(content, "parts", []) or [])
    if not parts:
        parts.extend(_value(response, "parts", []) or [])
    for part in parts:
        inline_data = _value(part, "inline_data")
        if inline_data is None:
            continue
        data = _value(inline_data, "data")
        mime_type = _value(inline_data, "mime_type") or _value(inline_data, "mimeType")
        if isinstance(data, str):
            try:
                data = base64.b64decode(data, validate=True)
            except Exception as exc:
                raise ProviderError("Gemini returned invalid image data") from exc
        if isinstance(data, bytes) and data:
            return data, str(mime_type or "image/png")
    raise ProviderError("Gemini did not return an image")


@dataclass
class GeminiImageAdapter:
    """Generate a fresh image with the official google-genai client."""

    api_key: str
    model: str = DEFAULT_GEMINI_IMAGE_MODEL
    client: Any | None = None

    def _get_client(self) -> Any:
        if self.client is not None:
            return self.client
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ProviderError("Gemini API key is required")
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
        except Exception as exc:
            raise ProviderError("Gemini client could not be initialized") from exc
        return self.client

    def generate(self, prompt: str, aspect_ratio: str | None = None) -> GeneratedImage:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("Gemini image prompt is required")
        try:
            from google.genai import types

            config: Any = types.GenerateContentConfig(response_modalities=["IMAGE"])
            if aspect_ratio:
                config.image_config = types.ImageConfig(aspect_ratio=aspect_ratio)
            response = self._get_client().models.generate_content(
                model=self.model,
                contents=prompt.strip(),
                config=config,
            )
            image_bytes, mime_type = _image_part(response)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Gemini image generation failed") from exc
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return GeneratedImage({
            "provider": "gemini",
            "model": self.model,
            "mime_type": mime_type,
            "base64": encoded,
            "data_uri": f"data:{mime_type};base64,{encoded}",
            "image_bytes": image_bytes,
        })

    generate_image = generate


@dataclass
class CanvaImageAdapter:
    """Generate a fresh image through Canva's asynchronous preview API."""

    access_token: str
    base_url: str = "https://api.canva.com/rest/v1"
    session: Any | None = None

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = "square",
        *,
        model: str | None = None,
        idempotency_key: str | None = None,
        poll_attempts: int = 60,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        if not self.access_token:
            raise ProviderError("Canva access token is required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("Canva image prompt is required")
        if poll_attempts < 1:
            raise ProviderError("Canva poll attempts must be positive")
        s = _session(self.session)
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        root = self.base_url.rstrip("/")
        payload: dict[str, Any] = {
            "prompt": prompt.strip()[:5000],
            "aspect_ratio": aspect_ratio,
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
            "asset_upload": {"type": "upload", "asset_name": "Malay Tyres AI image"},
        }
        if model:
            payload["model"] = model
        try:
            created = _check(s.post(f"{root}/image-generations", headers=headers, json=payload), "Canva image generation")
            job = _job(created, "Canva image generation")
            result = job if job.get("status") == "success" else _poll_job(
                s, f"{root}/image-generations/{job['id']}", headers, "Canva image generation",
                attempts=poll_attempts, interval_seconds=poll_interval_seconds,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Canva image generation request failed") from exc
        image = result.get("result", {}).get("image", {}) if isinstance(result.get("result"), dict) else {}
        image_url = image.get("url") if isinstance(image, dict) else None
        asset = image.get("asset", {}) if isinstance(image, dict) else {}
        if not isinstance(image_url, str) or not image_url:
            raise ProviderError("Canva image generation did not return an image URL")
        return {
            "provider": "canva",
            "job_id": job["id"],
            "image_url": image_url,
            "asset_id": asset.get("id") if isinstance(asset, dict) else None,
            "aspect_ratio": aspect_ratio,
            "model": model,
            "idempotency_key": payload["idempotency_key"],
        }

    generate_image = generate


GeminiImageGenerator = GeminiImageAdapter
CanvaImageGenerator = CanvaImageAdapter


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
                             model: str | None = None,
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
        if model and model not in CANVA_IMAGE_MODELS:
            raise ProviderError("Unsupported Canva image model")
        s = _session(self.session)
        root = self.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        request = {
            "prompt": prompt.strip()[:10000],
            "idempotency_key": str(uuid.uuid4()),
            "aspect_ratio": aspect_ratio,
            "asset_upload": {"type": "upload", "asset_name": name[:255]},
        }
        if model:
            request["model"] = model
        payload = _check(
            s.post(
                f"{root}/image-generations",
                headers={**headers, "Content-Type": "application/json"},
                json=request,
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
