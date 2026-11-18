"""Field-level encryption for task text and other private data.

Two forms, both keyed from FIELD_ENCRYPTION_KEY (backend/.env, apart from the database):

- ``enc1:<fernet token>`` — randomised (the same text stored twice looks different). Notes, instructions, memories.
- ``enc2:<aes-gcm>`` — deterministic (the same text always gives the same stored value), so an exact lookup such as
  ``filter(title="Send the report")`` still works in SQL. Used for titles only. The nonce is an HMAC of the text
  under a second derived key, so a nonce repeats only for the same text (the SIV idea); what it gives away is just
  that two tasks share a title.

A database dump or backup without the key reads neither. The running server can read both: JARVIS has to, to
remind, brief and answer about the work (see the privacy note in the README).
"""
import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.db import models

PREFIX = "enc1:"
PREFIX_DET = "enc2:"
UNREADABLE = "[unreadable]"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode())


@lru_cache(maxsize=1)
def _det_keys() -> tuple[bytes, bytes]:
    """The AES key and the nonce key for the deterministic form, both derived from the one configured key."""
    root = base64.urlsafe_b64decode(settings.FIELD_ENCRYPTION_KEY.encode())

    def derive(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(root)

    return derive(b"jarvis-field-aes-gcm"), derive(b"jarvis-field-nonce")


def encrypt(value: str) -> str:
    if value is None:
        return value
    if value.startswith(PREFIX) or value.startswith(PREFIX_DET):
        return value
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def encrypt_deterministic(value: str) -> str:
    if value is None:
        return value
    if value.startswith(PREFIX) or value.startswith(PREFIX_DET):
        return value
    aes_key, nonce_key = _det_keys()
    data = value.encode("utf-8")
    nonce = hmac.new(nonce_key, data, hashlib.sha256).digest()[:12]
    sealed = AESGCM(aes_key).encrypt(nonce, data, None)
    return PREFIX_DET + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def decrypt(value: str) -> str:
    if value is None:
        return value
    try:
        if value.startswith(PREFIX):
            return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        if value.startswith(PREFIX_DET):
            raw = base64.urlsafe_b64decode(value[len(PREFIX_DET):].encode("ascii"))
            aes_key, _ = _det_keys()
            return AESGCM(aes_key).decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except (InvalidToken, InvalidTag, ValueError):
        # Never crash the app or leak ciphertext to the UI.
        return UNREADABLE
    return value  # written before encryption: still readable, encrypted on its next save


class EncryptedTextField(models.TextField):
    """TextField encrypted at rest (randomised). Not searchable in SQL by design."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        if isinstance(value, str):
            return decrypt(value)
        return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None:
            return None
        return encrypt(str(value))


class DeterministicEncryptedTextField(EncryptedTextField):
    """TextField encrypted at rest so that the same text is stored the same way: exact lookups keep working
    (``filter(title=...)``, ``get(title=...)``), nothing else does (no icontains, no ordering by text). For titles."""

    def get_prep_value(self, value):
        value = models.TextField.get_prep_value(self, value)
        if value is None:
            return None
        return encrypt_deterministic(str(value))
