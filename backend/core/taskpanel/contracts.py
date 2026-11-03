"""Mapping between JARVIS and the TaskPanel integration contract (schema_version 1)."""
from datetime import datetime

from .. import timeutils as tu

SCHEMA_VERSION = 1

# TaskPanel has no "low" priority: JARVIS Low becomes Normal for staff tasks.
PRIORITY_TO_TP = {"URGENT": "urgent", "HIGH": "high", "NORMAL": "normal", "LOW": "normal"}
PRIORITY_FROM_TP = {"urgent": "URGENT", "high": "HIGH", "normal": "NORMAL"}

# TaskPanel stored statuses. "Overdue" is derived from the deadline, never stored.
STATUS_FROM_TP = {"todo": "OPEN", "working": "IN_PROGRESS", "waiting": "BLOCKED", "done": "COMPLETED"}
STATUS_LABEL = {"todo": "Not started", "working": "Working", "waiting": "Hold", "done": "Completed"}

ALL_STAFF_WORDS = {
    "all", "all staff", "everyone", "everybody", "sab", "sabko", "sab ko", "sabhi", "sabhi ko", "sab staff",
    "poori team", "puri team", "team", "whole team", "all team", "staff",
}


def build_create_command(task, command_id: str, draft_id: str | None = None) -> dict:
    """Freeze the exact command at confirmation (guide §6.2). Retries resend this byte-for-byte."""
    due_at = task.due_at.astimezone(tu.IST).isoformat() if task.due_at else None
    return {
        "schema_version": SCHEMA_VERSION,
        "command_id": command_id,
        "type": "task.create",
        "draft_id": draft_id,
        "data": {
            "category_key": task.category_key,
            "title": task.title[:500],
            "description": (task.description or task.title)[:5000],
            "assignee_key": task.staff.external_id,
            "assignment_mode": "manual",
            "priority": PRIORITY_TO_TP.get(task.priority, "normal"),
            "due_at": due_at,
            "due_date": task.due_date.isoformat() if task.due_date and not task.due_at else None,
            "timezone": "Asia/Kolkata",
            "client_id": None,
            "payment_id": None,
            "franchise_id": None,
            "partner_id": None,
            "remarks": (task.notes or "")[:1000],
        },
    }


def build_validate_body(*, category_key: str, title: str, description: str, assignee_key: str, priority: str,
                        due_at: datetime | None, due_date: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "task.create",
        "data": {
            "category_key": category_key,
            "title": title[:500],
            "description": (description or title)[:5000],
            "assignee_key": assignee_key,
            "assignment_mode": "manual",
            "priority": PRIORITY_TO_TP.get(priority, "normal"),
            "due_at": due_at.astimezone(tu.IST).isoformat() if due_at else None,
            "due_date": due_date if not due_at else None,
            "timezone": "Asia/Kolkata",
        },
    }


def apply_snapshot(task, snap: dict) -> list[str]:
    """Copy allowlisted canonical fields onto the projection. Returns the list of changed field names."""
    changed = []

    def put(field, value):
        if getattr(task, field) != value:
            setattr(task, field, value)
            changed.append(field)

    if snap.get("uuid"):
        put("remote_task_uuid", snap["uuid"])
    if snap.get("legacy_id"):
        put("remote_legacy_id", str(snap["legacy_id"]))
    if "status" in snap and snap["status"] in STATUS_FROM_TP:
        put("remote_status", snap["status"])
        put("status", STATUS_FROM_TP[snap["status"]])
    if snap.get("priority") in PRIORITY_FROM_TP:
        put("priority", PRIORITY_FROM_TP[snap["priority"]])
    if "version" in snap:
        put("remote_version", int(snap["version"] or 0))
    if "sync_revision" in snap:
        put("sync_revision", int(snap["sync_revision"] or 0))
    if "hold_reason" in snap:
        put("hold_reason", (snap.get("hold_reason") or "")[:300])
    if snap.get("completed_at"):
        put("completed_at", datetime.fromisoformat(snap["completed_at"].replace("Z", "+00:00")))
    return changed
