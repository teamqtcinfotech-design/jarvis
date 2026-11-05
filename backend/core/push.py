"""Live push through Firebase Cloud Messaging, so an alert reaches a closed app within seconds.

Every Alert row that is created is pushed to the recipient's phones as a small data message (the phone's
background part shows and speaks it straight away). The half-minute poll stays as the safety net.

Setup (.env), one of:
  FIREBASE_SERVICE_ACCOUNT_FILE   path to the Firebase service-account key (JSON)
  FIREBASE_SERVICE_ACCOUNT_JSON   the key's JSON itself (as is, or base64)
Without either, push is simply off and everything else works the same. The key never leaves the server.

Best effort, never raises into business code: a push that fails must never undo a task or a request.
"""
import base64
import json
import logging
import threading

from django.conf import settings
from django.db import connection

from .models import Alert, PushDevice

log = logging.getLogger("jarvis.push")

MAX_DEVICES_PER_USER = 10
TTL_SECONDS = 24 * 60 * 60
# FCM's answers that mean "this token will never work again".
DEAD_TOKEN_CODES = {"NOT_FOUND", "UNREGISTERED", "INVALID_ARGUMENT"}
SYNC = False  # tests: send inline instead of in a background thread

_app = None
_failed = False
_lock = threading.Lock()


def _credential_source():
    path = (getattr(settings, "FIREBASE_SERVICE_ACCOUNT_FILE", "") or "").strip()
    if path:
        return path
    raw = (getattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw if raw.startswith("{") else base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.error("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON; push stays off.")
        return None


def configured() -> bool:
    return bool(getattr(settings, "JARVIS_PUSH_ENABLED", True)) and _credential_source() is not None


def describe() -> str:
    if not configured():
        return "off"
    return "failed to start" if _failed else "on (Firebase Cloud Messaging)"


def _messaging():
    """The firebase_admin messaging module, initialised once; None when push is off or could not start."""
    global _app, _failed
    if _failed or not configured():
        return None
    with _lock:
        if _app is None:
            try:
                import firebase_admin
                from firebase_admin import credentials

                source = _credential_source()
                cred = credentials.Certificate(source)
                _app = firebase_admin.initialize_app(cred, name="jarvis-push")
            except Exception:  # noqa: BLE001 — push is optional; the reason goes to the log
                _failed = True
                log.exception("Firebase Cloud Messaging could not start; push stays off.")
                return None
    from firebase_admin import messaging
    return messaging


# ------------------------------------------------------------------ devices
def register_device(user, token: str, platform: str = "android") -> PushDevice:
    token = (token or "").strip()
    if not token or len(token) > 512:
        raise ValueError("A device token is required.")
    # A token belongs to one phone; if another account signs in on that phone, it moves to that account.
    device, _ = PushDevice.objects.update_or_create(token=token, defaults={"user": user, "platform": platform[:16], "failures": 0})
    extra = PushDevice.objects.filter(user=user).order_by("-updated_at")[MAX_DEVICES_PER_USER:]
    if extra:
        PushDevice.objects.filter(id__in=[d.id for d in extra]).delete()
    return device


def forget_device(user, token: str) -> int:
    return PushDevice.objects.filter(user=user, token=(token or "").strip()).delete()[0]


# ------------------------------------------------------------------ sending
def payload_for(alert: Alert) -> dict[str, str]:
    """Everything the phone needs to show and speak the alert without another round trip (FCM allows 4 KB)."""
    return {
        "type": "alert",
        "id": str(alert.id),
        "kind": alert.kind,
        "priority": alert.priority,
        "title": alert.title[:200],
        "body": (alert.body or "")[:1500],
        "spoken": (alert.spoken_text or "")[:1500],
        "ref_type": alert.ref_type,
        "ref_id": alert.ref_id,
    }


def _deliver(tokens: list[str], data: dict[str, str], urgent: bool) -> list[tuple[bool, str]]:
    """One (ok, error_code) per token, in order. Separated so tests can replace it."""
    messaging = _messaging()
    if messaging is None:
        return [(False, "not-configured") for _ in tokens]
    message = messaging.MulticastMessage(
        tokens=tokens,
        data=data,
        android=messaging.AndroidConfig(priority="high", ttl=TTL_SECONDS),
    )
    response = messaging.send_each_for_multicast(message)
    out = []
    for r in response.responses:
        if r.success:
            out.append((True, ""))
        else:
            code = getattr(r.exception, "code", "") or type(r.exception).__name__
            out.append((False, str(code)))
    return out


def send_to_user(user, data: dict[str, str], urgent: bool = False) -> dict:
    devices = list(PushDevice.objects.filter(user=user).order_by("-updated_at")[:MAX_DEVICES_PER_USER])
    if not devices:
        return {"sent": 0, "removed": 0, "devices": 0}
    results = _deliver([d.token for d in devices], data, urgent)
    sent = removed = 0
    for device, (ok, code) in zip(devices, results):
        if ok:
            sent += 1
            if device.failures:
                PushDevice.objects.filter(id=device.id).update(failures=0)
            continue
        if code in DEAD_TOKEN_CODES or device.failures + 1 >= 5:
            device.delete()
            removed += 1
        elif code != "not-configured":
            PushDevice.objects.filter(id=device.id).update(failures=device.failures + 1)
    return {"sent": sent, "removed": removed, "devices": len(devices)}


def send_alert(alert_id) -> dict:
    alert = Alert.objects.select_related("recipient").filter(id=alert_id).first()
    if alert is None or alert.acknowledged_at:
        return {"sent": 0, "removed": 0, "devices": 0}
    return send_to_user(alert.recipient, payload_for(alert), urgent=(alert.priority == "URGENT"))


def notify_alert(alert_id) -> None:
    """Called after the alert's transaction commits (see signals.py). Never raises."""
    if not configured():
        return
    if SYNC:
        try:
            send_alert(alert_id)
        except Exception:  # noqa: BLE001
            log.exception("push failed")
        return

    def run():
        try:
            send_alert(alert_id)
        except Exception:  # noqa: BLE001
            log.exception("push failed")
        finally:
            connection.close()

    threading.Thread(target=run, name="jarvis-push", daemon=True).start()
