"""Sign-in by phone, and one session per phone (Nov 2026).

- Sign-in: the app verifies the person's phone number with an SMS code through Firebase (on the phone) and hands
  the server Firebase's signed proof (an ID token). The server checks the proof with the Firebase key it already
  holds for pushes, finds the account with that number, and issues JARVIS's own tokens — each carrying the id of a
  new ``Session`` row for that phone ("sid").
- Every request: the token's session must still be active (SessionAuthentication). Signing a phone out sets
  ``revoked_at`` and the very next request from it is refused, whatever its tokens say; its push tokens go with it.
- The person is told on their other phones when a new sign-in happens, with "Sign it out" one tap away.
"""
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Alert, Priority, PushDevice, Session, User
from .services import ServiceError, audit

log = logging.getLogger("jarvis.sessions")

SID = "sid"
TOUCH_EVERY = timedelta(seconds=60)
DEFAULT_COUNTRY = "+91"


# --------------------------------------------------------------------------- phone numbers
def normalize_phone(raw: str) -> str:
    """"98765 43210", "098765-43210", "+91 98765 43210" -> "+919876543210"; "" when it is not a phone number."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if str(raw).strip().startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if len(digits) == 10:
        return DEFAULT_COUNTRY + digits
    if len(digits) == 11 and digits.startswith("0"):
        return DEFAULT_COUNTRY + digits[1:]
    if len(digits) == 12 and digits.startswith(DEFAULT_COUNTRY[1:]):
        return "+" + digits
    return "+" + digits if 11 <= len(digits) <= 15 else ""


def phone_label(phone: str) -> str:
    """"+919876543210" -> "+91 98765 43210" for the screen."""
    if phone.startswith(DEFAULT_COUNTRY) and len(phone) == 13:
        return f"{DEFAULT_COUNTRY} {phone[3:8]} {phone[8:]}"
    return phone


# --------------------------------------------------------------------------- Firebase's proof
def verify_firebase_phone(id_token: str) -> str:
    """The phone number Firebase verified on the phone, from its signed ID token. Raises ServiceError otherwise."""
    from . import push  # noqa: PLC0415

    app = push.firebase_app()
    if app is None:
        raise ServiceError("unavailable", "Phone sign-in is not set up on this server yet.", 503)
    try:
        from firebase_admin import auth as fb_auth  # noqa: PLC0415
        claims = fb_auth.verify_id_token(id_token, app=app, check_revoked=False)
    except Exception as exc:  # noqa: BLE001 — expired, wrong project, tampered: all the same to the caller
        log.info("phone sign-in refused: %s", exc.__class__.__name__)
        raise ServiceError("invalid_proof", "That code has expired or is not valid. Please ask for a new one.", 401) from exc
    provider = (claims.get("firebase") or {}).get("sign_in_provider")
    phone = normalize_phone(str(claims.get("phone_number") or ""))
    if provider != "phone" or not phone:
        raise ServiceError("invalid_proof", "This sign-in was not by phone number.", 401)
    return phone


# --------------------------------------------------------------------------- sessions
def _ip(request) -> str:
    fwd = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return (fwd or request.META.get("REMOTE_ADDR") or "")[:45]


def start_session(user: User, request, *, name: str = "", device_id: str = "", how: str = "phone") -> Session:
    """A new sign-in on a phone. The same phone signing in again (same device id) replaces its earlier session."""
    name = " ".join(str(name or "").split())[:80]
    device_id = str(device_id or "").strip()[:64]
    if device_id:
        Session.objects.filter(user=user, device_id=device_id, revoked_at__isnull=True) \
            .update(revoked_at=timezone.now(), revoked_why="replaced")
    session = Session.objects.create(user=user, device_id=device_id, name=name, how=how, last_ip=_ip(request))
    audit(user, "auth.sign_in", "session", session.id, f"{how} · {name or 'phone'}")
    return session


def tokens_for(session: Session) -> dict:
    """JARVIS's own tokens for this session: the refresh token carries the session id, the access token inherits it."""
    refresh = RefreshToken.for_user(session.user)
    refresh[SID] = str(session.id)
    return {"access": str(refresh.access_token), "refresh": str(refresh), "session_id": str(session.id)}


def session_of(request) -> Session | None:
    """The session behind this request (set by SessionAuthentication), or None for a token without one."""
    return getattr(request, "jarvis_session", None)


def revoke(session: Session, by: User | None, why: str) -> bool:
    """Sign that phone out: refused from its next request on; its push tokens go, so it hears nothing more."""
    if session.revoked_at is not None:
        return False
    Session.objects.filter(id=session.id, revoked_at__isnull=True).update(revoked_at=timezone.now(), revoked_by=by, revoked_why=why)
    PushDevice.objects.filter(session=session).delete()
    audit(by or session.user, "auth.sign_out", "session", session.id, why)
    return True


def revoke_all(user: User, *, keep: Session | None, by: User | None, why: str) -> int:
    n = 0
    for s in Session.objects.filter(user=user, revoked_at__isnull=True):
        if keep is not None and s.id == keep.id:
            continue
        n += int(revoke(s, by, why))
    return n


def notify_new_sign_in(user: User, session: Session) -> None:
    """"Signed in on Xiaomi M2101K7BI at 2:10 PM — not you? Sign it out." On the person's other phones, with the
    sign-out one tap away. Nothing when this is their only phone."""
    from . import timeutils as tu  # noqa: PLC0415

    others = Session.objects.filter(user=user, revoked_at__isnull=True).exclude(id=session.id).exists()
    if not others:
        return
    at = tu.fmt_time(session.created_at)
    where = session.name or "another phone"
    Alert.objects.create(
        recipient=user, kind=Alert.Kind.INFO, priority=Priority.HIGH,
        title=f"Signed in on {where}", body=f"Your account was signed in on {where} at {at}. Not you? Sign it out.",
        spoken_text=f"Your account was just signed in on {where}. If that was not you, say sign it out.",
        spoken_text_hi=f"आपका अकाउंट अभी {where} पर साइन इन हुआ। अगर ये आप नहीं थे, तो बोलिए साइन आउट कर दो।",
        ref_type="session", ref_id=str(session.id),
        question={"kind": "signout", "en": "Sign that phone out?", "hi": "उस फ़ोन को साइन आउट कर दूँ?", "session": str(session.id)},
    )


def session_item(s: Session, current: Session | None) -> dict:
    return {"id": str(s.id), "name": s.name or "Phone", "how": s.how, "signed_in_at": s.created_at.isoformat(),
            "last_seen_at": s.last_seen_at.isoformat(), "this_phone": bool(current and current.id == s.id),
            "active": s.revoked_at is None, "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
            "revoked_why": s.revoked_why, "device_id": s.device_id}


# --------------------------------------------------------------------------- every request
class SessionAuthentication(JWTAuthentication):
    """The usual token check, plus: the token's session must still be active. A token from before sessions existed
    (no "sid") is let through until it is refreshed — the refresh gives it a session (see SessionRefreshView)."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        sid = validated_token.get(SID)
        if not sid:
            return user
        session = Session.objects.filter(id=sid, user=user).first()
        if session is None or session.revoked_at is not None:
            raise AuthenticationFailed({"code": "signed_out", "message": "This phone was signed out. Please sign in again."}, code="signed_out")
        now = timezone.now()
        if now - session.last_seen_at >= TOUCH_EVERY:
            Session.objects.filter(id=session.id).update(last_seen_at=now)
        user.jarvis_session = session
        return user

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user, _token = result
            request.jarvis_session = getattr(user, "jarvis_session", None)
        return result


def session_from_token(token) -> Session | None:
    """For the walkie relay: the session a token names, when it is still active (None otherwise, or without one)."""
    sid = token.get(SID)
    if not sid:
        return None
    return Session.objects.filter(id=sid, revoked_at__isnull=True).first()


def password_login_allowed() -> bool:
    return bool(getattr(settings, "JARVIS_PASSWORD_LOGIN", True))
