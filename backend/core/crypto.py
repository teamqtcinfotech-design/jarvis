"""Field-level encryption for MD private data.

Values are stored as ``enc1:<fernet token>``. A database dump or backup without
FIELD_ENCRYPTION_KEY cannot be read. The key lives in backend/.env, apart from the DB.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

PREFIX = "enc1:"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode())


def encrypt(value: str) -> str:
    if value is None:
        return value
    if value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if value is None or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Never crash the app or leak ciphertext to the UI.
        return "[unreadable]"


class EncryptedTextField(models.TextField):
    """TextField encrypted at rest. Not searchable in SQL by design."""

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
