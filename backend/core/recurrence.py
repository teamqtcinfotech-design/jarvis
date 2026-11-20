"""Repeating items (Nov 2026): "har mahine 10 tarikh ko GST file karo", "har Monday review", "roz subah 9 baje".

A repeating task, meeting, call or reminder carries a small rule (TaskFields.repeat) and is otherwise an ordinary
item. The worker calls run() every cycle: once an occurrence is closed (done or cancelled), or its time or day has
passed, the NEXT occurrence is made from it, due on the next date the rule names (never in the past), with the same
words, kind, priority, section and people; it is given exactly as the first one was (the person is told, it rings
until seen) and points back to the one it came from, so no occurrence is ever made twice. repeat_until ends the
series. "Band kar do" on the item clears the rule.

Rules: "daily" | "weekly:0,2" (Monday = 0) | "monthly:10" ("monthly:last" = the last day) | "yearly:MM-DD".
"""
import calendar
import logging
import re
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from . import timeutils as tu
from .models import Kind, PrivateTask, SharedTask, Status, TaskEvent

log = logging.getLogger("jarvis.recurrence")

WEEKDAYS = {
    "monday": 0, "mon": 0, "somvar": 0, "somwar": 0, "सोमवार": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "mangalvar": 1, "mangalwar": 1, "मंगलवार": 1,
    "wednesday": 2, "wed": 2, "budhvar": 2, "budhwar": 2, "बुधवार": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "guruvar": 3, "guruwar": 3, "veervar": 3, "गुरुवार": 3,
    "friday": 4, "fri": 4, "shukravar": 4, "shukrawar": 4, "शुक्रवार": 4,
    "saturday": 5, "sat": 5, "shanivar": 5, "shaniwar": 5, "शनिवार": 5,
    "sunday": 6, "sun": 6, "ravivar": 6, "raviwar": 6, "itwar": 6, "रविवार": 6, "इतवार": 6,
}
WEEKDAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_HI = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

# Word boundaries by whitespace, not \b: Python's \b breaks inside Devanagari words (vowel signs are not "word"
# characters), so "महीने" would never match with \b around it.
def _words(alts: str) -> re.Pattern:
    return re.compile(rf"(?<!\S)(?:{alts})(?!\S)", re.IGNORECASE)


_DAILY = _words(r"daily|every ?day|each day|roz|rozana|har (?:din|roz)|हर (?:दिन|रोज़|रोज)|रोज़|रोज|रोज़ाना")
_WEEKDAYS_ALL = _words(r"weekdays?|every working day|har working day|mon(?:day)?[- ]?(?:to|se)[- ]?fri(?:day)?|som ?se shukra")
_WEEKLY = _words(r"weekly|every week|har (?:hafte|week|saptah)|हर (?:हफ़्ते|हफ्ते|सप्ताह)")
_MONTHLY = _words(r"monthly|every month|har (?:mahine|mahina|month)|हर (?:महीने|महीना)")
_YEARLY = _words(r"yearly|annually|every year|har (?:saal|year)|हर साल")
_EVERY_DAYNAME = re.compile(r"(?<!\S)(?:every|har|हर)\s+([A-Za-zऀ-ॿ]+)", re.IGNORECASE)
_DAY_OF_MONTH = re.compile(r"(?<!\S)(\d{1,2})\s*(?:tarikh|tareekh|तारीख़|तारीख|th|st|nd|rd|ko)?(?!\S)", re.IGNORECASE)
_LAST = _words(r"last day|month end|mahine ke (?:aakhir|ant|last)|महीने के (?:आख़िर|अंत)")
_EVERY_N = re.compile(r"(?<!\S)(?:har|every|हर)\s+\d{1,2}")
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_PUNCT = re.compile(r"[,.!?;:।]+")


def parse_words(words: str, anchor: date | None = None) -> str:
    """The repeat rule the speaker's words name, or "" when they name none. `anchor` (the first due date) fills in
    what the words leave out: "har hafte" = that weekday; "har mahine" = that day of the month."""
    text = " ".join(_PUNCT.sub(" ", str(words or "").translate(_DEVANAGARI_DIGITS).lower()).split())
    if not text:
        return ""
    if _WEEKDAYS_ALL.search(text):
        return "weekly:0,1,2,3,4"
    if _DAILY.search(text):
        return "daily"
    if _YEARLY.search(text):
        if anchor:
            return f"yearly:{anchor.month:02d}-{anchor.day:02d}"
        return "yearly"
    if _MONTHLY.search(text) or _LAST.search(text) or _EVERY_N.search(text):
        if _LAST.search(text):
            return "monthly:last"
        m = _DAY_OF_MONTH.search(_words(r"har|every|हर").sub(" ", text))
        day = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 31 else (anchor.day if anchor else 0)
        return f"monthly:{day}" if day else "monthly"
    days = set()
    for m in _EVERY_DAYNAME.finditer(text):
        name = m.group(1).strip(".,")
        if name in WEEKDAYS:
            days.add(WEEKDAYS[name])
    said_every = bool(_WEEKLY.search(text) or _words(r"every|har|हर").search(text))
    for name, idx in WEEKDAYS.items():
        if said_every and _words(rf"{re.escape(name)}s?").search(text):
            days.add(idx)
    if days:
        return "weekly:" + ",".join(str(d) for d in sorted(days))
    if _WEEKLY.search(text):
        return f"weekly:{anchor.weekday()}" if anchor else "weekly"
    return ""


def settle(rule: str, anchor: date | None) -> str:
    """A rule that still needs the first date ("weekly", "monthly", "yearly") filled in from it."""
    if not rule or not anchor:
        return rule or ""
    if rule == "weekly":
        return f"weekly:{anchor.weekday()}"
    if rule == "monthly":
        return f"monthly:{anchor.day}"
    if rule == "yearly":
        return f"yearly:{anchor.month:02d}-{anchor.day:02d}"
    return rule


def valid(rule: str) -> bool:
    try:
        next_date(rule, date(2026, 1, 1))
        return bool(rule)
    except (ValueError, IndexError):
        return False


def _month_day(year: int, month: int, day) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if day == "last" else min(int(day), last))


def next_date(rule: str, after: date) -> date | None:
    """The first date the rule names strictly after `after`; None for no rule."""
    if not rule:
        return None
    kind, _, arg = rule.partition(":")
    if kind == "daily":
        return after + timedelta(days=1)
    if kind == "weekly":
        days = sorted({int(x) for x in arg.split(",") if x.strip() != ""}) if arg else [after.weekday()]
        for i in range(1, 8):
            d = after + timedelta(days=i)
            if d.weekday() in days:
                return d
        return after + timedelta(days=7)
    if kind == "monthly":
        day = "last" if arg == "last" else int(arg or after.day)
        y, m = after.year, after.month
        candidate = _month_day(y, m, day)
        if candidate > after:
            return candidate
        m += 1
        if m > 12:
            y, m = y + 1, 1
        return _month_day(y, m, day)
    if kind == "yearly":
        mm, dd = (int(x) for x in (arg or f"{after.month:02d}-{after.day:02d}").split("-"))
        candidate = _month_day(after.year, mm, dd)
        if candidate > after:
            return candidate
        return _month_day(after.year + 1, mm, dd)
    raise ValueError(rule)


def describe(rule: str) -> tuple[str, str]:
    """("every month on the 10th", "हर महीने 10 तारीख़ को"); ("", "") for no rule."""
    if not rule:
        return "", ""
    kind, _, arg = rule.partition(":")
    if kind == "daily":
        return "every day", "हर दिन"
    if kind == "weekly":
        days = sorted({int(x) for x in arg.split(",") if x.strip() != ""}) if arg else []
        if days == [0, 1, 2, 3, 4]:
            return "every working day", "हर कामकाजी दिन"
        if not days:
            return "every week", "हर हफ़्ते"
        return ("every " + " and ".join(WEEKDAY_EN[d] for d in days), "हर " + " और " .join(WEEKDAY_HI[d] for d in days))
    if kind == "monthly":
        if arg == "last":
            return "every month on the last day", "हर महीने के आख़िरी दिन"
        if not arg:
            return "every month", "हर महीने"
        n = int(arg)
        suffix = "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"every month on the {n}{suffix}", f"हर महीने {n} तारीख़ को"
    if kind == "yearly":
        if arg:
            mm, dd = (int(x) for x in arg.split("-"))
            return f"every year on {dd} {calendar.month_name[mm]}", f"हर साल {dd} {calendar.month_name[mm]} को"
        return "every year", "हर साल"
    return rule, rule


# ------------------------------------------------------------------------------------------------ the next copy
def _due_passed(task, today: date, now) -> bool:
    if task.kind in (Kind.MEETING, Kind.CALL) and task.due_at:
        return task.due_at < now
    if task.due_at:
        return tu.to_local(task.due_at).date() < today
    return bool(task.due_date and task.due_date < today)


def _occurrence_due(task) -> date | None:
    return tu.to_local(task.due_at).date() if task.due_at else task.due_date


def _ready(task, today: date, now) -> bool:
    return task.is_closed or _due_passed(task, today, now)


def _next_due(task, today: date) -> date | None:
    """The next date for this series: after this occurrence's date, and never before today."""
    base = _occurrence_due(task) or today
    nxt = next_date(task.repeat, base)
    guard = 0
    while nxt and nxt < today and guard < 400:
        nxt = next_date(task.repeat, nxt)
        guard += 1
    if nxt and task.repeat_until and nxt > task.repeat_until:
        return None
    return nxt


def _data_from(task, due: date) -> dict:
    data = {"title": task.title, "notes": task.notes, "kind": task.kind, "priority": task.priority, "due_date": due,
            "due_time": tu.to_local(task.due_at).time().replace(second=0, microsecond=0) if task.due_at else None,
            "duration_minutes": task.estimated_minutes, "remind_before_minutes": task.remind_before_minutes,
            "announcement": task.announcement, "repeat": task.repeat, "repeat_until": task.repeat_until,
            "amount": task.amount, "steps": [{"text": s["text"], "done": False} for s in task.steps]}
    if task.section_id:
        data["section"] = task.section.name
    return data


def make_next(task) -> object | None:
    """The next occurrence of a repeating item, or None when the series is over. Idempotent: an occurrence that
    already has its successor is left alone."""
    from . import services  # noqa: PLC0415 — services imports this module's neighbours

    today = tu.today_local()
    if not task.repeat or task.repeats.exists():
        return None
    due = _next_due(task, today)
    if due is None:
        return None
    data = _data_from(task, due)
    with transaction.atomic():
        if isinstance(task, PrivateTask):
            copy = services.create_private_task(task.owner, data, source=task.source)
        elif task.origin in services.OWN_ORIGINS:
            copy = services.create_own_task(task.assignee_user, {**data, "description": task.description})
        elif task.assignee_type == SharedTask.Assignee.STAFF:
            return None  # staff work lives in TaskPanel: a repeat there is TaskPanel's to make
        else:
            giver = task.created_by
            person = task.assignee_user
            if giver is None or person is None or not person.is_active:
                return None
            copy = services.create_person_task(giver, person, {**data, "description": task.description},
                                               meeting=task.origin in services.MEET_ORIGINS, compose_note=False)
        copy.repeat_of = task
        copy.save(update_fields=["repeat_of"])
        if isinstance(copy, SharedTask):
            services.log_event(copy, TaskEvent.Kind.ADDED if task.origin in services.OWN_ORIGINS else TaskEvent.Kind.GIVEN,
                               task.created_by, **({"to": copy.assignee_user_id} if copy.assignee_user_id else {}),
                               repeat=describe(task.repeat)[0])
    log.info("repeat: next occurrence of %s made for %s", str(task.id)[:8], due)
    return copy


def run(now=None) -> int:
    """One pass for the worker: every repeating item whose turn has come gets its next occurrence."""
    now = now or timezone.now()
    today = tu.to_local(now).date()
    made = 0
    for model in (SharedTask, PrivateTask):
        qs = model.objects.exclude(repeat="").filter(repeats__isnull=True)
        for task in qs.iterator():
            if not _ready(task, today, now):
                continue
            try:
                if make_next(task) is not None:
                    made += 1
            except Exception:  # noqa: BLE001 — one bad series must not stop the others
                log.exception("repeat: next occurrence of %s not made", task.id)
    return made


def stop_series(task) -> None:
    """"Band kar do": this item stays, nothing more is made from it."""
    task.repeat = ""
    task.save(update_fields=["repeat"])


__all__ = ["parse_words", "settle", "valid", "next_date", "describe", "make_next", "run", "stop_series", "Status"]
