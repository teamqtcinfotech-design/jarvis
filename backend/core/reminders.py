"""Reminder schedule for the phone.

The phone schedules these as exact local alarms (works offline, survives sleep)
and its presence engine decides at fire time whether to speak, vibrate or hold.

Keys include the task version, so rescheduling a task automatically retires the
old reminders. Acknowledged keys are filtered out.
"""
from datetime import datetime, timedelta

from django.utils import timezone

from . import timeutils as tu
from .models import Kind, Priority, PrivateTask, ReminderAck, SharedTask, Status, User
from .services import (
    DATE_ONLY_REMINDER_TIME, busy_windows, delegated_qs, get_pa, md_private_qs, pa_tasks_qs, shared_md_tasks_qs,
)

LOOKBACK = timedelta(hours=12)


def _lead_phrase(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"in {hours} hour{'s' if hours > 1 else ''}"
    return f"in {minutes} minutes"


def _task_reminders(task, scope: str, is_private: bool, address: str) -> list[dict]:
    out = []
    base = {
        "priority": task.priority,
        "title": task.title,
        "kind": task.kind,
        "is_private": is_private,
        "ref": {"scope": scope, "id": str(task.id), "version": task.version},
    }
    key_prefix = f"{scope}:{task.id}:v{task.version}"
    start = task.scheduled_start or task.due_at
    if start:
        lead = task.remind_before_minutes or 0
        if lead > 0:
            spoken = task.announcement or f"{address}, {task.title} {_lead_phrase(lead)}."
            if task.kind == Kind.MEETING and "minute" not in spoken and "hour" not in spoken:
                spoken = f"{address}, your meeting {task.title} starts {_lead_phrase(lead)}."
            out.append({**base, "key": f"{key_prefix}:before", "label": "before",
                        "fire_at": (start - timedelta(minutes=lead)).isoformat(), "spoken": spoken})
        if task.kind == Kind.MEETING:
            due_spoken = f"{address}, your meeting {task.title} is starting now."
        elif task.kind == Kind.CALL:
            due_spoken = f"{address}, it's time for your call: {task.title}."
        else:
            due_spoken = f"{address}, it's time: {task.title}."
        out.append({**base, "key": f"{key_prefix}:due", "label": "due", "fire_at": start.isoformat(),
                    "spoken": due_spoken})
    elif task.due_date:
        fire = tu.combine_local(task.due_date, DATE_ONLY_REMINDER_TIME)
        out.append({**base, "key": f"{key_prefix}:day", "label": "day", "fire_at": fire.isoformat(),
                    "spoken": f"{address}, today's reminder: {task.title}."})
    return out


def reminders_for(user: User, hours: int = 48) -> dict:
    now = timezone.now()
    window_start, window_end = now - LOOKBACK, now + timedelta(hours=hours)
    items: list[dict] = []
    open_filter = {"status__in": [Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED]}

    if user.is_md:
        for task in md_private_qs(user).filter(**open_filter):
            items += _task_reminders(task, "private", True, "Sir")
        for task in shared_md_tasks_qs().filter(**open_filter):
            items += _task_reminders(task, "shared", False, "Sir")
        # Anything involving the PA is for both: the MD is reminded too.
        pa = get_pa()
        pa_name = pa.spoken_name if pa else "your PA"
        for task in pa_tasks_qs().filter(**open_filter):
            meeting = task.origin == SharedTask.Origin.MD_MEET_PA
            for item in _task_reminders(task, "shared", False, "Sir"):
                lead = _lead_phrase(task.remind_before_minutes or 0)
                if meeting:
                    item["title"] = f"Meeting with {pa_name}: {task.title}"
                    item["spoken"] = (f"Sir, your meeting with {pa_name} starts {lead}: {task.title}."
                                      if item["label"] == "before" else
                                      f"Sir, your meeting with {pa_name} is starting now: {task.title}.")
                else:
                    item["title"] = f"{pa_name}: {task.title}"
                    item["spoken"] = (f"Sir, {pa_name}'s task {task.title} is due {lead}."
                                      if item["label"] == "before" else
                                      f"Sir, {pa_name}'s task is due now: {task.title}. Please check with her.")
                items.append(item)
    else:
        name = user.spoken_name
        for task in pa_tasks_qs().filter(**open_filter):  # her own tasks from Sir
            items += _task_reminders(task, "shared", False, name)
        for task in delegated_qs().filter(created_by=user, **open_filter):
            start = task.due_at
            if not start:
                continue
            items.append({
                "key": f"shared:{task.id}:v{task.version}:followup",
                "label": "followup",
                "fire_at": start.isoformat(),
                "priority": task.priority,
                "title": f"{task.staff.name}: {task.title}",
                "kind": Kind.FOLLOW_UP,
                "is_private": False,
                "ref": {"scope": "shared", "id": str(task.id), "version": task.version},
                "spoken": f"{name}, {task.staff.name}'s task is due now: {task.title}. Please follow up.",
            })

    acked = set(ReminderAck.objects.filter(user=user, key__in=[i["key"] for i in items]).values_list("key", flat=True))
    result = []
    for item in items:
        fire = datetime.fromisoformat(item["fire_at"])
        if item["key"] in acked or not (window_start <= fire <= window_end):
            continue
        item["fire_at_label"] = f"{tu.fmt_day(tu.to_local(fire).date())} {tu.fmt_time(fire)}"
        result.append(item)
    result.sort(key=lambda r: r["fire_at"])

    today = tu.today_local()
    meetings = []
    if user.is_md:
        meetings = [{"start": w["start"], "end": w["end"]} for w in busy_windows(today)]
    return {"server_time": now.isoformat(), "reminders": result, "busy_windows": meetings}
