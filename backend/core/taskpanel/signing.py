"""Exact-byte HMAC-SHA256 request signing (TaskPanel integration guide §9.1 / §15.1).

Canonical string (UTF-8, LF-joined, no trailing LF):
    METHOD, EXACT_PATH_AND_QUERY, KEY_ID, TIMESTAMP, NONCE, SHA256(actor_grant), SHA256(raw_body)
"""
import hashlib
import hmac
import json
import time
import uuid


def serialize(payload) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def canonical_string(method: str, target: str, key_id: str, stamp: str, nonce: str, grant: str, body: bytes) -> bytes:
    return "\n".join(
        [
            method.upper(),
            target,
            key_id,
            stamp,
            nonce,
            hashlib.sha256(grant.encode("utf-8")).hexdigest(),
            hashlib.sha256(body).hexdigest(),
        ]
    ).encode("utf-8")


def sign(method: str, target: str, payload, key_id: str, secret: str, grant: str,
         *, stamp: str | None = None, nonce: str | None = None) -> tuple[bytes, dict]:
    """Return (body_bytes, headers). Send body_bytes exactly; never re-serialize after signing."""
    body = serialize(payload)
    stamp = stamp or str(int(time.time()))
    nonce = nonce or str(uuid.uuid4())
    signature = hmac.new(secret.encode("utf-8"), canonical_string(method, target, key_id, stamp, nonce, grant, body),
                         hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Integration-Key-Id": key_id,
        "X-Integration-Timestamp": stamp,
        "X-Integration-Nonce": nonce,
        "X-Actor-Grant": grant,
        "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
        "X-Integration-Signature": "v1=" + signature,
    }
    if isinstance(payload, dict) and payload.get("command_id"):
        headers["Idempotency-Key"] = payload["command_id"]
    return body, headers
