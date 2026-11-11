"""Reminders pushed by the server at their moment.

The phone's background part polls the reminder list every half minute, so a reminder could be up to half a minute
late; a live push (Firebase) at the exact second closes that gap. The worker calls ``run()`` every couple of
seconds: every reminder whose moment has just passed and that nobody has told yet is pushed once to its reader's
phones with everything needed to show and speak it. The poll stays as the safety net (and the only path when push
is not configured). Told-once is unchanged: the phone marks it told when it speaks it.
"""
import json
import logging
from datetime import datetime, timedelta

from django.utils import timezone

from . import push
from .models import User
from .reminders import reminders_for

log = logging.getLogger("jarvis.reminderpush")

LOOK_BACK = timedelta(seconds=45)   # a cycle that ran late still pushes what came due meanwhile
STATE: dict = {"checked_at": None, "pushed": []}  # keys pushed this process lifetime (bounded), so nothing goes twice


def payload_for(item: dict) -> dict[str, str]:
    """Everything the phone needs to show and speak the reminder without another round trip (FCM allows 4 KB)."""
    return {
        "type": "reminder",
        "key": item["key"],
        "title": item["title"][:200],
        "spoken": (item.get("spoken") or "")[:1500],
        "priority": item.get("priority", "NORMAL"),
        "kind": item.get("kind", "TASK"),
        "is_private": "1" if item.get("is_private") else "",
        "fire_at": item["fire_at"],
        "question": json.dumps(item["question"], ensure_ascii=False) if item.get("question") else "",
        "lang": item.get("lang", "en"),
    }


def run(now: datetime | None = None) -> int:
    """Push every reminder that came due since the last cycle. Returns how many were pushed."""
    if not push.configured():
        return 0
    now = now or timezone.now()
    since = (STATE["checked_at"] or now - LOOK_BACK) - timedelta(seconds=2)
    STATE["checked_at"] = now
    pushed = STATE["pushed"]
    n = 0
    for user in User.objects.filter(role__in=[User.Role.MD, User.Role.PA], is_active=True):
        try:
            items = reminders_for(user, hours=1)["reminders"]
        except Exception:  # noqa: BLE001 — one person's list must not stop the other's
            log.exception("reminder list failed for %s", user.username)
            continue
        for item in items:
            if item.get("told_at") or item["key"] in pushed:
                continue
            fire = datetime.fromisoformat(item["fire_at"])
            if not (since <= fire <= now):
                continue
            try:
                result = push.send_to_user(user, payload_for(item), urgent=item.get("priority") == "URGENT")
            except Exception:  # noqa: BLE001
                log.exception("reminder push failed")
                continue
            pushed.append(item["key"])
            if result.get("sent"):
                n += 1
    if len(pushed) > 500:
        del pushed[:-500]
    return n
