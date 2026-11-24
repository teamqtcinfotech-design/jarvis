"""Nudges JARVIS raises on its own, so nothing quietly slips:

- a staff task with no update for 2 days          -> Sir (and Mansi, who can chase)
- a request from Mansi unanswered for 4 hours     -> Sir
- a task Sir gave Mansi not started after 1 day   -> Mansi
- a promised time has passed, work still open     -> the person (once per promise), in the giver's name
- at the giver's summary time: what they gave     -> the giver, once a day ("3 things you gave haven't moved")
  that is not seen / not started / past its promise or time
- 18:30: the day's slipped items                  -> Sir

Each nudge is an Alert; the phone speaks it when he is with the phone (or shows it quietly in a meeting).
Nudges are worded so the natural reply is a command ("move both to tomorrow", "remind Mansi"), never a
dangling yes/no. Each one is raised at most once per two days. Nothing is raised at night.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from django.db.models import Q

from . import ai_brain, scheduling, services
from . import timeutils as tu
from .models import Alert, Priority, Status, TaskRequest, User
from .services import (delegated_qs, get_md, get_pa, get_prefs, given_qs, md_private_qs, person_tasks_qs, private_qs,
                       shared_md_tasks_qs)

log = logging.getLogger("jarvis.followups")

STAFF_STALE = timedelta(days=2)
REQUEST_STALE = timedelta(hours=4)
PA_NOT_STARTED = timedelta(days=1)
REPEAT_AFTER = timedelta(days=2)
ACTIVE_HOURS = range(9, 20)  # 9:00 .. 19:59 local
DIGEST_AT = (18, 30)
BRIEFING_WINDOW = timedelta(hours=4)  # a briefing missed (worker down) is still given within this long, then dropped
OPEN = [Status.OPEN, Status.IN_PROGRESS, Status.BLOCKED]
REMIND_AGAIN = timedelta(minutes=45)  # the free-time suggestion comes back this often while it goes unanswered


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
    for user in services.team():
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


PILE_AT_LEAST = 3                      # fewer than this is not worth an "in which order?"
PILE_WINDOW = timedelta(minutes=15)    # how long after a meeting ends the question may still be asked


def piled_up(now=None) -> int:
    """Work that arrived while someone was in a meeting. When the meeting ends and three or more things came in
    during it, that person is told once, in a sensible order (urgent first, then the nearest deadline, then first
    come first served), and asked whether the order is right. One or two items are not worth the question."""
    from . import scheduling
    from .models import Kind

    now = now or timezone.now()
    raised = 0
    rank = {Priority.URGENT: 0, Priority.HIGH: 1, Priority.NORMAL: 2, Priority.LOW: 3}
    for person in services.team():

        def arrived(start, end) -> list[tuple]:
            """(priority, due or None, when it came, title) for everything that reached this person in that time."""
            if person.is_md:
                # What the PA sends the MD waits as a request until the MD accepts it.
                rows = TaskRequest.objects.filter(state=TaskRequest.State.PENDING, kind=TaskRequest.Kind.CREATE,
                                                  created_at__gte=start, created_at__lte=end)
                return [(str((r.payload or {}).get("priority") or Priority.NORMAL), None, r.created_at,
                         str((r.payload or {}).get("title") or "Request")) for r in rows]
            rows = person_tasks_qs(person).filter(status__in=OPEN, created_at__gte=start, created_at__lte=end) \
                .exclude(origin__in=services.OWN_ORIGINS)
            return [(t.priority, t.due_at, t.created_at, t.title) for t in rows]

        for c in scheduling.commitments(person, tu.to_local(now).date()):
            if c.get("kind") not in (Kind.MEETING, Kind.CALL) or not (now - PILE_WINDOW <= c["end"] <= now):
                continue
            items = arrived(c["start"], c["end"])
            if len(items) < PILE_AT_LEAST:
                continue
            far = now + timedelta(days=3650)
            items.sort(key=lambda t: (rank.get(t[0], 2), t[1] or far, t[2]))
            names = "; ".join(t[3] for t in items[:5])
            n = len(items)
            address, address_hi = services.address_of(person), services.address_hi_of(person)
            spoken = (f"{address}, {n} things came in while you were in the meeting: {names}. "
                      "Shall I keep them in this order, or should one come first?")
            spoken_hi = f"{address_hi}, मीटिंग के दौरान {n} काम आए: {names}। इसी क्रम में रखूँ, या कोई पहले करना है?"
            raised += _raise(person, f"pile:{c['id']}:{int(c['end'].timestamp())}", f"{n} things came in during your meeting",
                             names, spoken, spoken_hi=spoken_hi)
    return raised


def free_time(now=None) -> int:
    """"You're free till 4. Two late items could be done now: X, Y. Shall I put them in?" Said once per free stretch,
    to each person inside their own office hours, when late or pending work exists and fits (scheduling.free_now,
    scheduling.fit_into_free_time). A yes lays the items into the stretch (services.answer_place), a no leaves the
    day alone; neither is asked again for the same stretch."""
    now = now or timezone.now()
    raised = 0
    for user in services.team():
        prefs = get_prefs(user)
        slot = scheduling.free_now(user, prefs, now)
        if not slot:
            continue
        start, end = slot
        key = f"free:{tu.to_local(now).date().isoformat()}:{tu.to_local(end):%H%M}"
        # Once per free stretch; and again every REMIND_AGAIN while he has not answered (overdue work is what matters
        # most to him). A yes or a no settles the stretch: nothing is asked twice after an answer.
        asked = Alert.objects.filter(recipient=user, ref_type="followup", ref_id__startswith=f"{key}")
        if asked.filter(acknowledged_at__isnull=False).exists():
            continue  # answered (whichever copy): settled for this stretch
        latest = asked.order_by("-created_at").first()
        if latest is not None:
            if now - latest.created_at < REMIND_AGAIN:
                continue
            key = f"{key}:{int((now - latest.created_at).total_seconds() // 60)}m"
        dash = services.dashboard(user)
        pool = list(dash.get("overdue") or []) + list(dash.get("today") or []) + list(dash.get("anytime") or [])
        plan = scheduling.fit_into_free_time(pool, start, end, now)
        if not plan["placed"]:
            continue
        names = [p["title"] for p in plan["placed"]]
        n = len(names)
        late = sum(1 for p in plan["placed"] if p["overdue"])
        what, what_hi = ("late", "लेट") if late == n else ("pending", "बाकी")
        listed = ", ".join(names[:3]) + (f" and {n - 3} more" if n > 3 else "")  # the banner's body, never spoken
        till, till_hi = tu.fmt_time(end), tu.fmt_time_hi(end)
        address, address_hi = services.address_of(user), services.address_hi_of(user)
        # Spoken without a single title (his rule: never read names out unasked; "dikhao" shows them, the banner lists them).
        if n == 1:
            spoken = f"{address}, you're free till {till}. One {what} item could be done now. Shall I put it in?"
            spoken_hi = f"{address_hi}, आप {till_hi} तक फ्री हैं। एक {what_hi} काम अभी हो सकता है। रख दूँ?"
        else:
            spoken = f"{address}, you're free till {till}. {n} {what} items could be done now. Shall I put them in?"
            spoken_hi = f"{address_hi}, आप {till_hi} तक फ्री हैं। {n} {what_hi} काम अभी हो सकते हैं। रख दूँ?"
        question = {"kind": "place", "en": "Shall I put it in?" if n == 1 else "Shall I put them in?", "hi": "रख दूँ?",
                    "plan": plan["placed"], "until": end.isoformat()}
        raised += _raise(user, key, f"Free till {till}: {n} {what} item{'s' if n > 1 else ''} could be done", listed, spoken,
                         spoken_hi=spoken_hi, question=question)
    return raised


def run(now=None) -> int:
    """One pass. Safe to call every few minutes; it only raises what is due and not yet raised."""
    now = now or timezone.now()
    local = tu.to_local(now)
    raised = 0
    try:
        raised += piled_up(now)  # right after a meeting, whatever the hour
    except Exception:  # noqa: BLE001 — the other nudges must still run
        log.exception("piled-up check failed")
    try:
        raised += morning_briefings(now)  # has its own clock: the briefing may be set before ACTIVE_HOURS
    except Exception:  # noqa: BLE001 — the other nudges must still run
        log.exception("morning briefing failed")
    if local.hour not in ACTIVE_HOURS:
        return raised
    try:
        raised += free_time(now)  # free now, with late work: offer to use the time
    except Exception:  # noqa: BLE001 — the other nudges must still run
        log.exception("free-time suggestion failed")
    md, pa = get_md(), get_pa()
    if not md:
        return raised
    pa_name = pa.spoken_name if pa else "your PA"
    sir, sir_hi = services.md_address(), services.md_address_hi()
    # The rules are the MD's settings, not constants: how long staff may go quiet, how long a request may wait,
    # and when each person's evening summary comes.
    md_prefs = get_prefs(md)
    staff_stale = timedelta(days=int(md_prefs.get("staleStaffDays") or STAFF_STALE.days))
    request_stale = timedelta(hours=int(md_prefs.get("requestWaitHours") or REQUEST_STALE.total_seconds() // 3600))

    def digest_due(prefs: dict) -> bool:
        raw = str(prefs.get("digestTime") or f"{DIGEST_AT[0]:02d}:{DIGEST_AT[1]:02d}")
        try:
            hh, mm = int(raw[:2]), int(raw[3:5])
        except ValueError:
            hh, mm = DIGEST_AT
        return (local.hour, local.minute) >= (hh, mm)

    # 1) Staff tasks with no update for a while
    for task in delegated_qs().filter(status__in=OPEN):
        last = task.updated_at or task.created_at
        if not last or now - last < staff_stale:
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
    for req in TaskRequest.objects.filter(state=TaskRequest.State.PENDING, created_at__lte=now - request_stale).select_related("requested_by"):
        hours = int((now - req.created_at).total_seconds() // 3600)
        title = (req.payload or {}).get("title") or "a request"
        raised += _raise(md, f"request:{req.id}", "Request waiting for your decision", title,
                         f"{sir}, {req.requested_by.spoken_name}'s request '{title}' has been waiting {hours} hours. "
                         f"You can say: accept it, or decline it.", Priority.HIGH,
                         spoken_hi=f"{sir_hi}, {req.requested_by.spoken_name} की रिक्वेस्ट '{title}' {hours} घंटे से इंतज़ार में है। "
                                   f"आप कह सकते हैं: मंज़ूर करो, या मना करो।")

    # 3) Work someone gave a team member, not started after a day: the team member is nudged, in the giver's name
    members = [u for u in services.team() if not u.is_md]
    for person in members:
        for task in person_tasks_qs(person).filter(status=Status.OPEN, created_at__lte=now - PA_NOT_STARTED,
                                                   origin__in=services.GIVEN_ORIGINS):
            giver, giver_hi = services.giver_words(task.created_by)
            name = person.spoken_name
            raised += _raise(person, f"notstarted:{task.id}", "Not started yet", task.title,
                             f"{name}, {giver}'s task '{task.title}' is not started yet. You can say: start it, or put it on hold with a reason.",
                             spoken_hi=f"{name}, {giver_hi} का टास्क '{task.title}' अभी शुरू नहीं हुआ। "
                                       f"आप कह सकते हैं: शुरू करो, या कारण के साथ होल्ड पर रखो।")

    today = local.date()

    def due_day(t):
        return tu.to_local(t.due_at).date() if t.due_at else t.due_date

    # 3b) Chasing (Nov 2026), the person's side: a time they promised has passed and the work is still open. Asked
    # once per promise, in the giver's name; the answer (a new time, "can't", or "done" as a command) goes back.
    for person in services.team():
        promised = person_tasks_qs(person).filter(status__in=OPEN).exclude(origin__in=services.OWN_ORIGINS + services.MEET_ORIGINS) \
            .filter(Q(promise_at__lt=now) | Q(promise_at__isnull=True, promise_date__lt=today))
        for task in promised:
            giver, giver_hi = services.giver_words(task.created_by)
            label = tu.due_phrase(task.promise_kind, task.promise_date, task.promise_at)
            label_hi = tu.due_phrase_hi(task.promise_kind, task.promise_date, task.promise_at)
            stamp = (task.promise_at or task.promise_date).isoformat()[:16]
            name = person.spoken_name
            raised += _raise(person, f"promise:{task.id}:{stamp}", f"Promised {label}: {task.title}", task.title,
                             f"{name}, you said you'd do {giver}'s task '{task.title}' by {label}. Is it done, or by when now?",
                             Priority.HIGH,
                             spoken_hi=f"{name}, आपने कहा था {giver_hi} का काम '{task.title}' {label_hi} तक कर देंगे। हो गया, या अब कब तक?",
                             question={"kind": "promise", "en": "", "hi": "", "task": str(task.id)})

    # 3c) Chasing, the giver's side: at their summary time, once a day, the work they gave that has not moved: not
    # seen for hours, not started for a day, a promise gone by, or past its time. One line, with what to say next.
    for giver in services.team():
        key = f"chase:{today.isoformat()}"
        if not digest_due(get_prefs(giver)) or _already(key + f"@{giver.id}"):
            continue
        stuck, stuck_hi = [], []
        for t in given_qs(giver).filter(status__in=OPEN).exclude(origin__in=services.MEET_ORIGINS).select_related("assignee_user"):
            who = t.assignee_user.spoken_name if t.assignee_user else "someone"
            age = now - t.created_at
            d = due_day(t)
            if t.seen_at is None and age >= timedelta(hours=4):
                why, why_hi = "not seen yet", "अभी तक देखा नहीं"
            elif (t.promise_at and t.promise_at < now) or (t.promise_at is None and t.promise_date and t.promise_date < today):
                label = tu.due_phrase(t.promise_kind, t.promise_date, t.promise_at)
                why, why_hi = f"promised {label}", f"{tu.due_phrase_hi(t.promise_kind, t.promise_date, t.promise_at)} का वादा"
            elif d and d < today:
                days = (today - d).days
                why, why_hi = f"{days} day{'s' if days != 1 else ''} late", f"{days} दिन लेट"
            elif t.status == Status.OPEN and age >= PA_NOT_STARTED and not t.promise_date:
                why, why_hi = "not started", "शुरू नहीं हुआ"
            else:
                continue
            stuck.append(f"{t.title} ({who}, {why})")
            stuck_hi.append(f"{t.title} ({who}, {why_hi})")
        if stuck:
            n = len(stuck)
            address, address_hi = services.address_of(giver), services.address_hi_of(giver)
            spoken = (f"{address}, {n} thing{'s' if n > 1 else ''} you gave {'have' if n > 1 else 'has'}n't moved: " + "; ".join(stuck[:4])
                      + ". You can say: remind them, move the date, or give it to someone else.")
            spoken_hi = (f"{address_hi}, आपके दिए {n} काम रुके हुए हैं: " + "; ".join(stuck_hi[:4])
                         + "। आप कह सकते हैं: याद दिलाओ, तारीख़ बदलो, या किसी और को दो।")
            raised += _raise(giver, key, f"{n} thing{'s' if n > 1 else ''} you gave hasn't moved", "; ".join(stuck[:4]), spoken,
                             spoken_hi=spoken_hi)

    # 4) End of day: what slipped, for each person about their own list, at each one's own summary time

    key = f"digest:{today.isoformat()}"
    for person in members:
        if not digest_due(get_prefs(person)) or _already(key + f"@{person.id}"):
            continue
        theirs = [t.title for t in person_tasks_qs(person).filter(status__in=OPEN).exclude(origin__in=services.MEET_ORIGINS)
                  if (d := due_day(t)) and d <= today]
        own_private = sum(1 for t in private_qs(person).filter(status__in=OPEN) if (d := due_day(t)) and d <= today)
        if theirs or own_private:
            name = person.spoken_name
            n = len(theirs) + own_private
            listed = "; ".join(theirs[:3]) or f"{own_private} personal"
            spoken = (f"{name}, {n} thing{'s' if n > 1 else ''} slipped today: " + listed +
                      ". Tell me if you want them moved, or put on hold with a reason.")
            spoken_hi = (f"{name}, आज {n} काम रह गए: " + ("; ".join(theirs[:3]) or f"{own_private} पर्सनल") +
                         "। बताइए अगर आगे बढ़ाऊँ, या कारण के साथ होल्ड पर रखूँ।")
            raised += _raise(person, key, f"{n} slipped today", listed, spoken, spoken_hi=spoken_hi)
    if digest_due(md_prefs):
        if digest_due(md_prefs) and not _already(key + f"@{md.id}"):
            # Oldest first, and how long it has been waiting: a thing three days late is said as such.
            def late_label(t, hindi=False):
                days = (today - due_day(t)).days
                if days <= 0:
                    return t.title
                return f"{t.title} ({days} {'दिन से' if hindi else 'day' + ('s' if days > 1 else '')})"
            open_md = sorted([t for t in shared_md_tasks_qs().filter(status__in=OPEN) if (d := due_day(t)) and d <= today],
                             key=lambda t: due_day(t))
            slipped = [t.title for t in open_md]
            private_count = sum(1 for t in md_private_qs(md).filter(status__in=OPEN) if (d := due_day(t)) and d <= today)
            total = private_count + len(slipped)
            if total:
                parts, parts_hi = [], []
                if slipped:
                    parts.append("; ".join(late_label(t) for t in open_md[:3]))
                    parts_hi.append("; ".join(late_label(t, True) for t in open_md[:3]))
                if private_count:
                    parts.append(f"{private_count} private")
                    parts_hi.append(f"{private_count} पर्सनल")
                oldest = (today - due_day(open_md[0])).days if open_md else 0
                head = (f"{sir}, {total} thing{'s' if total > 1 else ''} still open, the oldest {oldest} days: " if oldest > 1
                        else f"{sir}, {total} thing{'s' if total > 1 else ''} slipped today: ")
                head_hi = (f"{sir_hi}, {total} काम अभी भी बाकी, सबसे पुराना {oldest} दिन से: " if oldest > 1
                           else f"{sir_hi}, आज {total} काम रह गए: ")
                spoken = head + " and ".join(parts) + ". Tell me if you want them moved to tomorrow."
                spoken_hi = head_hi + " और ".join(parts_hi) + "। बताइए अगर कल पर ले जाऊँ।"
                raised += _raise(md, key, f"{total} slipped today", " · ".join(parts), spoken, spoken_hi=spoken_hi)

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
        for person in members:
            if _already(key + f"@{person.id}"):
                continue
            undated = [t.title for t in person_tasks_qs(person).filter(status__in=OPEN, due_at__isnull=True, due_date__isnull=True)
                       .exclude(origin__in=services.MEET_ORIGINS)]
            if undated:
                name = person.spoken_name
                spoken = (f"{name}, {len(undated)} task{'s' if len(undated) > 1 else ''} still {'have' if len(undated) > 1 else 'has'} "
                          f"no date: " + "; ".join(undated[:3]) + ". Say a day for any of them whenever you like.")
                spoken_hi = (f"{name}, {len(undated)} टास्क की अभी भी कोई तारीख़ नहीं है: " + "; ".join(undated[:3])
                             + "। जब चाहें, किसी के लिए भी दिन बता दीजिए।")
                raised += _raise(person, key, f"{len(undated)} without a date", "; ".join(undated[:3]), spoken, Priority.LOW,
                                 spoken_hi=spoken_hi)
    if raised:
        log.info("raised %d follow-up(s)", raised)
    return raised
