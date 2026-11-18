"""Business rules. Every write goes through here so permissions and audit are uniform.

The team: one MD and any number of other accounts (the PA, the manager, the accountant, a director ...). Anyone
gives anyone work, sets a meeting with them or sends them a message.

Privacy rules enforced in this module:
- PrivateTask (personal items) is only ever read with owner = the person asking. Nobody, not even the MD, reads
  someone else's personal items; nothing derived from them reaches anyone else except anonymous busy windows.
- Work (SharedTask) is seen by the person who gave it, the person it is for, and the MD (who sees all work). The
  PA and the manager also see the MD's official diary (never his personal items).
- Changes others want to the MD's work are TaskRequests; nothing changes until the MD accepts.
"""
import difflib
import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
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
    Message,
    MessageReceipt,
    Priority,
    PrivateTask,
    ReminderAck,
    ReminderSnooze,
    ReminderTold,
    SharedTask,
    StaffMember,
    Status,
    TaskCategory,
    TaskEvent,
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


def get_manager() -> User | None:
    return User.objects.filter(role=User.Role.MG, is_active=True).first()


# Where a work item came from, in the team's terms.
OWN_ORIGINS = (SharedTask.Origin.SELF,)          # a person's own work
GIVEN_ORIGINS = (SharedTask.Origin.GIVEN,)       # one person gave it to another
MEET_ORIGINS = (SharedTask.Origin.MEET,)         # a meeting between the giver and this person (both attend)
PERSON_TYPES = (SharedTask.Assignee.MD, SharedTask.Assignee.USER)  # work that is with a JARVIS account


def bucket_of(user: User) -> str:
    """The list a task for this person goes on: the MD's own official list, or a team member's list."""
    return SharedTask.Assignee.MD if user.is_md else SharedTask.Assignee.USER


def _with_people(qs):
    return qs.select_related("created_by", "staff", "assignee_user", "section")


def private_qs_with_section(qs):
    return qs.select_related("section")


def person_tasks_qs(person: User):
    """Everything on one person's work list: given to them, their own, meetings others set with them."""
    return _with_people(SharedTask.objects.filter(assignee_user=person, assignee_type__in=PERSON_TYPES))


def assistant_tasks_qs(user: User):
    """A team member's own work list (kept under the old name: the PA's and the manager's lists were the first)."""
    return person_tasks_qs(user)


def diary_qs(user: User):
    """The work items in this person's day: what is on their list, and meetings they set with someone else."""
    return _with_people(SharedTask.objects.filter(assignee_type__in=PERSON_TYPES).filter(
        Q(assignee_user=user) | Q(origin__in=MEET_ORIGINS, created_by=user)))


def given_qs(user: User):
    """Work this person gave someone else on the team (with its progress), meetings they set with others included."""
    return _with_people(SharedTask.objects.filter(created_by=user, assignee_type=SharedTask.Assignee.USER,
                                                  origin__in=GIVEN_ORIGINS + MEET_ORIGINS)
                        .exclude(assignee_user=user))


def team_work_qs():
    """All work on team members' lists (not the MD's own list, not TaskPanel staff): the MD sees all of it."""
    return _with_people(SharedTask.objects.filter(assignee_type=SharedTask.Assignee.USER))


def visible_shared_qs(user: User):
    """Every work item this person may see: the MD sees all work; anyone else sees what is on their list, what they
    gave, and (the PA and the manager) the MD's official diary."""
    qs = _with_people(SharedTask.objects.all())
    if user.is_md:
        return qs
    rule = Q(assignee_user=user) | Q(created_by=user)
    if user.sees_md_diary:
        rule |= Q(assignee_type=SharedTask.Assignee.MD)
    return qs.filter(rule)


def team(active: bool = True) -> list[User]:
    qs = User.objects.all().order_by("id")
    return list(qs.filter(is_active=True) if active else qs)


def person_item(u: User, viewer: User | None = None) -> dict:
    """One person as the phones show them: name, job, department(s)."""
    depts = [d.name for d in u.departments.all() if d.active] if not (u.is_md or u.is_pa or u.is_mg) else []
    return {"id": u.id, "name": u.spoken_name, "username": u.username, "role": u.role, "title": u.title_label,
            "department": u.department or (depts[0] if depts else ""), "departments": depts, "gender": u.gender,
            "active": u.is_active, **({"you": True} if viewer is not None and viewer.id == u.id else {})}


def role_label(user: User) -> str:
    """What the person is, in a sentence about them ("the Managing Director", "the MD's personal assistant",
    "Accountant")."""
    if user.is_md:
        return "the Managing Director"
    if user.is_pa:
        return "the MD's personal assistant"
    return user.title_label


# --------------------------------------------------------------------------- finding a person on the team
_MD_WORDS = {"sir", "md", "boss", "managing director", "the md", "sahab", "saab", "maam", "ma am", "mam", "madam",
             "madame"}
_PA_WORDS = {"pa", "p a", "assistant", "my pa", "meri pa", "mera pa", "personal assistant", "secretary", "my assistant"}
_MG_WORDS = {"manager", "the manager", "manager sahab", "manager ji", "mg"}
EVERYONE_WORDS = {"everyone", "everybody", "all", "sab", "sabhi", "sab log", "sabhi log", "sabko", "sab ko", "whole team",
                  "poori team", "puri team", "team", "all of them", "pure office", "poora office",
                  "entire team", "the team", "the whole team", "har kisi", "sabhi ko"}
# "Staff" means the TaskPanel staff list ("sab staff ko bolo"), never the people with a JARVIS account.
STAFF_GROUP = re.compile(r"\bstaff\b|स्टाफ़|स्टाफ", re.IGNORECASE)


def _name_keys(u: User) -> set[str]:
    keys = {norm_person(n) for n in (u.display_name, u.first_name, u.get_full_name(), u.username) if n}
    first = norm_person(u.display_name or u.first_name or "").split()[:1]
    if first:
        keys.add(first[0])
    return {k for k in keys if k}


def _title_keys(u: User) -> set[str]:
    keys = set()
    if u.job_title:
        t = norm_person(u.job_title)
        keys |= {t, t.rstrip("s")}
    if u.is_md:
        keys |= _MD_WORDS | {norm_person(md_address())}
    if u.is_pa:
        keys |= _PA_WORDS
    if u.is_mg:
        keys |= _MG_WORDS
    return {k for k in keys if k}


def find_people(name: str, *, exclude: User | None = None) -> dict:
    """Which team member(s) a spoken name or job means: {"status": known | ambiguous | close | unknown,
    "people": [User], "options": [labels]}. Exact names and job titles count as known; a name the microphone heard
    a little wrong ("Himanshi") that is clearly one person is "close" (taken, and said back by its real name)."""
    key = norm_person(name)
    out = {"status": "unknown", "people": [], "options": []}
    raw = " ".join(re.findall(r"[^\W\d_]+", (name or "").lower().replace("'", "")))
    pool = [u for u in team() if exclude is None or u.id != exclude.id]
    if raw in _MD_WORDS or raw in {"md sir", "sir ji", "boss sir"} or (raw and raw == norm_person(md_address()).replace("'", "")):
        md = next((u for u in pool if u.is_md), None)  # "Sir", "Ma'am", "MD", "boss": the MD
        return {"status": "known", "people": [md], "options": []} if md else out
    if not key:
        return out
    exact = [u for u in pool if key in _name_keys(u)]
    if not exact:
        exact = [u for u in pool if key in _title_keys(u)]
    if not exact:
        # Every word said is part of one name ("Sunil Sharma" said as "Sunil ji Sharma").
        words = set(key.split())
        exact = [u for u in pool if words and words <= set(" ".join(_name_keys(u)).split())]
    if len(exact) == 1:
        return {"status": "known", "people": exact, "options": []}
    if len(exact) > 1:
        return {"status": "ambiguous", "people": exact,
                "options": [f"{u.spoken_name} ({u.title_label})" for u in exact[:4]]}
    if len(key) >= 4:
        scored = []
        for u in pool:
            best = max((difflib.SequenceMatcher(None, key, k).ratio() for k in _name_keys(u)), default=0)
            scored.append((best, u))
        scored.sort(key=lambda p: p[0], reverse=True)
        # "Himanshi" / "Humanshi" for Himanshu, "Suneel" for Sunil: the speech recogniser's usual slips on Indian
        # names. Taken only when one person is clearly the closest; the reply then says the real name (and undo).
        if scored and scored[0][0] >= 0.7 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
            return {"status": "close", "people": [scored[0][1]], "options": []}
        near = [u for r, u in scored[:2] if r >= 0.6]
        if near:
            return {"status": "unknown", "people": [], "options": [f"{u.spoken_name} ({u.title_label})" for u in near]}
    return out


def find_group(name: str, *, exclude: User | None = None) -> tuple[list[User], str]:
    """A group of the team: everyone ("sab ko", "whole team"), a department ("accounts team"), or everyone with a
    job title ("sab managers"). Returns (people, label) or ([], "") when nothing on the team matches."""
    key = " ".join((name or "").lower().split())
    pool = [u for u in team() if exclude is None or u.id != exclude.id]
    if STAFF_GROUP.search(key):
        return [], ""  # the TaskPanel staff, not the team
    if key in EVERYONE_WORDS or key == "":
        return pool, "everyone"
    for suffix in (" team", " department", " dept", " waalon", " walon", " waale", " wale", " log", " ko", " sab"):
        if key.endswith(suffix):
            key = key[: -len(suffix)].strip()
    if not key:
        return pool, "everyone"
    # A department made by the MD (by voice or on the Departments page): its members.
    from . import sections as _sections  # local import: sections builds on this module
    found = _sections.find_department(key)
    if found["status"] in ("known", "close"):
        members = [u for u in found["department"].members.all() if u.is_active and (exclude is None or u.id != exclude.id)]
        return members, f"the {found['department'].name} team"
    dept = [u for u in pool if u.department and (u.department.lower() == key or u.department.lower().startswith(key)
                                                  or key.startswith(u.department.lower()))]
    if dept:
        return dept, f"the {dept[0].department} team"
    single = key[:-1] if key.endswith("s") else key
    titled = [u for u in pool if u.job_title and (single in u.job_title.lower().split() or u.job_title.lower() == single)]
    if len(titled) > 1:
        return titled, f"all {titled[0].job_title}s"
    return [], ""


def giver_words(user: User | None) -> tuple[str, str]:
    """How the giver of a task is named in a spoken line: the MD by the form of address, the others by name."""
    if user is None:
        return md_address(), md_address_hi()
    return (md_address(), md_address_hi()) if user.is_md else (user.spoken_name, user.spoken_name)


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


def gender_of(user: User | None) -> str:
    """"male" / "female" as the MD set it on the Team page (the MD's own from the form of address), or "" when
    nobody said: then JARVIS keeps neutral forms. Never guessed from a name."""
    if user is None:
        return ""
    if user.is_md:
        return md_gender()
    if user.gender in ("male", "female"):
        return user.gender
    if user.is_pa:  # before the Team page the PA's gender was an MD setting
        md = get_md()
        value = str(get_prefs(md).get("paGender") or "") if md else ""
        return value if value in ("male", "female") else ""
    return ""


def pa_gender() -> str:
    """"male" / "female" as the MD set it, or "" when not set (then JARVIS goes by the PA's name and stays neutral)."""
    return gender_of(get_pa())


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
    item.update({"assignee": {"type": "MD" if task.owner.is_md else "USER", "user_id": task.owner_id},
                 "source": task.source,
                 "section": task.section.name if task.section_id and task.section.active else "",
                 "section_id": task.section_id if task.section_id and task.section.active else None,
                 "section_colour": task.section.colour if task.section_id else ""})
    return item


def shared_item(task: SharedTask) -> dict:
    item = _base_item(task, "shared")
    person = task.assignee_user if task.assignee_user_id else None
    item.update(
        {
            "assignee": {
                "type": task.assignee_type,
                "staff_id": task.staff_id,
                "user_id": task.assignee_user_id,
                "staff_name": task.staff.name if task.staff else (person.spoken_name if person else None),
                "department": task.staff.department if task.staff else (person.title_label if person else None),
            },
            "origin": task.origin,
            "created_by": task.created_by.spoken_name if task.created_by_id else None,
            "created_by_id": task.created_by_id,
            "description": task.description,
            "category_key": task.category_key,
            "sync_state": task.sync_state,
            "sync_error": task.sync_error,
            "remote_task_id": task.remote_legacy_id,
            "remote_status": task.remote_status,
            "status_label": (
                tp_sync.status_label(task) if task.assignee_type == SharedTask.Assignee.STAFF
                else PA_STATUS_LABEL.get(task.status, "") if task.assignee_type == SharedTask.Assignee.USER
                else ""
            ),
            "hold_reason": task.hold_reason,
            "last_synced_at": task.last_synced_at.isoformat() if task.last_synced_at else None,
            # Ring until seen: when the person it is for saw it, and by when they said they would do it.
            "seen_at": task.seen_at.isoformat() if task.seen_at else None,
            "promise_label": tu.due_phrase(task.promise_kind, task.promise_date, task.promise_at),
            "promise_label_hi": tu.due_phrase_hi(task.promise_kind, task.promise_date, task.promise_at),
            # The section it is filed in (GST, Accounts…).
            "section": task.section.name if task.section_id and task.section.active else "",
            "section_id": task.section_id if task.section_id and task.section.active else None,
            "section_colour": task.section.colour if task.section_id else "",
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
    now = timezone.now()
    told = Alert.objects.filter(recipient=user, id__in=clean, delivered_at__isnull=True)
    message_ids = list(told.filter(ref_type="message").values_list("ref_id", flat=True))
    n = told.update(delivered_at=now)
    if message_ids:
        MessageReceipt.objects.filter(message_id__in=message_ids, recipient=user, delivered_at__isnull=True).update(delivered_at=now)
    return n


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
ALERT_KEEP_FOR = timedelta(days=7)      # and then it is deleted (alerts are news; the work itself lives in the tasks)


def cleanup_alerts(now: datetime | None = None) -> int:
    """Delete alert-feed entries older than a week. Alerts are temporary news ("Mansi completed a task", a ring,
    a follow-up); the task, its history (TaskEvent) and its reminders are never touched by this, so a task due in
    three months survives every cleanup. Run by the worker once a day; returns how many were deleted."""
    cutoff = (now or timezone.now()) - ALERT_KEEP_FOR
    deleted, _ = Alert.objects.filter(created_at__lt=cutoff).delete()
    if deleted:
        log.info("alert cleanup: %d entr%s older than %d days deleted", deleted, "y" if deleted == 1 else "ies", ALERT_KEEP_FOR.days)
    return deleted


def alert_question(alert: Alert) -> dict | None:
    """The one question JARVIS asks aloud right after this alert, and what a yes does. Kept tight so JARVIS never
    nags: a request to the MD ("Accept it?"), a task the MD gave the PA ("Shall I tell Sir you've seen it?"), a
    briefing that found the day too full ("Shall I move X to tomorrow?"). Decisions and staff news are spoken only.
    Both the app and the phone's background part read this contract; neither spells its own."""
    if isinstance(alert.question, dict) and alert.question.get("kind"):
        if alert.seen_at and alert.question.get("kind") == "seen":
            return None  # seen already (and the sender told): nothing left to ask
        return alert.question
    if alert.kind == Alert.Kind.NEW_REQUEST and alert.ref_type == "request" and alert.recipient.is_md:
        return {"kind": "accept", "en": "Accept it?", "hi": "मंज़ूर करूँ?"}
    if alert.kind == Alert.Kind.TASK_ASSIGNED and not alert.recipient.is_md:
        task = task_of(alert)
        giver = alert.sender or (task.created_by if task else None)
        if giver is not None and giver.id != alert.recipient_id:
            who, who_hi = giver_words(giver)
            # Work given to them (not a meeting): when will they do it? The answer goes back to the giver, and it
            # counts as seen. Once they have said, or once it is no longer theirs, nothing is asked.
            if task is not None and task.kind != Kind.MEETING:
                if task.is_closed or task.assignee_user_id != alert.recipient_id or task.promise_date:
                    return None
                return promise_question(giver)
            if alert.seen_at:
                return None
            return {"kind": "seen", "en": f"Shall I tell {who} you've seen it?", "hi": f"{who_hi} को बता दूँ कि आपने देख लिया?"}
    return None


def promise_question(giver: User) -> dict:
    """"Shall I tell Sir when you'll do it?": asked of the person a task was given to, once they have seen it."""
    who, who_hi = giver_words(giver)
    return {"kind": "promise", "en": f"Shall I tell {who} when you'll do it?",
            "hi": f"{who_hi} को बता दूँ कि आप ये कब तक कर देंगे?"}


def task_of(alert: Alert) -> SharedTask | None:
    """The work item an alert is about, if any."""
    if alert.ref_type != "shared_task" or not alert.ref_id:
        return None
    try:
        return SharedTask.objects.select_related("created_by", "assignee_user").filter(id=alert.ref_id).first()
    except (ValueError, TypeError, ValidationError):  # not a task id
        return None


_TITLE_HI = (
    # (words in the English title, the Hindi title; {who} = whoever it is from or about)
    ("has seen it", "{who} ने देख लिया"),
    ("hasn't seen it", "{who} ने अभी तक नहीं देखा"),
    ("will do it", "{who} कब तक करेंगे"),
    ("can't do it", "{who} नहीं कर पाएँगे"),
    ("gave your task to", "आपका काम किसी और को दिया गया"),
    ("completed a task", "{who} ने काम पूरा किया"),
    ("put a task on hold", "{who} ने काम होल्ड पर रखा"),
    ("started a task", "{who} ने काम शुरू किया"),
    ("reopened a task", "{who} ने काम फिर से खोला"),
    ("cancelled a task", "{who} ने काम कैंसल किया"),
    ("moved a task", "{who} ने काम का समय बदला"),
    ("took that back", "{who} ने वापस ले लिया"),
    ("delegated a task", "{who} ने स्टाफ़ को काम दिया"),
    ("Request accepted", "रिक्वेस्ट मंज़ूर"),
    ("Request declined", "रिक्वेस्ट नामंज़ूर"),
    ("Request conflict", "रिक्वेस्ट लागू नहीं हुई"),
    ("Your name", "आपका नाम"),
    ("A name has changed", "नाम बदला गया"),
    ("Good morning", "सुप्रभात, आज का दिन"),
    ("slipped today", "आज के बाकी काम"),
    ("without a date", "बिना तारीख़ के काम"),
    ("Not started yet", "अभी शुरू नहीं हुआ"),
    ("came in during your meeting", "मीटिंग के दौरान आए काम"),
    ("Request waiting", "रिक्वेस्ट इंतज़ार में"),
)


def alert_title_hi(alert: Alert) -> str:
    """The alert's title in Hindi, for someone JARVIS speaks Hindi with (the notification shows it). Built from the
    kind of alert and who it is from; anything not known keeps the English title."""
    sender = alert.sender
    who_hi = (md_address_hi() if sender.is_md else sender.spoken_name) if sender is not None else ""
    title = alert.title or ""
    if alert.kind == Alert.Kind.TASK_ASSIGNED and who_hi:
        return f"{who_hi} के साथ मीटिंग" if title.startswith("Meeting with") else f"{who_hi} से नया टास्क"
    if alert.kind == Alert.Kind.NEW_REQUEST and who_hi:
        return f"{who_hi} की रिक्वेस्ट" if title.startswith("New task") else f"{who_hi} का बदलाव का सुझाव"
    if alert.ref_type == "message" and who_hi:
        return f"{who_hi} का संदेश"
    for words, hindi in _TITLE_HI:
        if words.lower() in title.lower():
            if "{who}" in hindi:
                name = who_hi or title.split(" ")[0]
                return hindi.format(who=name)
            return hindi
    return title


def alert_item(alert: Alert, lang: str = "en") -> dict:
    """lang = the recipient's current language: the spoken line (and the title) come in Hindi when it is Hindi."""
    now = timezone.now()
    snoozed = alert.snoozed_until if alert.snoozed_until and alert.snoozed_until > now else None
    sender = alert.sender
    return {
        "id": str(alert.id),
        "kind": alert.kind,
        "priority": alert.priority,
        # Who it is from (a person on the team), for the banner: the name as JARVIS says it, and how it is said in
        # a line ("Sir" for the MD).
        "from_name": sender.spoken_name if sender else "",
        "from_word": giver_words(sender)[0] if sender else "",
        "title": alert_title_hi(alert) if lang == "hi" else alert.title,
        "title_en": alert.title,
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
        # Ring until seen: `ring` = it waits for a Seen (the banner shows the Seen button); `ringing` = the phone
        # rings again every few minutes right now; `rings` = how many times it has rung (the phone's own watcher
        # rings once per number, so a lost push still rings).
        "ring": bool(alert.ring and not alert.seen_at),
        "ringing": ringing_now(alert),
        "rings": alert.rings,
        "seen_at": alert.seen_at.isoformat() if alert.seen_at else None,
        "seen_label": "देख लिया" if lang == "hi" else "Seen",
    }


def ringing_now(alert: Alert) -> bool:
    return bool(alert.ring and alert.seen_at is None and alert.acknowledged_at is None and alert.next_ring_at is not None
                and alert.rings > 0)


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


NUDGE_AFTER_SNOOZES = 3  # the third "later" on the same task earns a suggestion (never a silent change of date)


def snooze_notes(user: User, key: str, until: datetime) -> dict:
    """What a snooze does NOT do, said plainly: the deadline stays where it is (so a "later" past the deadline is
    pointed out), and the third "later" on the same task suggests a new day or marking it blocked. Returns
    {"past_deadline": bool, "snoozes": n, "note": en, "note_hi": hi} — notes empty when there is nothing to say."""
    out = {"past_deadline": False, "snoozes": 0, "note": "", "note_hi": ""}
    parts = (key or "").split(":")
    if len(parts) < 3 or parts[0] not in ("private", "shared"):
        return out
    scope, task_id = parts[0], parts[1]
    task = (private_qs(user).filter(id=task_id).first() if scope == "private"
            else visible_shared_qs(user).filter(id=task_id).first())
    if task is None:
        return out
    prefix = f"{scope}:{task_id}:"
    out["snoozes"] = ReminderSnooze.objects.filter(user=user, key__startswith=prefix).count()
    deadline = task.due_at or (tu.combine_local(task.due_date, time(23, 59)) if task.due_date else None)
    notes_en, notes_hi = [], []
    if deadline is not None and until > deadline + timedelta(minutes=1) and not task.is_closed:  # the same minute is not "after"
        out["past_deadline"] = True
        when, when_hi = (tu.fmt_time(deadline), tu.fmt_time_hi(deadline)) if task.due_at else (tu.fmt_day(task.due_date), tu.fmt_day_hi(task.due_date))
        notes_en.append(f"That is after its deadline ({when}); the deadline itself stays.")
        notes_hi.append(f"ये इसकी डेडलाइन ({when_hi}) के बाद है; डेडलाइन वही रहेगी।")
    if out["snoozes"] >= NUDGE_AFTER_SNOOZES:
        notes_en.append(f"You have put this off {out['snoozes']} times. Shall I move it to another day, or mark it blocked?")
        notes_hi.append(f"आपने इसे {out['snoozes']} बार टाला है। किसी और दिन रख दूँ, या ब्लॉक्ड मार्क कर दूँ?")
    out["note"], out["note_hi"] = " ".join(notes_en), " ".join(notes_hi)
    return out


def snooze_alert(user: User, alert_id, until: datetime) -> Alert:
    """"Later" on an alert: out of sight until `until`, then announced afresh. On something that rings until seen
    this is NOT seen (18 Nov 2026: Seen is only the person's own word or tap): the ringing pauses until then and
    starts again; the sender is told nothing, and the "not seen yet" clock keeps running honestly."""
    alert = Alert.objects.get(id=alert_id, recipient=user)
    alert.snoozed_until = until
    alert.delivered_at = None  # announced afresh when it comes back
    fields = ["snoozed_until", "delivered_at"]
    if alert.ring and alert.seen_at is None and alert.next_ring_at is not None:
        alert.next_ring_at = max(alert.next_ring_at, until)  # the next ting comes when it comes back
        fields.append("next_ring_at")
    alert.save(update_fields=fields)
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
def private_qs(user: User):
    """This person's own personal items. Only ever theirs: there is no way to read anyone else's."""
    return PrivateTask.objects.filter(owner=user).select_related("owner")


def md_private_qs(md: User):
    """The MD's personal items (kept under the old name; every person now has their own, see private_qs)."""
    return private_qs(md)


def shared_md_tasks_qs():
    """The MD's own official list (work he accepted from someone's request)."""
    return _with_people(SharedTask.objects.filter(assignee_type=SharedTask.Assignee.MD))


def delegated_qs():
    return _with_people(SharedTask.objects.filter(assignee_type=SharedTask.Assignee.STAFF))


def delegated_visible_qs(user: User):
    """TaskPanel staff work this person may see: the MD, the PA and the manager see it all (as before the team);
    anyone else sees what they gave."""
    qs = delegated_qs()
    return qs if (user.is_md or user.sees_md_diary) else qs.filter(created_by=user)


def pa_tasks_qs():
    """The PA's list (work given to the PA, the PA's own, meetings set with the PA)."""
    pa = get_pa()
    return person_tasks_qs(pa) if pa else SharedTask.objects.none()


def mg_tasks_qs():
    """The manager's list: what was given to her, and her own."""
    mg = get_manager()
    return person_tasks_qs(mg) if mg else SharedTask.objects.none()


PA_STATUS_LABEL = {
    Status.OPEN: "Not started", Status.IN_PROGRESS: "Working", Status.BLOCKED: "Hold",
    Status.COMPLETED: "Completed", Status.CANCELLED: "Cancelled",
}


def _recent_first_open(qs):
    open_items = [shared_item(t) for t in qs.exclude(status__in=[Status.COMPLETED, Status.CANCELLED])]
    open_items.sort(key=_sort_key)
    done = [shared_item(t) for t in qs.filter(status=Status.COMPLETED).order_by("-completed_at")[:5]]
    return open_items + done


def busy_windows(day: date, include_titles_for: User | None = None, person: User | None = None) -> list[dict]:
    """Busy time of one person on a day (the MD when no person is given). Titles are NEVER included unless the
    caller is that person; overlapping windows are merged, so nobody can count anyone's private meetings."""
    start, end = tu.day_bounds(day)
    windows = []
    whose = person or get_md()
    sources = []
    if whose:
        sources.append(PrivateTask.objects.filter(owner=whose))
        sources.append(diary_qs(whose))
    for qs in sources:
        for task in qs.exclude(status__in=[Status.COMPLETED, Status.CANCELLED]).filter(
            scheduled_start__lt=end, scheduled_end__gt=start
        ):
            entry = {"start": task.scheduled_start.isoformat(), "end": task.scheduled_end.isoformat()}
            if include_titles_for is not None and whose is not None and include_titles_for.id == whose.id:
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

    Everyone has OFFICIAL work (shared: the people involved and the MD can see it) and PERSONAL items (private:
    only they can). Personal ones are listed and counted only when include_private is true (never on a locked
    phone), and a dashboard only ever holds the reader's own personal items. A team member's work is split once
    more: given to them by someone else, and their own.
    Returns {"items": late first, then today, then the rest; "counts": {...}}.
    """
    def keep(item):
        return include_private or item.get("scope") != "private"
    late = [i for i in dash["overdue"] if keep(i)]
    today = [i for i in dash["today"] if keep(i)]
    later = [i for i in dash["upcoming"] + dash["anytime"] if keep(i)]
    items = late + today + later
    counts = {"total": len(items), "late": len(late), "due_today": len(today), "later_or_no_date": len(later)}
    personal = sum(1 for i in items if i.get("scope") == "private")
    counts["official"] = len(items) - personal
    if include_private:
        counts["personal"] = personal
    if not user.is_md:
        work = [i for i in items if i.get("scope") != "private"]
        own = sum(1 for i in work if i.get("origin") in OWN_ORIGINS)
        counts["given_to_me"] = len(work) - own
        counts["own"] = own
    return {"items": items, "counts": counts}


def dashboard(user: User) -> dict:
    now = tu.now_local()
    taskpanel = {**tp_client().describe(), "linked": user_link(user) is not None}
    extra = {}
    # Everyone's own day: their personal items (theirs alone), their work list, and meetings they set with others.
    items = [private_item(t) for t in private_qs(user).exclude(status=Status.CANCELLED)]
    items += [shared_item(t) for t in diary_qs(user).exclude(status=Status.CANCELLED)]
    # What they gave others on the team, with its progress (meetings they set are in their own day instead).
    extra["given_tasks"] = _recent_first_open(given_qs(user).filter(origin__in=GIVEN_ORIGINS))
    if user.is_md:
        pending = TaskRequest.objects.filter(state=TaskRequest.State.PENDING).select_related("requested_by", "target")
        requests_out = [request_item(r) for r in pending]
        # Everybody else's work (the MD sees all work): their own and what they gave each other.
        extra["team_tasks"] = _recent_first_open(team_work_qs().exclude(created_by=user))
        # The PA's and the manager's lists, as the app showed them before the team (older phones still read these).
        extra["pa_tasks"] = _recent_first_open(pa_tasks_qs().exclude(origin__in=MEET_ORIGINS))
        extra["mg_tasks"] = _recent_first_open(mg_tasks_qs().exclude(origin__in=MEET_ORIGINS))
    else:
        # A meeting or call someone arranged for the MD (their request, accepted) belongs in their day as well: they
        # set it up and have to see it coming. The PA and the manager run his diary, so they see all of his.
        md_meetings = shared_md_tasks_qs().filter(kind__in=[Kind.MEETING, Kind.CALL]).exclude(status=Status.CANCELLED)
        if not user.sees_md_diary:
            md_meetings = md_meetings.filter(created_by=user)
        items += [shared_item(t) for t in md_meetings]
        mine = TaskRequest.objects.filter(requested_by=user).select_related("requested_by", "target")[:30]
        requests_out = [request_item(r) for r in mine]
        md_list = shared_md_tasks_qs() if user.sees_md_diary else shared_md_tasks_qs().filter(created_by=user)
        extra["md_tasks"] = _recent_first_open(md_list)  # the MD's official work: all of it (PA, manager) or theirs
    delegated = [shared_item(t) for t in delegated_visible_qs(user).exclude(status=Status.CANCELLED).order_by("-created_at")[:50]]
    # One item can reach the list twice (a meeting someone set with themselves): keep it once.
    seen_ids: set[tuple] = set()
    items = [i for i in items if not ((i["scope"], i["id"]) in seen_ids or seen_ids.add((i["scope"], i["id"])))]
    buckets = _bucket(items)
    completed_today = [
        i for i in items
        if i["status"] == Status.COMPLETED and i["completed_at"]
        and tu.to_local(datetime.fromisoformat(i["completed_at"])).date() == now.date()
    ]
    pa_user, md_user, mg_user = get_pa(), get_md(), get_manager()
    out = {
        "user": {"username": user.username, "role": user.role, "name": user.spoken_name, "title": user.title_label,
                 "department": user.department},
        # The names as JARVIS says them, and how the MD is addressed, so no screen has to carry a typed-in word.
        "pa_name": pa_user.spoken_name if pa_user else "",
        "md_name": md_user.spoken_name if md_user else "",
        "manager_name": mg_user.spoken_name if mg_user else "",
        # Everyone on the team (names and jobs only), for the screens that name people.
        "team": [person_item(u, user) for u in team()],
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
    if user.is_assistant:
        out["md_busy_today"] = busy_windows(now.date())
    return out


# --------------------------------------------------------------------------- personal items (each person's own)
@transaction.atomic
def create_private_task(md: User, data: dict, source: str = PrivateTask.Source.MANUAL) -> PrivateTask:
    """A personal item of the person speaking (`md` is whoever it is: every account has its own, seen by nobody
    else, not even the MD)."""
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
    from . import sections as _sections  # local import: sections builds on this module
    _sections.file_private(task, f"{task.title} {task.notes}", md, said=data.get("section") or "")
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
    """`md` is the owner (anyone): someone else's personal item is simply not found."""
    task = PrivateTask.objects.select_for_update().get(id=task_id, owner=md)
    _check_version(task, expected_version)
    _apply_changes(task, changes)
    task.save()
    audit(md, "private_task.update", "private_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


# --------------------------------------------------------------------------- work between people
PA_SETTABLE_STATUSES = {Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED, Status.COMPLETED}


@transaction.atomic
def update_shared_md_task(md: User, task_id, changes: dict, expected_version: int) -> SharedTask:
    """The MD changes any work item directly: his own official list, and everything on the team (he sees all work).
    The person a task is for hears when it is moved or cancelled."""
    if not md.is_md:
        raise ServiceError("forbidden", "Only the MD can change MD tasks directly.", 403)
    # Lock only the task row: PostgreSQL rejects FOR UPDATE across the nullable staff join.
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_type__in=PERSON_TYPES)
    _check_version(task, expected_version)
    old_status, old_due = task.status, (task.due_at, task.due_date)
    _apply_changes(task, changes)
    task.save()
    _log_changes(md, task, old_status, old_due)
    if task.assignee_user_id and task.assignee_user_id != md.id:
        _tell_assignee_of_change(md, task, old_status, old_due)
    elif task.status == Status.CANCELLED and old_status != Status.CANCELLED:
        close_task_alerts(task)
    audit(md, "shared_task.update", "shared_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


def _priority_words(priority: str) -> tuple[str, str]:
    return ({"URGENT": "urgent ", "HIGH": "high-priority "}.get(priority, ""),
            {"URGENT": "अर्जेंट ", "HIGH": "ज़रूरी "}.get(priority, ""))


@transaction.atomic
def create_person_task(giver: User, person: User, data: dict, *, meeting: bool = False,
                       compose_note: bool = True) -> SharedTask:
    """One person gives another on the team a task, or sets a meeting with them. Both see it; the person is told at
    once in the giver's name, and the giver hears when it is done (or put on hold).
    Work for the MD is never given like this: it goes to him as a request he accepts (create_md_request)."""
    if person is None or not person.is_active:
        raise ServiceError("no_person", "that person's account is switched off.", 422)
    if person.id == giver.id:
        return create_own_task(giver, data)
    if person.is_md and not giver.is_md:
        raise ServiceError("use_request", f"work for {md_address()} goes to {md_pronouns()[1]} as a request.", 422)
    task = SharedTask(
        title=data["title"].strip(), notes=data.get("notes", ""),
        kind=Kind.MEETING if meeting else data.get("kind", Kind.TASK),
        priority=data.get("priority", Priority.NORMAL), assignee_type=bucket_of(person), assignee_user=person,
        origin=SharedTask.Origin.MEET if meeting else SharedTask.Origin.GIVEN,
        created_by=giver, description=(data.get("description") or "").strip()[:5000],
        sync_state=SharedTask.Sync.NOT_REQUIRED,
    )
    apply_schedule(task, {**data, "kind": task.kind, "_prefs": get_prefs(giver)}, creating=True)
    task.announcement = (data.get("announcement") or "")[:300]
    task.save()
    if task.kind == Kind.MEETING and data.get("duration_minutes"):
        learn_meeting_minutes(giver, int(data["duration_minutes"]))
    from . import sections as _sections  # local import: sections builds on this module
    _sections.file_task(task, f"{task.title} {task.description} {task.notes}", giver=giver, person=person,
                        said=data.get("section") or "")
    who, who_hi = giver_words(giver)
    name = person.spoken_name
    when = tu.due_phrase(task.due_kind, task.due_date, task.due_at)
    when_hi = tu.due_phrase_hi(task.due_kind, task.due_date, task.due_at)
    pr_en, pr_hi = _priority_words(task.priority)
    if meeting:
        title = f"Meeting with {who}"
        spoken = f"{name}, {who} has scheduled a meeting with you" + (f" {when}" if when else "") + f": {task.title}."
        spoken_hi = f"{name}, {who_hi} ने आपके साथ मीटिंग रखी है" + (f" {when_hi}" if when_hi else "") + f": {task.title}।"
    else:
        title = f"New task from {who}"
        spoken = f"{name}, {who} has given you a new {pr_en}task: {task.title}" + (f", {when}" if when else "") + "."
        spoken_hi = f"{name}, {who_hi} ने आपको एक नया {pr_hi}टास्क दिया है: {task.title}" + (f", {when_hi}" if when_hi else "") + "।"
    message, message_hi = spoken, spoken_hi
    if compose_note:
        # A brief professional note in the giver's name (the template above if the AI is unavailable).
        from .compose import compose  # local import: compose depends on the AI client, services must not
        message, message_hi = compose(
            "meeting_with_pa" if meeting else "task_for_pa", f"{who} ({role_label(giver)})", name,
            {"title": task.title, "when": when, "priority": task.priority.lower(), "instruction": task.description,
             "notes": task.notes}, fallback=spoken, fallback_hi=spoken_hi, user=giver,
        )
    Alert.objects.create(
        recipient=person, sender=giver, kind=Alert.Kind.TASK_ASSIGNED, priority=task.priority, title=title,
        body=message[:400], spoken_text=message[:400], spoken_text_hi=message_hi[:400],
        ref_type="shared_task", ref_id=str(task.id), **ring_fields(person, task.priority),
    )
    log_event(task, TaskEvent.Kind.GIVEN, giver, to=person.id, **_sections.section_data(task))
    audit(giver, "person_meeting.create" if meeting else "person_task.create", "shared_task", task.id, f"to user {person.id}")
    return task


def create_pa_task(md: User, data: dict, *, meeting_with_pa: bool = False) -> SharedTask:
    """Work (or a meeting) for the PA, from anyone else on the team (the first such list; kept for older phones)."""
    pa = get_pa()
    if pa is None:
        raise ServiceError("no_pa", "There is no active PA account.", 422)
    if md.id == pa.id:
        raise ServiceError("forbidden", "Your own work goes on your own list.", 403)
    return create_person_task(md, pa, data, meeting=meeting_with_pa)


def create_mg_task(giver: User, data: dict) -> SharedTask:
    """Work for the manager, from anyone else on the team (kept for older phones)."""
    mg = get_manager()
    if mg is None:
        raise ServiceError("no_manager", "There is no manager account.", 422)
    if giver.id == mg.id:
        raise ServiceError("forbidden", "Your own work goes on your own list.", 403)
    return create_person_task(giver, mg, data)


@transaction.atomic
def create_own_task(user: User, data: dict) -> SharedTask:
    """A team member's own WORK item ("mujhe bank jaana hai", "I have to send the report"): on their list, seen by
    the MD (who sees all work); nobody is asked or nagged. Their PERSONAL items are private tasks instead."""
    if user.is_md:
        raise ServiceError("forbidden", "The MD's own items are kept as personal tasks.", 403)
    task = SharedTask(
        title=data["title"].strip(), notes=data.get("notes", ""), kind=data.get("kind", Kind.TASK),
        priority=data.get("priority", Priority.NORMAL), assignee_type=SharedTask.Assignee.USER, assignee_user=user,
        origin=SharedTask.Origin.SELF, created_by=user, description=(data.get("description") or "").strip()[:5000],
        sync_state=SharedTask.Sync.NOT_REQUIRED,
    )
    apply_schedule(task, {**data, "_prefs": get_prefs(user)}, creating=True)
    if task.kind == Kind.MEETING and data.get("duration_minutes"):
        learn_meeting_minutes(user, int(data["duration_minutes"]))
    task.announcement = (data.get("announcement") or "")[:300]
    task.save()
    from . import sections as _sections  # local import: sections builds on this module
    _sections.file_task(task, f"{task.title} {task.description} {task.notes}", giver=user, person=user,
                        said=data.get("section") or "")
    log_event(task, TaskEvent.Kind.ADDED, user, **_sections.section_data(task))
    audit(user, "own_task.create", "shared_task", task.id)
    return task


def create_pa_self_task(pa: User, data: dict) -> SharedTask:
    """Kept for older phones: a team member's own work item."""
    return create_own_task(pa, data)


@transaction.atomic
def update_given_task(user: User, task_id, changes: dict, expected_version: int) -> SharedTask:
    """Someone changes, moves or cancels work they gave another person (or a meeting they set with them). The
    person hears when it is moved or cancelled."""
    task = SharedTask.objects.select_for_update().get(id=task_id, created_by=user, origin__in=GIVEN_ORIGINS + MEET_ORIGINS)
    _check_version(task, expected_version)
    old_status, old_due = task.status, (task.due_at, task.due_date)
    _apply_changes(task, changes)
    task.save()
    _log_changes(user, task, old_status, old_due)
    _tell_assignee_of_change(user, task, old_status, old_due)
    audit(user, "given_task.update", "shared_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


def _tell_assignee_of_change(giver: User, task: SharedTask, old_status: str, old_due) -> None:
    """The person a task is for hears when its giver (or the MD) cancels or moves it: one short note."""
    person = task.assignee_user
    if person is None or person.id == giver.id or not person.is_active:
        return
    who, who_hi = giver_words(giver)
    name = person.spoken_name
    if task.status == Status.CANCELLED and old_status != Status.CANCELLED:
        close_task_alerts(task)
        en, hi = f"{name}, {who} has cancelled: {task.title}.", f"{name}, {who_hi} ने कैंसल कर दिया: {task.title}।"
        title = f"{who} cancelled a task"
    elif (task.due_at, task.due_date) != old_due and not task.is_closed:
        when = tu.due_phrase(task.due_kind, task.due_date, task.due_at) or "no fixed time"
        when_hi = tu.due_phrase_hi(task.due_kind, task.due_date, task.due_at) or "बिना तय समय"
        en, hi = f"{name}, {who} has moved {task.title} to {when}.", f"{name}, {who_hi} ने {task.title} {when_hi} पर कर दिया।"
        title = f"{who} moved a task"
    else:
        return
    Alert.objects.create(recipient=person, sender=giver, kind=Alert.Kind.INFO, priority=Priority.NORMAL, title=title,
                         body=task.title, spoken_text=en, spoken_text_hi=hi, ref_type="shared_task", ref_id=str(task.id))


@transaction.atomic
def update_own_task(user: User, task_id, changes: dict, expected_version: int) -> SharedTask:
    """A team member changes, moves or cancels their own work item (never one someone gave them)."""
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_user=user, origin__in=OWN_ORIGINS, created_by=user)
    _check_version(task, expected_version)
    old_status, old_due = task.status, (task.due_at, task.due_date)
    _apply_changes(task, changes)
    task.save()
    _log_changes(user, task, old_status, old_due)
    audit(user, "own_task.update", "shared_task", task.id, "fields: " + ",".join(sorted(changes)))
    return task


def update_pa_self_task(pa: User, task_id, changes: dict, expected_version: int) -> SharedTask:
    """Kept for older phones: see update_own_task."""
    return update_own_task(pa, task_id, changes, expected_version)


@transaction.atomic
def update_task_status(user: User, task_id, new_status: str, expected_version: int, note: str = "") -> SharedTask:
    """The person a task is for moves it: Not started / Working / Hold (with a reason) / Done. Whoever gave it is
    told (the MD, or the colleague who gave it); their own work tells nobody."""
    if new_status not in PA_SETTABLE_STATUSES:
        raise ServiceError("invalid_status", "Choose Not started, Working, Hold or Done.", 422)
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_user=user, assignee_type=SharedTask.Assignee.USER)
    if task.status == Status.CANCELLED:
        own = task.origin in OWN_ORIGINS
        raise ServiceError("cancelled", "This task was cancelled." if own else f"{giver_words(task.created_by)[0]} has cancelled this task.", 409)
    _check_version(task, expected_version)
    if new_status == Status.BLOCKED and not note.strip():
        raise ServiceError("reason_required", "Please give a reason for putting this on hold.", 422)
    old = task.status
    task.status = new_status
    task.hold_reason = note.strip()[:300] if new_status == Status.BLOCKED else ""
    task.completed_at = timezone.now() if new_status == Status.COMPLETED else None
    task.version += 1
    # Moving it at all means they have seen it: the ringing stops (the giver hears the step itself, not a "seen").
    first_look = task.seen_at is None and task.origin not in OWN_ORIGINS
    if first_look:
        task.seen_at = timezone.now()
    task.save()
    if first_look:
        log_event(task, TaskEvent.Kind.SEEN, user)
        Alert.objects.filter(recipient=user, ref_type="shared_task", ref_id=str(task.id), seen_at__isnull=True) \
            .update(seen_at=task.seen_at)
    _log_changes(user, task, old, (task.due_at, task.due_date))
    if new_status == Status.COMPLETED:
        close_task_alerts(task)
    giver = task.created_by if task.created_by_id and task.created_by_id != user.id else None
    if giver is not None and new_status != old and task.origin not in OWN_ORIGINS and giver.is_active:
        name, sir, sir_hi = user.spoken_name, address_of(giver), address_hi_of(giver)
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
            recipient=giver, sender=user, kind=Alert.Kind.STAFF_UPDATE, priority=priority, title=title, body=task.title,
            spoken_text=spoken[:400], spoken_text_hi=spoken_hi[:400], ref_type="shared_task", ref_id=str(task.id),
        )
    audit(user, "task.status", "shared_task", task.id, f"{old} -> {new_status}")
    return task


def update_pa_task_status(pa: User, task_id, new_status: str, expected_version: int, note: str = "") -> SharedTask:
    """Kept for older phones: see update_task_status."""
    return update_task_status(pa, task_id, new_status, expected_version, note)


def change_shared_task(user: User, task_id, changes: dict, expected_version: int, reason: str = ""):
    """Any change to a work item, by whoever asks, through the right door: the MD changes anything; the giver
    changes what they gave; anyone changes their own; the person it is for only moves its status; a change to the
    MD's own work becomes a request he accepts. Returns (task, None) when applied, (None, request) when it went to
    the MD. Someone else's work is refused (or not found, when they may not even see it)."""
    if user.is_md:
        return update_shared_md_task(user, task_id, changes, expected_version), None
    task = visible_shared_qs(user).filter(id=task_id).first()
    if task is None:
        raise SharedTask.DoesNotExist("not visible")
    if task.origin in OWN_ORIGINS and task.created_by_id == user.id:
        return update_own_task(user, task_id, changes, expected_version), None
    if task.origin in GIVEN_ORIGINS + MEET_ORIGINS and task.created_by_id == user.id:
        return update_given_task(user, task_id, changes, expected_version), None
    if task.assignee_user_id == user.id and task.assignee_type == SharedTask.Assignee.USER:
        if set(changes) == {"status"} and changes["status"] in PA_SETTABLE_STATUSES:
            return update_task_status(user, task_id, changes["status"], expected_version), None
        giver = giver_words(task.created_by)[0]
        raise ServiceError("forbidden", f"only {giver} can change this task. You can update its status.", 403)
    if task.assignee_type == SharedTask.Assignee.MD:
        return None, propose_change(user, task_id, changes, expected_version, reason)
    raise ServiceError("forbidden", "this task is not yours to change.", 403)


def complete_shared_task(user: User, task_id, expected_version: int):
    """"Done" on a work item: the person it is for completes it; the giver (or the MD) may close it; done on the
    MD's own work by someone else is sent to him to confirm. Returns (task, request) like change_shared_task."""
    task = visible_shared_qs(user).filter(id=task_id).first()
    if task is None:
        raise SharedTask.DoesNotExist("not visible")
    if task.assignee_type == SharedTask.Assignee.USER and task.assignee_user_id == user.id:
        return update_task_status(user, task_id, Status.COMPLETED, expected_version), None
    return change_shared_task(user, task_id, {"status": Status.COMPLETED}, expected_version, "Marked done")


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
    message, message_hi = compose(kind, f"{pa_name} ({role_label(req.requested_by)})", sir, {
        "title": item["title"], "when": item["due_label"], "priority": item["priority"].lower(),
        "reason": req.reason, "target": item.get("target_title") or "",
    }, fallback=spoken, fallback_hi=spoken_hi, user=req.requested_by)
    Alert.objects.create(
        recipient=md, sender=req.requested_by, kind=Alert.Kind.NEW_REQUEST, priority=item["priority"], title=title,
        body=message[:400], spoken_text=message[:400], spoken_text_hi=message_hi[:400], ref_type="request", ref_id=str(req.id),
    )


@transaction.atomic
def create_md_request(pa: User, data: dict) -> TaskRequest:
    """Anyone on the team asks the MD to do or attend something (`pa` is whoever asks): nothing reaches his list
    until he accepts."""
    if not pa.is_assistant:
        raise ServiceError("forbidden", "The MD adds his own work directly; requests come from the team.", 403)
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
    if not pa.is_assistant:
        raise ServiceError("forbidden", "The MD changes his own work directly.", 403)
    md_work = shared_md_tasks_qs() if pa.sees_md_diary else shared_md_tasks_qs().filter(created_by=pa)
    task = md_work.get(id=task_id)  # someone else's request to the MD is not theirs to touch: not found
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
        recipient=req.requested_by, sender=get_md(), kind=Alert.Kind.REQUEST_DECIDED, priority=Priority.NORMAL,
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
                assignee_user=md, origin=SharedTask.Origin.PA_REQUEST, created_by=req.requested_by,
            )
            apply_schedule(task, {**data, "_prefs": get_prefs(md)}, creating=True)
            task.announcement = (data.get("announcement") or "")[:300]
            task.save()
            from . import sections as _sections  # local import: sections builds on this module
            _sections.file_task(task, f"{task.title} {task.notes}", giver=req.requested_by, person=md,
                                said=data.get("section") or "")
            log_event(task, TaskEvent.Kind.REQUESTED, req.requested_by, by=md.id, **_sections.section_data(task))
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
                old_status, old_due = task.status, (task.due_at, task.due_date)
                _apply_changes(task, data)
                task.save()
                _log_changes(req.requested_by, task, old_status, old_due)
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


def staff_qs():
    """The TaskPanel staff directory, only while it is switched on (settings.JARVIS_TASKPANEL_STAFF). Off by default:
    until the real TaskPanel is connected it holds only test people, and the team is everyone with a JARVIS account."""
    if not settings.JARVIS_TASKPANEL_STAFF:
        return StaffMember.objects.none()
    return StaffMember.objects.filter(active=True)


def categories_qs():
    return TaskCategory.objects.filter(active=True) if settings.JARVIS_TASKPANEL_STAFF else TaskCategory.objects.none()


def resolve_staff(name: str) -> tuple[list[StaffMember], list[StaffMember]]:
    """Return (matches, suggestions) from the live directory.

    Only an exact name or whole-word match counts as a match ("QTC Developer" never matches
    "QTC Developer2"). A partial match ("Ram" -> Ramesh) is only a suggestion the user must confirm.
    """
    norm = " ".join((name or "").lower().split())
    if not norm:
        return [], []
    active = list(staff_qs())
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
    if not user.is_active:
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
        from . import sections as _sections  # local import: sections builds on this module
        _sections.file_task(task, f"{task.title} {task.description}", giver=user, said=data.get("section") or "")
        log_event(task, TaskEvent.Kind.DELEGATED, user, staff=staff.name, **_sections.section_data(task))
        command_id = uuid.uuid4()
        payload = build_create_command(task, str(command_id))
        command = IntegrationCommand.objects.create(
            id=command_id, actor=user, type="task.create", task=task, payload=payload,
            payload_hash=canonical_hash(payload["data"]), next_attempt_at=timezone.now(),
        )
        audit(user, "delegation.create", "shared_task", task.id, f"to staff {staff.external_id}, command {command_id}")
    command = tp_dispatcher.dispatch(command.id)
    task.refresh_from_db()
    if command.state != IntegrationCommand.State.REJECTED and not user.is_md:
        md = get_md()
        if md:
            Alert.objects.create(
                recipient=md, sender=user, kind=Alert.Kind.INFO, priority=Priority.LOW,
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
    active = [s for s in staff_qs() if s.external_id not in own_actor_keys()]
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
    for person in staff_qs().filter(id__in=staff_ids).order_by("name"):
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
    """The person answered yes to "shall I tell <giver> you've seen it?": one short note for whoever gave the task
    or sent the message. Spoken when it matters (urgent / high), otherwise it only sits in their lists. Never raised
    twice for the same alert."""
    if alert.recipient_id != user.id:
        return None
    if not (alert.kind == Alert.Kind.TASK_ASSIGNED or (alert.kind == Alert.Kind.INFO and alert.ref_type == "message")):
        return None  # only a task given or a message sent is worth a "seen" (never a note about a note)
    task = SharedTask.objects.filter(id=alert.ref_id).select_related("created_by").first() \
        if alert.ref_type == "shared_task" and alert.ref_id else None
    giver = alert.sender or (task.created_by if task else None)
    if giver is None and alert.kind == Alert.Kind.TASK_ASSIGNED:
        giver = get_md()
    if giver is None or giver.id == user.id or not giver.is_active:
        return None
    existing = Alert.objects.filter(recipient=giver, ref_type="seen", ref_id=str(alert.id)).first()
    if existing:
        return existing
    title = task.title if task else (alert.body or alert.title)
    # Spoken when it matters: urgent or important work, or the sender was already told it had NOT been seen.
    loud = (bool(task and task.priority in (Priority.URGENT, Priority.HIGH))
            or alert.priority in (Priority.URGENT, Priority.HIGH) or alert.unseen_told_at is not None)
    to, to_hi = address_of(giver), address_hi_of(giver)
    now_word, now_word_hi = (" now", " अब") if alert.unseen_told_at else ("", "")
    return Alert.objects.create(
        recipient=giver, sender=user, kind=Alert.Kind.INFO, priority=Priority.NORMAL if loud else Priority.LOW,
        title=f"{user.spoken_name} has seen it", body=title[:400],
        spoken_text=f"{to}, {user.spoken_name} has{now_word} seen: {title}."[:400],
        spoken_text_hi=f"{to_hi}, {user.spoken_name} ने{now_word_hi} देख लिया: {title}।"[:400],
        ref_type="seen", ref_id=str(alert.id)[:64],
    )


# --------------------------------------------------------------------------- ring until seen
# Work given and messages sent by a person ring the receiver's phone every few minutes until they tap Seen (or open
# it, or answer JARVIS about it). The ringing itself runs in core/ringing.py; the rules for one person live here.
def quiet_until(prefs: dict, at: datetime) -> datetime | None:
    """The end of the person's quiet hours when `at` falls inside them (a non-urgent ring waits until then), else None."""
    if not prefs.get("quietHoursEnabled", True):
        return None
    try:
        start, end = int(prefs.get("quietStartHour", 22)), int(prefs.get("quietEndHour", 7))
    except (TypeError, ValueError):
        return None
    if start == end:
        return None
    local = tu.to_local(at)
    inside = start <= local.hour < end if start < end else (local.hour >= start or local.hour < end)
    if not inside:
        return None
    out = local.replace(hour=end, minute=0, second=0, microsecond=0)
    return out if out > local else out + timedelta(days=1)


def _minutes_pref(prefs: dict, key: str, default: int) -> int:
    try:
        return max(1, int(prefs.get(key) or default))
    except (TypeError, ValueError):
        return default


def ring_every(prefs: dict, priority: str) -> timedelta:
    """How long until the next ring: every minute for urgent work, every five otherwise (the receiver's settings)."""
    if priority == Priority.URGENT:
        return timedelta(minutes=_minutes_pref(prefs, "ringUrgentEveryMinutes", 1))
    return timedelta(minutes=_minutes_pref(prefs, "ringEveryMinutes", 5))


def ring_for(prefs: dict) -> timedelta:
    """How long it rings before the sender is told it was not seen."""
    return timedelta(minutes=_minutes_pref(prefs, "ringForMinutes", 30))


def ring_fields(person: User, priority: str, start: datetime | None = None) -> dict:
    """How a new alert for this person rings. Not at all when they switched ringing off, or for "whenever you get
    time" (LOW). Otherwise the alert itself is the first ring and the next follows after their interval; in their
    quiet hours a non-urgent one waits for the quiet hours to end; a message held until later rings when it comes."""
    prefs = get_prefs(person)
    if not prefs.get("ringUntilSeen", True) or priority == Priority.LOW:
        return {"ring": False}
    now = timezone.now()
    at = start if start and start > now else now
    hold = quiet_until(prefs, at) if priority != Priority.URGENT else None
    if hold:
        return {"ring": True, "rings": 0, "ring_started_at": None, "next_ring_at": hold}
    if at > now:
        return {"ring": True, "rings": 0, "ring_started_at": None, "next_ring_at": at}
    return {"ring": True, "rings": 1, "ring_started_at": now, "next_ring_at": now + ring_every(prefs, priority)}


def _seen_everywhere(user: User, task: SharedTask, now: datetime, *, close: bool) -> None:
    """Everything this person still has open about the task counts as seen (and, with close, as dealt with)."""
    qs = Alert.objects.filter(recipient=user, ref_type="shared_task", ref_id=str(task.id))
    ids = [str(i) for i in qs.values_list("id", flat=True)]
    qs.filter(seen_at__isnull=True).update(seen_at=now)
    if close:
        qs.filter(acknowledged_at__isnull=True).update(acknowledged_at=now)
    # A "not seen yet" the giver still has waiting about it is out of date now.
    Alert.objects.filter(ref_type="unseen", ref_id__in=ids, acknowledged_at__isnull=True).update(acknowledged_at=now)


def mark_seen(user: User, alert: Alert, *, tell: bool = True) -> Alert | None:
    """The person saw this alert (tapped Seen, opened it, or answered JARVIS about it). The ringing stops on all their
    phones (core/ringing.py sends the stop), the work item shows it was seen (and its history says so), a "not seen
    yet" the sender may still have waiting goes away, and the sender is told, unless tell=False ("no, don't tell").
    Seeing it again changes nothing. Returns the note to the sender, if there is one."""
    if alert.recipient_id != user.id:
        return None
    now = timezone.now()
    if alert.seen_at is None:
        alert.seen_at = now
        alert.delivered_at = alert.delivered_at or now
        Alert.objects.filter(id=alert.id).update(seen_at=alert.seen_at, delivered_at=alert.delivered_at)
        Alert.objects.filter(ref_type="unseen", ref_id=str(alert.id), acknowledged_at__isnull=True).update(acknowledged_at=now)
        if alert.ref_type == "message":
            # The kept record of the message says this person saw it (the alert itself expires).
            MessageReceipt.objects.filter(message_id=alert.ref_id, recipient=user, seen_at__isnull=True) \
                .update(seen_at=now, delivered_at=alert.delivered_at)
        task = task_of(alert)
        if task is not None and task.assignee_user_id == user.id and task.seen_at is None:
            SharedTask.objects.filter(id=task.id).update(seen_at=now)  # not a change to the task: no new version
            log_event(task, TaskEvent.Kind.SEEN, user)
    return tell_sender_seen(user, alert) if tell else None


def ring_again(by: User, alert: Alert) -> str:
    """The sender asks to ring again after "not seen yet": the ringing starts afresh, for its full time. Returns
    "rang", "seen" (seen meanwhile, nothing to ring), "closed" (done or withdrawn) or "off" (they switched ringing off)."""
    if alert.sender_id != by.id:
        raise ServiceError("forbidden", "only the person who sent it can ring again.", 403)
    if alert.seen_at:
        return "seen"
    if alert.acknowledged_at:
        return "closed"
    if not get_prefs(alert.recipient).get("ringUntilSeen", True):
        return "off"
    now = timezone.now()
    Alert.objects.filter(id=alert.id).update(ring=True, ring_started_at=None, next_ring_at=now, unseen_told_at=None)
    task = task_of(alert)
    if task is not None:
        log_event(task, TaskEvent.Kind.RING_AGAIN, by, to=alert.recipient_id)
    audit(by, "alert.ring_again", "alert", alert.id)
    return "rang"


def _note_priority(task: SharedTask) -> str:
    return task.priority if task.priority in (Priority.URGENT, Priority.HIGH) else Priority.NORMAL


@transaction.atomic
def record_promise(user: User, task_id, day: date, at: datetime | None = None) -> SharedTask:
    """The person a task was given to says by when they will do it ("kal shaam tak"). It is kept on the task (and
    becomes its deadline when it had none, so they are reminded), written in its history, and the giver hears it,
    in one line with "has seen it" when this answer is the first sign that they saw it."""
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_user=user)
    if task.is_closed:
        raise ServiceError("closed", "this task is already closed.", 409)
    now = timezone.now()
    first_look = task.seen_at is None
    task.promise_date, task.promise_at = day, at
    task.seen_at = task.seen_at or now
    if task.due_kind == DueKind.NONE:
        task.due_kind = DueKind.TIMED if at else DueKind.DATE
        task.due_date, task.due_at = (tu.to_local(at).date(), at) if at else (day, None)
    task.version += 1
    task.save()
    label = tu.due_phrase(task.promise_kind, task.promise_date, task.promise_at)
    label_hi = tu.due_phrase_hi(task.promise_kind, task.promise_date, task.promise_at)
    if first_look:
        log_event(task, TaskEvent.Kind.SEEN, user)
    log_event(task, TaskEvent.Kind.PROMISED, user, label=label, label_hi=label_hi)
    _seen_everywhere(user, task, now, close=True)
    giver = task.created_by
    if giver is not None and giver.id != user.id and giver.is_active:
        name, to, to_hi = user.spoken_name, address_of(giver), address_hi_of(giver)
        if first_look:
            en = f"{to}, {name} has seen {task.title} and will do it by {label}."
            hi = f"{to_hi}, {name} ने {task.title} देख लिया है, {label_hi} तक कर देंगे।"
        else:
            en = f"{to}, {name} will do {task.title} by {label}."
            hi = f"{to_hi}, {name} {task.title} {label_hi} तक कर देंगे।"
        Alert.objects.create(recipient=giver, sender=user, kind=Alert.Kind.INFO, priority=_note_priority(task),
                             title=f"{name} will do it by {label}"[:200], body=task.title, spoken_text=en[:400],
                             spoken_text_hi=hi[:400], ref_type="shared_task", ref_id=str(task.id))
    audit(user, "task.promise", "shared_task", task.id, label)
    return task


@transaction.atomic
def record_cannot(user: User, task_id, reason: str = "") -> SharedTask:
    """The person a task was given to says they can't do it ("nahi ho payega, bank band hai"). It goes on hold with
    the reason, the history keeps it, and the giver hears it and is asked whether to give it to someone else."""
    task = SharedTask.objects.select_for_update().get(id=task_id, assignee_user=user)
    if task.is_closed:
        raise ServiceError("closed", "this task is already closed.", 409)
    now = timezone.now()
    reason = " ".join(str(reason or "").split())[:280]
    first_look = task.seen_at is None
    task.status = Status.BLOCKED
    task.hold_reason = reason or "Can't do it"
    task.seen_at = task.seen_at or now
    task.version += 1
    task.save()
    if first_look:
        log_event(task, TaskEvent.Kind.SEEN, user)
    log_event(task, TaskEvent.Kind.CANNOT, user, reason=reason)
    _seen_everywhere(user, task, now, close=True)
    giver = task.created_by
    if giver is not None and giver.id != user.id and giver.is_active:
        name, to, to_hi = user.spoken_name, address_of(giver), address_hi_of(giver)
        en = f"{to}, {name} can't do {task.title}." + (f" Reason: {reason}." if reason else "")
        hi = f"{to_hi}, {name} {task.title} नहीं कर पाएँगे।" + (f" कारण: {reason}।" if reason else "")
        Alert.objects.create(
            recipient=giver, sender=user, kind=Alert.Kind.INFO, priority=_note_priority(task),
            title=f"{name} can't do it", body=task.title, spoken_text=en[:400], spoken_text_hi=hi[:400],
            ref_type="shared_task", ref_id=str(task.id),
            question={"kind": "reassign", "en": "Shall I give it to someone else?", "hi": "किसी और को दे दूँ?",
                      "task": str(task.id)},
        )
    audit(user, "task.cannot", "shared_task", task.id)
    return task


@transaction.atomic
def reassign_task(by: User, task_id, person: User) -> SharedTask:
    """Give a task to someone else instead (after "can't do it" or "not seen yet", or any time): only whoever gave it,
    or the MD. It starts afresh with the new person (rings until seen, asks them by when); the one who had it is told
    and its alerts go quiet; the history keeps both."""
    task = SharedTask.objects.select_for_update().get(id=task_id)
    if not (by.is_md or task.created_by_id == by.id):
        raise ServiceError("forbidden", "only whoever gave it can give it to someone else.", 403)
    if task.assignee_type != SharedTask.Assignee.USER or task.origin not in GIVEN_ORIGINS + MEET_ORIGINS:
        raise ServiceError("invalid", "only work given to someone on the team can be given to someone else.", 422)
    if task.is_closed:
        raise ServiceError("closed", "this task is already closed.", 409)
    if person is None or not person.is_active:
        raise ServiceError("no_person", "that person's account is switched off.", 422)
    if person.is_md and not by.is_md:
        raise ServiceError("use_request", f"work for {md_address()} goes to {md_pronouns()[1]} as a request.", 422)
    if person.id == by.id:
        raise ServiceError("self", "say who should get it instead.", 422)
    old = task.assignee_user
    if old is not None and old.id == person.id:
        raise ServiceError("same", f"it is already with {person.spoken_name}.", 422)
    now = timezone.now()
    old_alert_ids = [str(i) for i in Alert.objects.filter(ref_type="shared_task", ref_id=str(task.id)).values_list("id", flat=True)]
    # Everything still open about it, for anybody (the old person's ringing, the giver's "can't do it"), is settled.
    Alert.objects.filter(ref_type="shared_task", ref_id=str(task.id), acknowledged_at__isnull=True).update(acknowledged_at=now)
    Alert.objects.filter(ref_type="unseen", ref_id__in=old_alert_ids, acknowledged_at__isnull=True).update(acknowledged_at=now)
    task.assignee_user = person
    task.assignee_type = bucket_of(person)
    task.status, task.hold_reason, task.completed_at = Status.OPEN, "", None
    task.seen_at, task.promise_date, task.promise_at = None, None, None
    task.version += 1
    task.save()
    log_event(task, TaskEvent.Kind.REASSIGNED, by, to=person.id, **({"from": old.id} if old else {}))
    who, who_hi = giver_words(by)
    name = person.spoken_name
    if old is not None and old.id != by.id and old.is_active:
        Alert.objects.create(
            recipient=old, sender=by, kind=Alert.Kind.INFO, priority=Priority.NORMAL,
            title=f"{who} gave your task to {name}"[:200], body=task.title,
            spoken_text=f"{old.spoken_name}, {who} has given {task.title} to {name} instead."[:400],
            spoken_text_hi=f"{old.spoken_name}, {who_hi} ने {task.title} अब {name} को दे दिया है।"[:400],
            ref_type="reassigned", ref_id=str(task.id),
        )
    when = tu.due_phrase(task.due_kind, task.due_date, task.due_at)
    when_hi = tu.due_phrase_hi(task.due_kind, task.due_date, task.due_at)
    if task.kind == Kind.MEETING:
        title = f"Meeting with {who}"
        en = f"{name}, {who} has scheduled a meeting with you" + (f" {when}" if when else "") + f": {task.title}."
        hi = f"{name}, {who_hi} ने आपके साथ मीटिंग रखी है" + (f" {when_hi}" if when_hi else "") + f": {task.title}।"
    else:
        pr_en, pr_hi = _priority_words(task.priority)
        title = f"New task from {who}"
        en = f"{name}, {who} has given you a new {pr_en}task: {task.title}" + (f", {when}" if when else "") + "."
        hi = f"{name}, {who_hi} ने आपको एक नया {pr_hi}टास्क दिया है: {task.title}" + (f", {when_hi}" if when_hi else "") + "।"
    Alert.objects.create(
        recipient=person, sender=by, kind=Alert.Kind.TASK_ASSIGNED, priority=task.priority, title=title,
        body=en[:400], spoken_text=en[:400], spoken_text_hi=hi[:400], ref_type="shared_task", ref_id=str(task.id),
        **ring_fields(person, task.priority),
    )
    audit(by, "task.reassign", "shared_task", task.id, f"to user {person.id}")
    return task


# --------------------------------------------------------------------------- a task's history
def log_event(task, kind: str, actor: User | None = None, **data) -> None:
    """One step in a work item's history (see TaskEvent). Never stops the work itself: a failure is only logged."""
    if not isinstance(task, SharedTask) or task.pk is None:
        return
    try:
        with transaction.atomic():
            TaskEvent.objects.create(task_id=task.pk, kind=kind, actor=actor, data=jsonable(data))
    except Exception:  # noqa: BLE001 — the history must never undo the work it describes
        log.exception("task history step not recorded")


_STATUS_EVENT = {Status.IN_PROGRESS: TaskEvent.Kind.STARTED, Status.BLOCKED: TaskEvent.Kind.HOLD,
                 Status.COMPLETED: TaskEvent.Kind.DONE, Status.CANCELLED: TaskEvent.Kind.CANCELLED,
                 Status.OPEN: TaskEvent.Kind.REOPENED}


def _log_changes(actor: User, task: SharedTask, old_status: str, old_due) -> None:
    """The steps a change made: a new status, a new time."""
    if task.status != old_status:
        kind = _STATUS_EVENT[task.status]
        extra = {"reason": task.hold_reason} if kind == TaskEvent.Kind.HOLD and task.hold_reason else {}
        if kind == TaskEvent.Kind.DONE:
            # The deadline it had when it was finished, so a later move never changes whether it was on time.
            extra["due"] = (task.due_at or task.due_date).isoformat() if (task.due_at or task.due_date) else ""
            extra["due_kind"] = task.due_kind
        log_event(task, kind, actor, **extra)
    if (task.due_at, task.due_date) != tuple(old_due) and task.status != Status.CANCELLED:
        log_event(task, TaskEvent.Kind.MOVED, actor,
                  label=tu.due_phrase(task.due_kind, task.due_date, task.due_at) or "no fixed time",
                  label_hi=tu.due_phrase_hi(task.due_kind, task.due_date, task.due_at) or "बिना तय समय")


def _person_words(u: User | None) -> tuple[str, str]:
    if u is None:
        return "someone", "किसी"
    return (md_address(), md_address_hi()) if u.is_md else (u.spoken_name, u.spoken_name)


def _history_text(kind: str, actor: User | None, data: dict, task: SharedTask, people: dict) -> tuple[str, str]:
    a, a_hi = _person_words(actor)
    to, to_hi = _person_words(people.get(data.get("to")))
    frm, frm_hi = _person_words(people.get(data.get("from")))
    label, label_hi = data.get("label") or "", data.get("label_hi") or data.get("label") or ""
    reason = data.get("reason") or ""
    K = TaskEvent.Kind
    sec = data.get("section") or ""
    in_sec, in_sec_hi = (f" · in {sec}", f" · {sec} में") if sec else ("", "")
    if kind == K.GIVEN:
        if task.kind == Kind.MEETING:
            return f"Meeting set by {a} with {to}{in_sec}", f"{a_hi} ने {to_hi} के साथ मीटिंग रखी{in_sec_hi}"
        return f"Given by {a} to {to}{in_sec}", f"{a_hi} ने {to_hi} को दिया{in_sec_hi}"
    if kind == K.ADDED:
        return f"Added by {a}{in_sec}", f"{a_hi} ने जोड़ा{in_sec_hi}"
    if kind == K.REQUESTED:
        return (f"Asked for by {a}, accepted by {md_address()}{in_sec}", f"{a_hi} की रिक्वेस्ट, {md_address_hi()} ने मंज़ूर की{in_sec_hi}")
    if kind == K.DELEGATED:
        staff = data.get("staff") or "staff"
        return f"Given by {a} to {staff} (staff){in_sec}", f"{a_hi} ने {staff} को दिया (स्टाफ़){in_sec_hi}"
    if kind == K.SEEN:
        return f"Seen by {a}", f"{a_hi} ने देख लिया"
    if kind == K.PROMISED:
        return f"{a}: will do it by {label}", f"{a_hi}: {label_hi} तक कर देंगे"
    if kind == K.CANNOT:
        return (f"{a} can't do it" + (f": {reason}" if reason else ""),
                f"{a_hi} नहीं कर पाएँगे" + (f"। कारण: {reason}" if reason else ""))
    if kind == K.NOT_SEEN:
        m = data.get("minutes") or 0
        return f"Not seen by {to} after {m} minutes of ringing", f"{to_hi} ने {m} मिनट रिंग के बाद भी नहीं देखा"
    if kind == K.RING_AGAIN:
        return f"{a} rang {to} again", f"{a_hi} ने {to_hi} को फिर से रिंग किया"
    if kind == K.REASSIGNED:
        if data.get("from"):
            return f"{a} gave it to {to} instead of {frm}", f"{a_hi} ने {frm_hi} की जगह {to_hi} को दिया"
        return f"{a} gave it to {to}", f"{a_hi} ने {to_hi} को दिया"
    if kind == K.MOVED:
        return f"Moved to {label} by {a}", f"{a_hi} ने {label_hi} पर किया"
    if kind == K.STARTED:
        return f"Started by {a}", f"{a_hi} ने शुरू किया"
    if kind == K.HOLD:
        return (f"Put on hold by {a}" + (f": {reason}" if reason else ""),
                f"{a_hi} ने होल्ड पर रखा" + (f"। कारण: {reason}" if reason else ""))
    if kind == K.DONE:
        return (f"Done by {a}", f"{a_hi} ने पूरा किया") if actor else ("Done", "पूरा हुआ")
    if kind == K.REOPENED:
        return f"Reopened by {a}", f"{a_hi} ने फिर से खोला"
    if kind == K.CANCELLED:
        return f"Cancelled by {a}", f"{a_hi} ने कैंसल किया"
    if kind == K.FILED:
        if data.get("from"):
            return f"Moved from {data['from']} to {sec} by {a}", f"{a_hi} ने {data['from']} से {sec} में रखा"
        return f"Put in {sec} by {a}", f"{a_hi} ने {sec} में रखा"
    return kind.title(), kind.title()


def task_history(task: SharedTask) -> list[dict]:
    """The task's steps, oldest first, each as a short line in English and Hindi with its time. A task older than the
    history gets its start (and its finish) from the task itself."""
    K = TaskEvent.Kind
    steps = [(e.kind, e.at, e.actor, e.data or {}) for e in task.events.select_related("actor")]
    if not any(k in (K.GIVEN, K.ADDED, K.REQUESTED, K.DELEGATED) for k, *_ in steps):
        if task.assignee_type == SharedTask.Assignee.STAFF:
            start = (K.DELEGATED, {"staff": task.staff.name if task.staff_id else ""})
        elif task.origin == SharedTask.Origin.PA_REQUEST:
            start = (K.REQUESTED, {})
        elif task.origin in OWN_ORIGINS or task.created_by_id == task.assignee_user_id:
            start = (K.ADDED, {})
        else:
            start = (K.GIVEN, {"to": task.assignee_user_id})
        steps.insert(0, (start[0], task.created_at, task.created_by, start[1]))
    if task.status == Status.COMPLETED and task.completed_at and not any(k == K.DONE for k, *_ in steps):
        steps.append((K.DONE, task.completed_at, None, {}))
    ids = {d.get(k) for _, _, _, d in steps for k in ("to", "from") if isinstance(d.get(k), int)}
    people = {u.id: u for u in User.objects.filter(id__in=ids)}
    out = []
    for kind, at, actor, data in steps:
        en, hi = _history_text(kind, actor, data, task, people)
        local = tu.to_local(at)
        out.append({"kind": kind, "at": at.isoformat(), "text": en, "text_hi": hi,
                    "at_label": f"{tu.fmt_day(local.date())}, {tu.fmt_time(local)}",
                    "at_label_hi": f"{tu.fmt_day_hi(local.date())} {tu.fmt_time_hi(local)}"})
    return out


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
    """Facts the AI may use. Strictly scoped: a context holds only the speaker's own personal items (never anyone
    else's) and only the work the speaker may see.

    include_private=False (phone locked, talking to the background listener): the speaker's own personal items are
    left out as well, so nothing private is spoken to whoever is holding a locked phone.
    """
    now = tu.now_local()
    tp_sync.ensure_directory(user)
    staff = [{"id": s.external_id, "name": s.name, "department": s.department, "on_leave": s.on_leave}
             for s in staff_qs()[:80]]
    categories = [{"key": c.key, "name": c.name, "department": c.department}
                  for c in categories_qs()[:100]]
    ctx = {
        "now_india": now.isoformat(),
        "weekday": now.strftime("%A"),
        "speaker_role": user.role,
        "speaker_name": user.spoken_name,
        "speaker_title": user.title_label,
        # Everyone else on the team with a JARVIS account: work and messages go to them by name or by job.
        "team": [{"name": u.spoken_name, "title": u.title_label, "department": u.department,
                  "gender": gender_of(u), **({"is_md": True} if u.is_md else {})}
                 for u in team() if u.id != user.id],
        "staff": staff,
        "task_categories": categories,
        "default_category": settings.TASKPANEL_DEFAULT_CATEGORY,
    }
    # Departments (groups of people) and sections (folders of work), for organising by voice and asking by them.
    from . import sections as _sections  # local import: sections builds on this module
    ctx.update(_sections.ai_lists(user))
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
        ctx["note"] = ("The phone is locked, so the speaker's personal items are not in this list. If asked about them, "
                       "say they will be available once the phone is unlocked.")
    ctx["delegated"] = [brief(i) for i in dash["delegated"][:10]]
    pa, mg = get_pa(), get_manager()
    ctx["pa_name"] = pa.spoken_name if pa else ""
    ctx["manager_name"] = mg.spoken_name if mg else ""
    # Work the speaker gave others, with its progress ("Sunil ne bank wala kaam kiya?").
    ctx["tasks_i_gave"] = [brief(i) | {"status_label": i["status_label"]} for i in dash.get("given_tasks", [])[:15]]
    if user.is_md:
        # How much open work each person has (the MD sees all work); the lists themselves come from the views.
        load: dict[str, int] = {}
        for t in team_work_qs().exclude(status__in=[Status.COMPLETED, Status.CANCELLED]):
            if t.assignee_user_id:
                load[t.assignee_user.spoken_name] = load.get(t.assignee_user.spoken_name, 0) + 1
        ctx["team_open_work"] = load
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
    ctx["speaker_gender"] = gender_of(user)
    md_user = get_md()
    ctx["md_name"] = md_user.spoken_name if md_user else ""
    closed = (Status.COMPLETED, Status.CANCELLED)
    # Work the speaker is WAITING on from others (given to colleagues, and to TaskPanel staff).
    ctx["pending_summary"]["given_to_others_open"] = len([t for t in dash.get("given_tasks", []) if t["status"] not in closed])
    if user.is_md:
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
    "managerName": {"type": "text", "default": "", "say": "the manager's name", "hint": "the manager's name (MD only)", "virtual": True, "md_only": True},
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
    # Ring until seen: work given and messages sent by someone on the team ring this person's phone ("ting ting")
    # every few minutes until they tap Seen; after a while the sender is told it was not seen. These are the
    # receiver's settings (it is their phone that rings). Quiet hours hold a non-urgent ring until they are over.
    "ringUntilSeen": {"type": "bool", "default": True, "say": "ring until I tap Seen (work and messages from the team)"},
    "ringEveryMinutes": {"type": "int", "min": 2, "max": 30, "default": 10, "say": "ring again every (minutes) until seen"},
    "ringUrgentEveryMinutes": {"type": "int", "min": 1, "max": 10, "default": 1,
                               "say": "urgent work: ring again every (minutes) until seen"},
    "ringForMinutes": {"type": "int", "min": 5, "max": 240, "default": 30,
                       "say": "ring for (minutes) before the sender is told it was not seen"},
    # Walkie-talkie: the person's own switch, their quiet time (set by voice: "walkie quiet for 30 minutes"), and
    # the MD's rule on who may start a talk.
    "walkieEnabled": {"type": "bool", "default": True, "say": "walkie-talkie"},
    "walkieQuietUntil": {"type": "text", "default": "", "say": "walkie-talkie quiet until", "hidden": True},
    "walkieAnyoneStarts": {"type": "bool", "default": True, "say": "anyone may start a walkie-talkie talk (off = only the MD)", "md_only": True},
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
              "urgentRepeatMinutes", "digestTime", "staleStaffDays", "requestWaitHours", "ringEveryMinutes",
              "ringUrgentEveryMinutes", "ringForMinutes")


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
    add("ringEveryMinutes", "Work and messages ring again every", f"{prefs['ringEveryMinutes']} minutes until seen",
        "a rule you can change")
    add("ringUrgentEveryMinutes", "Urgent ones ring again every", f"{prefs['ringUrgentEveryMinutes']} minutes until seen",
        "a rule you can change")
    add("ringForMinutes", "The sender hears “not seen” after", f"{prefs['ringForMinutes']} minutes of ringing",
        "a rule you can change")
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
    if "walkieQuiet" in patch:
        # "walkie quiet for 30 minutes" -> quiet until then; "off" / 0 -> quiet over.
        raw = str(patch.pop("walkieQuiet")).strip().lower()
        nums = re.findall(r"\d{1,4}", raw)
        minutes = int(nums[0]) if nums else 0
        if "hour" in raw or "ghant" in raw or "घंट" in raw:
            minutes = (int(nums[0]) if nums else 1) * 60
        if raw in _FALSE_WORDS or raw in ("hatao", "hata do", "khatam", "over") or minutes <= 0:
            patch["walkieQuietUntil"] = ""
        else:
            patch["walkieQuietUntil"] = (timezone.now() + timedelta(minutes=min(minutes, 24 * 60))).isoformat()
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
    now = now or timezone.now()
    ends = [datetime.fromisoformat(w["end"]) for w in busy_windows(now.astimezone(tu.IST).date(), person=user)
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


MESSAGE_MAX_CHARS = 3000  # a long message (a few minutes of speaking) is delivered whole


def names_phrase(people: list[User], hindi: bool = False) -> str:
    """"Sunil", "Sunil and Mansi", "Sunil, Mansi and Himanshu" (the MD by the form of address)."""
    names = [md_address_hi() if (u.is_md and hindi) else md_address() if u.is_md else u.spoken_name for u in people]
    if len(names) <= 1:
        return names[0] if names else ""
    joiner = " और " if hindi else " and "
    return ", ".join(names[:-1]) + joiner + names[-1]


def message_recipients(sender: User, to: str = "") -> list[User]:
    """Who a message is for: the person named or their job ("Sunil", "accountant", "Sir", "PA"), a group
    ("accounts team", "sab ko"), else the usual one (the MD's message goes to the PA; anyone else's to the MD)."""
    if (to or "").strip():
        found = find_people(to, exclude=sender)
        if found["status"] in ("known", "close"):
            return found["people"]
        if found["status"] == "ambiguous":
            raise ServiceError("ambiguous", f"which {to}: {' or '.join(found['options'])}?", 422)
        group, _label = find_group(to, exclude=sender)
        if group:
            return group
        hint = f" Did you mean {found['options'][0]}?" if found["options"] else ""
        raise ServiceError("no_recipient", f"I don't know anyone called {to} on the team.{hint}", 422)
    default = get_pa() if sender.is_md else get_md()
    return [default] if default is not None and default.id != sender.id else []


def message_recipient(sender: User, to: str = "") -> User | None:
    """The first person a message is for (see message_recipients)."""
    try:
        people = message_recipients(sender, to)
    except ServiceError:
        return None
    return people[0] if people else None


def send_message_to(sender: User, people: list[User], text: str, *, when=None,
                    priority: str = Priority.NORMAL, kind: str = "message") -> list[Alert]:
    """A message (never a task: nothing to tick off) to one or more people on the team, delivered now or held until
    `when` (out of sight until then, announced at that moment). Each person gets their own alert, from the sender,
    and is asked once it is spoken whether to tell the sender it was seen. Returns the alerts made."""
    text = " ".join(str(text or "").split())[:MESSAGE_MAX_CHARS]
    if not text:
        raise ServiceError("invalid", "what should the message say?", 422)
    people = [u for u in people if u is not None and u.id != sender.id and u.is_active]
    if not people:
        raise ServiceError("no_recipient", "who should get the message?", 422)
    from_en, from_hi = giver_words(sender)
    priority = priority if priority in Priority.values else Priority.NORMAL
    since = timezone.now() - timedelta(minutes=3)
    people = [p for p in people
              if not Alert.objects.filter(recipient=p, ref_type="message", body=text, created_at__gte=since).exists()]
    if not people:
        return []  # the same message, sent moments ago (heard twice): not sent twice
    # The message itself is kept for good (Message + a receipt per person); the alerts below are its news.
    message = Message.objects.create(sender=sender, text=text, priority=priority, kind=kind, deliver_at=when)
    made = []
    for person in people:
        MessageReceipt.objects.create(message=message, recipient=person)
        to_addr, to_addr_hi = address_of(person), address_hi_of(person)
        made.append(Alert.objects.create(
            recipient=person, sender=sender, kind=Alert.Kind.INFO, priority=priority,
            title=f"Message from {from_en}"[:200], body=text,
            spoken_text=f"{to_addr}, a message from {from_en}: {text}",
            spoken_text_hi=f"{to_addr_hi}, {from_hi} का संदेश: {text}",
            ref_type="message", ref_id=str(message.id), snoozed_until=when,
            question={"kind": "seen", "en": f"Shall I tell {from_en} you've seen it?",
                      "hi": f"{from_hi} को बता दूँ कि आपने देख लिया?"},
            **ring_fields(person, priority, start=when),
        ))
        audit(sender, "message.send", "user", person.id, "later" if when else "now")
    return made


def send_message(sender: User, text: str, *, when=None, priority: str = Priority.NORMAL, to: str = "") -> str:
    """A message to the person (or group) named in `to`. Returns their names as said ("Sunil and Mansi")."""
    people = message_recipients(sender, to)
    if not people:
        raise ServiceError("no_recipient", "who should get the message?", 422)
    send_message_to(sender, people, text, when=when, priority=priority)
    return names_phrase(people)


def set_names(user: User, names: dict) -> list[str]:
    """Names by voice, from either phone: the speaker's own (myName) and, from the MD, the PA's (paName). The other
    person is told in one short note, and every phone re-reads the names from the server. Returns what changed."""
    changed = []
    pa = get_pa()
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
        elif key == "managerName":
            if not user.is_md:
                raise ServiceError("forbidden", f"Only {md_address()} can set the manager's name.", 403)
            target = get_manager()
            if target is None:
                raise ServiceError("invalid", "There is no manager account yet.", 422)
        else:
            continue
        if target.spoken_name == name:
            continue
        _rename(user, target, name)
        changed.append(name)
    return changed


def _rename(by: User, target: User, name: str) -> None:
    """Give a person a new name; whoever needs to know is told in one short note: the person renamed by someone
    else, the MD when a team member renames themselves, and the PA when the MD changes his own name."""
    old = target.spoken_name
    target.display_name = name
    if not target.is_md:
        target.first_name = name
    target.save(update_fields=["display_name", "first_name"])
    audit(by, "name.set", "user", target.id, f"{old} -> {name}")
    notes = []
    if target.id != by.id:
        notes.append((target, f"{name}, {giver_words(by)[0]} has set your name to {name}.",
                      f"{name}, {giver_words(by)[1]} ने आपका नाम {name} रखा है।", Priority.NORMAL, "Your name"))
    md, pa = get_md(), get_pa()
    if target.is_md and pa is not None and pa.id != by.id:
        notes.append((pa, f"{pa.spoken_name}, {md_address()} has asked me to use the name {name} from now.",
                      f"{pa.spoken_name}, {md_address_hi()} ने कहा है कि अब से {name} नाम इस्तेमाल करूँ।", Priority.LOW,
                      "A name has changed"))
    elif not target.is_md and md is not None and md.id != by.id:
        notes.append((md, f"{md_address()}, {old} will be called {name} from now.",
                      f"{md_address_hi()}, {old} को अब से {name} कहा जाएगा।", Priority.LOW, "A name has changed"))
    for to, en, hi, priority, title in notes:
        Alert.objects.create(recipient=to, sender=by, kind=Alert.Kind.INFO, priority=priority, title=title,
                             body=en, spoken_text=en, spoken_text_hi=hi, ref_type="profile", ref_id=str(target.id))


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
    if "paGender" in clean and user.is_md:
        # The PA's gender lives on the PA's own account now (the Team page sets everyone's); this setting keeps it.
        pa = get_pa()
        if pa is not None and clean["paGender"] in ("male", "female", "unset"):
            pa.gender = "" if clean["paGender"] == "unset" else clean["paGender"]
            pa.save(update_fields=["gender"])
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
    # Someone on the team (by name or by job: "Sunil", "accountant", "PA", "manager").
    on_team = find_people(name)
    if on_team["status"] in ("known", "close"):
        p = on_team["people"][0]
        return {**known, "who": f"{p.spoken_name} ({p.title_label})"}
    if on_team["status"] == "ambiguous":
        return {"status": "ambiguous", "who": "", "suggestions": [], "options": on_team["options"]}
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
    titles += [i["title"].lower() for i in dash.get("team_tasks", []) + dash.get("md_tasks", [])
               + dash.get("given_tasks", []) + dash["delegated"]]
    main = max(key.split(), key=len)
    if len(main) >= 3 and any(re.search(rf"\b{re.escape(main)}\b", t) for t in titles):
        return {**known, "who": "seen in your tasks"}
    # Unknown: offer the nearest names we do know.
    pool: dict[str, str] = {}
    for s in staff_qs()[:200]:
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
        "reader": "MD" if user.is_md else f"{user.title_label} ({user.spoken_name})",
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
        # Work the reader gave colleagues that is still open (worth chasing when late).
        "given_open": [{"title": d["title"], "with": d["assignee"]["staff_name"], "status": d.get("status_label") or d["status"],
                        "overdue": d["overdue"]} for d in dash.get("given_tasks", [])
                       if d["status"] not in (Status.COMPLETED, Status.CANCELLED)][:12],
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
    if facts["pending_requests"] and user.is_md:
        parts.append(f"{len(facts['pending_requests'])} request{'s are' if len(facts['pending_requests']) > 1 else ' is'} waiting for your decision.")
    late = [d for d in facts["delegated_open"] if d["overdue"]] + \
        [{"staff": g["with"], "title": g["title"]} for g in facts["given_open"] if g["overdue"]]
    if late:
        parts.append(f"{late[0]['staff']} is late on {late[0]['title']}.")
    return facts, " ".join(parts)


# ------------------------------------------------------------------ the team page (the MD adds and manages people)
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")
MIN_PASSWORD = 3  # the MD chooses short, easy logins for the team ("smm" / "smm")
TEAM_LIMIT = 60


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_gender(value) -> str:
    v = str(value or "").strip().lower()
    return {"male": "male", "man": "male", "m": "male", "female": "female", "woman": "female", "f": "female"}.get(v, "")


def team_add(md: User, data: dict) -> User:
    """The MD adds a person: name, job, department, gender (optional), and their login (username + password).
    They can sign in at once on their own phone with the same app."""
    if not md.is_md:
        raise ServiceError("forbidden", f"Only {md_address()} can add people.", 403)
    name = _clean_text(data.get("name"), 40)
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    if not name:
        raise ServiceError("invalid", "Please give the person's name.", 422)
    if not _USERNAME.match(username):
        raise ServiceError("invalid", "The username needs 3 to 30 small letters or numbers (dots, dashes allowed).", 422)
    if len(password) < MIN_PASSWORD:
        raise ServiceError("invalid", f"The password needs at least {MIN_PASSWORD} characters.", 422)
    if User.objects.filter(username__iexact=username).exists():
        raise ServiceError("taken", f"The username {username} is already used. Please pick another.", 409)
    if User.objects.filter(is_active=True).count() >= TEAM_LIMIT:
        raise ServiceError("team_full", f"JARVIS holds at most {TEAM_LIMIT} people.", 409)
    person = User.objects.create_user(
        username=username, password=password, role=User.Role.TM, display_name=name, first_name=name,
        job_title=_clean_text(data.get("title"), 80), department=_clean_text(data.get("department"), 80),
        gender=_clean_gender(data.get("gender")),
    )
    if person.department:
        _join_department(md, person, person.department)
    audit(md, "team.add", "user", person.id, f"{username} ({person.job_title})")
    return person


def _join_department(md: User, person: User, name: str) -> None:
    """A department typed on the Team page: the person joins it (made if new); the old text stays as a mirror."""
    from . import sections as _sections  # local import: sections builds on this module
    try:
        _sections.eligible(person)
    except ServiceError:
        return
    found = _sections.find_department(name)
    dept = found["department"] if found["status"] in ("known", "close") else _sections.create_department(md, name)[0]
    _sections.add_members(md, dept, [person])


def team_update(md: User, user_id, data: dict) -> User:
    """The MD changes a person's name, job, department or gender, resets their password, or switches the account
    off (or on again). The MD's own account is never switched off; nothing anyone had is deleted."""
    if not md.is_md:
        raise ServiceError("forbidden", f"Only {md_address()} can change people.", 403)
    person = User.objects.get(id=user_id)
    fields = []
    if "name" in data:
        name = _clean_text(data.get("name"), 40)
        if not name:
            raise ServiceError("invalid", "The name cannot be empty.", 422)
        if name != person.spoken_name:
            _rename(md, person, name)
    for key, field, limit in (("title", "job_title", 80), ("department", "department", 80)):
        if key in data:
            setattr(person, field, _clean_text(data.get(key), limit))
            fields.append(field)
    if "department" in data and not (person.is_md or person.is_pa or person.is_mg):
        # The text typed is the person's department now: in it (made if new), and out of the others.
        from . import sections as _sections  # local import: sections builds on this module
        wanted = _clean_text(data.get("department"), 80)
        current = _sections.member_of(person)
        keep = _sections.find_department(wanted)["department"] if wanted else None
        for d in current:
            if keep is None or d.id != keep.id:
                _sections.remove_members(md, d, [person])
        if wanted:
            _join_department(md, person, wanted)
            person.refresh_from_db(fields=["department"])
            person.department = person.department or wanted
    if "gender" in data:
        person.gender = _clean_gender(data.get("gender"))
        fields.append("gender")
    if "active" in data:
        active = bool(data.get("active"))
        if person.is_md and not active:
            raise ServiceError("forbidden", "The MD's own account cannot be switched off.", 422)
        if person.is_active != active:
            person.is_active = active
            fields.append("is_active")
            if not active:
                from .models import PushDevice  # no more alerts to a phone of a switched-off account
                PushDevice.objects.filter(user=person).delete()
    if data.get("password"):
        password = str(data["password"])
        if len(password) < MIN_PASSWORD:
            raise ServiceError("invalid", f"The password needs at least {MIN_PASSWORD} characters.", 422)
        person.set_password(password)
        fields.append("password")
    if fields:
        person.save(update_fields=fields)
        audit(md, "team.update", "user", person.id, "fields: " + ",".join(sorted(set(fields) - {"password"}))
              + (" + password" if "password" in fields else ""))
    return person
