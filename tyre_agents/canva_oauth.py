"""PKCE helpers for Canva Connect OAuth."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode


CANVA_AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
CANVA_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
DEFAULT_CANVA_SCOPES = (
    "design:content:write",
    "design:content:read",
    "design:meta:read",
    "asset:write",
    "brandtemplate:meta:read",
    "brandtemplate:content:read",
)


@dataclass(frozen=True)
class PkceRequest:
    state: str
    code_verifier: str
    authorization_url: str


def create_pkce_request(client_id: str, redirect_uri: str, scopes: tuple[str, ...] = DEFAULT_CANVA_SCOPES) -> PkceRequest:
    if not client_id or not redirect_uri:
        raise ValueError("Canva client ID and redirect URI are required")
    code_verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(scopes),
            "response_type": "code",
            "client_id": client_id,
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )
    return PkceRequest(state=state, code_verifier=code_verifier, authorization_url=f"{CANVA_AUTHORIZE_URL}?{query}")


def exchange_code(
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    session: Any | None = None,
) -> Mapping[str, Any]:
    if not all((code, code_verifier, client_id, client_secret, redirect_uri)):
        raise ValueError("Canva OAuth code exchange is missing required inputs")
    if session is None:
        import requests
        session = requests.Session()
    response = session.post(
        CANVA_TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
    )
    if not 200 <= int(response.status_code) < 300:
        raise ValueError(f"Canva OAuth token exchange failed (HTTP {response.status_code})")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("Canva OAuth response did not include an access token")
    return payload


__all__ = ["DEFAULT_CANVA_SCOPES", "PkceRequest", "create_pkce_request", "exchange_code"]
