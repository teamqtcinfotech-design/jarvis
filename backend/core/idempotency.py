"""Idempotency-Key handling for mutating endpoints.

A retried request with the same key and body returns the original result.
The same key with a different body is rejected (409). The key is claimed
before work starts, so two concurrent retries cannot both execute.
"""
import functools
import json

from django.db import IntegrityError
from rest_framework.response import Response

from .crypto import decrypt, encrypt
from .models import CommandReceipt
from .services import canonical_hash, jsonable

PENDING = 0


def _seal(data) -> dict:
    """Receipts keep a copy of the response (it can contain a private task title), so it is encrypted at rest."""
    return {"enc": encrypt(json.dumps(jsonable(data), ensure_ascii=False))}


def _unseal(stored):
    if isinstance(stored, dict) and set(stored) == {"enc"}:
        try:
            return json.loads(decrypt(stored["enc"]))
        except ValueError:
            return {}
    return stored  # receipts written before encryption was added


def error(code: str, message: str, status: int, **extra) -> Response:
    body = {"code": code, "message": message, "retryable": status >= 500 or status == 429}
    body.update(extra)
    return Response(body, status=status)


def idempotent(endpoint: str):
    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, request, *args, **kwargs):
            key = (request.headers.get("Idempotency-Key") or "").strip()
            if not key or len(key) > 100:
                return error("idempotency_key_required", "Idempotency-Key header (max 100 chars) is required.", 400)
            request_hash = canonical_hash({"endpoint": endpoint, "args": kwargs, "body": request.data})
            existing = CommandReceipt.objects.filter(user=request.user, key=key).first()
            if existing:
                if existing.request_hash != request_hash or existing.endpoint != endpoint:
                    return error("idempotency_key_mismatch", "This key was already used for a different request.", 409)
                if existing.status_code == PENDING:
                    return error("in_progress", "This request is still being processed. Retry shortly.", 409,
                                 retryable=True)
                resp = Response(_unseal(existing.response), status=existing.status_code)
                resp["Idempotent-Replay"] = "true"
                return resp
            try:
                receipt = CommandReceipt.objects.create(user=request.user, key=key, endpoint=endpoint,
                                                        request_hash=request_hash, status_code=PENDING, response={})
            except IntegrityError:
                return error("in_progress", "This request is still being processed. Retry shortly.", 409,
                             retryable=True)
            try:
                response = method(self, request, *args, **kwargs)
            except Exception:
                receipt.delete()
                raise
            if 200 <= response.status_code < 300:
                receipt.status_code = response.status_code
                receipt.response = _seal(response.data)
                receipt.save(update_fields=["status_code", "response"])
            else:
                # Failed attempts can be retried with the same key.
                receipt.delete()
            return response

        return wrapper

    return decorator
