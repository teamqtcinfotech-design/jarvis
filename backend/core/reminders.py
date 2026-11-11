"""Reminder schedule for the phone.

The phone schedules these as exact local alarms (works offline, survives sleep)
and its presence engine decides at fire time whether to speak, vibrate or hold.

Keys include the task version, so rescheduling a task automatically retires the
old reminders. Acknowledged keys are filtered out.
"""
from datetime import datetime, timedelta

from django.utils import timezone

from . import lang as lang_mod
from . import scheduling
from . import timeutils as tu
from .models import Kind, Priority, PrivateTask, ReminderAck, ReminderSnooze, ReminderTold, SharedTask, Status, User
from .services import (
    address_hi_of, address_of, busy_windows, day_reminder_time, default_remind_before, delegated_qs, get_pa, get_prefs,
    md_address, md_address_hi, md_private_qs, meeting_override_until, pa_tasks_qs, shared_md_tasks_qs,
)

LOOKBACK = timedelta(hours=12)
# A timed task still open this long after its time gets one gentle check-in ("done, move it, or leave it?"),
# in working hours only. After that it is left to the evening summary and the next morning's briefing.
CHECKIN_AFTER = timedelta(minutes=45)
CHECKIN_HOURS = range(9, 20)
# A date-only item written down after the day's reminder time still gets its reminder that day: an hour before the
# office closes, if that is still ahead. Later than that, the evening summary covers it.
LATE_DAY_REMINDER_BEFORE_CLOSE = timedelta(hours=1)

# The question JARVIS asks right after a reminder, and what a yes does. Both the app and the phone's background
# part read this; neither spells its own. A meeting that is starting needs no question.
Q_DONE = {"kind": "done", "en": "Done, or remind you later?", "hi": "हो गया, या बाद में याद दिलाऊँ?"}
Q_CHECKIN = {"kind": "done", "en": "", "hi": ""}  # the check-in line itself asks ("Done, move it, or leave it?")


def _lead_phrase(minutes: int) -> str:
    return f"in {tu.minutes_phrase(minutes)}"


def _lead_phrase_hi(minutes: int) -> str:
    return f"{tu.minutes_phrase_hi(minutes)} में"


def lead_for(task, prefs: dict) -> int:
    """How early to speak: the task's own lead if the user asked for one, else the reader's setting for that kind."""
    if task.remind_before_minutes is not None:
        return int(task.remind_before_minutes)
    return default_remind_before(task.kind, task.priority, prefs)


def _task_reminders(task, scope: str, is_private: bool, address: str, prefs: dict, checkin: bool = True,
                    address_hi: str = "") -> list[dict]:
    """Every reminder line comes with its Hindi twin ("spoken_hi"); reminders_for picks by the reader's language.

    Keys name the item and its MOMENT (not its version): a rename or a note added later keeps the same key, so it
    is not announced again; a new time is a new reminder."""
    out = []
    address_hi = address_hi or address
    base = {
        "priority": task.priority,
        "title": task.title,
        "kind": task.kind,
        "is_private": is_private,
        "ref": {"scope": scope, "id": str(task.id), "version": task.version},
        "question": None,
    }
    # A reminder whose time had already passed when the task was written down is not a reminder he missed:
    # "kal 4 baje" said at 3:55 gets no "15 minutes before", only the "it's time".
    born = (task.created_at or timezone.now()) - timedelta(minutes=1)
    start = task.scheduled_start or task.due_at
    if start:
        key_prefix = f"{scope}:{task.id}:t{int(start.timestamp())}"
        lead = lead_for(task, prefs)
        before_at = start - timedelta(minutes=lead)
        if lead > 0 and before_at >= born:
            lead_en, lead_hi = _lead_phrase(lead), _lead_phrase_hi(lead)
            if task.announcement:
                # The person's own words for this reminder ("remind me: take the blue file"): said as they are.
                spoken = spoken_hi = task.announcement
            elif task.kind == Kind.MEETING:
                spoken = f"{address}, your meeting {task.title} starts {lead_en}."
                spoken_hi = f"{address_hi}, आपकी मीटिंग {task.title} {lead_hi} शुरू होगी।"
            elif task.kind == Kind.CALL:
                spoken = f"{address}, your call {task.title} is {lead_en}."
                spoken_hi = f"{address_hi}, आपका कॉल {task.title} {lead_hi} है।"
            else:
                spoken = f"{address}, {task.title} is due {lead_en}."
                spoken_hi = f"{address_hi}, {task.title} {lead_hi} ड्यू है।"
            out.append({**base, "key": f"{key_prefix}:before", "label": "before",
                        "fire_at": before_at.isoformat(), "spoken": spoken, "spoken_hi": spoken_hi})
        if task.kind == Kind.MEETING:
            due_spoken = f"{address}, your meeting {task.title} is starting now."
            due_hi = f"{address_hi}, आपकी मीटिंग {task.title} अभी शुरू हो रही है।"
        elif task.kind == Kind.CALL:
            due_spoken = f"{address}, it's time for your call: {task.title}."
            due_hi = f"{address_hi}, आपके कॉल का समय हो गया: {task.title}।"
        else:
            due_spoken = f"{address}, it's time: {task.title}."
            due_hi = f"{address_hi}, समय हो गया: {task.title}।"
        if start >= born:
            out.append({**base, "key": f"{key_prefix}:due", "label": "due", "fire_at": start.isoformat(),
                        "spoken": due_spoken, "spoken_hi": due_hi,
                        "question": None if task.kind == Kind.MEETING else Q_DONE})
        # Still open well after its time: one check-in, in working hours on a working day, then it is left to the
        # evening summary.
        at = start + CHECKIN_AFTER
        at_local = tu.to_local(at)
        if checkin and at_local.hour in CHECKIN_HOURS and not scheduling.is_off_day(prefs, at_local.date()):
            what = {Kind.MEETING: "your meeting", Kind.CALL: "your call"}.get(task.kind, "")
            what_hi = {Kind.MEETING: "आपकी मीटिंग", Kind.CALL: "आपका कॉल"}.get(task.kind, "")
            out.append({**base, "key": f"{key_prefix}:checkin", "label": "checkin", "fire_at": at.isoformat(),
                        "priority": Priority.NORMAL if task.priority == Priority.LOW else task.priority,
                        "spoken": f"{address}, {_join(what, task.title)} was due at {tu.fmt_time(start)} and is still open. "
                                  f"Done, move it, or leave it?",
                        "spoken_hi": f"{address_hi}, {_join(what_hi, task.title)} {tu.fmt_time_hi(start)} पर था और अभी भी खुला है। "
                                     f"हो गया, आगे बढ़ाऊँ, या रहने दूँ?",
                        "question": Q_CHECKIN})
    elif task.due_date:
        key_prefix = f"{scope}:{task.id}:d{task.due_date.isoformat()}"
        fire = tu.combine_local(task.due_date, day_reminder_time(prefs))
        if fire < born and task.due_date == tu.to_local(born).date():
            # Written down after the day's reminder time: still remind today, an hour before the office closes.
            _, close = scheduling.office_hours(prefs, task.due_date)
            fire = close - LATE_DAY_REMINDER_BEFORE_CLOSE
        if fire >= born:
            out.append({**base, "key": f"{key_prefix}:day", "label": "day", "fire_at": fire.isoformat(),
                        "spoken": f"{address}, today's reminder: {task.title}.",
                        "spoken_hi": f"{address_hi}, आज का रिमाइंडर: {task.title}।", "question": Q_DONE})
    return out


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def reminders_for(user: User, hours: int = 48) -> dict:
    now = timezone.now()
    window_start, window_end = now - LOOKBACK, now + timedelta(hours=hours)
    items: list[dict] = []
    open_filter = {"status__in": [Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED]}
    prefs = get_prefs(user)  # the reader's own lead times and day-reminder hour

    if user.is_md:
        sir, sir_hi = address_of(user), address_hi_of(user)
        for task in md_private_qs(user).filter(**open_filter):
            items += _task_reminders(task, "private", True, sir, prefs, address_hi=sir_hi)
        for task in shared_md_tasks_qs().filter(**open_filter):
            items += _task_reminders(task, "shared", False, sir, prefs, address_hi=sir_hi)
        # Anything involving the PA is for both: the MD is reminded too.
        pa = get_pa()
        pa_name = pa.spoken_name if pa else "your PA"
        for task in pa_tasks_qs().filter(**open_filter).exclude(origin=SharedTask.Origin.PA_SELF):
            meeting = task.origin == SharedTask.Origin.MD_MEET_PA
            # The check-in on the PA's tasks is the PA's, not the MD's (told once that it is due, and in the evening).
            for item in _task_reminders(task, "shared", False, sir, prefs, checkin=meeting, address_hi=sir_hi):
                lead, lead_hi = _lead_phrase(lead_for(task, prefs)), _lead_phrase_hi(lead_for(task, prefs))
                label = item["label"]
                if meeting:
                    item["title"] = f"Meeting with {pa_name}: {task.title}"
                    if label == "before":
                        item["spoken"] = f"{sir}, your meeting with {pa_name} starts {lead}: {task.title}."
                        item["spoken_hi"] = f"{sir_hi}, {pa_name} के साथ आपकी मीटिंग {lead_hi} शुरू होगी: {task.title}।"
                    elif label == "due":
                        item["spoken"] = f"{sir}, your meeting with {pa_name} is starting now: {task.title}."
                        item["spoken_hi"] = f"{sir_hi}, {pa_name} के साथ आपकी मीटिंग अभी शुरू हो रही है: {task.title}।"
                else:
                    item["title"] = f"{pa_name}: {task.title}"
                    if label == "before":
                        item["spoken"] = f"{sir}, {pa_name}'s task {task.title} is due {lead}."
                        item["spoken_hi"] = f"{sir_hi}, {pa_name} का टास्क {task.title} {lead_hi} ड्यू है।"
                    elif label == "day":
                        item["spoken"] = f"{sir}, {pa_name}'s task is due today: {task.title}."
                        item["spoken_hi"] = f"{sir_hi}, {pa_name} का टास्क आज ड्यू है: {task.title}।"
                    else:
                        item["spoken"] = f"{sir}, {pa_name}'s task is due now: {task.title}. Please check with {pa_name}."
                        item["spoken_hi"] = f"{sir_hi}, {pa_name} का टास्क अभी ड्यू है: {task.title}। {pa_name} से पूछ लीजिए।"
                items.append(item)
    else:
        name, sir, sir_hi = user.spoken_name, md_address(), md_address_hi()
        for task in pa_tasks_qs().filter(**open_filter):  # her own tasks, and the ones from Sir
            meeting, from_sir = task.origin == SharedTask.Origin.MD_MEET_PA, task.origin != SharedTask.Origin.PA_SELF
            for item in _task_reminders(task, "shared", False, name, prefs, address_hi=name):
                label = item["label"]
                lead, lead_hi = _lead_phrase(lead_for(task, prefs)), _lead_phrase_hi(lead_for(task, prefs))
                if meeting and label == "before" and not task.announcement:
                    item["spoken"] = f"{name}, your meeting with {sir} starts {lead}: {task.title}."
                    item["spoken_hi"] = f"{name}, {sir_hi} के साथ आपकी मीटिंग {lead_hi} शुरू होगी: {task.title}।"
                elif meeting and label == "due":
                    item["spoken"] = f"{name}, your meeting with {sir} is starting now: {task.title}."
                    item["spoken_hi"] = f"{name}, {sir_hi} के साथ आपकी मीटिंग अभी शुरू हो रही है: {task.title}।"
                elif from_sir and label == "before" and not task.announcement:
                    item["spoken"] = f"{name}, {sir}'s task {task.title} is due {lead}."
                    item["spoken_hi"] = f"{name}, {sir_hi} का टास्क {task.title} {lead_hi} ड्यू है।"
                elif from_sir and label == "due":
                    item["spoken"] = f"{name}, {sir}'s task is due now: {task.title}."
                    item["spoken_hi"] = f"{name}, {sir_hi} का टास्क अभी ड्यू है: {task.title}।"
                elif from_sir and label == "day":
                    item["spoken"] = f"{name}, {sir}'s task is due today: {task.title}."
                    item["spoken_hi"] = f"{name}, {sir_hi} का टास्क आज ड्यू है: {task.title}।"
                items.append(item)
        for task in delegated_qs().filter(created_by=user, **open_filter):
            # A staff task she gave: a follow-up at its time, or on its day at her reminder hour when it has no time.
            if task.due_at:
                start, moment = task.due_at, f"t{int(task.due_at.timestamp())}"
            elif task.due_date:
                start, moment = tu.combine_local(task.due_date, day_reminder_time(prefs)), f"d{task.due_date.isoformat()}"
            else:
                continue
            today_hi = "आज " if not task.due_at else ""
            items.append({
                "key": f"shared:{task.id}:{moment}:followup",
                "label": "followup",
                "fire_at": start.isoformat(),
                "priority": task.priority,
                "title": f"{task.staff.name}: {task.title}",
                "kind": Kind.FOLLOW_UP,
                "is_private": False,
                "ref": {"scope": "shared", "id": str(task.id), "version": task.version},
                "spoken": f"{name}, {task.staff.name}'s task is due {'now' if task.due_at else 'today'}: {task.title}. Please follow up.",
                "spoken_hi": f"{name}, {task.staff.name} का टास्क {today_hi or 'अभी '}ड्यू है: {task.title}। कृपया फ़ॉलो-अप कीजिए।",
                "question": None,
            })

    # "Later": a snoozed reminder is served once more at the time he chose, under a new key, so it is announced
    # afresh by whichever part of the phone sees it first (and the original leaves the list).
    keys = [i["key"] for i in items]
    snoozes = dict(ReminderSnooze.objects.filter(user=user, key__in=keys, until__gte=now - LOOKBACK).values_list("key", "until"))
    for item in items:
        until = snoozes.get(item["key"])
        if until:
            item["snoozed_from"] = item["key"]
            item["key"] = f"{item['key']}:s{int(until.timestamp())}"
            item["fire_at"] = until.isoformat()
    keys = [i["key"] for i in items]
    acked = set(ReminderAck.objects.filter(user=user, key__in=keys).values_list("key", flat=True))
    told = dict(ReminderTold.objects.filter(user=user, key__in=keys).values_list("key", "told_at"))
    in_hindi = lang_mod.current(user) == "hi"  # the reader's language right now decides which twin is spoken
    result = []
    for item in items:
        fire = datetime.fromisoformat(item["fire_at"])
        if item["key"] in acked or not (window_start <= fire <= window_end):
            continue
        item["fire_at_label"] = f"{tu.fmt_day(tu.to_local(fire).date())} {tu.fmt_time(fire)}"
        item["told_at"] = told[item["key"]].isoformat() if item["key"] in told else None
        item["spoken_en"] = item["spoken"]
        if in_hindi and item.get("spoken_hi"):
            item["spoken"] = item["spoken_hi"]
        item["lang"] = "hi" if lang_mod.DEVANAGARI.search(item["spoken"]) else "en"
        result.append(item)
    result.sort(key=lambda r: r["fire_at"])

    today = tu.today_local()
    meetings = []
    if user.is_md:
        # His own diary meetings, for the phone's "in a meeting" sense. A meeting he switched meeting mode off
        # during no longer counts (the override is kept on the server, so closing the app does not bring it back).
        override = meeting_override_until(prefs)
        for w in busy_windows(today):
            if override and datetime.fromisoformat(w["end"]) <= override:
                continue
            meetings.append({"start": w["start"], "end": w["end"]})
    return {"server_time": now.isoformat(), "reminders": result, "busy_windows": meetings}
