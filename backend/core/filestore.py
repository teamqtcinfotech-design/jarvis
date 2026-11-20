"""Files on tasks (Nov 2026): photos, documents and voice notes, kept on disk under JARVIS_FILES_DIR with every
byte encrypted by the field key (the same key that guards the task text), so a copy of the disk is as unreadable
as a copy of the database. The row (core.models.TaskFile) holds the name, the type and the size; who may see a
file is whoever may see its task."""
import logging
import mimetypes
import uuid
from pathlib import Path

from django.conf import settings

from . import crypto
from .models import PrivateTask, SharedTask, TaskFile, User
from .services import ServiceError

log = logging.getLogger("jarvis.files")

MAX_BYTES = 15 * 1024 * 1024
MAX_PER_TASK = 20
ALLOWED = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "image/gif": ".gif",
    "application/pdf": ".pdf", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
    "audio/x-m4a": ".m4a", "audio/3gpp": ".3gp", "audio/webm": ".webm",
    "text/plain": ".txt", "text/csv": ".csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def files_dir() -> Path:
    d = Path(getattr(settings, "JARVIS_FILES_DIR", settings.BASE_DIR.parent / "files-store"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _kind_of(name: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in ALLOWED:
        guessed, _ = mimetypes.guess_type(name or "")
        ct = (guessed or "").lower()
    if ct not in ALLOWED:
        raise ServiceError("file_type", "Only photos, PDFs, voice notes and plain documents can go on a task.", 422)
    return ct


def task_for(user: User, scope: str, task_id):
    """The task a file belongs to, as this person may see it (someone else's is simply not found)."""
    from . import services  # noqa: PLC0415

    if scope == "private":
        return services.private_qs(user).get(id=task_id)
    return services.visible_shared_qs(user).get(id=task_id)


def file_item(f: TaskFile) -> dict:
    return {"id": str(f.id), "name": f.name, "content_type": f.content_type, "size": f.size,
            "kind": "photo" if f.content_type.startswith("image/") else "voice" if f.content_type.startswith("audio/") else "document",
            "uploader": f.uploader.spoken_name if f.uploader_id else "", "created_at": f.created_at.isoformat()}


def list_files(task) -> list[dict]:
    return [file_item(f) for f in task.files.select_related("uploader")]


def add_file(user: User, task, name: str, content_type: str, data: bytes) -> TaskFile:
    """Keep one file on the task: encrypted on disk, a row in the database. Raises ServiceError when it is too
    big, of a kind that is not kept, or the task already holds its share."""
    if not data:
        raise ServiceError("empty", "The file is empty.", 422)
    if len(data) > MAX_BYTES:
        raise ServiceError("too_big", "A file can be at most 15 MB.", 422)
    if task.files.count() >= MAX_PER_TASK:
        raise ServiceError("too_many", f"A task holds at most {MAX_PER_TASK} files.", 422)
    ct = _kind_of(name, content_type)
    ext = ALLOWED[ct]
    clean = " ".join((name or "").split())[:200] or f"file{ext}"
    if not clean.lower().endswith(ext) and "." not in clean[-6:]:
        clean += ext
    fid = uuid.uuid4()
    stored = f"{fid.hex}{ext}.enc"
    (files_dir() / stored).write_bytes(crypto._fernet().encrypt(data))  # noqa: SLF001 — the field key guards files too
    row = TaskFile.objects.create(
        id=fid, shared_task=task if isinstance(task, SharedTask) else None,
        private_task=task if isinstance(task, PrivateTask) else None,
        uploader=user, name=clean, content_type=ct, size=len(data), stored=stored,
    )
    return row


def read_file(f: TaskFile) -> bytes:
    path = files_dir() / f.stored
    return crypto._fernet().decrypt(path.read_bytes())  # noqa: SLF001


def remove_file(user: User, f: TaskFile) -> None:
    """Whoever put it there, the task's giver, the person it is for or the MD takes it off."""
    task = f.shared_task or f.private_task
    allowed = user.is_md or f.uploader_id == user.id or getattr(task, "created_by_id", None) == user.id \
        or getattr(task, "assignee_user_id", None) == user.id or getattr(task, "owner_id", None) == user.id
    if not allowed:
        raise ServiceError("forbidden", "Only whoever added it, or the task's people, can remove it.", 403)
    try:
        (files_dir() / f.stored).unlink(missing_ok=True)
    except OSError:
        log.warning("file %s could not be deleted from disk", f.stored)
    f.delete()
