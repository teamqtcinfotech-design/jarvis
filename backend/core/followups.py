"""Nudges JARVIS raises on its own, so nothing quietly slips:

- a staff task with no update for 2 days          -> Sir (and Mansi, who can chase)
- a request from Mansi unanswered for 4 hours     -> Sir
- a task Sir gave Mansi not started after 1 day   -> Mansi
- 18:30: the day's slipped items                  -> Sir

Each nudge is an Alert; the phone speaks it when he is with the phone (or shows it quietly in a meeting).
Nudges are worded so the natural reply is a command ("move both to tomorrow", "remind Mansi"), never a
dangling yes/no. Each one is raised at most once per two days. Nothing is raised at night.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from . import timeutils as tu
from .models import Alert, Priority, SharedTask, Status, TaskRequest, User
from .services import delegated_qs, get_md, get_pa, md_private_qs, pa_tasks_qs, shared_md_tasks_qs

log = logging.getLogger("jarvis.followups")

STAFF_STALE = timedelta(days=2)
REQUEST_STALE = timedelta(hours=4)
PA_NOT_STARTED = timedelta(days=1)
REPEAT_AFTER = timedelta(days=2)
ACTIVE_HOURS = range(9, 20)  # 9:00 .. 19:59 local
DIGEST_AT = (18, 30)
OPEN = [Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED]


def _already(key: str) -> bool:
    return Alert.objects.filter(ref_type="followup", ref_id=key, created_at__gte=timezone.now() - REPEAT_AFTER).exists()


def _raise(recipient: User, key: str, title: str, body: str, spoken: str, priority: str = Priority.NORMAL) -> bool:
    if _already(key + f"@{recipient.id}"):
        return False
    Alert.objects.create(recipient=recipient, kind=Alert.Kind.INFO, priority=priority, title=title[:200], body=body[:400],
                         spoken_text=spoken[:400], ref_type="followup", ref_id=(key + f"@{recipient.id}")[:64])
    return True


def _days(delta: timedelta) -> str:
    d = delta.days
    return f"{d} day{'s' if d != 1 else ''}"


def run(now=None) -> int:
    """One pass. Safe to call every few minutes; it only raises what is due and not yet raised."""
    now = now or timezone.now()
    local = tu.to_local(now)
    if local.hour not in ACTIVE_HOURS:
        return 0
    md, pa = get_md(), get_pa()
    if not md:
        return 0
    pa_name = pa.spoken_name if pa else "your PA"
    raised = 0

    # 1) Staff tasks with no update for 2 days
    for task in delegated_qs().filter(status__in=OPEN):
        last = task.updated_at or task.created_at
        if not last or now - last < STAFF_STALE:
            continue
        who = task.staff.name if task.staff else "a staff member"
        age = _days(now - last)
        key = f"stale:{task.id}"
        raised += _raise(md, key, f"{who}: no update for {age}", task.title,
                         f"Sir, {who}'s task '{task.title}' has had no update for {age}. "
                         f"You can say: remind {pa_name}, or move the date.")
        if pa:
            raised += _raise(pa, key, f"{who}: no update for {age}", task.title,
                             f"{pa_name}, {who}'s task '{task.title}' has had no update for {age}. Please check with them.")

    # 2) Mansi's requests waiting for Sir
    for req in TaskRequest.objects.filter(state=TaskRequest.State.PENDING, created_at__lte=now - REQUEST_STALE).select_related("requested_by"):
        hours = int((now - req.created_at).total_seconds() // 3600)
        title = (req.payload or {}).get("title") or "a request"
        raised += _raise(md, f"request:{req.id}", "Request waiting for your decision", title,
                         f"Sir, {req.requested_by.spoken_name}'s request '{title}' has been waiting {hours} hours. "
                         f"You can say: accept it, or decline it.", Priority.HIGH)

    # 3) Tasks Sir gave Mansi, not started after a day
    if pa:
        for task in pa_tasks_qs().filter(status=Status.OPEN, created_at__lte=now - PA_NOT_STARTED).exclude(origin=SharedTask.Origin.MD_MEET_PA):
            raised += _raise(pa, f"notstarted:{task.id}", "Not started yet", task.title,
                             f"{pa_name}, Sir's task '{task.title}' is not started yet. You can say: start it, or put it on hold with a reason.")

    # 4) End of day: what slipped
    if (local.hour, local.minute) >= DIGEST_AT:
        key = f"digest:{local.date().isoformat()}"
        if not _already(key + f"@{md.id}"):
            today = local.date()
            slipped = []
            private_count = 0
            for t in md_private_qs(md).filter(status__in=OPEN):
                due = tu.to_local(t.due_at).date() if t.due_at else t.due_date
                if due and due <= today:
                    private_count += 1
            for t in shared_md_tasks_qs().filter(status__in=OPEN):
                due = tu.to_local(t.due_at).date() if t.due_at else t.due_date
                if due and due <= today:
                    slipped.append(t.title)
            total = private_count + len(slipped)
            if total:
                names = "; ".join(slipped[:3])
                parts = []
                if slipped:
                    parts.append(names)
                if private_count:
                    parts.append(f"{private_count} private")
                spoken = (f"Sir, {total} thing{'s' if total > 1 else ''} slipped today: " + " and ".join(parts) +
                          ". Tell me if you want them moved to tomorrow.")
                raised += _raise(md, key, f"{total} slipped today", " · ".join(parts), spoken)
    if raised:
        log.info("raised %d follow-up(s)", raised)
    return raised
