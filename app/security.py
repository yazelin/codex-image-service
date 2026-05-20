from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


def generate_api_key() -> str:
    return f"cimg_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def constant_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def verify_signed_payload(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
        expected = _b64encode(
            hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def create_admin_session(username: str, secret: str, ttl_seconds: int = 86400) -> str:
    return sign_payload({"sub": username, "exp": int(time.time()) + ttl_seconds}, secret)


def verify_admin_session(token: str | None, secret: str) -> str | None:
    if not token:
        return None
    payload = verify_signed_payload(token, secret)
    if not payload:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None

