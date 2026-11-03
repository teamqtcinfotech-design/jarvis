"""Directory sync and change-feed projection (guide §8, §15.3).

Applying an event updates the local projection only; it never calls the command dispatcher.
Older or repeated revisions are no-ops; the cursor advances only after the batch is stored.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import (
    Alert, EventInbox, Priority, SharedTask, StaffMember, Status, SyncCursor, TaskCategory, TaskPanelLink, User,
)
from .client import TaskPanelError, get_client
from .contracts import STATUS_FROM_TP, apply_snapshot

log = logging.getLogger("jarvis.taskpanel")

_last_directory_sync = {"at": None}


def link_for(user: User | None) -> TaskPanelLink | None:
    if user is not None:
        link = TaskPanelLink.objects.filter(user=user, active=True).first()
        if link:
            return link
    md = User.objects.filter(role=User.Role.MD).first()
    return TaskPanelLink.objects.filter(user=md, active=True).first() if md else None


# --------------------------------------------------------------------------- directory
def sync_directory(user: User | None = None, *, force: bool = False) -> int:
    last = _last_directory_sync["at"]
    if not force and last and timezone.now() - last < timedelta(seconds=settings.TASKPANEL_DIRECTORY_TTL_SECONDS) \
            and StaffMember.objects.exists():
        return 0
    data = get_client().directory(link_for(user))
    seen_staff, seen_cats = set(), set()
    with transaction.atomic():
        for row in data.get("staff", []):
            key = str(row.get("key") or "")
            if not key or not row.get("name"):
                continue
            leave_until = row.get("leave_until")
            StaffMember.objects.update_or_create(
                external_id=key,
                defaults={
                    "name": str(row["name"])[:120],
                    "department": str(row.get("department") or "")[:120],
                    "designation": str(row.get("designation") or "")[:120],
                    "active": bool(row.get("active", True)),
                    "on_leave": bool(row.get("on_leave", False)),
                    "leave_until": leave_until or None,
                    "source": get_client().mode,
                },
            )
            seen_staff.add(key)
        for row in data.get("categories", []):
            key = str(row.get("key") or "")
            if not key:
                continue
            TaskCategory.objects.update_or_create(
                key=key,
                defaults={"name": str(row.get("name") or key)[:120], "department": str(row.get("department") or "")[:120],
                          "active": bool(row.get("active", True))},
            )
            seen_cats.add(key)
        StaffMember.objects.exclude(external_id__in=seen_staff).update(active=False)
        TaskCategory.objects.exclude(key__in=seen_cats).update(active=False)
    _last_directory_sync["at"] = timezone.now()
    return len(seen_staff)


def ensure_directory(user: User | None = None):
    """Best effort: refresh if stale, never block the caller on TaskPanel being down."""
    try:
        sync_directory(user)
    except TaskPanelError as err:
        log.warning("Directory refresh failed: %s", err.code)


# --------------------------------------------------------------------------- change feed
def _alert_status_change(task: SharedTask, old_status: str, snap: dict):
    new = snap.get("status")
    if new == old_status or not task.staff:
        return
    staff = task.staff.name
    if new == "working":
        title, priority, spoken = f"{staff} started a task", Priority.LOW, ""
    elif new == "waiting":
        reason = snap.get("hold_reason") or ""
        title, priority = f"{staff} put a task on hold", Priority.NORMAL
        spoken = f"{staff} has put {task.title} on hold." + (f" Reason: {reason}." if reason else "")
    elif new == "done":
        title, priority = f"{staff} completed a task", Priority.NORMAL
        spoken = f"{staff} has completed: {task.title}."
    else:
        return
    recipients = {task.created_by}
    md = User.objects.filter(role=User.Role.MD).first()
    if md and new == "done":
        recipients.add(md)
    for user in recipients:
        address = "Sir" if user.is_md else user.spoken_name
        Alert.objects.create(
            recipient=user, kind=Alert.Kind.STAFF_UPDATE, priority=priority, title=title, body=task.title,
            spoken_text=f"{address}, {spoken}"[:400] if spoken else "", ref_type="shared_task", ref_id=str(task.id),
        )


def apply_event(event: dict, link: TaskPanelLink) -> bool:
    """Apply one event inside the caller's transaction. Returns True if the projection changed."""
    if int(event.get("schema_version", 0)) != 1:
        raise TaskPanelError(422, "schema_unsupported", "Unsupported event schema; feed paused.")
    if settings.TASKPANEL_MODE == "live":
        if event.get("environment") != settings.TASKPANEL_ENV:
            raise TaskPanelError(422, "environment_mismatch", "Event from a different TaskPanel environment.")
        if settings.TASKPANEL_EXPECTED_INSTANCE and event.get("source_instance") != settings.TASKPANEL_EXPECTED_INSTANCE:
            raise TaskPanelError(422, "instance_mismatch", "Event from an unexpected TaskPanel instance.")
    _, created = EventInbox.objects.get_or_create(
        event_id=str(event["event_id"]),
        defaults={"event_type": str(event.get("type", ""))[:40], "task_uuid": str(event.get("task_uuid", ""))[:64],
                  "sync_revision": int(event.get("sync_revision") or 0)},
    )
    if not created:
        return False  # duplicate delivery
    snap = dict(event.get("task") or {})
    snap.setdefault("uuid", event.get("task_uuid"))
    snap.setdefault("sync_revision", event.get("sync_revision"))
    snap.setdefault("version", event.get("task_version"))
    task = SharedTask.objects.select_for_update().filter(remote_task_uuid=event.get("task_uuid")).first()
    if task is None:
        # A task this actor assigned directly in TaskPanel web: start projecting it (two-way).
        if snap.get("sender_key") and snap.get("sender_key") == link.actor_key and snap.get("title"):
            staff = StaffMember.objects.filter(external_id=snap.get("assignee_key")).first()
            task = SharedTask(
                title=str(snap["title"])[:200], assignee_type=SharedTask.Assignee.STAFF, staff=staff,
                origin=SharedTask.Origin.TASKPANEL_WEB, created_by=link.user, sync_state=SharedTask.Sync.SYNCED,
            )
            apply_snapshot(task, snap)
            task.last_synced_at = timezone.now()
            task.save()
            return True
        return False
    if int(snap.get("sync_revision") or 0) <= task.sync_revision:
        return False  # older or same revision cannot undo newer state
    old_status = task.remote_status
    if snap.get("assignee_key") and (not task.staff or task.staff.external_id != snap["assignee_key"]):
        new_staff = StaffMember.objects.filter(external_id=snap["assignee_key"]).first()
        if new_staff:
            task.staff = new_staff
    apply_snapshot(task, snap)
    if task.status == Status.COMPLETED and not task.completed_at:
        task.completed_at = timezone.now()
    task.sync_state = SharedTask.Sync.SYNCED
    task.last_synced_at = timezone.now()
    task.version += 1
    task.save()
    _alert_status_change(task, old_status, snap)
    return True


def _rebuild_from_snapshot(link: TaskPanelLink, cursor: SyncCursor):
    client = get_client()
    page = ""
    high_water = ""
    for _ in range(20):
        data = client.snapshot(link, page)
        high_water = high_water or data.get("high_water_cursor", "")
        with transaction.atomic():
            for snap in data.get("tasks", []):
                task = SharedTask.objects.select_for_update().filter(remote_task_uuid=snap.get("uuid")).first()
                if task and int(snap.get("sync_revision") or 0) > task.sync_revision:
                    old = task.remote_status
                    apply_snapshot(task, snap)
                    task.save()
                    _alert_status_change(task, old, snap)
        page = data.get("next_page") or ""
        if not page:
            break
    cursor.cursor = high_water
    cursor.last_success_at = timezone.now()
    cursor.last_error = ""
    cursor.save()


def pull_changes(link: TaskPanelLink, max_batches: int = 5) -> int:
    cursor, _ = SyncCursor.objects.get_or_create(link=link)
    client = get_client()
    applied = 0
    for _ in range(max_batches):
        try:
            data = client.changes(link, cursor.cursor, limit=100)
        except TaskPanelError as err:
            if err.status == 410:
                log.info("Change cursor expired for %s; rebuilding from snapshot", link.actor_key)
                _rebuild_from_snapshot(link, cursor)
                return applied
            cursor.last_error = f"{err.code}: {err.message}"[:200]
            cursor.save(update_fields=["last_error", "updated_at"])
            raise
        with transaction.atomic():
            for event in data.get("events", []):
                if apply_event(event, link):
                    applied += 1
            cursor.cursor = data.get("next_cursor") or cursor.cursor
            cursor.last_success_at = timezone.now()
            cursor.last_error = ""
            cursor.save()
        if not data.get("has_more"):
            break
    return applied


def pull_all(max_batches: int = 5) -> int:
    total = 0
    for link in TaskPanelLink.objects.filter(active=True).select_related("user"):
        try:
            total += pull_changes(link, max_batches=max_batches)
        except TaskPanelError as err:
            log.warning("Change feed for %s failed: %s", link.actor_key, err.code)
    return total


def status_label(task: SharedTask) -> str:
    from .contracts import STATUS_LABEL

    if task.sync_state == SharedTask.Sync.PENDING:
        return "Pending confirmation"
    if task.sync_state == SharedTask.Sync.FAILED:
        return "Not assigned"
    return STATUS_LABEL.get(task.remote_status, "Assigned")


__all__ = ["sync_directory", "ensure_directory", "pull_changes", "pull_all", "apply_event", "link_for",
           "status_label", "STATUS_FROM_TP"]
