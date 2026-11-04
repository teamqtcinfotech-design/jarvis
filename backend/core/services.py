"""Business rules. Every write goes through here so permissions and audit are uniform.

Privacy rules enforced in this module:
- PrivateTask is only ever read with owner=MD and only by MD-authorised views.
- Nothing derived from a PrivateTask reaches a PA except anonymous busy windows.
- PA changes to MD work are TaskRequests; nothing changes until the MD accepts.
"""
import hashlib
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django.conf import settings

from . import timeutils as tu
from .models import (
    PRIORITY_RANK,
    Alert,
    AuditLog,
    DueKind,
    IntegrationCommand,
    Kind,
    Memory,
    Priority,
    PrivateTask,
    SharedTask,
    StaffMember,
    Status,
    TaskCategory,
    TaskRequest,
    User,
)
from .taskpanel import dispatcher as tp_dispatcher
from .taskpanel import sync as tp_sync
from .taskpanel.client import TaskPanelError, get_client as tp_client
from .taskpanel.contracts import build_create_command

log = logging.getLogger("jarvis.services")

DEFAULT_MEETING_MINUTES = 60
DATE_ONLY_REMINDER_TIME = time(9, 30)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


def jsonable(obj):
    return json.loads(json.dumps(obj, cls=DjangoJSONEncoder))


def canonical_hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), cls=DjangoJSONEncoder)
    return hashlib.sha256(raw.encode()).hexdigest()


def audit(actor, action: str, target_type: str, target_id, detail: str = ""):
    AuditLog.objects.create(actor=actor, action=action, target_type=target_type, target_id=str(target_id),
                            detail=detail[:300])


def get_md() -> User | None:
    return User.objects.filter(role=User.Role.MD, is_active=True).first()


def get_pa() -> User | None:
    return User.objects.filter(role=User.Role.PA, is_active=True).first()


# --------------------------------------------------------------------------- schedule helpers
def default_remind_before(kind: str, priority: str) -> int:
    if kind == Kind.MEETING:
        return 15
    if kind == Kind.CALL:
        return 10
    if priority == Priority.URGENT:
        return 15
    return 30


def apply_schedule(task, data: dict, *, creating: bool):
    """Apply due date/time, meeting slot and reminder lead time from validated input."""
    kind = data.get("kind", task.kind)
    priority = data.get("priority", task.priority)
    due_date_val: date | None = data.get("due_date")
    due_time_val: time | None = data.get("due_time")

    if "due_date" in data or "due_time" in data or creating:
        if due_date_val and due_time_val:
            task.due_kind = DueKind.TIMED
            task.due_date = due_date_val
            task.due_at = tu.combine_local(due_date_val, due_time_val)
        elif due_date_val:
            task.due_kind = DueKind.DATE
            task.due_date = due_date_val
            task.due_at = None
        else:
            task.due_kind = DueKind.NONE
            task.due_date = None
            task.due_at = None
        if creating:
            task.original_due_date = task.due_date
            task.original_due_at = task.due_at

    duration = data.get("duration_minutes")
    if duration:
        task.estimated_minutes = duration
    if kind == Kind.MEETING and task.due_at:
        task.scheduled_start = task.due_at
        task.scheduled_end = task.due_at + timedelta(minutes=task.estimated_minutes or DEFAULT_MEETING_MINUTES)
    elif kind != Kind.MEETING:
        task.scheduled_start = None
        task.scheduled_end = None

    if data.get("remind_before_minutes") is not None and data.get("remind_before_minutes") != 0:
        task.remind_before_minutes = data["remind_before_minutes"]
    elif creating or task.remind_before_minutes is None:
        task.remind_before_minutes = default_remind_before(kind, priority)


def is_overdue(task, now: datetime | None = None) -> bool:
    if task.is_closed:
        return False
    now = now or timezone.now()
    if task.due_kind == DueKind.TIMED and task.due_at:
        return task.due_at < now
    if task.due_kind == DueKind.DATE and task.due_date:
        return task.due_date < tu.today_local()
    return False


def default_announcement(title: str, kind: str, for_name: str = "Sir") -> str:
    if kind == Kind.MEETING:
        return f"{for_name}, your meeting {title} is coming up."
    if kind == Kind.CALL:
        return f"{for_name}, reminder to call: {title}."
    if kind == Kind.PAYMENT:
        return f"{for_name}, payment reminder: {title}."
    return f"{for_name}, reminder: {title}."


# --------------------------------------------------------------------------- serialisation
def _base_item(task, scope: str) -> dict:
    return {
        "id": str(task.id),
        "scope": scope,
        "title": task.title,
        "notes": task.notes,
        "kind": task.kind,
        "priority": task.priority,
        "status": task.status,
        "due_kind": task.due_kind,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "original_due_at": task.original_due_at.isoformat() if task.original_due_at else None,
        "original_due_date": task.original_due_date.isoformat() if task.original_due_date else None,
        "scheduled_start": task.scheduled_start.isoformat() if task.scheduled_start else None,
        "scheduled_end": task.scheduled_end.isoformat() if task.scheduled_end else None,
        "estimated_minutes": task.estimated_minutes,
        "remind_before_minutes": task.remind_before_minutes,
        "due_label": tu.due_phrase(task.due_kind, task.due_date, task.due_at),
        "overdue": is_overdue(task),
        "version": task.version,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def private_item(task: PrivateTask) -> dict:
    item = _base_item(task, "private")
    item.update({"assignee": {"type": "MD"}, "source": task.source})
    return item


def shared_item(task: SharedTask) -> dict:
    item = _base_item(task, "shared")
    pa = get_pa() if task.assignee_type == SharedTask.Assignee.PA else None
    item.update(
        {
            "assignee": {
                "type": task.assignee_type,
                "staff_id": task.staff_id,
                "staff_name": task.staff.name if task.staff else (pa.spoken_name if pa else None),
                "department": task.staff.department if task.staff else ("PA" if pa else None),
            },
            "origin": task.origin,
            "created_by": task.created_by.spoken_name if task.created_by_id else None,
            "description": task.description,
            "category_key": task.category_key,
            "sync_state": task.sync_state,
            "sync_error": task.sync_error,
            "remote_task_id": task.remote_legacy_id,
            "remote_status": task.remote_status,
            "status_label": (
                tp_sync.status_label(task) if task.assignee_type == SharedTask.Assignee.STAFF
                else PA_STATUS_LABEL.get(task.status, "") if task.assignee_type == SharedTask.Assignee.PA
                else ""
            ),
            "hold_reason": task.hold_reason,
            "last_synced_at": task.last_synced_at.isoformat() if task.last_synced_at else None,
        }
    )
    return item


def request_item(req: TaskRequest) -> dict:
    payload = req.payload or {}
    due_label = ""
    if payload.get("due_date"):
        d = date.fromisoformat(payload["due_date"])
        if payload.get("due_time"):
            due_label = tu.due_phrase("TIMED", d, tu.combine_local(d, time.fromisoformat(payload["due_time"])))
        else:
            due_label = tu.fmt_day(d)
    return {
        "id": str(req.id),
        "kind": req.kind,
        "state": req.state,
        "title": payload.get("title") or (req.target.title if req.target_id else ""),
        "payload": payload,
        "priority": payload.get("priority", req.target.priority if req.target_id else "NORMAL"),
        "due_label": due_label,
        "reason": req.reason,
        "target_id": str(req.target_id) if req.target_id else None,
        "target_title": req.target.title if req.target_id else None,
        "base_version": req.base_version,
        "requested_by": req.requested_by.spoken_name,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "decision_note": req.decision_note,
        "created_at": req.created_at.isoformat(),
    }


def alert_item(alert: Alert) -> dict:
    return {
        "id": str(alert.id),
        "kind": alert.kind,
        "priority": alert.priority,
        "title": alert.title,
        "body": alert.body,
        "spoken_text": alert.spoken_text,
        "ref_type": alert.ref_type,
        "ref_id": alert.ref_id,
        "created_at": alert.created_at.isoformat(),
    }


def _sort_key(item: dict):
    when = item.get("scheduled_start") or item.get("due_at") or (
        (item["due_date"] + "T23:59:00+05:30") if item.get("due_date") else "9999"
    )
    return (when, PRIORITY_RANK.get(item["priority"], 9))


def _bucket(items: list[dict]) -> dict:
    today = tu.today_local()
    week_end = today + timedelta(days=7)
    buckets = {"overdue": [], "today": [], "upcoming": [], "anytime": []}
    for item in items:
        if item["status"] in (Status.COMPLETED, Status.CANCELLED):
            continue
        if item["overdue"]:
            buckets["overdue"].append(item)
            continue
        when = item.get("scheduled_start") or item.get("due_at")
        d = tu.to_local(datetime.fromisoformat(when)).date() if when else (
            date.fromisoformat(item["due_date"]) if item.get("due_date") else None
        )
        if d is None:
            buckets["anytime"].append(item)
        elif d == today:
            buckets["today"].append(item)
        elif d <= week_end:
            buckets["upcoming"].append(item)
        else:
            buckets["anytime"].append(item)
    for key in buckets:
        buckets[key].sort(key=_sort_key)
    return buckets


# --------------------------------------------------------------------------- queries
def md_private_qs(md: User):
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can access personal tasks.", 403)
    return PrivateTask.objects.filter(owner=md)


def shared_md_tasks_qs():
    return SharedTask.objects.filter(assignee_type=SharedTask.Assignee.MD).select_related("created_by", "staff")


def delegated_qs():
    return SharedTask.objects.filter(assignee_type=SharedTask.Assignee.STAFF).select_related("created_by", "staff")


def pa_tasks_qs():
    """Tasks the MD gave directly to the PA (kept inside JARVIS, not TaskPanel)."""
    return SharedTask.objects.filter(assignee_type=SharedTask.Assignee.PA).select_related("created_by", "staff")


PA_STATUS_LABEL = {
    Status.OPEN: "Not started", Status.IN_PROGRESS: "Working", Status.BLOCKED: "Hold",
    Status.COMPLETED: "Completed", Status.CANCELLED: "Cancelled",
}


def _recent_first_open(qs):
    open_items = [shared_item(t) for t in qs.exclude(status__in=[Status.COMPLETED, Status.CANCELLED])]
    open_items.sort(key=_sort_key)
    done = [shared_item(t) for t in qs.filter(status=Status.COMPLETED).order_by("-completed_at")[:5]]
    return open_items + done


def busy_windows(day: date, include_titles_for: User | None = None) -> list[dict]:
    """Busy time on a day. Titles are NEVER included unless the caller is the MD."""
    start, end = tu.day_bounds(day)
    windows = []
    md = get_md()
    sources = []
    if md:
        sources.append(PrivateTask.objects.filter(owner=md))
    sources.append(shared_md_tasks_qs())
    sources.append(pa_tasks_qs().filter(origin=SharedTask.Origin.MD_MEET_PA))  # MD's time with the PA
    for qs in sources:
        for task in qs.exclude(status__in=[Status.COMPLETED, Status.CANCELLED]).filter(
            scheduled_start__lt=end, scheduled_end__gt=start
        ):
            entry = {"start": task.scheduled_start.isoformat(), "end": task.scheduled_end.isoformat()}
            if include_titles_for is not None and include_titles_for.is_md:
                entry["title"] = task.title
            windows.append(entry)
    windows.sort(key=lambda w: w["start"])
    # Merge overlaps so a PA cannot infer the number of private meetings.
    merged: list[dict] = []
    for w in windows:
        if merged and w["start"] <= merged[-1]["end"] and "title" not in w:
            merged[-1]["end"] = max(merged[-1]["end"], w["end"])
        else:
            merged.append(dict(w))
    for w in merged:
        w["label"] = f"Busy {tu.fmt_time(datetime.fromisoformat(w['start']))}–{tu.fmt_time(datetime.fromisoformat(w['end']))}"
    return merged


def dashboard(user: User) -> dict:
    now = tu.now_local()
    taskpanel = {**tp_client().describe(), "linked": user_link(user) is not None}
    extra = {}
    if user.is_md:
        items = [private_item(t) for t in md_private_qs(user).exclude(status=Status.CANCELLED)]
        items += [shared_item(t) for t in shared_md_tasks_qs().exclude(status=Status.CANCELLED)]
        # Meetings with the PA are the MD's own schedule too.
        items += [shared_item(t) for t in pa_tasks_qs().filter(origin=SharedTask.Origin.MD_MEET_PA)
                  .exclude(status=Status.CANCELLED)]
        pending = TaskRequest.objects.filter(state=TaskRequest.State.PENDING).select_related("requested_by", "target")
        requests_out = [request_item(r) for r in pending]
        # What the MD gave the PA to do, with progress.
        extra["pa_tasks"] = _recent_first_open(pa_tasks_qs().exclude(origin=SharedTask.Origin.MD_MEET_PA))
    else:
        # The PA's own to-do list = tasks the MD assigned to her.
        items = [shared_item(t) for t in pa_tasks_qs().exclude(status=Status.CANCELLED)]
        mine = TaskRequest.objects.filter(requested_by=user).select_related("requested_by", "target")[:30]
        requests_out = [request_item(r) for r in mine]
        extra["md_tasks"] = _recent_first_open(shared_md_tasks_qs())  # MD tasks that came from her requests
    delegated = [shared_item(t) for t in delegated_qs().exclude(status=Status.CANCELLED).order_by("-created_at")[:50]]
    buckets = _bucket(items)
    completed_today = [
        i for i in items
        if i["status"] == Status.COMPLETED and i["completed_at"]
        and tu.to_local(datetime.fromisoformat(i["completed_at"])).date() == now.date()
    ]
    out = {
        "user": {"username": user.username, "role": user.role, "name": user.spoken_name},
        "server_time": now.isoformat(),
        "date_label": f"{now.strftime('%A')}, {now.day} {now.strftime('%B')}",
        **buckets,
        "completed_today": len(completed_today),
        "requests": requests_out,
        "delegated": delegated,
        "taskpanel": taskpanel,
        **extra,
    }
    if user.is_pa:
        out["md_busy_today"] = busy_windows(now.date())
    return out


# --------------------------------------------------------------------------- private tasks (MD only)
@transaction.atomic
def create_private_task(md: User, data: dict, source: str = PrivateTask.Source.MANUAL) -> PrivateTask:
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can create personal tasks.", 403)
    task = PrivateTask(
        owner=md,
        title=data["title"].strip(),
        notes=data.get("notes", ""),
        kind=data.get("kind", Kind.TASK),
        priority=data.get("priority", Priority.NORMAL),
        source=source,
    )
    apply_schedule(task, data, creating=True)
    task.announcement = data.get("announcement") or default_announcement(task.title, task.kind)
    task.save()
    audit(md, "private_task.create", "private_task", task.id)  # no title in audit, by design
    return task


EDITABLE_FIELDS = {"title", "notes", "priority", "kind", "status"}


def _check_version(task, expected_version: int):
    if task.version != expected_version:
        raise ServiceError("version_conflict", "This task was changed elsewhere. Refresh and try again.", 409,
                           current_version=task.version)


def _apply_changes(task, changes: dict):
    for field in EDITABLE_FIELDS:
        if field in changes:
            setattr(task, field, changes[field])
    if {"due_date", "due_time", "duration_minutes", "remind_before_minutes", "kind"} & changes.keys():
        if "due_date" in changes and "due_time" not in changes and task.due_kind == DueKind.TIMED and changes["due_date"]:
            changes = {**changes, "due_time": tu.to_local(task.due_at).time()}
        if "due_time" in changes and "due_date" not in changes and changes["due_time"]:
            changes = {**changes, "due_date": task.due_date or tu.today_local()}
        apply_schedule(task, changes, creating=False)
    if task.status == Status.COMPLETED and not task.completed_at:
        task.completed_at = timezone.now()
    if task.status != Status.COMPLETED:
        task.completed_at = None
    task.version += 1


@transaction.atomic
def update_private_task(md: User, task_id, changes: dict, expected_version: int) -> PrivateTask:
    task = md_private_qs(md).select_for_update().get(id=task_id)
    _check_version(task, expected_version)
    _apply_changes(task, changes)
    task.save()
    audit(md, "private_task.update", "private_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


# --------------------------------------------------------------------------- shared MD tasks
@transaction.atomic
def update_shared_md_task(md: User, task_id, changes: dict, expected_version: int) -> SharedTask:
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can change MD tasks directly.", 403)
    # Lock only the task row: PostgreSQL rejects FOR UPDATE across the nullable staff join.
    # The MD may edit his own shared tasks and the tasks he gave the PA.
    task = SharedTask.objects.select_for_update().get(
        id=task_id, assignee_type__in=[SharedTask.Assignee.MD, SharedTask.Assignee.PA]
    )
    _check_version(task, expected_version)
    _apply_changes(task, changes)
    task.save()
    audit(md, "shared_task.update", "shared_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


# --------------------------------------------------------------------------- MD -> PA tasks
@transaction.atomic
def create_pa_task(md: User, data: dict, *, meeting_with_pa: bool = False) -> SharedTask:
    """The MD gives the PA a task, or schedules a meeting with her. Both see it and both get reminders."""
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can assign tasks to the PA.", 403)
    pa = get_pa()
    if pa is None:
        raise ServiceError("no_pa", "There is no active PA account.", 422)
    task = SharedTask(
        title=data["title"].strip(), notes=data.get("notes", ""),
        kind=Kind.MEETING if meeting_with_pa else data.get("kind", Kind.TASK),
        priority=data.get("priority", Priority.NORMAL), assignee_type=SharedTask.Assignee.PA,
        origin=SharedTask.Origin.MD_MEET_PA if meeting_with_pa else SharedTask.Origin.MD_TO_PA,
        created_by=md, description=(data.get("description") or "").strip()[:5000],
        sync_state=SharedTask.Sync.NOT_REQUIRED,
    )
    apply_schedule(task, {**data, "kind": task.kind}, creating=True)
    task.announcement = (f"{pa.spoken_name}, your meeting with Sir is coming up: {task.title}." if meeting_with_pa
                         else data.get("announcement") or f"{pa.spoken_name}, reminder for Sir's task: {task.title}.")
    task.save()
    when = tu.due_phrase(task.due_kind, task.due_date, task.due_at)
    if meeting_with_pa:
        title = "Meeting with Sir"
        spoken = f"{pa.spoken_name}, Sir has scheduled a meeting with you" + (f" {when}" if when else "") + f": {task.title}."
    else:
        title = "New task from Sir"
        spoken = (f"{pa.spoken_name}, Sir has given you a new {task.priority.lower()} priority task: "
                  f"{task.title}" + (f", {when}" if when else "") + ".")
    Alert.objects.create(
        recipient=pa, kind=Alert.Kind.TASK_ASSIGNED, priority=task.priority, title=title,
        body=task.title + (f" · {when}" if when else ""), spoken_text=spoken[:400],
        ref_type="shared_task", ref_id=str(task.id),
    )
    audit(md, "pa_meeting.create" if meeting_with_pa else "pa_task.create", "shared_task", task.id)
    return task


PA_SETTABLE_STATUSES = {Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED, Status.COMPLETED}


@transaction.atomic
def update_pa_task_status(pa: User, task_id, new_status: str, expected_version: int, note: str = "") -> SharedTask:
    """The PA moves her task: Not started / Working / Hold (with reason) / Done. MD is told."""
    if not pa.is_pa:
        raise ServiceError("forbidden", "Only the PA updates the status of her tasks.", 403)
    if new_status not in PA_SETTABLE_STATUSES:
        raise ServiceError("invalid_status", "Choose Not started, Working, Hold or Done.", 422)
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_type=SharedTask.Assignee.PA)
    if task.status == Status.CANCELLED:
        raise ServiceError("cancelled", "Sir has cancelled this task.", 409)
    _check_version(task, expected_version)
    if new_status == Status.BLOCKED and not note.strip():
        raise ServiceError("reason_required", "Please give a reason for putting this on hold.", 422)
    old = task.status
    task.status = new_status
    task.hold_reason = note.strip()[:300] if new_status == Status.BLOCKED else ""
    task.completed_at = timezone.now() if new_status == Status.COMPLETED else None
    task.version += 1
    task.save()
    md = get_md()
    if md and new_status != old:
        name = pa.spoken_name
        if new_status == Status.COMPLETED:
            title, priority, spoken = f"{name} completed a task", Priority.NORMAL, f"Sir, {name} has completed: {task.title}."
        elif new_status == Status.BLOCKED:
            title, priority = f"{name} put a task on hold", Priority.NORMAL
            spoken = f"Sir, {name} has put {task.title} on hold. Reason: {task.hold_reason}."
        elif new_status == Status.IN_PROGRESS:
            title, priority, spoken = f"{name} started a task", Priority.LOW, ""
        else:
            title, priority, spoken = f"{name} reopened a task", Priority.LOW, ""
        Alert.objects.create(
            recipient=md, kind=Alert.Kind.STAFF_UPDATE, priority=priority, title=title, body=task.title,
            spoken_text=spoken[:400], ref_type="shared_task", ref_id=str(task.id),
        )
    audit(pa, "pa_task.status", "shared_task", task.id, f"{old} -> {new_status}")
    return task


def _payload_for_storage(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if isinstance(value, (date, time)):
            out[key] = value.isoformat(timespec="minutes") if isinstance(value, time) else value.isoformat()
        else:
            out[key] = value
    return out


def _payload_to_input(payload: dict) -> dict:
    data = dict(payload)
    if data.get("due_date"):
        data["due_date"] = date.fromisoformat(data["due_date"])
    if data.get("due_time"):
        data["due_time"] = time.fromisoformat(data["due_time"])
    return data


def _notify_md_new_request(req: TaskRequest):
    md = get_md()
    if not md:
        return
    item = request_item(req)
    pa_name = req.requested_by.spoken_name
    if req.kind == TaskRequest.Kind.CREATE:
        when = f", {item['due_label']}" if item["due_label"] else ""
        spoken = f"Sir, {pa_name} has sent you a new {item['priority'].lower()} priority task: {item['title']}{when}. Would you like to accept it?"
        title = f"New task from {pa_name}"
    else:
        spoken = f"Sir, {pa_name} has proposed a change to {item['target_title']}. Please review it."
        title = f"Change proposed by {pa_name}"
    Alert.objects.create(
        recipient=md, kind=Alert.Kind.NEW_REQUEST, priority=item["priority"], title=title,
        body=item["title"] + (f" · {item['due_label']}" if item["due_label"] else ""),
        spoken_text=spoken[:400], ref_type="request", ref_id=str(req.id),
    )


@transaction.atomic
def create_md_request(pa: User, data: dict) -> TaskRequest:
    if not pa.is_pa:
        raise ServiceError("forbidden", "Only the PA sends requests to the MD.", 403)
    payload = _payload_for_storage({k: v for k, v in data.items() if k != "reason"})
    req = TaskRequest.objects.create(
        kind=TaskRequest.Kind.CREATE, payload=payload, payload_hash=canonical_hash(payload),
        reason=data.get("reason", ""), requested_by=pa,
    )
    _notify_md_new_request(req)
    audit(pa, "request.create", "request", req.id)
    return req


@transaction.atomic
def propose_change(pa: User, task_id, changes: dict, expected_version: int, reason: str = "") -> TaskRequest:
    if not pa.is_pa:
        raise ServiceError("forbidden", "Only the PA proposes changes.", 403)
    task = shared_md_tasks_qs().get(id=task_id)
    _check_version(task, expected_version)
    payload = _payload_for_storage(changes)
    req = TaskRequest.objects.create(
        kind=TaskRequest.Kind.CHANGE, target=task, payload=payload, payload_hash=canonical_hash(payload),
        base_version=task.version, reason=reason, requested_by=pa,
    )
    _notify_md_new_request(req)
    audit(pa, "request.propose_change", "request", req.id, f"target shared_task {task.id}")
    return req


def _notify_requester(req: TaskRequest, accepted: bool, conflict: bool = False):
    item = request_item(req)
    if conflict:
        text = f"Your request '{item['title']}' could not be applied because the task changed. Please send a fresh one."
    elif accepted:
        text = f"MD accepted: {item['title']}."
    else:
        text = f"MD declined: {item['title']}." + (f" Note: {req.decision_note}" if req.decision_note else "")
    Alert.objects.create(
        recipient=req.requested_by, kind=Alert.Kind.REQUEST_DECIDED, priority=Priority.NORMAL,
        title="Request accepted" if accepted else ("Request conflict" if conflict else "Request declined"),
        body=item["title"], spoken_text=f"{req.requested_by.spoken_name}, {text}"[:400],
        ref_type="request", ref_id=str(req.id),
    )


def accept_request(md: User, request_id) -> TaskRequest:
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can accept requests.", 403)
    with transaction.atomic():
        req = TaskRequest.objects.select_for_update(of=("self",)).select_related("requested_by").get(id=request_id)
        if req.state != TaskRequest.State.PENDING:
            raise ServiceError("not_pending", f"This request is already {req.get_state_display().lower()}.", 409)
        if canonical_hash(req.payload) != req.payload_hash:
            raise ServiceError("tampered", "Request payload does not match what was proposed.", 409)
        data = _payload_to_input(req.payload)
        if req.kind == TaskRequest.Kind.CREATE:
            task = SharedTask(
                title=data["title"], notes=data.get("notes", ""), kind=data.get("kind", Kind.TASK),
                priority=data.get("priority", Priority.NORMAL), assignee_type=SharedTask.Assignee.MD,
                origin=SharedTask.Origin.PA_REQUEST, created_by=req.requested_by,
            )
            apply_schedule(task, data, creating=True)
            task.announcement = data.get("announcement") or default_announcement(task.title, task.kind)
            task.save()
            req.resulting_task = task
        else:
            task = SharedTask.objects.select_for_update().get(id=req.target_id)
            if task.version != req.base_version:
                req.state = TaskRequest.State.CONFLICT
                req.decided_by, req.decided_at = md, timezone.now()
                req.save()
                _notify_requester(req, accepted=False, conflict=True)
                conflict = True
            else:
                _apply_changes(task, data)
                task.save()
                req.resulting_task = task
                conflict = False
            if conflict:
                audit(md, "request.conflict", "request", req.id)
                return req
        req.state = TaskRequest.State.ACCEPTED
        req.decided_by, req.decided_at = md, timezone.now()
        req.save()
        _notify_requester(req, accepted=True)
        Alert.objects.filter(recipient=md, ref_type="request", ref_id=str(req.id), acknowledged_at__isnull=True) \
            .update(acknowledged_at=timezone.now())
        audit(md, "request.accept", "request", req.id)
    return req


@transaction.atomic
def decline_request(md: User, request_id, note: str = "") -> TaskRequest:
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can decline requests.", 403)
    req = TaskRequest.objects.select_for_update(of=("self",)).select_related("requested_by").get(id=request_id)
    if req.state != TaskRequest.State.PENDING:
        raise ServiceError("not_pending", f"This request is already {req.get_state_display().lower()}.", 409)
    req.state = TaskRequest.State.DECLINED
    req.decided_by, req.decided_at, req.decision_note = md, timezone.now(), note[:300]
    req.save()
    _notify_requester(req, accepted=False)
    Alert.objects.filter(recipient=md, ref_type="request", ref_id=str(req.id), acknowledged_at__isnull=True) \
        .update(acknowledged_at=timezone.now())
    audit(md, "request.decline", "request", req.id)
    return req


@transaction.atomic
def withdraw_request(pa: User, request_id) -> TaskRequest:
    req = TaskRequest.objects.select_for_update().get(id=request_id, requested_by=pa)
    if req.state != TaskRequest.State.PENDING:
        raise ServiceError("not_pending", "Only pending requests can be withdrawn.", 409)
    req.state = TaskRequest.State.WITHDRAWN
    req.decided_at = timezone.now()
    req.save()
    md = get_md()
    if md:
        Alert.objects.filter(recipient=md, ref_type="request", ref_id=str(req.id), acknowledged_at__isnull=True) \
            .update(acknowledged_at=timezone.now())
    audit(pa, "request.withdraw", "request", req.id)
    return req


# --------------------------------------------------------------------------- staff + TaskPanel
def user_link(user: User):
    from .models import TaskPanelLink

    return TaskPanelLink.objects.filter(user=user, active=True).first() if user else None


def sync_staff(user: User | None = None) -> int:
    """Refresh staff and categories from the TaskPanel directory."""
    return tp_sync.sync_directory(user, force=True)


def resolve_staff(name: str) -> tuple[list[StaffMember], list[StaffMember]]:
    """Return (matches, suggestions) from the live directory.

    Only an exact name or whole-word match counts as a match ("QTC Developer" never matches
    "QTC Developer2"). A partial match ("Ram" -> Ramesh) is only a suggestion the user must confirm.
    """
    norm = " ".join((name or "").lower().split())
    if not norm:
        return [], []
    active = list(StaffMember.objects.filter(active=True))
    exact = [s for s in active if " ".join(s.name.lower().split()) == norm]
    if exact:
        return exact, []
    tokens = norm.split()
    whole_words = [s for s in active if all(t in s.name.lower().split() for t in tokens)]
    if whole_words:
        return whole_words, []
    if len(tokens[0]) < 3:
        return [], []
    partial = [s for s in active if any(w.startswith(tokens[0]) for w in s.name.lower().split())]
    return [], partial[:5]


def delegate_to_staff(user: User, data: dict, staff: StaffMember, *, category_key: str = "",
                      description: str = "") -> tuple[SharedTask, IntegrationCommand]:
    """Create the TaskPanel command (frozen payload) and try to deliver it right away.

    The task only counts as assigned when TaskPanel returns a committed receipt.
    """
    if not (user.is_md or user.is_pa):
        raise ServiceError("forbidden", "Not allowed.", 403)
    if not staff.active:
        raise ServiceError("staff_inactive", "This staff member is no longer active.", 422)
    if user_link(user) is None:
        raise ServiceError("not_linked", "Your account is not linked to TaskPanel yet.", 403)
    category_key = category_key or settings.TASKPANEL_DEFAULT_CATEGORY
    with transaction.atomic():
        task = SharedTask(
            title=data["title"].strip(), notes=data.get("notes", ""), kind=data.get("kind", Kind.TASK),
            priority=data.get("priority", Priority.NORMAL), assignee_type=SharedTask.Assignee.STAFF, staff=staff,
            category_key=category_key, description=(description or data["title"]).strip()[:5000],
            origin=SharedTask.Origin.MD_DELEGATION if user.is_md else SharedTask.Origin.PA_DELEGATION,
            created_by=user, sync_state=SharedTask.Sync.PENDING,
        )
        apply_schedule(task, data, creating=True)
        task.announcement = f"Follow up with {staff.name}: {task.title}."
        task.save()
        command_id = uuid.uuid4()
        payload = build_create_command(task, str(command_id))
        command = IntegrationCommand.objects.create(
            id=command_id, actor=user, type="task.create", task=task, payload=payload,
            payload_hash=canonical_hash(payload["data"]), next_attempt_at=timezone.now(),
        )
        audit(user, "delegation.create", "shared_task", task.id, f"to staff {staff.external_id}, command {command_id}")
    command = tp_dispatcher.dispatch(command.id)
    task.refresh_from_db()
    if command.state != IntegrationCommand.State.REJECTED and user.is_pa:
        md = get_md()
        if md:
            Alert.objects.create(
                recipient=md, kind=Alert.Kind.INFO, priority=Priority.LOW,
                title=f"{user.spoken_name} delegated a task", body=f"{task.title} → {staff.name}", spoken_text="",
                ref_type="shared_task", ref_id=str(task.id),
            )
    return task, command


ALL_TEAM_WORDS = {"", "all", "everyone", "everybody", "whole team", "all staff", "team", "sab", "sabhi", "sab staff",
                  "poori team", "puri team", "sab log", "sabhi log"}
MAX_TEAM_SIZE = 60


def own_actor_keys() -> set[str]:
    """TaskPanel identities of the MD and PA themselves: never included in a 'whole team' assignment."""
    from .models import TaskPanelLink

    return set(TaskPanelLink.objects.filter(active=True).values_list("actor_key", flat=True))


def team_members(team_name: str) -> tuple[list[StaffMember], list[StaffMember], str]:
    """Resolve 'all' or a department name to (members, skipped_on_leave, label)."""
    name = " ".join((team_name or "").lower().split())
    active = [s for s in StaffMember.objects.filter(active=True) if s.external_id not in own_actor_keys()]
    if name in ALL_TEAM_WORDS:
        chosen, label = active, "the whole team"
    else:
        for suffix in (" team", " department", " dept", " waalon", " walon", " waale", " wale", " log"):
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip()
        chosen = [s for s in active if s.department and (s.department.lower() == name or s.department.lower().startswith(name))]
        label = f"the {chosen[0].department} team" if chosen else ""
    return [s for s in chosen if not s.on_leave], [s for s in chosen if s.on_leave], label


def delegate_to_team(user: User, data: dict, staff_ids: list[int], *, category_key: str, description: str) -> dict:
    """One TaskPanel task per person (only the assignee can progress a TaskPanel task)."""
    if not staff_ids:
        raise ServiceError("no_members", "There is no one to assign this to.", 422)
    if len(staff_ids) > MAX_TEAM_SIZE:
        raise ServiceError("team_too_large", f"A team task can go to at most {MAX_TEAM_SIZE} people at once.", 422)
    results = {"assigned": [], "pending": [], "not_assigned": []}
    for person in StaffMember.objects.filter(id__in=staff_ids, active=True).order_by("name"):
        try:
            task, command = delegate_to_staff(user, data, person, category_key=category_key, description=description)
        except ServiceError as err:
            results["not_assigned"].append({"name": person.name, "reason": err.message})
            continue
        if command.state == IntegrationCommand.State.REJECTED:
            results["not_assigned"].append({"name": person.name, "reason": command.error_message})
            task.delete()
        elif command.state == IntegrationCommand.State.SUCCEEDED:
            results["assigned"].append(person.name)
        else:
            results["pending"].append(person.name)
    audit(user, "delegation.team", "staff", ",".join(map(str, staff_ids))[:64],
          f"assigned {len(results['assigned'])}, pending {len(results['pending'])}, "
          f"not assigned {len(results['not_assigned'])}")
    return results


def refresh_delegations(user: User | None = None) -> int:
    """Deliver due commands, then pull TaskPanel's change feed (bounded)."""
    tp_dispatcher.dispatch_due()
    link = user_link(user) if user else None
    if link:
        return tp_sync.pull_changes(link, max_batches=2)
    return tp_sync.pull_all(max_batches=2)


def recheck_delegation(user: User, task_id) -> SharedTask:
    """"Check again" on one delegated task: resend if unconfirmed, then refresh its status."""
    task = delegated_qs().get(id=task_id)
    pending = task.commands.filter(state__in=[IntegrationCommand.State.QUEUED, IntegrationCommand.State.OUTCOME_UNKNOWN])
    for command in pending:
        IntegrationCommand.objects.filter(id=command.id).update(next_attempt_at=timezone.now(), lease_until=None)
        tp_dispatcher.dispatch(command.id)
    link = user_link(user)
    if link:
        try:
            tp_sync.pull_changes(link, max_batches=1)
        except TaskPanelError:
            pass
    task.refresh_from_db()
    return task


# --------------------------------------------------------------------------- AI context
def ai_context(user: User) -> dict:
    """Facts the AI may use. Strictly scoped: a PA context contains no MD private data."""
    now = tu.now_local()
    tp_sync.ensure_directory(user)
    staff = [{"id": s.external_id, "name": s.name, "department": s.department, "on_leave": s.on_leave}
             for s in StaffMember.objects.filter(active=True)[:300]]
    categories = [{"key": c.key, "name": c.name, "department": c.department}
                  for c in TaskCategory.objects.filter(active=True)[:100]]
    ctx = {
        "now_india": now.isoformat(),
        "weekday": now.strftime("%A"),
        "speaker_role": user.role,
        "speaker_name": user.spoken_name,
        "staff": staff,
        "task_categories": categories,
        "default_category": settings.TASKPANEL_DEFAULT_CATEGORY,
    }
    dash = dashboard(user)
    def brief(item):
        return {k: item.get(k) for k in ("id", "title", "kind", "priority", "status", "due_label", "scope", "version")} | (
            {"assignee": item["assignee"].get("staff_name")} if item.get("assignee", {}).get("staff_name") else {}
        )
    # Kept short on purpose: every item here is sent to the model on every command, and size costs seconds.
    ctx["open_items"] = {
        "overdue": [brief(i) for i in dash["overdue"][:8]],
        "today": [brief(i) for i in dash["today"][:12]],
        "upcoming": [brief(i) for i in dash["upcoming"][:8]],
    }
    ctx["delegated"] = [brief(i) for i in dash["delegated"][:10]]
    pa = get_pa()
    if user.is_md:
        ctx["pa_name"] = pa.spoken_name if pa else ""
        ctx["tasks_given_to_pa"] = [brief(i) | {"status_label": i["status_label"]} for i in dash.get("pa_tasks", [])[:10]]
    else:
        ctx["md_tasks_from_my_requests"] = [brief(i) for i in dash.get("md_tasks", [])[:10]]
    if user.is_md:
        ctx["pending_requests"] = [
            {"id": r["id"], "title": r["title"], "from": r["requested_by"], "priority": r["priority"],
             "due": r["due_label"], "kind": r["kind"]}
            for r in dash["requests"] if r["state"] == "PENDING"
        ]
    else:
        ctx["md_busy_today"] = [w["label"] for w in dash.get("md_busy_today", [])]
        ctx["my_requests"] = [{"title": r["title"], "state": r["state"]} for r in dash["requests"][:10]]
    # Only the speaker's own remembered facts (the MD's never reach a PA context and vice versa).
    ctx["memories"] = [m.text for m in Memory.objects.filter(owner=user)[:MEMORY_LIMIT]]
    return ctx


# ------------------------------------------------------------------ memory (explicit "remember this")
MEMORY_LIMIT = 50


def memory_item(m: Memory) -> dict:
    return {"id": str(m.id), "text": m.text, "created_at": m.created_at.isoformat()}


def list_memories(user: User) -> list[dict]:
    return [memory_item(m) for m in Memory.objects.filter(owner=user)[:MEMORY_LIMIT]]


def add_memory(user: User, text: str) -> dict:
    text = " ".join((text or "").split())[:300]
    if not text:
        raise ServiceError("invalid", "Tell me what to remember.", 422)
    if Memory.objects.filter(owner=user).count() >= MEMORY_LIMIT:
        raise ServiceError("memory_full", "My notes are full. Please delete an old one in Settings first.", 409)
    m = Memory.objects.create(owner=user, text=text)
    audit(user, "memory.add", "memory", m.id)
    return memory_item(m)


def delete_memory(user: User, memory_id) -> None:
    m = Memory.objects.get(id=memory_id, owner=user)  # someone else's note -> not found
    m.delete()
    audit(user, "memory.delete", "memory", memory_id)


def briefing_facts(user: User, kind: str) -> tuple[dict, str]:
    dash = dashboard(user)
    facts = {
        "reader": "MD" if user.is_md else f"PA ({user.spoken_name})",
        "kind": kind,
        "date": dash["date_label"],
        "now": tu.now_local().strftime("%I:%M %p"),
        "overdue": [{"title": i["title"], "priority": i["priority"], "was_due": i["due_label"]} for i in dash["overdue"]],
        "today": [{"title": i["title"], "priority": i["priority"], "kind": i["kind"], "when": i["due_label"],
                   "minutes": i["estimated_minutes"]} for i in dash["today"]],
        "upcoming": [{"title": i["title"], "when": i["due_label"]} for i in dash["upcoming"][:8]],
        "completed_today": dash["completed_today"],
        "pending_requests": [{"title": r["title"], "from": r["requested_by"], "priority": r["priority"]}
                             for r in dash["requests"] if r["state"] == "PENDING"],
        "delegated_open": [{"title": d["title"], "staff": d["assignee"]["staff_name"], "status": d["status"],
                            "overdue": d["overdue"]} for d in dash["delegated"] if d["status"] != Status.COMPLETED][:12],
    }
    address = "Sir" if user.is_md else user.spoken_name
    parts = [f"Good {'evening' if kind == 'evening' else 'morning' if tu.now_local().hour < 12 else 'afternoon'}, {address}."]
    if facts["overdue"]:
        parts.append(f"{len(facts['overdue'])} item{'s are' if len(facts['overdue']) > 1 else ' is'} overdue, starting with {facts['overdue'][0]['title']}.")
    if facts["today"]:
        firsts = ", ".join(f"{t['title']} {t['when']}" for t in facts["today"][:3])
        parts.append(f"Today you have {len(facts['today'])} item{'s' if len(facts['today']) > 1 else ''}: {firsts}.")
    else:
        parts.append("Nothing else is scheduled for today.")
    if facts["pending_requests"]:
        parts.append(f"{len(facts['pending_requests'])} request{'s are' if len(facts['pending_requests']) > 1 else ' is'} waiting for your decision.")
    late = [d for d in facts["delegated_open"] if d["overdue"]]
    if late:
        parts.append(f"{late[0]['staff']} is late on {late[0]['title']}.")
    return facts, " ".join(parts)
