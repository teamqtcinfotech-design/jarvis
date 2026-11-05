"""Does the diary make sense? Clashes, free slots and how full a day is.

JARVIS never moves anything by itself. These functions only produce facts and one suggestion; the
person answers by voice. Everything is worked out per person from their own items plus their routine
(office hours, weekly off, fixed blocks such as the gym) kept in their settings.

Privacy: a PA asking for the MD's time only ever gets anonymous busy spans (``anonymous=True``);
titles of the MD's private items never leave the MD's own view.
"""
from datetime import date, datetime, time, timedelta

from django.db.models import Q

from . import timeutils as tu
from .models import Kind, Priority, PrivateTask, SharedTask, Status, User

OPEN = [Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED]
DEFAULT_MINUTES = {Kind.MEETING: 60, Kind.CALL: 15, Kind.TASK: 30, Kind.PAYMENT: 10, Kind.FOLLOW_UP: 15}
HEAVY_DAY_ITEMS = 7          # this many items on one day is "bhari din"
BUFFER = 0.2                 # a fifth of the office day stays free
SLOT_STEP = timedelta(minutes=15)
LOOKAHEAD_DAYS = 7
MOVABLE_KINDS = {Kind.TASK, Kind.PAYMENT, Kind.FOLLOW_UP}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def minutes_for(kind: str, estimated: int | None) -> int:
    return int(estimated) if estimated else DEFAULT_MINUTES.get(kind, 30)


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def office_hours(prefs: dict, day: date) -> tuple[datetime, datetime]:
    start = tu.combine_local(day, _parse_hhmm(prefs.get("officeStart", "10:00"), time(10, 0)))
    end = tu.combine_local(day, _parse_hhmm(prefs.get("officeEnd", "19:00"), time(19, 0)))
    if end <= start:
        end = start + timedelta(hours=9)
    return start, end


def is_off_day(prefs: dict, day: date) -> bool:
    return day.weekday() in set(prefs.get("weeklyOff") or [])


def routine_blocks(prefs: dict, day: date) -> list[dict]:
    """Fixed blocks on this day (gym, lunch, market), as busy spans with a label."""
    out = []
    for block in prefs.get("routineBlocks") or []:
        days = block.get("days")
        if days and day.weekday() not in days:
            continue
        start = tu.combine_local(day, _parse_hhmm(block.get("start"), time(0, 0)))
        end = tu.combine_local(day, _parse_hhmm(block.get("end"), time(0, 0)))
        if end <= start:
            end = start + timedelta(hours=1)
        out.append({"id": "", "title": block.get("label") or "Routine", "start": start, "end": end, "routine": True,
                    "private": False})
    return out


def _own_sources(user: User):
    if user.is_md:
        return [
            (PrivateTask.objects.filter(owner=user), "private"),
            (SharedTask.objects.filter(assignee_type=SharedTask.Assignee.MD), "shared"),
            (SharedTask.objects.filter(assignee_type=SharedTask.Assignee.PA, origin=SharedTask.Origin.MD_MEET_PA), "shared"),
        ]
    return [(SharedTask.objects.filter(assignee_type=SharedTask.Assignee.PA), "shared")]


def _span(task) -> tuple[datetime, datetime] | None:
    start = task.scheduled_start or task.due_at
    if not start:
        return None
    end = task.scheduled_end or start + timedelta(minutes=minutes_for(task.kind, task.estimated_minutes))
    return start, end


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def commitments(user: User, day: date, exclude_ids=()) -> list[dict]:
    """Timed items of this person that touch the day, as busy spans."""
    day_start, day_end = tu.day_bounds(day)
    excluded = {str(x) for x in exclude_ids}
    out = []
    for qs, scope in _own_sources(user):
        rows = qs.filter(status__in=OPEN).filter(
            Q(scheduled_start__lt=day_end, scheduled_end__gt=day_start)
            | Q(scheduled_start__isnull=True, due_at__gte=day_start - timedelta(days=1), due_at__lt=day_end)
        )
        for task in rows:
            if str(task.id) in excluded:
                continue
            span = _span(task)
            if not span or not _overlaps(span[0], span[1], day_start, day_end):
                continue
            out.append({"id": str(task.id), "title": task.title, "start": span[0], "end": span[1], "routine": False,
                        "private": scope == "private", "kind": task.kind, "priority": task.priority, "scope": scope,
                        "version": task.version})
    out.sort(key=lambda c: c["start"])
    return out


def dated_items(user: User, day: date, exclude_ids=()) -> list[dict]:
    """Items due on the day without a time (they take room in the day but no slot)."""
    excluded = {str(x) for x in exclude_ids}
    out = []
    for qs, scope in _own_sources(user):
        for task in qs.filter(status__in=OPEN, due_at__isnull=True, due_date=day):
            if str(task.id) in excluded:
                continue
            out.append({"id": str(task.id), "title": task.title, "kind": task.kind, "priority": task.priority,
                        "private": scope == "private", "scope": scope, "version": task.version,
                        "minutes": minutes_for(task.kind, task.estimated_minutes)})
    return out


def busy_on(user: User, day: date, prefs: dict, exclude_ids=()) -> list[dict]:
    spans = commitments(user, day, exclude_ids) + routine_blocks(prefs, day)
    spans.sort(key=lambda c: c["start"])
    return spans


def clashes(user: User, start: datetime, end: datetime, prefs: dict, exclude_ids=()) -> list[dict]:
    day = tu.to_local(start).date()
    busy = busy_on(user, day, prefs, exclude_ids)
    if tu.to_local(end).date() != day:
        busy += busy_on(user, tu.to_local(end).date(), prefs, exclude_ids)
    return [b for b in busy if _overlaps(start, end, b["start"], b["end"])]


def _ceil(dt: datetime, step: timedelta = SLOT_STEP) -> datetime:
    local = tu.to_local(dt)
    seconds = (local - local.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    step_s = step.total_seconds()
    rounded = ((seconds + step_s - 1) // step_s) * step_s
    return local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=rounded)


def free_slots(user: User, day: date, minutes: int, prefs: dict, not_before: datetime | None = None,
               exclude_ids=()) -> list[tuple[datetime, datetime]]:
    """Free stretches inside office hours, each at least ``minutes`` long."""
    if is_off_day(prefs, day):
        return []
    o_start, o_end = office_hours(prefs, day)
    cursor = o_start
    if not_before and not_before > cursor:
        cursor = _ceil(not_before)
    busy = busy_on(user, day, prefs, exclude_ids)
    need = timedelta(minutes=minutes)
    slots = []
    guard = 0
    while cursor + need <= o_end and guard < 200:
        guard += 1
        hit = next((b for b in busy if _overlaps(cursor, cursor + need, b["start"], b["end"])), None)
        if hit:
            cursor = max(_ceil(hit["end"]), cursor + SLOT_STEP)
            continue
        nxt = min((b["start"] for b in busy if b["start"] >= cursor + need), default=o_end)
        slots.append((cursor, min(nxt, o_end)))
        cursor = nxt if nxt > cursor else cursor + SLOT_STEP
    return slots


def suggest(user: User, *, minutes: int, prefs: dict, day: date | None = None, not_before: datetime | None = None,
            exclude_ids=()) -> dict | None:
    """The first free slot: on ``day`` after ``not_before``, else on the following working days."""
    now = tu.now_local()
    base = day or now.date()
    for i in range(LOOKAHEAD_DAYS + 1):
        d = base + timedelta(days=i)
        if d < now.date():
            continue
        floor = now if d == now.date() else None
        if i == 0 and not_before and (floor is None or not_before > floor):
            floor = not_before
        for start, end in free_slots(user, d, minutes, prefs, floor, exclude_ids):
            local = tu.to_local(start)
            return {"due_date": d.isoformat(), "due_time": local.strftime("%H:%M"), "start": start, "end": end,
                    "same_day": d == base}
    return None


def day_load(user: User, day: date, prefs: dict, exclude_ids=()) -> dict:
    timed = commitments(user, day, exclude_ids)
    dated = dated_items(user, day, exclude_ids)
    minutes = sum(int((c["end"] - c["start"]).total_seconds() // 60) for c in timed) + sum(d["minutes"] for d in dated)
    if is_off_day(prefs, day):
        capacity = 0
    else:
        o_start, o_end = office_hours(prefs, day)
        capacity = int((o_end - o_start).total_seconds() // 60 * (1 - BUFFER))
    count = len(timed) + len(dated)
    heavy = count >= HEAVY_DAY_ITEMS or (capacity > 0 and minutes > capacity)
    movable = None
    candidates = [d for d in dated if d["kind"] in MOVABLE_KINDS and d["priority"] in (Priority.LOW, Priority.NORMAL)]
    if candidates:
        rank = {Priority.LOW: 0, Priority.NORMAL: 1}
        movable = min(candidates, key=lambda d: rank.get(d["priority"], 9))
    return {"count": count, "minutes": minutes, "capacity": capacity, "heavy": heavy, "movable": movable}


def overlapping_pairs(user: User, day: date, prefs: dict) -> list[tuple[dict, dict]]:
    """Items on the day that collide with each other (for the morning briefing)."""
    busy = busy_on(user, day, prefs)
    pairs = []
    for i, a in enumerate(busy):
        for b in busy[i + 1:]:
            if _overlaps(a["start"], a["end"], b["start"], b["end"]):
                pairs.append((a, b))
    return pairs


def review(user: User, *, kind: str, due_date: str, due_time: str, minutes: int | None = None, prefs: dict,
           exclude_ids=()) -> dict:
    """Facts about one proposed slot (or day): clashes, a free alternative, how full the day is."""
    d = date.fromisoformat(due_date) if due_date else None
    mins = minutes or DEFAULT_MINUTES.get(kind, 30)
    out = {"clashes": [], "suggested": None, "load": None, "off_day": False, "needs_slot": False, "minutes": mins}
    if d and due_time:
        start = tu.combine_local(d, time.fromisoformat(due_time))
        end = start + timedelta(minutes=mins)
        out["clashes"] = clashes(user, start, end, prefs, exclude_ids)
        if out["clashes"]:
            out["suggested"] = suggest(user, minutes=mins, prefs=prefs, day=d, not_before=start, exclude_ids=exclude_ids)
    elif kind in (Kind.MEETING, Kind.CALL):
        out["needs_slot"] = True
        out["suggested"] = suggest(user, minutes=mins, prefs=prefs, day=d, exclude_ids=exclude_ids)
    if d:
        out["off_day"] = is_off_day(prefs, d)
        out["load"] = day_load(user, d, prefs, exclude_ids)
    return out


def routine_summary(prefs: dict) -> dict:
    """Plain words for the AI context and the settings screen."""
    off = [WEEKDAY_NAMES[i] for i in sorted(set(prefs.get("weeklyOff") or [])) if 0 <= i <= 6]
    blocks = []
    for b in prefs.get("routineBlocks") or []:
        days = b.get("days")
        when = "daily" if not days or len(days) == 7 else ", ".join(WEEKDAY_NAMES[i][:3] for i in sorted(days))
        blocks.append(f"{b.get('label', 'Routine')} {b.get('start', '')}-{b.get('end', '')} ({when})")
    return {"office_hours": f"{prefs.get('officeStart', '10:00')}-{prefs.get('officeEnd', '19:00')}",
            "weekly_off": off or ["none"], "fixed_blocks": blocks,
            "morning_briefing": prefs.get("briefingTime", "09:00") if prefs.get("briefingEnabled", True) else "off"}
