"""Ring until seen: work given and messages sent by a person keep ringing the receiver's phone ("ting ting") every few
minutes until they tap Seen, open it, or answer JARVIS about it.

The alert itself is the first ring (its push carries ring=1, see push.payload_for). The worker calls ``run()`` every
couple of seconds, which:
  1. stops the ringing of everything seen, done or withdrawn since (a "ring_stop" push takes the notification off
     every phone of that person);
  2. rings what is due again: ONE "ring" push per person carrying everything of theirs still waiting (one "ting"
     for all of it, the notification says "3 waiting"), every minute when any of it is urgent and every ten minutes
     otherwise (the receiver's settings); from then on their clocks run together. A non-urgent ring waits through
     their quiet hours;
  3. after the ringing time (30 minutes by default) gives up on an item and tells its sender "not seen yet. Ring
     again, or give it to someone else?".
Without live push the phone's own watcher rings from the alert list (it knows each ring count), so nothing depends
on Firebase. Best effort: one person's failure never stops the others.
"""
import json
import logging
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from . import lang, push, services
from .models import PRIORITY_RANK, Alert, Priority, TaskEvent

log = logging.getLogger("jarvis.ringing")

BATCH = 200
ITEMS_IN_PUSH = 5  # a push is 4 KB: the first few in full, the rest counted


def _item(alert: Alert, hindi: bool) -> dict:
    text = (alert.spoken_text_hi if hindi and alert.spoken_text_hi else alert.body) or alert.title
    sender = alert.sender
    return {
        "id": str(alert.id),
        "title": (services.alert_title_hi(alert) if hindi else alert.title)[:120],
        "body": push._fit(text, 240),
        "priority": alert.priority,
        "ring_no": alert.rings,
        "from": (services.giver_words(sender)[0] if sender else "")[:40],
    }


def ring_payload(person, alerts: list[Alert]) -> dict[str, str]:
    """What the phone needs to ring once for everything waiting and show each item with its Seen button, in the
    reader's language, without another round trip."""
    hindi = lang.current(person) == "hi"
    prefs = services.get_prefs(person)
    ordered = sorted(alerts, key=lambda a: (PRIORITY_RANK.get(a.priority, 9), a.created_at))
    items = [_item(a, hindi) for a in ordered[:ITEMS_IN_PUSH]]
    n = len(ordered)
    if n == 1:
        title, body = items[0]["title"], items[0]["body"]
    else:
        title = f"{n} चीज़ें आपके देखने का इंतज़ार कर रही हैं" if hindi else f"{n} waiting for you"
        lines = [f"{i['from'] + ': ' if i['from'] else ''}{i['body'] or i['title']}" for i in items]
        if n > len(items):
            lines.append(f"… और {n - len(items)}" if hindi else f"… and {n - len(items)} more")
        body = push._fit("\n".join(lines), 900)
    return {
        "type": "ring",
        "count": str(n),
        "ids": ",".join(i["id"] for i in items),
        "items": json.dumps(items, ensure_ascii=False),
        "title": title[:200],
        "body": body,
        "priority": ordered[0].priority,
        # Meeting mode on: it vibrates, no sound (the phone's own silent switch is obeyed by Android anyway).
        "quiet": "1" if prefs.get("meetingMode") else "",
        "seen_label": "देख लिया" if hindi else "Seen",
    }


def _send(user, data: dict[str, str], urgent: bool = False) -> None:
    if not push.configured():
        return
    try:
        push.send_to_user(user, data, urgent=urgent)
    except Exception:  # noqa: BLE001 — a ring that does not go out is retried by the phone's own watcher
        log.exception("ring push failed")


def _stop(alert: Alert) -> None:
    Alert.objects.filter(id=alert.id).update(next_ring_at=None)
    if alert.rings:
        _send(alert.recipient, {"type": "ring_stop", "id": str(alert.id)})


def _give_up(alert: Alert, now: datetime) -> None:
    """Rang for its full time and still not seen: the ringing stops and the sender is asked what to do."""
    Alert.objects.filter(id=alert.id).update(next_ring_at=None, unseen_told_at=now)
    if alert.rings:
        _send(alert.recipient, {"type": "ring_stop", "id": str(alert.id)})
    sender, person = alert.sender, alert.recipient
    if sender is None or not sender.is_active or sender.id == person.id:
        return
    task = services.task_of(alert)
    if task is not None and (task.is_closed or task.assignee_user_id != person.id):
        return  # done, cancelled or given to someone else meanwhile: nothing to report
    started = alert.ring_started_at or alert.created_at
    minutes = max(1, int((now - started).total_seconds() // 60))
    name, to, to_hi = person.spoken_name, services.address_of(sender), services.address_hi_of(sender)
    what = task.title if task is not None else " ".join((alert.body or alert.title).split())[:120]
    if task is not None:
        question = {"kind": "unseen", "en": "Ring again, or give it to someone else?",
                    "hi": "फिर से रिंग करूँ, या किसी और को दे दूँ?", "alert": str(alert.id), "task": str(task.id)}
    else:
        question = {"kind": "unseen", "en": "Shall I ring again?", "hi": "फिर से रिंग करूँ?", "alert": str(alert.id), "task": ""}
    Alert.objects.create(
        recipient=sender, sender=person, kind=Alert.Kind.INFO,
        priority=alert.priority if alert.priority in (Priority.URGENT, Priority.HIGH) else Priority.NORMAL,
        title=f"{name} hasn't seen it", body=what[:400],
        spoken_text=f"{to}, {name} hasn't seen it yet: {what}."[:400],
        spoken_text_hi=f"{to_hi}, {name} ने अभी तक नहीं देखा: {what}।"[:400],
        ref_type="unseen", ref_id=str(alert.id), question=question,
    )
    if task is not None:
        services.log_event(task, TaskEvent.Kind.NOT_SEEN, None, to=person.id, minutes=minutes)


def _ring_person(person, now: datetime, out: dict) -> None:
    """One ring for everything this person still has waiting (at least one item of it is due)."""
    prefs = services.get_prefs(person)
    waiting = list(Alert.objects.filter(recipient=person, ring=True, seen_at__isnull=True, acknowledged_at__isnull=True,
                                        next_ring_at__isnull=False).select_related("recipient", "sender"))
    if not person.is_active or not prefs.get("ringUntilSeen", True):
        for alert in waiting:
            _stop(alert)
        return
    hold = services.quiet_until(prefs, now)
    ring_for = services.ring_for(prefs)
    together: list[Alert] = []
    for alert in waiting:
        urgent = alert.priority == Priority.URGENT
        if hold and not urgent:
            # Quiet hours: no ring until they end; the ringing time starts afresh then.
            if alert.next_ring_at <= now:
                Alert.objects.filter(id=alert.id).update(next_ring_at=hold, ring_started_at=None)
            continue
        if alert.ring_started_at and now - alert.ring_started_at >= ring_for:
            _give_up(alert, now)
            out["given_up"] += 1
            continue
        together.append(alert)
    if not together or not any(a.next_ring_at <= now for a in together):
        return
    # Everything still waiting rings together, and from here on their clocks run together: every minute while any
    # of it is urgent, else the person's usual interval.
    urgent = any(a.priority == Priority.URGENT for a in together)
    next_at = now + services.ring_every(prefs, Priority.URGENT if urgent else Priority.NORMAL)
    rung = []
    for alert in together:
        started = alert.ring_started_at or now
        claimed = Alert.objects.filter(id=alert.id, next_ring_at=alert.next_ring_at, seen_at__isnull=True).update(
            rings=alert.rings + 1, ring_started_at=started, next_ring_at=next_at)
        if not claimed:
            continue  # changed meanwhile (seen, or another pass took it)
        alert.rings += 1
        alert.ring_started_at = started
        rung.append(alert)
    if rung:
        _send(person, ring_payload(person, rung), urgent=urgent)
        out["rung"] += len(rung)
        out["pushes"] += 1


def run(now: datetime | None = None) -> dict:
    """One pass. Returns {"stopped", "rung", "pushes", "given_up"} counts (rung = items, pushes = people rung)."""
    now = now or timezone.now()
    out = {"stopped": 0, "rung": 0, "pushes": 0, "given_up": 0}

    # 1) Seen, done, withdrawn or switched off: the ringing stops on the person's phones.
    stop = (Alert.objects.filter(next_ring_at__isnull=False)
            .filter(Q(seen_at__isnull=False) | Q(acknowledged_at__isnull=False) | Q(ring=False))
            .select_related("recipient")[:BATCH])
    for alert in stop:
        if not Alert.objects.filter(id=alert.id, next_ring_at=alert.next_ring_at).update(next_ring_at=None):
            continue
        if alert.rings:
            _send(alert.recipient, {"type": "ring_stop", "id": str(alert.id)})
        out["stopped"] += 1

    # 2) Due to ring again (or to give up), one person at a time.
    due = (Alert.objects.filter(ring=True, next_ring_at__lte=now, seen_at__isnull=True, acknowledged_at__isnull=True)
           .select_related("recipient").order_by("next_ring_at")[:BATCH])
    people = {}
    for alert in due:
        people.setdefault(alert.recipient_id, alert.recipient)
    for person in people.values():
        try:
            _ring_person(person, now, out)
        except Exception:  # noqa: BLE001 — one person must never stop the others
            log.exception("ring failed for %s", person.username)
    return out
