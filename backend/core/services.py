"""Business rules. Every write goes through here so permissions and audit are uniform.

Privacy rules enforced in this module:
- PrivateTask is only ever read with owner=MD and only by MD-authorised views.
- Nothing derived from a PrivateTask reaches a PA except anonymous busy windows.
- PA changes to MD work are TaskRequests; nothing changes until the MD accepts.
"""
import difflib
import hashlib
import json
import logging
import re
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
    Contact,
    ConversationState,
    DueKind,
    IntegrationCommand,
    Kind,
    Memory,
    Priority,
    PrivateTask,
    ReminderAck,
    ReminderSnooze,
    ReminderTold,
    SharedTask,
    StaffMember,
    Status,
    TaskCategory,
    TaskRequest,
    User,
    UserPrefs,
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


# --------------------------------------------------------------------------- how people are addressed
# The MD chooses how JARVIS addresses them, and the PA refers to the MD the same way: "Sir", "Ma'am", "Madam" or
# the MD's own name. The PA is always addressed by first name. Nothing else in the code should spell "Sir".
# JARVIS speaks with a male voice. Naksh is the Indian voice; the others are offered so the MD can pick by ear.
VOICE_CHOICES = ("naksh", "rex", "leo", "orion", "atlas", "sirius")
DEFAULT_VOICE = "naksh"

ADDRESS_HI = {"sir": "सर", "ma'am": "मैम", "maam": "मैम", "mam": "मैम", "madam": "मैडम", "madame": "मैडम", "boss": "बॉस"}
FEMALE_ADDRESS = {"ma'am", "maam", "mam", "madam", "madame", "miss", "mrs", "ms"}


def md_address() -> str:
    """How the MD is addressed and referred to ("Sir", "Ma'am", or the MD's name): the MD's own setting."""
    md = get_md()
    if md is None:
        return "Sir"
    raw = " ".join(str(get_prefs(md).get("addressAs") or "").split())
    if not raw:
        return "Sir"
    if raw.lower() in ("name", "naam", "my name", "mera naam", "by name"):
        return md.spoken_name
    return raw[:40]


def md_address_hi() -> str:
    word = md_address()
    return ADDRESS_HI.get(word.lower(), word)


def md_pronouns() -> tuple[str, str, str]:
    """(he, him, his) or (she, her, her), from the form of address."""
    return ("she", "her", "her") if md_address().lower() in FEMALE_ADDRESS else ("he", "him", "his")


def md_gender() -> str:
    return "female" if md_address().lower() in FEMALE_ADDRESS else "male"


def pa_gender() -> str:
    """"male" / "female" as the MD set it, or "" when not set (then JARVIS goes by the PA's name and stays neutral)."""
    md = get_md()
    value = str(get_prefs(md).get("paGender") or "") if md else ""
    return value if value in ("male", "female") else ""


def pa_pronouns() -> tuple[str, str, str]:
    """(he, him, his), (she, her, her), or (they, them, their) when the MD has not said."""
    return {"male": ("he", "him", "his"), "female": ("she", "her", "her")}.get(pa_gender(), ("they", "them", "their"))


def voice_of(user: User) -> str:
    """The voice JARVIS speaks to this person with: their own choice in Settings (a male voice either way)."""
    value = str(get_prefs(user).get("voiceName") or "")
    return value if value in VOICE_CHOICES else DEFAULT_VOICE


def address_of(user: User) -> str:
    """How JARVIS addresses this person when speaking to them."""
    return md_address() if user.is_md else user.spoken_name


def address_hi_of(user: User) -> str:
    return md_address_hi() if user.is_md else user.spoken_name


# --------------------------------------------------------------------------- schedule helpers
def default_remind_before(kind: str, priority: str, prefs: dict | None = None) -> int:
    """Lead time for a reminder when the user did not ask for one: from the reader's settings, by kind."""
    prefs = prefs or pref_defaults()
    if kind == Kind.MEETING:
        return int(prefs.get("remindBeforeMeeting", 15))
    if kind == Kind.CALL:
        return int(prefs.get("remindBeforeCall", 5))
    lead = int(prefs.get("remindBeforeTask", 30))
    return min(lead, 15) if priority == Priority.URGENT else lead


def day_reminder_time(prefs: dict | None = None) -> time:
    raw = (prefs or {}).get("dayReminderTime") or "09:30"
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return DATE_ONLY_REMINDER_TIME


def apply_schedule(task, data: dict, *, creating: bool):
    """Apply due date/time, meeting slot and reminder lead time from validated input."""
    kind = data.get("kind", task.kind)
    priority = data.get("priority", task.priority)
    due_date_val: date | None = data.get("due_date")
    due_time_val: time | None = data.get("due_time")
    exact: datetime | None = data.get("due_at")  # "in 5 minutes": the exact moment, seconds included

    if "due_date" in data or "due_time" in data or exact or creating:
        if exact:
            task.due_kind = DueKind.TIMED
            task.due_date = tu.to_local(exact).date()
            task.due_at = exact
        elif due_date_val and due_time_val:
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
        usual = int((data.get("_prefs") or {}).get("meetingMinutes") or DEFAULT_MEETING_MINUTES)  # the person's usual length
        task.scheduled_end = task.due_at + timedelta(minutes=task.estimated_minutes or usual)
    elif kind != Kind.MEETING:
        task.scheduled_start = None
        task.scheduled_end = None

    # Only an explicit lead ("15 minute pehle yaad dilana") is stored. Otherwise it stays empty and the reader's
    # own setting for that kind applies at reminder time, so changing the setting by voice covers every task.
    if data.get("remind_before_minutes"):
        task.remind_before_minutes = data["remind_before_minutes"]
    elif data.get("reminder"):
        task.remind_before_minutes = 0  # a plain reminder rings at its moment, never "30 minutes before"


def is_overdue(task, now: datetime | None = None) -> bool:
    if task.is_closed:
        return False
    now = now or timezone.now()
    if task.due_kind == DueKind.TIMED and task.due_at:
        return task.due_at < now
    if task.due_kind == DueKind.DATE and task.due_date:
        return task.due_date < tu.today_local()
    return False


# A task's "announcement" holds only the person's OWN reminder line ("remind me: take the blue file"); every other
# reminder sentence is built when it is served (reminders.py), so it follows the reader's language and the MD's
# form of address as they are today, not as they were when the task was written down.


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
    due_label = due_label_hi = ""
    if payload.get("due_date"):
        d = date.fromisoformat(payload["due_date"])
        if payload.get("due_time"):
            at = tu.combine_local(d, time.fromisoformat(payload["due_time"]))
            due_label, due_label_hi = tu.due_phrase("TIMED", d, at), tu.due_phrase_hi("TIMED", d, at)
        else:
            due_label, due_label_hi = tu.fmt_day(d), tu.fmt_day_hi(d)
    return {
        "id": str(req.id),
        "kind": req.kind,
        "state": req.state,
        "title": payload.get("title") or (req.target.title if req.target_id else ""),
        "payload": payload,
        "priority": payload.get("priority", req.target.priority if req.target_id else "NORMAL"),
        "due_label": due_label,
        "due_label_hi": due_label_hi,
        "reason": req.reason,
        "target_id": str(req.target_id) if req.target_id else None,
        "target_title": req.target.title if req.target_id else None,
        "base_version": req.base_version,
        "requested_by": req.requested_by.spoken_name,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "decision_note": req.decision_note,
        "created_at": req.created_at.isoformat(),
    }


def mark_alerts_told(user: User, ids: list) -> int:
    """The phone (app or background part) spoke or showed these: nobody announces them again."""
    clean = [str(i)[:64] for i in ids if i][:100]
    if not clean:
        return 0
    return Alert.objects.filter(recipient=user, id__in=clean, delivered_at__isnull=True).update(delivered_at=timezone.now())


def mark_reminders_told(user: User, keys: list) -> int:
    from .models import ReminderTold

    n = 0
    for key in [str(k)[:200] for k in keys if k][:100]:
        _, made = ReminderTold.objects.get_or_create(user=user, key=key)
        n += int(made)
    return n


def close_task_alerts(task) -> int:
    """A task that is finished or cancelled has nothing left to announce: its alerts count as seen."""
    return Alert.objects.filter(ref_type="shared_task", ref_id=str(task.id), acknowledged_at__isnull=True) \
        .update(acknowledged_at=timezone.now())


ALERT_FRESH_FOR = timedelta(hours=24)   # older than this it is history, never news
ALERT_LIST_FOR = timedelta(days=7)      # and older than this it is not even listed


def alert_question(alert: Alert) -> dict | None:
    """The one question JARVIS asks aloud right after this alert, and what a yes does. Kept tight so JARVIS never
    nags: a request to the MD ("Accept it?"), a task the MD gave the PA ("Shall I tell Sir you've seen it?"), a
    briefing that found the day too full ("Shall I move X to tomorrow?"). Decisions and staff news are spoken only.
    Both the app and the phone's background part read this contract; neither spells its own."""
    if isinstance(alert.question, dict) and alert.question.get("kind"):
        return alert.question
    if alert.kind == Alert.Kind.NEW_REQUEST and alert.ref_type == "request" and alert.recipient.is_md:
        return {"kind": "accept", "en": "Accept it?", "hi": "मंज़ूर करूँ?"}
    if alert.kind == Alert.Kind.TASK_ASSIGNED and alert.recipient.is_pa:
        return {"kind": "seen", "en": f"Shall I tell {md_address()} you've seen it?",
                "hi": f"{md_address_hi()} को बता दूँ कि आपने देख लिया?"}
    return None


def alert_item(alert: Alert, lang: str = "en") -> dict:
    """lang = the recipient's current language: the spoken line comes in Hindi when it has a Hindi twin."""
    now = timezone.now()
    snoozed = alert.snoozed_until if alert.snoozed_until and alert.snoozed_until > now else None
    return {
        "id": str(alert.id),
        "kind": alert.kind,
        "priority": alert.priority,
        "title": alert.title,
        "body": alert.body,
        "spoken_text": alert.spoken_text_hi if lang == "hi" and alert.spoken_text_hi else alert.spoken_text,
        "spoken_text_hi": alert.spoken_text_hi,
        "spoken_text_en": alert.spoken_text,
        "ref_type": alert.ref_type,
        "ref_id": alert.ref_id,
        "created_at": alert.created_at.isoformat(),
        "delivered_at": alert.delivered_at.isoformat() if alert.delivered_at else None,
        "snoozed_until": snoozed.isoformat() if snoozed else None,
        # The key the phone announces under: a snoozed alert comes back as a new announcement, not a repeat.
        "announce_key": f"alert:{alert.id}" + (f":s{int(alert.snoozed_until.timestamp())}" if alert.snoozed_until else ""),
        "question": alert_question(alert),
        # Worth announcing: not told yet, and recent (a snooze that has come due counts as new). Everything else is
        # only for the list.
        "fresh": alert.delivered_at is None and not snoozed and (
            alert.created_at >= now - ALERT_FRESH_FOR or (alert.snoozed_until is not None and alert.snoozed_until >= now - ALERT_FRESH_FOR)),
    }


# --------------------------------------------------------------------------- "later"
SNOOZE_LOOKBACK = timedelta(hours=3)  # "baad mein yaad dilana" means the thing JARVIS said within this long


def moment_label(at: datetime) -> str:
    """"at 4:25 PM" today, "tomorrow at 10 AM", "Mon 17 Nov at 9 AM": for a confirmation that names the exact time."""
    local = tu.to_local(at)
    day = tu.fmt_day(local.date())
    return f"at {tu.fmt_time(local)}" if day == "today" else f"{day} at {tu.fmt_time(local)}"


def moment_label_hi(at: datetime) -> str:
    local = tu.to_local(at)
    day = tu.fmt_day_hi(local.date())
    return tu.fmt_time_hi(local) if day == "आज" else f"{day} {tu.fmt_time_hi(local)}"


def snooze_reminder(user: User, key: str, until: datetime) -> None:
    """This reminder comes back at `until`, as a fresh announcement (the list serves it under a new key)."""
    ReminderSnooze.objects.update_or_create(user=user, key=key[:200], defaults={"until": until})


def snooze_alert(user: User, alert_id, until: datetime) -> Alert:
    alert = Alert.objects.get(id=alert_id, recipient=user)
    alert.snoozed_until = until
    alert.delivered_at = None  # announced afresh when it comes back
    alert.save(update_fields=["snoozed_until", "delivered_at"])
    return alert


def snooze_last_told(user: User, until: datetime) -> dict | None:
    """"Later" with nothing on screen: the reminder or alert JARVIS spoke to this person most recently (within
    SNOOZE_LOOKBACK) is the one meant. Returns what was snoozed ({"kind", "title"}) or None when nothing was."""
    since = timezone.now() - SNOOZE_LOOKBACK
    told = ReminderTold.objects.filter(user=user, told_at__gte=since).order_by("-told_at").first()
    alert = (Alert.objects.filter(recipient=user, acknowledged_at__isnull=True, delivered_at__gte=since)
             .exclude(priority=Priority.LOW).order_by("-delivered_at").first())
    if told and (not alert or told.told_at >= alert.delivered_at):
        from .reminders import reminders_for  # local import: reminders imports services
        if ReminderAck.objects.filter(user=user, key=told.key).exists():
            return None  # already dealt with
        snooze_reminder(user, told.key, until)
        title = next((r["title"] for r in reminders_for(user, hours=48)["reminders"] if r["key"].startswith(told.key)), "")
        return {"kind": "reminder", "key": told.key, "title": title}
    if alert:
        snooze_alert(user, alert.id, until)
        return {"kind": "alert", "id": str(alert.id), "title": alert.title}
    return None


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
            # No date on it, but it was given today: it is today's work until it is done or dated.
            made = item.get("created_at")
            given_today = bool(made) and tu.to_local(datetime.fromisoformat(made)).date() == today
            buckets["today" if given_today else "anytime"].append(item)
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


def pending_split(user: User, dash: dict, include_private: bool = True) -> dict:
    """Everything still open on the speaker's own list, split the way they think about it.

    Sir has OFFICIAL work (shared: his PA can see it) and PERSONAL items (private: only he can). Personal ones
    are listed and counted only when include_private is true. A PA's dashboard holds no private items at all,
    so nothing of his can reach her through this. Mansi's split is what Sir gave her and what is her own.
    Returns {"items": late first, then today, then the rest; "counts": {...}}.
    """
    def keep(item):
        return include_private or item.get("scope") != "private"
    late = [i for i in dash["overdue"] if keep(i)]
    today = [i for i in dash["today"] if keep(i)]
    later = [i for i in dash["upcoming"] + dash["anytime"] if keep(i)]
    items = late + today + later
    counts = {"total": len(items), "late": len(late), "due_today": len(today), "later_or_no_date": len(later)}
    if user.is_md:
        personal = sum(1 for i in items if i.get("scope") == "private")
        counts["official"] = len(items) - personal
        if include_private:
            counts["personal"] = personal
    else:
        own = sum(1 for i in items if i.get("origin") == SharedTask.Origin.PA_SELF)
        counts["from_sir"] = len(items) - own
        counts["own"] = own
    return {"items": items, "counts": counts}


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
        # A meeting or call the PA arranged for the MD (her request, accepted) belongs in the PA's day as well:
        # she set it up and has to see it coming, not only in the "approved" list.
        items += [shared_item(t) for t in shared_md_tasks_qs().filter(kind__in=[Kind.MEETING, Kind.CALL])
                  .exclude(status=Status.CANCELLED)]
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
    pa_user, md_user = get_pa(), get_md()
    out = {
        "user": {"username": user.username, "role": user.role, "name": user.spoken_name},
        # The two names as JARVIS says them, and how the MD is addressed, so no screen has to carry a typed-in word.
        "pa_name": pa_user.spoken_name if pa_user else "",
        "md_name": md_user.spoken_name if md_user else "",
        "md_address": md_address(),
        "md_address_hi": md_address_hi(),
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
    apply_schedule(task, {**data, "_prefs": get_prefs(md)}, creating=True)
    task.announcement = (data.get("announcement") or "")[:300]
    task.save()
    if task.kind == Kind.MEETING and data.get("duration_minutes"):
        learn_meeting_minutes(md, int(data["duration_minutes"]))
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
    apply_schedule(task, {**data, "kind": task.kind, "_prefs": get_prefs(md)}, creating=True)
    sir, sir_hi = md_address(), md_address_hi()
    task.announcement = (data.get("announcement") or "")[:300]
    task.save()
    if task.kind == Kind.MEETING and data.get("duration_minutes"):
        learn_meeting_minutes(md, int(data["duration_minutes"]))
    when = tu.due_phrase(task.due_kind, task.due_date, task.due_at)
    when_hi = tu.due_phrase_hi(task.due_kind, task.due_date, task.due_at)
    pr_en = {"URGENT": "urgent ", "HIGH": "high-priority "}.get(task.priority, "")
    pr_hi = {"URGENT": "अर्जेंट ", "HIGH": "ज़रूरी "}.get(task.priority, "")
    if meeting_with_pa:
        title = f"Meeting with {sir}"
        spoken = f"{pa.spoken_name}, {sir} has scheduled a meeting with you" + (f" {when}" if when else "") + f": {task.title}."
        spoken_hi = f"{pa.spoken_name}, {sir_hi} ने आपके साथ मीटिंग रखी है" + (f" {when_hi}" if when_hi else "") + f": {task.title}।"
    else:
        title = f"New task from {sir}"
        spoken = f"{pa.spoken_name}, {sir} has given you a new {pr_en}task: {task.title}" + (f", {when}" if when else "") + "."
        spoken_hi = f"{pa.spoken_name}, {sir_hi} ने आपको एक नया {pr_hi}टास्क दिया है: {task.title}" + (f", {when_hi}" if when_hi else "") + "।"
    # A brief professional note in the MD's name (template above if the AI is unavailable), in both languages.
    from .compose import compose  # local import: compose depends on the AI client, services must not
    message, message_hi = compose(
        "meeting_with_pa" if meeting_with_pa else "task_for_pa", f"{sir} (the Managing Director)", pa.spoken_name,
        {"title": task.title, "when": when, "priority": task.priority.lower(), "instruction": task.description,
         "notes": task.notes}, fallback=spoken, fallback_hi=spoken_hi, user=md,
    )
    Alert.objects.create(
        recipient=pa, kind=Alert.Kind.TASK_ASSIGNED, priority=task.priority, title=title,
        body=message[:400], spoken_text=message[:400], spoken_text_hi=message_hi[:400],
        ref_type="shared_task", ref_id=str(task.id),
    )
    audit(md, "pa_meeting.create" if meeting_with_pa else "pa_task.create", "shared_task", task.id)
    return task


@transaction.atomic
def create_pa_self_task(pa: User, data: dict) -> SharedTask:
    """Mansi's own task or reminder ("mere liye", "mujhe yaad dilana"). Sir can see it in her list; it is not
    sent to him for approval and he is not nagged about it."""
    if not pa.is_pa:
        raise ServiceError("forbidden", "Only the PA keeps their own tasks here.", 403)
    task = SharedTask(
        title=data["title"].strip(), notes=data.get("notes", ""), kind=data.get("kind", Kind.TASK),
        priority=data.get("priority", Priority.NORMAL), assignee_type=SharedTask.Assignee.PA,
        origin=SharedTask.Origin.PA_SELF, created_by=pa, description=(data.get("description") or "").strip()[:5000],
        sync_state=SharedTask.Sync.NOT_REQUIRED,
    )
    apply_schedule(task, {**data, "_prefs": get_prefs(pa)}, creating=True)
    if task.kind == Kind.MEETING and data.get("duration_minutes"):
        learn_meeting_minutes(pa, int(data["duration_minutes"]))
    task.announcement = (data.get("announcement") or "")[:300]
    task.save()
    audit(pa, "pa_self_task.create", "shared_task", task.id)
    return task


@transaction.atomic
def update_pa_self_task(pa: User, task_id, changes: dict, expected_version: int) -> SharedTask:
    """Mansi changes, moves or cancels a task that is her own (never one Sir gave her)."""
    if not pa.is_pa:
        raise ServiceError("forbidden", "Not allowed.", 403)
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_type=SharedTask.Assignee.PA,
                                                      origin=SharedTask.Origin.PA_SELF, created_by=pa)
    _check_version(task, expected_version)
    _apply_changes(task, changes)
    task.save()
    audit(pa, "pa_self_task.update", "shared_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


PA_SETTABLE_STATUSES = {Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED, Status.COMPLETED}


@transaction.atomic
def update_pa_task_status(pa: User, task_id, new_status: str, expected_version: int, note: str = "") -> SharedTask:
    """The PA moves her task: Not started / Working / Hold (with reason) / Done. MD is told."""
    if not pa.is_pa:
        raise ServiceError("forbidden", "Only the PA updates the status of their tasks.", 403)
    if new_status not in PA_SETTABLE_STATUSES:
        raise ServiceError("invalid_status", "Choose Not started, Working, Hold or Done.", 422)
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_type=SharedTask.Assignee.PA)
    if task.status == Status.CANCELLED:
        own = task.origin == SharedTask.Origin.PA_SELF
        raise ServiceError("cancelled", "This task was cancelled." if own else f"{md_address()} has cancelled this task.", 409)
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
    if md and new_status != old and task.origin != SharedTask.Origin.PA_SELF:
        name, sir, sir_hi = pa.spoken_name, md_address(), md_address_hi()
        spoken_hi = ""
        if new_status == Status.COMPLETED:
            title, priority, spoken = f"{name} completed a task", Priority.NORMAL, f"{sir}, {name} has completed: {task.title}."
            spoken_hi = f"{sir_hi}, {name} ने पूरा कर दिया: {task.title}।"
        elif new_status == Status.BLOCKED:
            title, priority = f"{name} put a task on hold", Priority.NORMAL
            spoken = f"{sir}, {name} has put {task.title} on hold. Reason: {task.hold_reason}."
            spoken_hi = f"{sir_hi}, {name} ने {task.title} होल्ड पर रखा है। कारण: {task.hold_reason}।"
        elif new_status == Status.IN_PROGRESS:
            title, priority, spoken = f"{name} started a task", Priority.LOW, ""
        else:
            title, priority, spoken = f"{name} reopened a task", Priority.LOW, ""
        Alert.objects.create(
            recipient=md, kind=Alert.Kind.STAFF_UPDATE, priority=priority, title=title, body=task.title,
            spoken_text=spoken[:400], spoken_text_hi=spoken_hi[:400], ref_type="shared_task", ref_id=str(task.id),
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
    pa_name, sir, sir_hi = req.requested_by.spoken_name, md_address(), md_address_hi()
    pr_en = {"URGENT": "urgent ", "HIGH": "high-priority "}.get(item["priority"], "")
    pr_hi = {"URGENT": "अर्जेंट ", "HIGH": "ज़रूरी "}.get(item["priority"], "")
    if req.kind == TaskRequest.Kind.CREATE:
        when = f", {item['due_label']}" if item["due_label"] else ""
        when_hi = f", {item['due_label_hi']}" if item["due_label_hi"] else ""
        spoken = f"{sir}, {pa_name} has sent you a new {pr_en}task: {item['title']}{when}. Would you like to accept it?"
        spoken_hi = f"{sir_hi}, {pa_name} ने आपको एक नया {pr_hi}टास्क भेजा है: {item['title']}{when_hi}। मंज़ूर करूँ?"
        title = f"New task from {pa_name}"
        kind = "request_to_md"
    else:
        spoken = f"{sir}, {pa_name} has proposed a change to {item['target_title']}. Please review it."
        spoken_hi = f"{sir_hi}, {pa_name} ने {item['target_title']} में बदलाव सुझाया है। कृपया देख लीजिए।"
        title = f"Change proposed by {pa_name}"
        kind = "change_request"
    # A courteous note in the PA's name (template above if the AI is unavailable), in both languages.
    from .compose import compose
    message, message_hi = compose(kind, f"{pa_name} (the MD's personal assistant)", sir, {
        "title": item["title"], "when": item["due_label"], "priority": item["priority"].lower(),
        "reason": req.reason, "target": item.get("target_title") or "",
    }, fallback=spoken, fallback_hi=spoken_hi, user=req.requested_by)
    Alert.objects.create(
        recipient=md, kind=Alert.Kind.NEW_REQUEST, priority=item["priority"], title=title,
        body=message[:400], spoken_text=message[:400], spoken_text_hi=message_hi[:400], ref_type="request", ref_id=str(req.id),
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
    sir, sir_hi, name = md_address(), md_address_hi(), req.requested_by.spoken_name
    if conflict:
        text = f"Your request '{item['title']}' could not be applied because the task changed. Please send a fresh one."
        text_hi = f"आपकी रिक्वेस्ट '{item['title']}' लागू नहीं हो सकी क्योंकि टास्क बदल गया था। कृपया नई भेजिए।"
    elif accepted:
        text = f"{sir} accepted: {item['title']}."
        text_hi = f"{sir_hi} ने मंज़ूर किया: {item['title']}।"
    else:
        text = f"{sir} declined: {item['title']}." + (f" Note: {req.decision_note}" if req.decision_note else "")
        text_hi = f"{sir_hi} ने अस्वीकार किया: {item['title']}।" + (f" नोट: {req.decision_note}" if req.decision_note else "")
    Alert.objects.create(
        recipient=req.requested_by, kind=Alert.Kind.REQUEST_DECIDED, priority=Priority.NORMAL,
        title="Request accepted" if accepted else ("Request conflict" if conflict else "Request declined"),
        body=item["title"], spoken_text=f"{name}, {text}"[:400], spoken_text_hi=f"{name}, {text_hi}"[:400],
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
            apply_schedule(task, {**data, "_prefs": get_prefs(md)}, creating=True)
            task.announcement = (data.get("announcement") or "")[:300]
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
def tell_sender_seen(user: User, alert: Alert) -> Alert | None:
    """Mansi answered yes to "shall I tell Sir you've seen it?": one short note for Sir. Spoken when the task
    matters (urgent / high), otherwise it only sits in his lists. Never raised twice for the same alert."""
    if not user.is_pa or alert.kind != Alert.Kind.TASK_ASSIGNED:
        return None
    md = get_md()
    if md is None:
        return None
    existing = Alert.objects.filter(recipient=md, ref_type="seen", ref_id=str(alert.id)).first()
    if existing:
        return existing
    task = SharedTask.objects.filter(id=alert.ref_id).first() if alert.ref_type == "shared_task" and alert.ref_id else None
    title = task.title if task else (alert.body or alert.title)
    loud = bool(task and task.priority in (Priority.URGENT, Priority.HIGH))
    return Alert.objects.create(
        recipient=md, kind=Alert.Kind.INFO, priority=Priority.NORMAL if loud else Priority.LOW,
        title=f"{user.spoken_name} has seen it", body=title[:400],
        spoken_text=f"{md_address()}, {user.spoken_name} has seen: {title}."[:400],
        spoken_text_hi=f"{md_address_hi()}, {user.spoken_name} ने देख लिया: {title}।"[:400],
        ref_type="seen", ref_id=str(alert.id)[:64],
    )


LAST_VIEW_TTL = timedelta(minutes=15)


def remember_last_view(user: User, request: dict, summary: str, has_private: bool = False) -> None:
    """Keep the list JARVIS just answered with (its filters and what was said), so the next question can narrow
    or widen the SAME list ("sirf meetings", "aur Sir ke diye hue", "sab dikhao") even when the phone did not
    send the card back. Stored beside the voice conversation's short memory, with the same encryption.
    has_private: the sentence names one of Sir's personal items, so it is only ever repeated to an unlocked phone."""
    row = ConversationState.objects.filter(user=user).first()
    try:
        data = json.loads(row.state_json or "{}") if row else {}
    except ValueError:
        data = {}
    data["last_view"] = {"request": request, "summary": (summary or "")[:200], "private": bool(has_private),
                         "at": timezone.now().isoformat()}
    ConversationState.objects.update_or_create(user=user, defaults={"state_json": json.dumps(data, ensure_ascii=False, default=str)})


def last_view(user: User, include_private: bool = True) -> dict | None:
    """The list answered last (within a quarter of an hour), or None. On a locked phone (include_private=False)
    a sentence that named a personal item is left out; the filters alone are kept."""
    row = ConversationState.objects.filter(user=user).first()
    if not row:
        return None
    try:
        last = (json.loads(row.state_json or "{}") or {}).get("last_view")
    except ValueError:
        return None
    if not isinstance(last, dict) or not last.get("at"):
        return None
    try:
        if timezone.now() - datetime.fromisoformat(last["at"]) > LAST_VIEW_TTL:
            return None
    except (TypeError, ValueError):
        return None
    said = "" if (last.get("private") and not include_private) else (last.get("summary") or "")
    return {"filters": last.get("request") or {}, "jarvis_said": said}


def ai_context(user: User, include_private: bool = True) -> dict:
    """Facts the AI may use. Strictly scoped: a PA context contains no MD private data.

    include_private=False (phone locked, talking to the background listener): the MD's private items are
    left out as well, so nothing private is spoken to whoever is holding a locked phone.
    """
    now = tu.now_local()
    tp_sync.ensure_directory(user)
    staff = [{"id": s.external_id, "name": s.name, "department": s.department, "on_leave": s.on_leave}
             for s in StaffMember.objects.filter(active=True)[:80]]
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
    def keep(item):
        return include_private or item.get("scope") != "private"
    ctx["open_items"] = {
        "overdue": [brief(i) for i in dash["overdue"] if keep(i)][:8],
        "today": [brief(i) for i in dash["today"] if keep(i)][:12],
        "upcoming": [brief(i) for i in dash["upcoming"] if keep(i)][:8],
    }
    if not include_private:
        ctx["note"] = ("The phone is locked, so the MD's private items are not in this list. If asked about them, "
                       "say they will be available once the phone is unlocked.")
    ctx["delegated"] = [brief(i) for i in dash["delegated"][:10]]
    pa = get_pa()
    if user.is_md:
        ctx["pa_name"] = pa.spoken_name if pa else ""
        ctx["tasks_given_to_pa"] = [
            brief(i) | {"status_label": i["status_label"]}
            | ({"her_own_task": True} if i.get("origin") == SharedTask.Origin.PA_SELF else {})
            for i in dash.get("pa_tasks", [])[:10]]
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
    # People outside the staff list that the speaker has told JARVIS about (owner-only, like memories).
    ctx["contacts"] = [{"name": c.name, "who": c.note} for c in Contact.objects.filter(owner=user)[:60]]
    # How much is waiting, split the way the speaker thinks about it. Sir has official work and personal items;
    # personal ones exist only in his own context (and not at all on a locked phone).
    ctx["pending_summary"] = dict(pending_split(user, dash, include_private)["counts"])
    # How the MD is addressed and named: every "Sir" in the rules means this word.
    ctx["md_address"], ctx["md_address_hi"] = md_address(), md_address_hi()
    # Their genders, for "he / she" and the Hindi verb forms when JARVIS speaks ABOUT them (pa_gender "" = not set:
    # go by the name and keep neutral forms).
    ctx["md_gender"], ctx["pa_gender"] = md_gender(), pa_gender()
    md_user = get_md()
    ctx["md_name"] = md_user.spoken_name if md_user else ""
    if user.is_md:
        closed = (Status.COMPLETED, Status.CANCELLED)
        ctx["pending_summary"]["with_pa_open"] = len([
            t for t in dash.get("pa_tasks", []) if t["status"] not in closed and t.get("origin") != SharedTask.Origin.PA_SELF])
        ctx["pending_summary"]["with_staff_open"] = len([t for t in dash["delegated"] if t["status"] not in closed])
    # JARVIS settings the speaker may change by voice.
    current = get_prefs(user)
    def allowed(v):
        return {"enum": v.get("values"), "bool": "on/off", "time": "HH:MM (24h)",
                "days": "day names: sunday / sat,sun / weekdays / none",
                "blocks": "set with routineAdd / routineRemove",
                "text": v.get("hint", "short text")}.get(v["type"]) or f"{v['min']}-{v['max']}"
    ctx["settings"] = {k: {"now": current.get(k, ""), "allowed": allowed(v), "means": v["say"]}
                       for k, v in PREF_SCHEMA.items() if not v.get("hidden") and not (v.get("md_only") and not user.is_md)}
    ctx["settings"]["myName"]["now"] = user.spoken_name
    if user.is_md:
        ctx["settings"]["paName"]["now"] = pa.spoken_name if pa else ""
    # How this person works, as JARVIS has learned it (the profile block): the model shapes its replies with it.
    ctx["habits"] = {"snooze_minutes": current["snoozeMinutes"], "meeting_minutes": current["meetingMinutes"],
                     "favourite_lists": favourite_views(current)}
    ctx["settings"]["quietHours"] = {"now": f"{current['quietStartHour']}-{current['quietEndHour']}", "allowed": "START-END in 24h",
                                     "means": "quiet hours start and end together"}
    ctx["settings"]["officeHours"] = {"now": f"{current['officeStart']}-{current['officeEnd']}", "allowed": "START-END in 24h, e.g. 10-19",
                                      "means": "office hours start and end together"}
    ctx["settings"]["routineAdd"] = {"now": "", "allowed": "Label HH:MM-HH:MM [daily | weekdays | mon,wed | mon-sat]",
                                     "means": "add a fixed block to the routine (gym, lunch, market) that JARVIS keeps free"}
    ctx["settings"]["routineRemove"] = {"now": "", "allowed": "the block's label", "means": "remove a fixed block from the routine"}
    # The routine in plain words, so "office kab tak hai" / "Sunday off hai?" can be answered.
    from . import scheduling  # local import: scheduling reads prefs from here
    ctx["routine"] = scheduling.routine_summary(current)
    return ctx


# ------------------------------------------------------------------ settings (shared by app + background listener)
PREF_SCHEMA = {
    "listenLang": {"type": "enum", "values": ["auto", "hi-IN"], "default": "auto", "say": "listening language",
                   "aliases": {"auto": "auto", "english": "auto", "hinglish": "auto", "both": "auto", "mixed": "auto",
                               "hindi": "hi-IN", "hi": "hi-IN", "hi-in": "hi-IN"},
                   "labels": {"auto": "Hindi and English together", "hi-IN": "pure Hindi"}},
    # How JARVIS addresses the MD (and how the PA's JARVIS refers to the MD): Sir, Ma'am, Madam, or "name".
    "addressAs": {"type": "text", "default": "Sir", "say": "how JARVIS addresses you (Sir, Ma'am, Madam or your name)",
                  "hint": "Sir / Ma'am / Madam / name (= your own name)"},
    # JARVIS's voice (the Grok voice it speaks with). Male voices only; Naksh is the Indian one.
    "voiceName": {"type": "enum", "values": list(VOICE_CHOICES), "default": DEFAULT_VOICE, "say": "JARVIS's voice",
                  "labels": {v: v.title() for v in VOICE_CHOICES}},
    # Whether the PA is a man or a woman (set by the MD): how JARVIS refers to the PA (he / she, and the Hindi forms).
    "paGender": {"type": "enum", "values": ["unset", "male", "female"], "default": "female", "say": "the PA's gender",
                 "aliases": {"man": "male", "male": "male", "m": "male", "ladka": "male", "purush": "male",
                             "woman": "female", "female": "female", "f": "female", "ladki": "female", "mahila": "female"},
                 "labels": {"unset": "not set", "male": "a man", "female": "a woman"}, "md_only": True},
    # The language JARVIS replies in: auto = the language of what was just said (Hindi or English), or pinned.
    "replyLanguage": {"type": "enum", "values": ["auto", "hi", "en"], "default": "auto", "say": "reply language",
                      "aliases": {"auto": "auto", "match": "auto", "same": "auto", "mixed": "auto", "hinglish": "auto",
                                  "hindi": "hi", "hi": "hi", "english": "en", "en": "en"},
                      "labels": {"auto": "the language you speak", "hi": "Hindi", "en": "English"}},
    # The languages of the last few sentences spoken (kept by core.lang; never set by hand).
    "langRecent": {"type": "text", "default": "", "say": "recent languages", "hidden": True},
    "aiSlowToldAt": {"type": "text", "default": "", "say": "last told the AI is slow", "hidden": True},
    "voiceAccent": {"type": "enum", "values": ["hi-IN", "en-IN"], "default": "hi-IN", "say": "voice",
                    "aliases": {"hindi": "hi-IN", "hi": "hi-IN", "hi-in": "hi-IN", "english": "en-IN", "en": "en-IN",
                                "en-in": "en-IN", "indian english": "en-IN"},
                    "labels": {"hi-IN": "Hindi voice", "en-IN": "Indian English voice"}},
    "speakPrivateDetails": {"type": "bool", "default": True, "say": "speaking private reminders aloud"},
    "privateAloudWhenLocked": {"type": "bool", "default": False, "say": "private reminders aloud when the phone is locked"},
    "awayAfterMinutes": {"type": "int", "min": 1, "max": 120, "default": 10, "say": "away time (minutes)"},
    "quietHoursEnabled": {"type": "bool", "default": True, "say": "quiet hours"},
    "quietStartHour": {"type": "int", "min": 0, "max": 23, "default": 22, "say": "quiet hours start"},
    "quietEndHour": {"type": "int", "min": 0, "max": 23, "default": 7, "say": "quiet hours end"},
    "wakeWordEnabled": {"type": "bool", "default": False, "say": "Hey JARVIS listening"},
    "meetingMode": {"type": "bool", "default": False, "say": "meeting mode"},
    # Set by the server when meeting mode is switched OFF during a diary meeting: that meeting no longer counts
    # as "in a meeting" (until this time). Kept here so it survives the app being closed and reopened.
    "meetingOverrideUntil": {"type": "text", "default": "", "say": "diary meeting override", "hidden": True},
    # Work routine: used to find free slots, spot clashes and judge how full a day is.
    "officeStart": {"type": "time", "default": "10:00", "say": "office start"},
    "officeEnd": {"type": "time", "default": "19:00", "say": "office end"},
    "weeklyOff": {"type": "days", "default": [6], "say": "weekly off"},
    "routineBlocks": {"type": "blocks", "default": [], "say": "fixed routine"},
    # Reminders: how early JARVIS speaks, by kind, when the task itself did not say.
    "remindBeforeMeeting": {"type": "int", "min": 0, "max": 240, "default": 15, "say": "meeting reminder (minutes before)"},
    "remindBeforeCall": {"type": "int", "min": 0, "max": 240, "default": 5, "say": "call reminder (minutes before)"},
    "remindBeforeTask": {"type": "int", "min": 0, "max": 1440, "default": 30, "say": "task reminder (minutes before)"},
    "dayReminderTime": {"type": "time", "default": "09:30", "say": "time for reminders of date-only items"},
    # "Later" on a reminder: how long, when no time is said. JARVIS learns it from what the person says by voice.
    "snoozeMinutes": {"type": "int", "min": 1, "max": 1440, "default": 15, "say": "snooze (minutes)"},
    # The person's usual meeting length when none is said (learned from the lengths they do say).
    "meetingMinutes": {"type": "int", "min": 5, "max": 480, "default": 60, "say": "usual meeting length (minutes)"},
    "meetingMinutesRecent": {"type": "text", "default": "", "say": "recent meeting lengths", "hidden": True},
    # The lists this person asks for most (learned), for the home screen's shortcuts.
    "viewsRecent": {"type": "text", "default": "", "say": "recent lists", "hidden": True},
    # The rules JARVIS applies, as settings rather than constants: when a day is "full", how long after a timed
    # task's time the check-in comes, how many minutes an urgent alert repeats after, when the evening summary is,
    # how many days of staff silence or hours of a waiting request earn a nudge.
    "heavyDayItems": {"type": "int", "min": 3, "max": 20, "default": 7, "say": "items that make a day full"},
    "checkinAfterMinutes": {"type": "int", "min": 10, "max": 240, "default": 45, "say": "check-in after a due time (minutes)"},
    "urgentRepeatMinutes": {"type": "int", "min": 2, "max": 60, "default": 5, "say": "urgent alert repeats after (minutes)"},
    "digestTime": {"type": "time", "default": "18:30", "say": "evening summary time"},
    "staleStaffDays": {"type": "int", "min": 1, "max": 14, "default": 2, "say": "days of staff silence before a nudge"},
    "requestWaitHours": {"type": "int", "min": 1, "max": 72, "default": 4, "say": "hours a request waits before a nudge"},
    # Names, set by voice from either phone. Not stored as settings: they go to the person (see set_prefs).
    "myName": {"type": "text", "default": "", "say": "your name", "hint": "the name JARVIS calls you by", "virtual": True},
    "paName": {"type": "text", "default": "", "say": "the PA's name", "hint": "the PA's name (MD only)", "virtual": True, "md_only": True},
    # Talking over JARVIS while it speaks stops it and it listens (like a person). Off if the room echoes.
    "bargeIn": {"type": "bool", "default": True, "say": "interrupt JARVIS by talking over it"},
    # The notification sound for reminders that arrive while the app is closed.
    "reminderSound": {"type": "enum", "values": ["default", "silent"], "default": "default", "say": "reminder sound",
                      "aliases": {"phone": "default", "normal": "default", "on": "default", "sound": "default",
                                  "silent": "silent", "off": "silent", "vibrate": "silent", "quiet": "silent"},
                      "labels": {"default": "reminder sound on", "silent": "reminders silent (vibrate only)"}},
    # Morning briefing, raised by the server and spoken when he picks up the phone.
    "briefingEnabled": {"type": "bool", "default": True, "say": "morning briefing"},
    "briefingTime": {"type": "time", "default": "09:00", "say": "morning briefing time"},
}
_TRUE_WORDS = {"on", "true", "yes", "1", "haan", "han", "ha", "enable", "enabled", "chalu", "chaalu", "start"}
_FALSE_WORDS = {"off", "false", "no", "0", "nahi", "nahin", "disable", "disabled", "band", "stop"}
_DAY_WORDS = {
    0: {"mon", "monday", "somvaar", "somvar", "somwar", "सोमवार"},
    1: {"tue", "tues", "tuesday", "mangalvaar", "mangalvar", "mangalwar", "मंगलवार"},
    2: {"wed", "wednesday", "budhvaar", "budhvar", "budhwar", "बुधवार"},
    3: {"thu", "thur", "thurs", "thursday", "guruvaar", "guruvar", "guruwar", "गुरुवार"},
    4: {"fri", "friday", "shukravaar", "shukravar", "shukrawar", "शुक्रवार"},
    5: {"sat", "saturday", "shanivaar", "shanivar", "shaniwar", "शनिवार"},
    6: {"sun", "sunday", "ravivaar", "ravivar", "raviwar", "itvaar", "itwar", "रविवार", "इतवार"},
}
_NO_DAYS = {"none", "no", "nahi", "nahin", "koi nahi", "off", "nothing", "0", "-", ""}
_ALL_DAYS = {"daily", "roz", "everyday", "every day", "all", "sab", "har din", "hardin"}
_WEEKDAYS_WORDS = {"weekdays", "weekday", "mon-fri", "monday to friday"}
_TIME_TOKEN = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|baje|बजे)?", re.IGNORECASE)


def _parse_time_text(raw) -> str:
    """'9', '09:00', '9.30', '7 pm', '19:30' -> 'HH:MM'. Raises ServiceError when it is not a time."""
    if isinstance(raw, time):
        return raw.strftime("%H:%M")
    s = str(raw or "").strip().lower()
    m = _TIME_TOKEN.search(s)
    if not m:
        raise ServiceError("invalid", "I need a time like 9:30 or 18:00.", 422)
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "")
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ServiceError("invalid", "I need a time like 9:30 or 18:00.", 422)
    return f"{hour:02d}:{minute:02d}"


def _parse_days(raw) -> list[int]:
    """Weekday numbers (Monday = 0) from a list or words such as 'sunday', 'sat,sun', 'weekdays', 'none'."""
    if isinstance(raw, (list, tuple)):
        out = set()
        for item in raw:
            if isinstance(item, bool):
                raise ServiceError("invalid", "Days must be names or numbers.", 422)
            if isinstance(item, int) and 0 <= item <= 6:
                out.add(item)
            else:
                out.update(_parse_days(str(item)))
        return sorted(out)
    s = " ".join(str(raw or "").lower().replace("&", " and ").split())
    if s in _NO_DAYS:
        return []
    if s in _ALL_DAYS:
        return list(range(7))
    if s in _WEEKDAYS_WORDS:
        return [0, 1, 2, 3, 4]
    out = set()
    # "mon-sat" style ranges
    for a, b in re.findall(r"([a-zऀ-ॿ]+)\s*(?:-|to|se|से)\s*([a-zऀ-ॿ]+)", s):
        da, db = _day_index(a), _day_index(b)
        if da is not None and db is not None:
            i = da
            while True:
                out.add(i)
                if i == db:
                    break
                i = (i + 1) % 7
            s = s.replace(f"{a}", " ").replace(f"{b}", " ")
    for word in re.split(r"[,\s/]+|\band\b|\baur\b|\bऔर\b", s):
        word = word.strip(" .")
        if not word or word in {"and", "aur", "to", "se", "-", "off", "ko", "chhutti", "holiday"}:
            continue
        idx = _day_index(word)
        if idx is None:
            raise ServiceError("invalid", f"I don't know the day '{word}'. Say day names like Sunday or Saturday.", 422)
        out.add(idx)
    return sorted(out)


def _day_index(word: str) -> int | None:
    w = word.strip().lower()
    for idx, names in _DAY_WORDS.items():
        if w in names:
            return idx
    return None


def _parse_block(raw) -> dict:
    """'Gym 07:00-08:00', 'gym 7 to 8 daily', 'Lunch 13:00-13:45 weekdays' or a dict -> a routine block."""
    if isinstance(raw, dict):
        label = str(raw.get("label") or "").strip()[:40]
        start, end = _parse_time_text(raw.get("start")), _parse_time_text(raw.get("end"))
        days = _parse_days(raw.get("days")) if raw.get("days") not in (None, "") else list(range(7))
    else:
        s = str(raw or "").strip()
        times = list(_TIME_TOKEN.finditer(s))
        if len(times) < 2:
            raise ServiceError("invalid", "A routine block needs a name, a start and an end, like 'Gym 7:00-8:00'.", 422)
        label = s[:times[0].start()].strip(" :-,") or "Routine"
        label = re.sub(r"\b(from|se|at)\s*$", "", label, flags=re.IGNORECASE).strip()[:40]
        start, end = _parse_time_text(times[0].group(0)), _parse_time_text(times[1].group(0))
        tail = s[times[1].end():].strip(" ,.-")
        tail = re.sub(r"^(on|ko|every|har)\s+", "", tail, flags=re.IGNORECASE)
        days = _parse_days(tail) if tail else list(range(7))
    if not label:
        raise ServiceError("invalid", "What is this routine block called (gym, lunch, market)?", 422)
    if not days:
        days = list(range(7))
    return {"label": label, "start": start, "end": end, "days": days}


def pref_defaults() -> dict:
    return {k: v["default"] for k, v in PREF_SCHEMA.items()}


def get_prefs(user: User) -> dict:
    row = UserPrefs.objects.filter(user=user).first()
    stored = (row.data if row else None) or {}
    real = {k: v for k, v in PREF_SCHEMA.items() if not v.get("virtual")}  # names are not settings (see set_names)
    return {**{k: v["default"] for k, v in real.items()}, **{k: v for k, v in stored.items() if k in real}}


# --------------------------------------------------------------------------- what JARVIS has learned
def _recent_ints(prefs: dict, key: str) -> list[int]:
    out = []
    for x in str(prefs.get(key) or "").split(","):
        try:
            out.append(int(x))
        except ValueError:
            continue
    return out


def learn_meeting_minutes(user: User, minutes: int) -> None:
    """A meeting length the person said: after a few, their usual length is the middle of the recent ones."""
    if not minutes or minutes <= 0:
        return
    prefs = get_prefs(user)
    recent = (_recent_ints(prefs, "meetingMinutesRecent") + [int(minutes)])[-7:]
    set_pref_quiet(user, "meetingMinutesRecent", ",".join(str(m) for m in recent))
    if len(recent) >= 3:
        usual = sorted(recent)[len(recent) // 2]
        if usual != prefs.get("meetingMinutes"):
            set_pref_quiet(user, "meetingMinutes", max(5, min(usual, 480)))


def learn_view(user: User, kind: str) -> None:
    """Which lists the person asks for: the last 30 are kept, so the home screen can offer the usual ones first."""
    if not kind:
        return
    prefs = get_prefs(user)
    recent = ([k for k in str(prefs.get("viewsRecent") or "").split(",") if k] + [kind])[-30:]
    set_pref_quiet(user, "viewsRecent", ",".join(recent))


def favourite_views(prefs: dict, limit: int = 4) -> list[str]:
    recent = [k for k in str(prefs.get("viewsRecent") or "").split(",") if k]
    counts: dict[str, int] = {}
    for k in recent:
        counts[k] = counts.get(k, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], recent[::-1].index(kv[0])))][:limit]


HABIT_KEYS = ("snoozeMinutes", "meetingMinutes", "langRecent", "viewsRecent", "heavyDayItems", "checkinAfterMinutes",
              "urgentRepeatMinutes", "digestTime", "staleStaffDays", "requestWaitHours")


def habits(user: User) -> list[dict]:
    """What JARVIS has learned or been told about how this person works, in plain words, each one deletable."""
    from . import lang as _lang
    prefs = get_prefs(user)
    row = UserPrefs.objects.filter(user=user).first()
    stored = (row.data if row else None) or {}
    out = []

    def add(key, label, value, learned):
        out.append({"key": key, "label": label, "value": value, "learned": learned, "set": key in stored})

    add("snoozeMinutes", "“Later” means", f"{prefs['snoozeMinutes']} minutes", "from what you say (“10 minute baad”)")
    add("meetingMinutes", "Usual meeting length", f"{prefs['meetingMinutes']} minutes", "from the lengths you mention")
    add("langRecent", "Language lately", {"hi": "Hindi", "en": "English"}[_lang.current(user)], "from how you speak")
    favs = favourite_views(prefs)
    add("viewsRecent", "Lists you ask for most", ", ".join(favs) if favs else "none yet", "from what you ask")
    add("heavyDayItems", "A day is full at", f"{prefs['heavyDayItems']} items", "a rule you can change")
    add("checkinAfterMinutes", "Check-in after a due time", f"{prefs['checkinAfterMinutes']} minutes", "a rule you can change")
    add("urgentRepeatMinutes", "Urgent alerts repeat after", f"{prefs['urgentRepeatMinutes']} minutes", "a rule you can change")
    add("digestTime", "Evening summary at", prefs["digestTime"], "a rule you can change")
    add("staleStaffDays", "Staff silence before a nudge", f"{prefs['staleStaffDays']} days", "a rule you can change")
    add("requestWaitHours", "A request waits before a nudge", f"{prefs['requestWaitHours']} hours", "a rule you can change")
    return out


def forget_habit(user: User, key: str) -> None:
    """Back to the default (or to nothing learned) for one habit."""
    if key not in HABIT_KEYS:
        raise ServiceError("invalid", "There is no such habit.", 422)
    row = UserPrefs.objects.filter(user=user).first()
    if not row:
        return
    data = dict(row.data or {})
    for k in (key, "meetingMinutesRecent" if key == "meetingMinutes" else key):
        data.pop(k, None)
    row.data = data
    row.save(update_fields=["data"])
    audit(user, "habit.forget", "user", user.id, key)


def coerce_pref(key: str, value) -> tuple[str, object]:
    """Validate one setting from the API or from the AI ("Hindi", "on", "5"). Returns (key, clean value)."""
    spec = PREF_SCHEMA.get(key)
    if not spec:
        raise ServiceError("invalid", f"There is no setting called {key}.", 422)
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return key, value
        s = str(value).strip().lower()
        if s in _TRUE_WORDS:
            return key, True
        if s in _FALSE_WORDS:
            return key, False
        raise ServiceError("invalid", f"Say on or off for {spec['say']}.", 422)
    if t == "int":
        try:
            n = int(re.sub(r"[^\d-]", "", str(value).strip()) or "x") if not isinstance(value, int) else value
        except ValueError:
            raise ServiceError("invalid", f"{spec['say']} needs a number.", 422)
        if not spec["min"] <= n <= spec["max"]:
            raise ServiceError("invalid", f"{spec['say']} must be between {spec['min']} and {spec['max']}.", 422)
        return key, n
    if t == "time":
        return key, _parse_time_text(value)
    if t == "text":
        return key, str(value or "")[:64]
    if t == "days":
        return key, _parse_days(value)
    if t == "blocks":
        if not isinstance(value, (list, tuple)):
            raise ServiceError("invalid", "The routine must be a list of blocks.", 422)
        blocks = [_parse_block(b) for b in value][:12]
        return key, blocks
    s = str(value).strip()
    s = spec.get("aliases", {}).get(s.lower(), s)
    if s not in spec["values"]:
        raise ServiceError("invalid", f"{spec['say']} can be: " + ", ".join(spec["values"]) + ".", 422)
    return key, s


def _say_time(hhmm: str) -> str:
    try:
        return tu.fmt_time(tu.combine_local(tu.today_local(), time.fromisoformat(hhmm)))
    except ValueError:
        return hhmm


def describe_pref(key: str, value) -> str:
    spec = PREF_SCHEMA[key]
    if spec["type"] == "bool":
        return f"{spec['say']} {'on' if value else 'off'}"
    if spec["type"] == "int":
        return f"{spec['say']} {value}"
    if spec["type"] == "time":
        return f"{spec['say']} {_say_time(value)}"
    if spec["type"] == "text":
        return f"{spec['say']} {value}" if value else f"{spec['say']} cleared"
    if spec["type"] == "days":
        from .scheduling import WEEKDAY_NAMES
        return f"{spec['say']} " + (", ".join(WEEKDAY_NAMES[d] for d in value) if value else "none")
    if spec["type"] == "blocks":
        if not value:
            return "no fixed routine"
        return spec["say"] + ": " + "; ".join(f"{b['label']} {_say_time(b['start'])} to {_say_time(b['end'])}" for b in value)
    return spec.get("labels", {}).get(value, f"{spec['say']} {value}")


def _two_times(raw: str, what: str) -> tuple[str, str]:
    tokens = list(_TIME_TOKEN.finditer(str(raw)))
    if len(tokens) < 2:
        raise ServiceError("invalid", f"{what} need a start and an end, like 10 to 19.", 422)
    return _parse_time_text(tokens[0].group(0)), _parse_time_text(tokens[1].group(0))


def expand_pref_patch(patch: dict, current: dict | None = None) -> dict:
    """Spoken shorthand -> real settings.

    quietHours '22-7' -> start, end, enabled; officeHours '10-19' (or '10 to 7') -> officeStart, officeEnd;
    routineAdd 'Gym 07:00-08:00 daily' / routineRemove 'gym' -> the full routineBlocks list (needs ``current``).
    """
    patch = dict(patch)
    if "quietHours" in patch:
        raw = str(patch.pop("quietHours"))
        nums = [int(n) for n in re.findall(r"\d{1,2}", raw)]
        if len(nums) != 2:
            raise ServiceError("invalid", "Quiet hours need a start and an end hour, like 22 to 7.", 422)
        patch.update({"quietStartHour": nums[0], "quietEndHour": nums[1], "quietHoursEnabled": True})
    if "officeHours" in patch:
        start, end = _two_times(patch.pop("officeHours"), "Office hours")
        sh, eh = int(start[:2]), int(end[:2])
        if eh <= sh and eh < 12:  # "10 to 7" means 10:00 to 19:00
            end = f"{eh + 12:02d}{end[2:]}"
        patch.update({"officeStart": start, "officeEnd": end})
    if "routineAdd" in patch or "routineRemove" in patch:
        blocks = list((current or pref_defaults()).get("routineBlocks") or [])
        if "routineRemove" in patch:
            label = str(patch.pop("routineRemove") or "").strip().lower()
            kept = [b for b in blocks if label and label not in b["label"].lower()]
            if len(kept) == len(blocks):
                raise ServiceError("invalid", f"There is no routine block called '{label}'.", 422)
            blocks = kept
        if "routineAdd" in patch:
            block = _parse_block(patch.pop("routineAdd"))
            blocks = [b for b in blocks if b["label"].lower() != block["label"].lower()] + [block]
        patch["routineBlocks"] = blocks
    return patch


def current_busy_until(user: User, now: datetime | None = None) -> datetime | None:
    """If a diary meeting of this person is running right now, when it ends (the latest end among overlaps)."""
    if not user.is_md:
        return None
    now = now or timezone.now()
    ends = [datetime.fromisoformat(w["end"]) for w in busy_windows(now.astimezone(tu.IST).date())
            if datetime.fromisoformat(w["start"]) <= now < datetime.fromisoformat(w["end"])]
    return max(ends) if ends else None


def meeting_override_until(prefs: dict) -> datetime | None:
    raw = (prefs or {}).get("meetingOverrideUntil") or ""
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def set_pref_quiet(user: User, key: str, value) -> None:
    """One setting JARVIS keeps for itself (what it has noticed), stored without an audit line."""
    if key not in PREF_SCHEMA:
        return
    row, _ = UserPrefs.objects.get_or_create(user=user)
    row.data = {**(row.data or {}), key: value}
    row.save(update_fields=["data"])


def set_names(user: User, names: dict) -> list[str]:
    """Names by voice, from either phone: the speaker's own (myName) and, from the MD, the PA's (paName). The other
    person is told in one short note, and every phone re-reads the names from the server. Returns what changed."""
    changed = []
    pa, md = get_pa(), get_md()
    for key, raw in names.items():
        name = " ".join(str(raw or "").split())[:40]
        if not name:
            raise ServiceError("invalid", "Say the name.", 422)
        if key == "myName":
            target = user
        elif key == "paName":
            if not user.is_md:
                raise ServiceError("forbidden", f"Only {md_address()} can set the PA's name.", 403)
            if pa is None:
                raise ServiceError("invalid", "There is no PA account yet.", 422)
            target = pa
        else:
            continue
        if target.spoken_name == name:
            continue
        old = target.spoken_name
        target.display_name = name
        if target.is_pa:
            target.first_name = name
        target.save(update_fields=["display_name", "first_name"])
        audit(user, "name.set", "user", target.id, f"{old} -> {name}")
        changed.append(name)
        other = pa if target.is_md else md
        if other and other.id != user.id:
            if target.is_md:
                en, hi = f"{other.spoken_name}, {md_address()} has asked me to use the name {name} from now.", \
                         f"{other.spoken_name}, {md_address_hi()} ने कहा है कि अब से {name} नाम इस्तेमाल करूँ।"
            else:
                en, hi = f"{address_of(other)}, {old} will be called {name} from now.", f"{address_hi_of(other)}, {old} को अब से {name} कहा जाएगा।"
            Alert.objects.create(recipient=other, kind=Alert.Kind.INFO, priority=Priority.LOW, title="A name has changed",
                                 body=en, spoken_text=en, spoken_text_hi=hi, ref_type="profile", ref_id=str(target.id))
        elif other and target.is_pa:
            en, hi = f"{name}, {md_address()} has set your name to {name}.", f"{name}, {md_address_hi()} ने आपका नाम {name} रखा है।"
            Alert.objects.create(recipient=target, kind=Alert.Kind.INFO, priority=Priority.NORMAL, title="Your name",
                                 body=en, spoken_text=en, spoken_text_hi=hi, ref_type="profile", ref_id=str(target.id))
    return changed


def set_prefs(user: User, patch: dict) -> dict:
    expanded = expand_pref_patch(patch or {}, get_prefs(user))
    # Names are not settings: they go to the person (and the other one is told).
    names = {k: expanded.pop(k) for k in list(expanded) if PREF_SCHEMA.get(k, {}).get("virtual")}
    if names:
        set_names(user, names)
    clean = dict(coerce_pref(k, v) for k, v in expanded.items())
    if not clean and not names:
        raise ServiceError("invalid", "Nothing to change.", 422)
    if not clean:
        return get_prefs(user)
    # Meeting mode switched OFF while the diary says he is in a meeting: he is overriding the diary, and the
    # override must outlive the app (it is applied in reminders_for, which the phone reads every few seconds).
    if clean.get("meetingMode") is False:
        until = current_busy_until(user)
        clean["meetingOverrideUntil"] = until.isoformat() if until else ""
    elif clean.get("meetingMode") is True:
        clean["meetingOverrideUntil"] = ""
    row, _ = UserPrefs.objects.get_or_create(user=user)
    row.data = {**(row.data or {}), **clean}
    row.save()
    audit(user, "prefs.update", "user", user.id, "keys: " + ",".join(sorted(clean)))
    return get_prefs(user)


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


# ------------------------------------------------------------------ people JARVIS knows
CONTACT_LIMIT = 200
_HONORIFICS = {"ji", "jee", "sir", "madam", "maam", "mam", "sahab", "saab", "sahib", "bhai", "bhaiya", "didi",
               "mr", "mrs", "ms", "dr", "shri", "smt", "miss", "the"}


def norm_person(name: str) -> str:
    """'Sharma ji' -> 'sharma'; 'Mr. Anil Verma' -> 'anil verma'."""
    tokens = [t for t in re.findall(r"[^\W\d_]+", (name or "").lower()) if t not in _HONORIFICS]
    return " ".join(tokens)


def contact_item(c: Contact) -> dict:
    return {"id": c.id, "name": c.name, "note": c.note, "created_at": c.created_at.isoformat()}


def list_contacts(user: User) -> list[dict]:
    return [contact_item(c) for c in Contact.objects.filter(owner=user)[:CONTACT_LIMIT]]


def add_contact(user: User, name: str, note: str = "") -> dict:
    """Remember an outside person for this user. The same name again only updates the note."""
    name = " ".join((name or "").split())[:60]
    note = " ".join((note or "").split())[:120]
    key = norm_person(name)
    if not key:
        raise ServiceError("invalid", "Tell me the person's name.", 422)
    if name == name.lower():
        name = name.title()
    for c in Contact.objects.filter(owner=user)[:CONTACT_LIMIT]:
        if norm_person(c.name) == key:
            if note and note != c.note:
                c.note = note
                c.save(update_fields=["note"])
            return contact_item(c)
    if Contact.objects.filter(owner=user).count() >= CONTACT_LIMIT:
        raise ServiceError("contacts_full", "My contact list is full. Please remove someone in Settings first.", 409)
    c = Contact.objects.create(owner=user, name=name, note=note)
    audit(user, "contact.add", "contact", c.id)
    return contact_item(c)


def delete_contact(user: User, contact_id) -> None:
    c = Contact.objects.get(id=contact_id, owner=user)  # someone else's contact -> not found
    c.delete()
    audit(user, "contact.delete", "contact", contact_id)


def resolve_person(user: User, name: str) -> dict:
    """Does JARVIS know this person, for this user?

    Checked in order: Sir / the PA, the staff directory, the user's saved contacts, then names already in the
    user's own tasks. Returns {"status": "known" | "ambiguous" | "unknown", "who", "options", "suggestions"}.
    The MD's private titles are only ever read for the MD himself.
    """
    key = norm_person(name)
    known = {"status": "known", "who": "", "options": [], "suggestions": []}
    if not key or key in {"md", "boss", "managing director", "me", "myself", "main", "mujhe", "mere"}:
        return known
    pa = get_pa()
    if pa:
        pa_names = {norm_person(n) for n in (pa.display_name, pa.first_name, pa.username, pa.get_full_name()) if n}
        if key in pa_names or key in {"pa", "assistant", "personal assistant"}:
            return {**known, "who": "PA"}
    matches, partial = resolve_staff(key)
    if len(matches) == 1:
        return {**known, "who": f"{matches[0].name} ({matches[0].department})"}
    if len(matches) > 1:
        return {"status": "ambiguous", "who": "", "suggestions": [],
                "options": [f"{m.name} from {m.department}" for m in matches[:3]]}
    contacts = list(Contact.objects.filter(owner=user)[:CONTACT_LIMIT])
    for c in contacts:
        cn = norm_person(c.name)
        if cn and (cn == key or key in cn.split() or cn in key.split() or cn in key):
            return {**known, "who": f"{c.name}{' – ' + c.note if c.note else ''}"}
    # Already in his own diary ("Meeting with Sharma ji" exists): he clearly knows who that is.
    dash = dashboard(user)
    titles = [i["title"].lower() for bucket in ("overdue", "today", "upcoming", "anytime") for i in dash[bucket]]
    titles += [i["title"].lower() for i in dash.get("pa_tasks", []) + dash.get("md_tasks", []) + dash["delegated"]]
    main = max(key.split(), key=len)
    if len(main) >= 3 and any(re.search(rf"\b{re.escape(main)}\b", t) for t in titles):
        return {**known, "who": "seen in your tasks"}
    # Unknown: offer the nearest names we do know.
    pool: dict[str, str] = {}
    for s in StaffMember.objects.filter(active=True)[:200]:
        label = f"{s.name} from {s.department}" if s.department else s.name
        pool[norm_person(s.name)] = label
        first = norm_person(s.name).split()[:1]
        if first:
            pool.setdefault(first[0], label)
    for c in contacts:
        pool[norm_person(c.name)] = c.name
    close = difflib.get_close_matches(key, list(pool), n=2, cutoff=0.72)
    suggestions = []
    for k in close:
        if pool[k] not in suggestions:
            suggestions.append(pool[k])
    for s in partial[:2]:
        label = f"{s.name} from {s.department}" if s.department else s.name
        if label not in suggestions:
            suggestions.append(label)
    return {"status": "unknown", "who": "", "options": [], "suggestions": suggestions[:2]}


def briefing_facts(user: User, kind: str, conceal_private: bool = False) -> tuple[dict, str]:
    """Facts for a spoken briefing plus a plain fallback text.

    conceal_private=True (the stored morning briefing): the MD's private items are counted and timed but never
    named, because an Alert row is not encrypted and may be spoken from a locked phone.
    """
    from . import scheduling  # local import: scheduling imports this module

    dash = dashboard(user)
    prefs = get_prefs(user)
    today = tu.today_local()

    def name(item):
        return "a personal item" if conceal_private and item.get("scope") == "private" else item["title"]

    facts = {
        "reader": "MD" if user.is_md else f"PA ({user.spoken_name})",
        "kind": kind,
        "date": dash["date_label"],
        "now": tu.now_local().strftime("%I:%M %p"),
        "overdue": [{"title": name(i), "priority": i["priority"], "was_due": i["due_label"]} for i in dash["overdue"]],
        "today": [{"title": name(i), "priority": i["priority"], "kind": i["kind"], "when": i["due_label"],
                   "minutes": i["estimated_minutes"]} for i in dash["today"]],
        "upcoming": [{"title": name(i), "when": i["due_label"]} for i in dash["upcoming"][:8]],
        "completed_today": dash["completed_today"],
        "pending_requests": [{"title": r["title"], "from": r["requested_by"], "priority": r["priority"]}
                             for r in dash["requests"] if r["state"] == "PENDING"],
        "delegated_open": [{"title": d["title"], "staff": d["assignee"]["staff_name"], "status": d["status"],
                            "overdue": d["overdue"]} for d in dash["delegated"] if d["status"] != Status.COMPLETED][:12],
        "routine": scheduling.routine_summary(prefs),
        "weekly_off_today": scheduling.is_off_day(prefs, today),
    }
    if conceal_private:
        facts["private_items_today"] = sum(1 for i in dash["today"] if i.get("scope") == "private")

    # Where today collides with itself, and whether it is simply too full.
    def span_name(c):
        return "a personal item" if conceal_private and c.get("private") else c["title"]
    pairs = scheduling.overlapping_pairs(user, today, prefs)
    facts["clashes_today"] = [
        {"a": span_name(a), "b": span_name(b), "at": tu.fmt_time(max(a["start"], b["start"]))} for a, b in pairs[:4]
    ]
    load = scheduling.day_load(user, today, prefs)
    facts["day_load"] = {"items": load["count"], "busy_minutes": load["minutes"], "free_capacity_minutes": load["capacity"],
                         "heavy": load["heavy"]}
    if load["heavy"] and load["movable"]:
        m = load["movable"]
        facts["day_load"]["could_move"] = {"title": span_name(m), "to": tu.fmt_day(today + timedelta(days=1))}
        # The item itself (never sent to the model: it is what a "yes" to the offer moves).
        facts["day_load"]["could_move_item"] = {"id": m.get("id"), "scope": "private" if m.get("private") else "shared",
                                                "title": span_name(m), "version": m.get("version", 1)}

    from . import lang as _lang
    address = address_of(user)
    facts["address"] = address  # how the reader is addressed (the briefing writer uses exactly this word)
    facts["language"] = "Hindi (Devanagari script)" if _lang.current(user) == "hi" else "Indian English"
    hour = tu.now_local().hour
    greeting = {"morning": "Good morning", "evening": "Good evening"}.get(
        kind, "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening")
    parts = [f"{greeting}, {address}."]
    if facts["overdue"]:
        parts.append(f"{len(facts['overdue'])} item{'s are' if len(facts['overdue']) > 1 else ' is'} overdue, starting with {facts['overdue'][0]['title']}.")
    if facts["today"]:
        firsts = ", ".join(f"{t['title']} {t['when']}" for t in facts["today"][:3])
        parts.append(f"Today you have {len(facts['today'])} item{'s' if len(facts['today']) > 1 else ''}: {firsts}.")
    else:
        parts.append("Nothing else is scheduled for today.")
    for c in facts["clashes_today"][:2]:
        parts.append(f"{c['a']} and {c['b']} overlap at {c['at']}; tell me which one to move.")
    if load["heavy"]:
        hint = f" You could move {facts['day_load']['could_move']['title']} to {facts['day_load']['could_move']['to']}." \
            if facts["day_load"].get("could_move") else ""
        parts.append(f"Today is heavy with {load['count']} items.{hint}")
    if facts["pending_requests"]:
        parts.append(f"{len(facts['pending_requests'])} request{'s are' if len(facts['pending_requests']) > 1 else ' is'} waiting for your decision.")
    late = [d for d in facts["delegated_open"] if d["overdue"]]
    if late:
        parts.append(f"{late[0]['staff']} is late on {late[0]['title']}.")
    return facts, " ".join(parts)
