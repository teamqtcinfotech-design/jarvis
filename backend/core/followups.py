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

from . import ai_brain, scheduling, services
from . import timeutils as tu
from .models import Alert, Priority, SharedTask, Status, TaskRequest, User
from .services import delegated_qs, get_md, get_pa, get_prefs, md_private_qs, pa_tasks_qs, shared_md_tasks_qs

log = logging.getLogger("jarvis.followups")

STAFF_STALE = timedelta(days=2)
REQUEST_STALE = timedelta(hours=4)
PA_NOT_STARTED = timedelta(days=1)
REPEAT_AFTER = timedelta(days=2)
ACTIVE_HOURS = range(9, 20)  # 9:00 .. 19:59 local
DIGEST_AT = (18, 30)
BRIEFING_WINDOW = timedelta(hours=4)  # a briefing missed (worker down) is still given within this long, then dropped
OPEN = [Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED]


def _already(key: str) -> bool:
    return Alert.objects.filter(ref_type="followup", ref_id=key, created_at__gte=timezone.now() - REPEAT_AFTER).exists()


def _raise(recipient: User, key: str, title: str, body: str, spoken: str, priority: str = Priority.NORMAL,
           spoken_hi: str = "", question: dict | None = None) -> bool:
    if _already(key + f"@{recipient.id}"):
        return False
    Alert.objects.create(recipient=recipient, kind=Alert.Kind.INFO, priority=priority, title=title[:200], body=body[:2000],
                         spoken_text=spoken[:2000], spoken_text_hi=spoken_hi[:2000], ref_type="followup",
                         ref_id=(key + f"@{recipient.id}")[:64], question=question)
    return True


def _days(delta: timedelta) -> str:
    d = delta.days
    return f"{d} day{'s' if d != 1 else ''}"


def _days_hi(delta: timedelta) -> str:
    return f"{delta.days} दिन"


def morning_briefings(now=None) -> int:
    """At each person's chosen time: one spoken briefing of the day, raised as an alert.

    Private items are counted and timed but never named in the stored text (an alert row is not encrypted and may
    be spoken from a locked phone); he can ask JARVIS for the details.
    """
    now = now or timezone.now()
    local = tu.to_local(now)
    raised = 0
    for user in [u for u in (get_md(), get_pa()) if u]:
        prefs = get_prefs(user)
        if not prefs.get("briefingEnabled", True):
            continue
        hhmm = prefs.get("briefingTime") or "09:00"
        try:
            hour, minute = int(hhmm[:2]), int(hhmm[3:5])
        except ValueError:
            hour, minute = 9, 0
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not (at <= local < at + BRIEFING_WINDOW):
            continue
        key = f"morning:{local.date().isoformat()}"
        if _already(key + f"@{user.id}"):
            continue
        facts, fallback = services.briefing_facts(user, "morning", conceal_private=True)
        if scheduling.is_off_day(prefs, local.date()) and not facts["today"] and not facts["overdue"]:
            continue  # a quiet day off: nothing to say
        move = (facts.get("day_load") or {}).pop("could_move_item", None)  # for the yes below, not for the model
        text, _engine = ai_brain.briefing(facts, fallback, user=user)
        # The briefing may have offered one move ("Shall I move X to tomorrow?"): the yes to it is a real move.
        question = None
        if move:
            question = {"kind": "move", "en": f"Shall I move {move['title']} to tomorrow?",
                        "hi": f"{move['title']} कल पर ले जाऊँ?",
                        "target": {"id": move["id"], "scope": move["scope"], "title": move["title"], "version": move["version"]},
                        "to": (local.date() + timedelta(days=1)).isoformat()}
        raised += _raise(user, key, "Good morning, here is your day", text, text, question=question)
    return raised


def run(now=None) -> int:
    """One pass. Safe to call every few minutes; it only raises what is due and not yet raised."""
    now = now or timezone.now()
    local = tu.to_local(now)
    raised = 0
    try:
        raised += morning_briefings(now)  # has its own clock: the briefing may be set before ACTIVE_HOURS
    except Exception:  # noqa: BLE001 — the other nudges must still run
        log.exception("morning briefing failed")
    if local.hour not in ACTIVE_HOURS:
        return raised
    md, pa = get_md(), get_pa()
    if not md:
        return raised
    pa_name = pa.spoken_name if pa else "your PA"
    sir, sir_hi = services.md_address(), services.md_address_hi()

    # 1) Staff tasks with no update for 2 days
    for task in delegated_qs().filter(status__in=OPEN):
        last = task.updated_at or task.created_at
        if not last or now - last < STAFF_STALE:
            continue
        who = task.staff.name if task.staff else "a staff member"
        age, age_hi = _days(now - last), _days_hi(now - last)
        key = f"stale:{task.id}"
        raised += _raise(md, key, f"{who}: no update for {age}", task.title,
                         f"{sir}, {who}'s task '{task.title}' has had no update for {age}. "
                         f"You can say: remind {pa_name}, or move the date.",
                         spoken_hi=f"{sir_hi}, {who} के टास्क '{task.title}' पर {age_hi} से कोई अपडेट नहीं है। "
                                   f"आप कह सकते हैं: {pa_name} को याद दिलाओ, या तारीख़ बदलो।")
        if pa:
            raised += _raise(pa, key, f"{who}: no update for {age}", task.title,
                             f"{pa_name}, {who}'s task '{task.title}' has had no update for {age}. Please check with them.",
                             spoken_hi=f"{pa_name}, {who} के टास्क '{task.title}' पर {age_hi} से कोई अपडेट नहीं है। कृपया उनसे पूछ लीजिए।")

    # 2) Mansi's requests waiting for Sir
    for req in TaskRequest.objects.filter(state=TaskRequest.State.PENDING, created_at__lte=now - REQUEST_STALE).select_related("requested_by"):
        hours = int((now - req.created_at).total_seconds() // 3600)
        title = (req.payload or {}).get("title") or "a request"
        raised += _raise(md, f"request:{req.id}", "Request waiting for your decision", title,
                         f"{sir}, {req.requested_by.spoken_name}'s request '{title}' has been waiting {hours} hours. "
                         f"You can say: accept it, or decline it.", Priority.HIGH,
                         spoken_hi=f"{sir_hi}, {req.requested_by.spoken_name} की रिक्वेस्ट '{title}' {hours} घंटे से इंतज़ार में है। "
                                   f"आप कह सकते हैं: मंज़ूर करो, या मना करो।")

    # 3) Tasks Sir gave Mansi, not started after a day
    if pa:
        for task in pa_tasks_qs().filter(status=Status.OPEN, created_at__lte=now - PA_NOT_STARTED).exclude(origin=SharedTask.Origin.MD_MEET_PA):
            raised += _raise(pa, f"notstarted:{task.id}", "Not started yet", task.title,
                             f"{pa_name}, {sir}'s task '{task.title}' is not started yet. You can say: start it, or put it on hold with a reason.",
                             spoken_hi=f"{pa_name}, {sir_hi} का टास्क '{task.title}' अभी शुरू नहीं हुआ। "
                                       f"आप कह सकते हैं: शुरू करो, या कारण के साथ होल्ड पर रखो।")

    # 4) End of day: what slipped, for each of them about their own list
    if (local.hour, local.minute) >= DIGEST_AT:
        today = local.date()

        def due_day(t):
            return tu.to_local(t.due_at).date() if t.due_at else t.due_date

        key = f"digest:{today.isoformat()}"
        if not _already(key + f"@{md.id}"):
            slipped = [t.title for t in shared_md_tasks_qs().filter(status__in=OPEN) if (d := due_day(t)) and d <= today]
            private_count = sum(1 for t in md_private_qs(md).filter(status__in=OPEN) if (d := due_day(t)) and d <= today)
            total = private_count + len(slipped)
            if total:
                parts, parts_hi = [], []
                if slipped:
                    parts.append("; ".join(slipped[:3]))
                    parts_hi.append("; ".join(slipped[:3]))
                if private_count:
                    parts.append(f"{private_count} private")
                    parts_hi.append(f"{private_count} पर्सनल")
                spoken = (f"{sir}, {total} thing{'s' if total > 1 else ''} slipped today: " + " and ".join(parts) +
                          ". Tell me if you want them moved to tomorrow.")
                spoken_hi = f"{sir_hi}, आज {total} काम रह गए: " + " और ".join(parts_hi) + "। बताइए अगर कल पर ले जाऊँ।"
                raised += _raise(md, key, f"{total} slipped today", " · ".join(parts), spoken, spoken_hi=spoken_hi)
        if pa and not _already(key + f"@{pa.id}"):
            hers = [t.title for t in pa_tasks_qs().filter(status__in=OPEN).exclude(origin=SharedTask.Origin.MD_MEET_PA)
                    if (d := due_day(t)) and d <= today]
            if hers:
                spoken = (f"{pa_name}, {len(hers)} task{'s' if len(hers) > 1 else ''} slipped today: " + "; ".join(hers[:3]) +
                          ". Tell me if you want them moved, or put on hold with a reason.")
                spoken_hi = (f"{pa_name}, आज {len(hers)} टास्क रह गए: " + "; ".join(hers[:3]) +
                             "। बताइए अगर आगे बढ़ाऊँ, या कारण के साथ होल्ड पर रखूँ।")
                raised += _raise(pa, key, f"{len(hers)} slipped today", "; ".join(hers[:3]), spoken, spoken_hi=spoken_hi)

    # 5) Once a week (Monday from 10:00): tasks that still have no date, so they are not forgotten
    if local.weekday() == 0 and local.hour >= 10:
        key = f"nodate:{local.isocalendar()[0]}-{local.isocalendar()[1]}"
        if not _already(key + f"@{md.id}"):
            undated = [t.title for t in shared_md_tasks_qs().filter(status__in=OPEN, due_at__isnull=True, due_date__isnull=True)]
            private_undated = md_private_qs(md).filter(status__in=OPEN, due_at__isnull=True, due_date__isnull=True).count()
            total = len(undated) + private_undated
            if total:
                parts = ([f"{len(undated)} official" if undated else ""] + [f"{private_undated} private" if private_undated else ""])
                parts_hi = ([f"{len(undated)} ऑफ़िशियल" if undated else ""] + [f"{private_undated} पर्सनल" if private_undated else ""])
                spoken = (f"{sir}, {total} task{'s' if total > 1 else ''} still {'have' if total > 1 else 'has'} no date: "
                          + " and ".join(p for p in parts if p) + ". Say a day for any of them whenever you like.")
                spoken_hi = (f"{sir_hi}, {total} टास्क की अभी भी कोई तारीख़ नहीं है: " + " और ".join(p for p in parts_hi if p)
                             + "। जब चाहें, किसी के लिए भी दिन बता दीजिए।")
                raised += _raise(md, key, f"{total} without a date", "; ".join(undated[:3]) or f"{private_undated} private", spoken,
                                 Priority.LOW, spoken_hi=spoken_hi)
        if pa and not _already(key + f"@{pa.id}"):
            undated = [t.title for t in pa_tasks_qs().filter(status__in=OPEN, due_at__isnull=True, due_date__isnull=True)
                       .exclude(origin=SharedTask.Origin.MD_MEET_PA)]
            if undated:
                spoken = (f"{pa_name}, {len(undated)} task{'s' if len(undated) > 1 else ''} still {'have' if len(undated) > 1 else 'has'} "
                          f"no date: " + "; ".join(undated[:3]) + ". Say a day for any of them whenever you like.")
                spoken_hi = (f"{pa_name}, {len(undated)} टास्क की अभी भी कोई तारीख़ नहीं है: " + "; ".join(undated[:3])
                             + "। जब चाहें, किसी के लिए भी दिन बता दीजिए।")
                raised += _raise(pa, key, f"{len(undated)} without a date", "; ".join(undated[:3]), spoken, Priority.LOW,
                                 spoken_hi=spoken_hi)
    if raised:
        log.info("raised %d follow-up(s)", raised)
    return raised
