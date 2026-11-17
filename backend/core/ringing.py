"""Ring until seen: work given and messages sent by a person keep ringing the receiver's phone ("ting ting") every few
minutes until they tap Seen, open it, or answer JARVIS about it.

The alert itself is the first ring (its push carries ring=1, see push.payload_for). The worker calls ``run()`` every
couple of seconds, which:
  1. stops the ringing of everything seen, done or withdrawn since (a "ring_stop" push takes the notification off
     every phone of that person);
  2. rings what is due again (a "ring" push: the phone plays the ting and shows the Seen button), every minute for
     urgent work and every five otherwise (the receiver's settings), holding a non-urgent ring through their quiet
     hours;
  3. after the ringing time (30 minutes by default) gives up and tells the sender "not seen yet. Ring again, or give it
     to someone else?".
Without live push the phone's own watcher rings from the alert list (it knows the ring count), so nothing depends on
Firebase. Best effort: one person's failure never stops the others.
"""
import logging
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from . import lang, push, services
from .models import Alert, Priority, TaskEvent

log = logging.getLogger("jarvis.ringing")

BATCH = 200


def ring_payload(alert: Alert) -> dict[str, str]:
    """What the phone needs to ring and show the Seen button without another round trip (in the reader's language)."""
    hindi = lang.current(alert.recipient) == "hi"
    prefs = services.get_prefs(alert.recipient)
    text = (alert.spoken_text_hi if hindi and alert.spoken_text_hi else alert.body) or alert.title
    return {
        "type": "ring",
        "id": str(alert.id),
        "kind": alert.kind,
        "priority": alert.priority,
        "title": (services.alert_title_hi(alert) if hindi else alert.title)[:200],
        "body": push._fit(text, 1100),
        "ring_no": str(alert.rings),
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


def run(now: datetime | None = None) -> dict:
    """One pass. Returns {"stopped", "rung", "given_up"} counts."""
    now = now or timezone.now()
    out = {"stopped": 0, "rung": 0, "given_up": 0}

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

    # 2) Due to ring again (or to give up).
    due = (Alert.objects.filter(ring=True, next_ring_at__lte=now, seen_at__isnull=True, acknowledged_at__isnull=True)
           .select_related("recipient", "sender").order_by("next_ring_at")[:BATCH])
    for alert in due:
        try:
            person = alert.recipient
            prefs = services.get_prefs(person)
            if not person.is_active or not prefs.get("ringUntilSeen", True):
                Alert.objects.filter(id=alert.id).update(next_ring_at=None)
                if alert.rings:
                    _send(person, {"type": "ring_stop", "id": str(alert.id)})
                continue
            urgent = alert.priority == Priority.URGENT
            hold = None if urgent else services.quiet_until(prefs, now)
            if hold:
                # Quiet hours: no ring until they end; the ringing time starts afresh then.
                Alert.objects.filter(id=alert.id).update(next_ring_at=hold, ring_started_at=None)
                continue
            if alert.ring_started_at and now - alert.ring_started_at >= services.ring_for(prefs):
                _give_up(alert, now)
                out["given_up"] += 1
                continue
            started = alert.ring_started_at or now
            claimed = Alert.objects.filter(id=alert.id, next_ring_at=alert.next_ring_at).update(
                rings=alert.rings + 1, ring_started_at=started, next_ring_at=now + services.ring_every(prefs, alert.priority))
            if not claimed:
                continue  # changed meanwhile (seen, or another pass took it)
            alert.rings += 1
            alert.ring_started_at = started
            _send(person, ring_payload(alert), urgent=urgent)
            out["rung"] += 1
        except Exception:  # noqa: BLE001 — one alert must never stop the others
            log.exception("ring failed for alert %s", alert.id)
    return out
