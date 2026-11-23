"""One staff member's work for the app's person screen (Nov 2026): what TaskPanel holds for them right now, with the
progress counts. In gateway mode it comes straight from TaskPanel (everything they have, whoever gave it); otherwise
it is what JARVIS gave them (the projection)."""
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .. import services
from .. import timeutils as tu
from ..models import SharedTask, StaffMember, Status, User
from .client import get_client
from .contracts import PRIORITY_FROM_TP, STATUS_LABEL

_PROGRESS = {"todo": 0, "working": 50, "waiting": 50, "done": 100}
_FROM_LOCAL = {Status.OPEN: "todo", Status.IN_PROGRESS: "working", Status.BLOCKED: "waiting", Status.COMPLETED: "done", Status.CANCELLED: "done"}


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tu.IST)


def _label(dt: datetime | None) -> str:
    if dt is None:
        return ""
    local = tu.to_local(dt)
    return f"{tu.fmt_day(local.date())} {tu.fmt_time(local)}"


def _row(task_id: str, title: str, status: str, priority: str, due_at, category: str, sender: str, created_at, updated_at,
         completed_at, hold_reason: str, detail: str = "", source: str = "taskpanel") -> dict:
    due = _parse(due_at)
    status = status if status in _PROGRESS else "todo"
    overdue = bool(due and status != "done" and due < timezone.now())
    return {
        "id": task_id, "title": title, "status": status, "status_label": "Late" if overdue else STATUS_LABEL.get(status, "Not started"),
        "progress": _PROGRESS[status], "priority": PRIORITY_FROM_TP.get(str(priority).lower(), str(priority).upper() or "NORMAL"),
        "due_at": due.isoformat() if due else None, "due_label": _label(due), "category": category or "", "sender": sender or "",
        "created_at": _parse(created_at).isoformat() if _parse(created_at) else None,
        "updated_at": _parse(updated_at).isoformat() if _parse(updated_at) else None,
        "completed_at": _parse(completed_at).isoformat() if _parse(completed_at) else None,
        "hold_reason": hold_reason or "", "detail": (detail or "")[:600], "overdue": overdue, "source": source,
    }


def _from_snap(s: dict) -> dict:
    return _row(str(s.get("uuid") or s.get("legacy_id") or ""), s.get("title") or "", str(s.get("status") or "todo"),
                str(s.get("priority") or "normal"), s.get("due_at"), s.get("category_key") or "", s.get("sender_name") or s.get("sender_key") or "",
                s.get("created_at"), s.get("updated_at"), s.get("completed_at"), s.get("hold_reason") or "", s.get("detail") or "")


def _from_task(t: SharedTask) -> dict:
    status = t.remote_status if t.remote_status in _PROGRESS else _FROM_LOCAL.get(t.status, "todo")
    return _row(t.remote_legacy_id or str(t.id), t.title, status, {"URGENT": "urgent", "HIGH": "high"}.get(t.priority, "normal"),
                t.due_at, t.category_key, t.created_by.spoken_name if t.created_by_id else "", t.created_at, t.updated_at,
                t.completed_at, t.hold_reason, t.description, source="jarvis")


def _order(r: dict) -> tuple:
    open_first = 1 if r["status"] == "done" else 0
    return (open_first, r["due_at"] or "9999" if not open_first else "", -(float(datetime.fromisoformat(r["completed_at"]).timestamp()) if r.get("completed_at") else 0))


def person_card(user: User, staff: StaffMember) -> dict:
    client = get_client()
    live = hasattr(client, "person_tasks")
    if live:
        rows = [_from_snap(s) for s in client.person_tasks(staff.external_id)]
    else:
        qs = services.delegated_qs().filter(staff=staff).select_related("created_by").order_by("-created_at")[:100]
        rows = [_from_task(t) for t in qs]
    rows.sort(key=_order)
    summary = {k: sum(1 for r in rows if r["status"] == k) for k in ("todo", "working", "waiting", "done")}
    summary["open"] = summary["todo"] + summary["working"] + summary["waiting"]
    summary["overdue"] = sum(1 for r in rows if r["overdue"])
    return {
        "staff": {"id": staff.id, "key": staff.external_id, "name": staff.name, "department": staff.department, "designation": staff.designation,
                  "level": staff.level, "role": staff.role, "on_leave": staff.on_leave,
                  "leave_until": staff.leave_until.isoformat() if staff.leave_until else None},
        "tasks": rows, "summary": summary, "connected": settings.TASKPANEL_MODE in ("live", "gateway"), "live": live,
    }
