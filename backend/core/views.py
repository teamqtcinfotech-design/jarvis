import logging
import re
from datetime import date

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404, HttpResponse, StreamingHttpResponse
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView, exception_handler as drf_exception_handler
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from . import ai_brain, ai_usage, assistant, conversation, datacards, lang, push, services, xai
from . import timeutils as tu
from .idempotency import error, idempotent
from .models import (
    Alert, IntegrationCommand, PrivateTask, ReminderAck, Status, TaskRequest,
)
from .taskpanel.client import TaskPanelError, get_client as tp_client
from .permissions import IsMD, IsMDOrPA, IsPA
from .reminders import reminders_for
from .serializers import (
    AckSerializer,
    ToldSerializer,
    AskSerializer,
    MemoryInputSerializer,
    DecisionSerializer,
    DelegationInputSerializer,
    InterpretSerializer,
    LoginSerializer,
    PaTaskInputSerializer,
    PaTaskStatusSerializer,
    DiagnosticsSerializer,
    RequestInputSerializer,
    SnoozeSerializer,
    TeamDelegationSerializer,
    TaskInputSerializer,
    TaskPatchSerializer,
    TurnSerializer,
    VersionSerializer,
)
from .services import ServiceError

log = logging.getLogger("jarvis.api")


def exception_handler(exc, context):
    if isinstance(exc, ServiceError):
        return error(exc.code, exc.message, exc.status, **exc.extra)
    if isinstance(exc, (ObjectDoesNotExist, Http404)):
        return error("not_found", "Not found.", 404)
    if isinstance(exc, ValidationError):
        return error("invalid", "Please check the highlighted fields.", 422, field_errors=exc.detail)
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "code" not in response.data:
        detail = response.data.get("detail", "Request failed.")
        response.data = {"code": getattr(detail, "code", "error"), "message": str(detail),
                         "retryable": response.status_code >= 500 or response.status_code == 429}
    return response


# ------------------------------------------------------------------ auth
class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class LogoutView(APIView):
    def post(self, request):
        token = request.data.get("refresh", "")
        try:
            RefreshToken(token).blacklist()
        except TokenError:
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        u = request.user
        return Response({
            "username": u.username,
            "role": u.role,
            "name": u.spoken_name,
            "title": u.title_label,
            "department": u.department,
            "address": services.address_of(u),
            "address_hi": services.address_hi_of(u),
            "language": lang.current(u),  # the language JARVIS is speaking to this person right now ("hi" / "en")
            "features": {
                "ai": ai_brain.ai_available(),
                "taskpanel": {**tp_client().describe(), "linked": services.user_link(u) is not None},
            },
        })


class HealthView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        unresolved = IntegrationCommand.objects.filter(
            state__in=[IntegrationCommand.State.QUEUED, IntegrationCommand.State.SENDING,
                       IntegrationCommand.State.OUTCOME_UNKNOWN]
        ).count()
        link = services.user_link(request.user)
        cursor = getattr(link, "cursor", None) if link else None
        from .models import PushDevice

        return Response({
            "server_time": tu.now_local().isoformat(),
            "ai": {"enabled": ai_brain.ai_available()},
            "voice": xai.describe() if settings.JARVIS_GROK_VOICE else "off (phone voice)",
            "push": push.describe(),
            # Phones registered for live push for THIS login, and what the last push attempt did.
            "push_devices": PushDevice.objects.filter(user=request.user).count(),
            "push_last": push.LAST,
            "taskpanel": {
                **tp_client().describe(),
                "linked": link is not None,
                "unconfirmed_commands": unresolved,
                "last_sync": cursor.last_success_at.isoformat() if cursor and cursor.last_success_at else None,
                "last_sync_error": cursor.last_error if cursor else "",
            },
        })


# ------------------------------------------------------------------ dashboard
class TodayView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        if request.query_params.get("refresh") == "1":
            try:
                services.refresh_delegations(request.user)
            except Exception:  # never block the dashboard on TaskPanel
                log.warning("TaskPanel refresh failed", exc_info=True)
        return Response(services.dashboard(request.user))


class BusyView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        raw = request.query_params.get("date")
        try:
            day = date.fromisoformat(raw) if raw else tu.today_local()
        except ValueError:
            return error("invalid", "date must be YYYY-MM-DD.", 422)
        return Response({"date": day.isoformat(), "busy": services.busy_windows(day)})


# ------------------------------------------------------------------ personal items (each person's own, theirs alone)
class PrivateTaskListView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        qs = services.private_qs(request.user).order_by("-created_at")
        if request.query_params.get("include_closed") != "1":
            qs = qs.exclude(status__in=[Status.COMPLETED, Status.CANCELLED])
        return Response({"results": [services.private_item(t) for t in qs[:200]]})

    @idempotent("private_tasks.create")
    def post(self, request):
        s = TaskInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        source = PrivateTask.Source.VOICE if request.data.get("source") == "VOICE" else PrivateTask.Source.MANUAL
        task = services.create_private_task(request.user, s.validated_data, source=source)
        return Response(services.private_item(task), status=201)


class PrivateTaskDetailView(APIView):
    permission_classes = [IsMDOrPA]  # someone else's personal item is simply not found

    def get(self, request, task_id):
        return Response(services.private_item(services.private_qs(request.user).get(id=task_id)))

    def patch(self, request, task_id):
        s = TaskPatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task = services.update_private_task(request.user, task_id, s.changes(), s.validated_data["expected_version"])
        return Response(services.private_item(task))


class PrivateTaskCompleteView(APIView):
    permission_classes = [IsMDOrPA]

    def post(self, request, task_id):
        s = VersionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task = services.update_private_task(request.user, task_id, {"status": Status.COMPLETED},
                                            s.validated_data["expected_version"])
        return Response(services.private_item(task))


# ------------------------------------------------------------------ shared MD tasks
class SharedTaskDetailView(APIView):
    """A work item. The MD changes anything; the giver changes what they gave; anyone changes their own; the person
    it is for only moves its status; a change to the MD's own work becomes a request for him (202). Someone else's
    work is not even found."""

    permission_classes = [IsMDOrPA]

    def get(self, request, task_id):
        return Response(services.shared_item(services.visible_shared_qs(request.user).get(id=task_id)))

    def patch(self, request, task_id):
        s = TaskPatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        version = s.validated_data["expected_version"]
        task, req = services.change_shared_task(request.user, task_id, s.changes(), version, s.validated_data.get("reason", ""))
        if req is not None:
            return Response({"status": "proposed", "request": services.request_item(req)}, status=202)
        return Response(services.shared_item(task))


class SharedTaskCompleteView(APIView):
    permission_classes = [IsMDOrPA]

    def post(self, request, task_id):
        s = VersionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task, req = services.complete_shared_task(request.user, task_id, s.validated_data["expected_version"])
        if req is not None:
            return Response({"status": "proposed", "request": services.request_item(req)}, status=202)
        return Response(services.shared_item(task))


# ------------------------------------------------------------------ work for the PA and the manager (older phones)
class PaTaskListView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        qs = services.pa_tasks_qs().exclude(status=Status.CANCELLED).order_by("-created_at")
        if not request.user.is_md and not request.user.is_pa:
            qs = qs.filter(created_by=request.user)  # only what they gave the PA
        return Response({"results": [services.shared_item(t) for t in qs[:100]]})

    @idempotent("pa_tasks.create")
    def post(self, request):
        if request.user.is_pa:
            return error("forbidden", "Your own work goes on your own list.", 403)
        s = PaTaskInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task = services.create_pa_task(request.user, s.validated_data,
                                       meeting_with_pa=s.validated_data["meeting_with_pa"])
        return Response(services.shared_item(task), status=201)


class MgTaskListView(APIView):
    """The manager's list; anyone else on the team gives her a task."""

    permission_classes = [IsMDOrPA]

    def get(self, request):
        qs = services.mg_tasks_qs().exclude(status=Status.CANCELLED).order_by("-created_at")
        if not request.user.is_md and not request.user.is_mg:
            qs = qs.filter(created_by=request.user)
        return Response({"results": [services.shared_item(t) for t in qs[:100]]})

    @idempotent("mg_tasks.create")
    def post(self, request):
        if request.user.is_mg:
            return error("forbidden", "Your own work goes on your own list.", 403)
        s = PaTaskInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task = services.create_mg_task(request.user, s.validated_data)
        return Response(services.shared_item(task), status=201)


class PaSelfTaskView(APIView):
    """Mansi's own task or reminder: on her list (Sir can see it), no approval, no nagging Sir."""

    permission_classes = [IsPA]

    @idempotent("pa_tasks.self")
    def post(self, request):
        s = PaTaskInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task = services.create_pa_self_task(request.user, s.validated_data)
        return Response(services.shared_item(task), status=201)


class ContactListView(APIView):
    """People JARVIS knows for this user besides the staff list. Owner-only, like memories."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        return Response({"results": services.list_contacts(request.user)})

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        return Response(services.add_contact(request.user, str(data.get("name") or ""), str(data.get("note") or "")), status=201)


class ContactDetailView(APIView):
    permission_classes = [IsMDOrPA]

    def delete(self, request, contact_id):
        services.delete_contact(request.user, contact_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PaTaskStatusView(APIView):
    """The PA updates her task: Not started / Working / Hold (reason required) / Done."""

    permission_classes = [IsPA]

    def post(self, request, task_id):
        s = PaTaskStatusSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        task = services.update_pa_task_status(request.user, task_id, s.validated_data["status"],
                                              s.validated_data["expected_version"], s.validated_data["note"])
        return Response(services.shared_item(task))


# ------------------------------------------------------------------ delegation to TaskPanel staff
class StaffListView(APIView):
    """TaskPanel directory: staff (with leave) and task categories."""

    permission_classes = [IsMDOrPA]

    def get(self, request):
        from .taskpanel import sync as tp_sync

        tp_sync.ensure_directory(request.user)
        staff = services.staff_qs()
        categories = services.categories_qs()
        return Response({
            "results": [{"id": s.id, "name": s.name, "department": s.department, "designation": s.designation,
                         "on_leave": s.on_leave, "leave_until": s.leave_until.isoformat() if s.leave_until else None}
                        for s in staff],
            "categories": [{"key": c.key, "name": c.name, "department": c.department} for c in categories],
        })


class StaffSyncView(APIView):
    permission_classes = [IsMDOrPA]

    def post(self, request):
        try:
            count = services.sync_staff(request.user)
        except TaskPanelError as exc:
            return error(exc.code, exc.message, 503 if exc.status in (0, 503) else exc.status)
        return Response({"synced": count})


class DelegationListView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        qs = services.delegated_visible_qs(request.user).order_by("-created_at")[:100]
        return Response({"results": [services.shared_item(t) for t in qs]})

    @idempotent("delegations.create")
    def post(self, request):
        s = DelegationInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        staff = services.staff_qs().get(id=s.validated_data["staff_id"])
        task, command = services.delegate_to_staff(
            request.user, s.validated_data, staff,
            category_key=s.validated_data["category_key"], description=s.validated_data["description"],
        )
        if command.state == IntegrationCommand.State.REJECTED:
            # TaskPanel refused (leave, department rule, unknown person…): nothing was assigned.
            message = command.error_message or "TaskPanel did not accept this task."
            code = command.error_code or "rejected"
            task.delete()
            return error(code, message, 422)
        if command.state == IntegrationCommand.State.SUCCEEDED:
            return Response({**services.shared_item(task), "result": "assigned"}, status=201)
        # Sent but not confirmed yet (TaskPanel slow/unreachable): never claim "assigned".
        return Response({**services.shared_item(task), "result": "pending_confirmation"}, status=202)


class TeamDelegationView(APIView):
    """One TaskPanel task per person in the chosen team (people on leave were already left out)."""

    permission_classes = [IsMDOrPA]

    @idempotent("delegations.team")
    def post(self, request):
        s = TeamDelegationSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        results = services.delegate_to_team(
            request.user, s.validated_data, s.validated_data["staff_ids"],
            category_key=s.validated_data["category_key"], description=s.validated_data["description"],
        )
        status_code = 201 if results["assigned"] and not results["pending"] else 202
        return Response(results, status=status_code)


class DelegationRefreshView(APIView):
    permission_classes = [IsMDOrPA]

    def post(self, request):
        try:
            changed = services.refresh_delegations(request.user)
        except TaskPanelError as exc:
            return error(exc.code, exc.message, 503)
        return Response({"changed": changed})


class DelegationCheckView(APIView):
    """"Check again" for one staff task: resend if unconfirmed, then refresh its status."""

    permission_classes = [IsMDOrPA]

    def post(self, request, task_id):
        task = services.recheck_delegation(request.user, task_id)
        return Response(services.shared_item(task))


# ------------------------------------------------------------------ PA -> MD requests
class RequestListView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        qs = TaskRequest.objects.select_related("requested_by", "target")
        if not request.user.is_md:
            qs = qs.filter(requested_by=request.user)
        state = request.query_params.get("state")
        if state:
            qs = qs.filter(state=state.upper())
        return Response({"results": [services.request_item(r) for r in qs[:100]]})

    @idempotent("requests.create")
    def post(self, request):
        if not request.user.is_assistant:
            return error("forbidden", "MD creates tasks directly; requests come from the PA or the manager.", 403)
        s = RequestInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        req = services.create_md_request(request.user, s.validated_data)
        return Response(services.request_item(req), status=201)


class RequestAcceptView(APIView):
    permission_classes = [IsMD]

    @idempotent("requests.accept")
    def post(self, request, request_id):
        req = services.accept_request(request.user, request_id)
        body = services.request_item(req)
        if req.state == TaskRequest.State.CONFLICT:
            return error("conflict", "The task changed after this request was made. The PA has been told.", 409,
                         request=body)
        return Response(body)


class RequestDeclineView(APIView):
    permission_classes = [IsMD]

    @idempotent("requests.decline")
    def post(self, request, request_id):
        s = DecisionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        req = services.decline_request(request.user, request_id, s.validated_data["note"])
        return Response(services.request_item(req))


class RequestWithdrawView(APIView):
    permission_classes = [IsPA]

    def post(self, request, request_id):
        return Response(services.request_item(services.withdraw_request(request.user, request_id)))


class MePrefsView(APIView):
    """Settings shared by the app and the background listener (JARVIS changes them by voice too)."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        return Response(services.get_prefs(request.user))

    def patch(self, request):
        patch = request.data if isinstance(request.data, dict) else {}
        return Response(services.set_prefs(request.user, patch))


class DiagnosticsView(APIView):
    """The phone sends what went wrong on it (short lines, never task titles); the same person can read them back.
    Kept to the last 300 lines per person."""
    permission_classes = [IsMDOrPA]

    def post(self, request):
        from .models import PhoneLog

        s = DiagnosticsSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        rows = [PhoneLog(user=request.user, level=e["level"], where=e["where"][:80], message=e["message"][:500], at=e["at"],
                         app_version=d["app_version"][:20]) for e in d["events"]]
        PhoneLog.objects.bulk_create(rows)
        extra = PhoneLog.objects.filter(user=request.user).order_by("-at").values_list("id", flat=True)[300:]
        if extra:
            PhoneLog.objects.filter(id__in=list(extra)).delete()
        for e in d["events"][:20]:
            log.warning("phone %s [%s] %s: %s", request.user.username, e["level"], e["where"], e["message"][:200])
        return Response({"stored": len(rows)})

    def get(self, request):
        from .models import PhoneLog

        rows = PhoneLog.objects.filter(user=request.user).order_by("-at")[:100]
        return Response({"results": [{"level": r.level, "where": r.where, "message": r.message, "at": r.at.isoformat(),
                                      "app_version": r.app_version} for r in rows]})


class MeHabitsView(APIView):
    """What JARVIS has learned or been told about how this person works, in plain words; any one can be forgotten."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        return Response({"results": services.habits(request.user)})

    def delete(self, request, key):
        services.forget_habit(request.user, key)
        return Response({"forgotten": key, "results": services.habits(request.user)})


class PushTokenView(APIView):
    """The phone's Firebase token, so alerts reach it the moment they are created (even with the app closed)."""
    permission_classes = [IsMDOrPA]

    def post(self, request):
        token = str((request.data or {}).get("token") or "")
        try:
            push.register_device(request.user, token, str((request.data or {}).get("platform") or "android"))
        except ValueError as err:
            return error("invalid", str(err), 422)
        services.audit(request.user, "push.register", "user", request.user.id)
        return Response({"registered": True, "push": push.describe()})

    def delete(self, request):
        token = str((request.data or {}).get("token") or request.query_params.get("token") or "")
        removed = push.forget_device(request.user, token)
        return Response({"removed": removed})


class DeviceTokenView(APIView):
    """A separate login for the phone's background "Hey JARVIS" service.

    Refresh tokens rotate and the old one is blacklisted, so the app and the service must each have
    their own chain; sharing one would log the other out.
    """
    permission_classes = [IsMDOrPA]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        refresh = RefreshToken.for_user(request.user)
        services.audit(request.user, "auth.device_token", "user", request.user.id)
        return Response({"access": str(refresh.access_token), "refresh": str(refresh)})


# ------------------------------------------------------------------ assistant
class InterpretView(APIView):
    permission_classes = [IsMDOrPA]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "assistant"

    def post(self, request):
        s = InterpretSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        xai.tts_warm()  # he is talking: have the voice connection open by the time the reply is ready
        draft = assistant.build_draft(request.user, s.validated_data["text"], s.validated_data["previous"],
                                      s.validated_data["history"], s.validated_data["awaiting"],
                                      s.validated_data.get("choices"), pending=s.validated_data.get("pending"),
                                      screen=s.validated_data.get("screen"))
        return Response(draft)


class ConversationTurnView(APIView):
    """Voice-only conversation for the phone's background listener: say → (JARVIS does the work) → reply."""
    permission_classes = [IsMDOrPA]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "assistant"

    def post(self, request):
        s = TurnSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        xai.tts_warm()  # he is talking: have the voice connection open by the time the reply is ready
        return Response(conversation.turn(request.user, d["text"], event=d["event"], locked=d["locked"],
                                          private_ok=d["private_ok"], about=d.get("about")))


class AskView(APIView):
    """General questions (weather, prices, news…) answered by Claude with web search.

    Receives only the question and the last turns of the conversation — no tasks, memories or staff data.
    """
    permission_classes = [IsMDOrPA]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ask"

    def post(self, request):
        s = AskSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = request.user
        result = ai_brain.answer(
            s.validated_data["question"], s.validated_data["history"], user=user,
            address=services.address_of(user),
            reader="the Managing Director" if user.is_md else f"{user.spoken_name}, {services.role_label(user)},",
            lang=s.validated_data["lang"],
        )
        return Response(result)


_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")


def _speakable(text: str, limit: int) -> str:
    """One spoken reply: whitespace tidied, and cut at a sentence end if it runs past the limit."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    out = ""
    for sentence in _SENTENCE_END.split(text):
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return out or text[:limit]


class SpeakView(APIView):
    """JARVIS's voice: text in, audio out, voiced by Grok. The phone plays it (and keeps its own voice as fallback).

    Nothing is stored here: the phone caches what it has already heard, so repeats cost nothing.
    """
    permission_classes = [IsMDOrPA]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "speak"

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        text = _speakable(str(data.get("text") or ""), settings.JARVIS_TTS_MAX_CHARS)
        if not text:
            return error("invalid", "Nothing to say.", 422)
        if not (settings.JARVIS_GROK_VOICE and xai.configured()):
            return error("voice_off", "The Grok voice is not set up on this server.", 503)
        if ai_usage.over_voice_limit(request.user):
            return error("voice_limit", "Today's voice limit is reached; the phone's own voice is used until tomorrow.", 429)
        lang = "hi" if str(data.get("lang") or "").lower().startswith("hi") else "en"
        # The phone names the voice it caches under; only JARVIS's own (male) voices are used, else the MD's choice.
        asked = str(data.get("voice") or "").strip().lower()
        voice = asked if asked in services.VOICE_CHOICES else services.voice_of(request.user)
        try:
            if str(data.get("format") or "").lower() == "pcm":
                # The phone's player: raw sound streamed as Grok speaks, so the voice starts in about half a second.
                rate = 24000
                stream = xai.tts_stream(text, voice=voice, sample_rate=rate)
                head = next(stream)  # raises here (before any sound) if the voice cannot be reached
                ai_usage.record_voice(request.user, len(text))

                def body():
                    yield head
                    yield from stream

                response = StreamingHttpResponse(body(), content_type="audio/L16")
                response["X-Sample-Rate"] = str(rate)
                response["X-Voice"] = "grok"
                return response
            audio = xai.tts(text, language=lang, voice=voice)
        except StopIteration:
            return error("voice_failed", "The voice service returned no sound.", 502)
        except xai.XaiError as err:
            log.warning("Grok voice failed (%s): %s", err.status, err.message)
            return error("voice_failed", "The voice service did not answer.", 502)
        except Exception:  # noqa: BLE001 — network trouble: the phone falls back to its own voice
            log.warning("Grok voice unreachable", exc_info=True)
            return error("voice_failed", "The voice service did not answer.", 502)
        ai_usage.record_voice(request.user, len(text))
        response = HttpResponse(audio, content_type="audio/mpeg")
        response["X-Voice"] = "grok"
        response["Cache-Control"] = "private, max-age=604800"
        return response


class AiUsageView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        return Response(ai_usage.month_summary(request.user))


class MemoryListView(APIView):
    """The speaker's own remembered facts. The MD's and the PA's lists are completely separate."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        return Response({"results": services.list_memories(request.user)})

    @idempotent("memories.create")
    def post(self, request):
        s = MemoryInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return Response(services.add_memory(request.user, s.validated_data["text"]), status=status.HTTP_201_CREATED)


class MemoryDetailView(APIView):
    permission_classes = [IsMDOrPA]

    def delete(self, request, memory_id):
        services.delete_memory(request.user, memory_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BriefingView(APIView):
    permission_classes = [IsMDOrPA]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "assistant"

    def get(self, request):
        kind = request.query_params.get("kind", "now")
        if kind not in {"morning", "evening", "now"}:
            kind = "now"
        facts, fallback = services.briefing_facts(request.user, kind)
        text, engine = ai_brain.briefing(facts, fallback, user=request.user)
        return Response({"kind": kind, "text": text, "engine": engine, "generated_at": tu.now_local().isoformat(),
                         "view": datacards.build(request.user, "today")})


class DataViewView(APIView):
    """One data view (timeline / chart / table) for the screen: the tablet's workspace and the phone's dashboard."""
    permission_classes = [IsMDOrPA]

    def get(self, request, kind):
        q = request.query_params
        # The same request form the voice uses: /views/tasks/?who=Ramesh&from=2026-11-10&to=2026-11-16&by=given
        params = {k: q.get(k, "") for k in ("who", "from", "to", "by", "filter", "group")}
        view = datacards.build(request.user, kind, include_private=q.get("private") != "0", params=params)
        if view is None:
            return error("not_found", "No such view.", 404)
        if view.get("ask"):
            return error("ambiguous", view["ask"], 422)
        return Response(view)


# ------------------------------------------------------------------ reminders + alerts
class ReminderListView(APIView):
    permission_classes = [IsMDOrPA]

    def get(self, request):
        try:
            hours = max(1, min(int(request.query_params.get("hours", 48)), 168))
        except ValueError:
            hours = 48
        return Response(reminders_for(request.user, hours))


class ReminderAckView(APIView):
    permission_classes = [IsMDOrPA]

    def post(self, request):
        s = AckSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        ReminderAck.objects.get_or_create(user=request.user, key=s.validated_data["key"])
        return Response({"acknowledged": s.validated_data["key"]})


class ReminderSnoozeView(APIView):
    """"Later": a reminder or an alert comes back at the given moment (or in N minutes, or after the person's usual
    snooze). Kept on the server, so the app and the phone's background part agree and a restart loses nothing."""
    permission_classes = [IsMDOrPA]

    def post(self, request):
        from django.utils import timezone

        s = SnoozeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        minutes = d["minutes"] or int(services.get_prefs(request.user).get("snoozeMinutes", 15))
        until = d["until"] or timezone.now() + tu.timedelta(minutes=minutes)
        if until <= timezone.now():
            return error("invalid", "That time has already passed.", status.HTTP_422_UNPROCESSABLE_ENTITY)
        what = None
        if d["key"]:
            services.snooze_reminder(request.user, d["key"], until)
            what = {"kind": "reminder", "key": d["key"]}
        elif d["alert_id"]:
            try:
                alert = services.snooze_alert(request.user, d["alert_id"], until)
            except (Alert.DoesNotExist, ValueError):
                raise Http404
            what = {"kind": "alert", "id": str(alert.id), "title": alert.title}
        elif d["last"]:
            what = services.snooze_last_told(request.user, until)
            if what is None:
                return error("nothing", "Nothing was said recently that could be snoozed.", status.HTTP_422_UNPROCESSABLE_ENTITY)
        else:
            return error("invalid", "Say which reminder or alert.", status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response({"until": until.isoformat(), "until_label": services.moment_label(until),
                         "until_label_hi": services.moment_label_hi(until), **what})


class AlertListView(APIView):
    """Unseen alerts of the last week. Each says whether it was already told (spoken or shown once) and whether it
    is still fresh enough to be announced; the phone never announces the same one twice."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        from django.utils import timezone

        from django.db.models import Q

        spoken_in = lang.current(request.user)  # the recipient's language right now: Hindi twins when it is Hindi
        now = timezone.now()
        qs = (Alert.objects.filter(recipient=request.user, acknowledged_at__isnull=True, created_at__gte=now - services.ALERT_LIST_FOR)
              .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now))  # snoozed: out of sight until then
              .select_related("recipient")[:50])
        return Response({"results": [services.alert_item(a, spoken_in) for a in qs]})


class ToldView(APIView):
    """The phone reports what it has just spoken or shown (reminder keys, alert ids), so that whoever looks next,
    the app or the background part, does not say it again."""
    permission_classes = [IsMDOrPA]

    def post(self, request):
        s = ToldSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        reminders = services.mark_reminders_told(request.user, s.validated_data["keys"])
        alerts = services.mark_alerts_told(request.user, s.validated_data["ids"])
        return Response({"reminders": reminders, "alerts": alerts})


class TeamView(APIView):
    """The team: everyone may see who is on it (names and jobs only); the MD adds people."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        people = services.team(active=not request.user.is_md)  # the MD also sees switched-off accounts
        return Response({"results": [services.person_item(u, request.user) for u in people]})

    def post(self, request):
        person = services.team_add(request.user, request.data if isinstance(request.data, dict) else {})
        return Response(services.person_item(person, request.user), status=201)


class DepartmentListView(APIView):
    """Departments with their members (everyone may look); the MD makes one (with people, by id)."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        from . import sections
        return Response({"results": [sections.department_item(d, request.user) for d in sections.departments(active=not request.user.is_md)],
                         "management": [services.person_item(u, request.user) for u in sections.management()]})

    def post(self, request):
        from . import sections
        from .models import User as _User
        data = request.data if isinstance(request.data, dict) else {}
        people = list(_User.objects.filter(id__in=[int(i) for i in (data.get("people") or []) if str(i).isdigit()]))
        dept, _created = sections.create_department(request.user, str(data.get("name") or ""), people)
        return Response(sections.department_item(dept, request.user), status=201)


class DepartmentView(APIView):
    """One department: rename, add or remove people (by id), switch it off."""
    permission_classes = [IsMD]

    def patch(self, request, dept_id):
        from . import sections
        from .models import Department, User as _User
        dept = Department.objects.get(id=dept_id)
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("name"):
            sections.rename_department(request.user, dept, str(data["name"]))
        for key, fn in (("add", sections.add_members), ("remove", sections.remove_members)):
            ids = [int(i) for i in (data.get(key) or []) if str(i).isdigit()]
            if ids:
                fn(request.user, dept, list(_User.objects.filter(id__in=ids)))
        if data.get("active") is False and dept.active:
            sections.delete_department(request.user, dept)
        elif data.get("active") is True and not dept.active:
            dept.active = True
            dept.save(update_fields=["active"])
        return Response(sections.department_item(dept, request.user))

    def delete(self, request, dept_id):
        from . import sections
        from .models import Department
        dept = Department.objects.get(id=dept_id)
        sections.delete_department(request.user, dept)
        return Response({"deleted": dept.id})


class SectionListView(APIView):
    """Sections: the shared work ones plus the caller's own personal ones; anyone makes a work section."""
    permission_classes = [IsMDOrPA]

    def get(self, request):
        from . import sections
        return Response({"results": [sections.section_item(s, request.user) for s in sections.sections(request.user, active=not request.user.is_md)]})

    def post(self, request):
        from . import sections
        from .models import Section
        data = request.data if isinstance(request.data, dict) else {}
        kind = Section.Kind.PERSONAL if data.get("kind") == "PERSONAL" else Section.Kind.WORK
        section, _created = sections.create_section(request.user, str(data.get("name") or ""), kind)
        return Response(sections.section_item(section, request.user), status=201)


class SectionView(APIView):
    """One section: rename, merge into another, switch off (work sections: the MD; personal: its owner)."""
    permission_classes = [IsMDOrPA]

    def patch(self, request, section_id):
        from . import sections
        from .models import Section
        section = Section.objects.get(id=section_id)
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("name"):
            sections.rename_section(request.user, section, str(data["name"]))
        if data.get("merge_into"):
            dst = Section.objects.get(id=int(data["merge_into"]))
            sections.merge_sections(request.user, section, dst)
        if data.get("active") is False and section.active:
            sections.delete_section(request.user, section)
        return Response(sections.section_item(section, request.user))

    def delete(self, request, section_id):
        from . import sections
        from .models import Section
        section = Section.objects.get(id=section_id)
        sections.delete_section(request.user, section)
        return Response({"deleted": section.id})


class TaskSectionView(APIView):
    """Move a work item into a section (by name; made if new): whoever gave it, whoever does it, or the MD."""
    permission_classes = [IsMDOrPA]

    def post(self, request, task_id):
        from . import sections
        data = request.data if isinstance(request.data, dict) else {}
        task, section = sections.move_task(request.user, task_id, str(data.get("section") or ""))
        return Response({"task": services.shared_item(task), "section": sections.section_item(section, request.user)})


class TeamMemberView(APIView):
    """The MD changes one person: name, job, department, gender, password, or switches the account off / on."""
    permission_classes = [IsMD]

    def patch(self, request, user_id):
        person = services.team_update(request.user, user_id, request.data if isinstance(request.data, dict) else {})
        return Response(services.person_item(person, request.user))


class ExecuteView(APIView):
    """The phone's "save" for a card of the team's kind (work for a person, a meeting with them, a message, one's
    own item), and for a card with several such instructions: done on the server by the same path the voice uses.
    The card is untrusted: every person is looked up again, and the service functions check what the speaker may do."""
    permission_classes = [IsMDOrPA]

    @idempotent("assistant.execute")
    def post(self, request):
        from . import actions

        card = (request.data or {}).get("card") if isinstance(request.data, dict) else None
        if not isinstance(card, dict) or not card.get("action"):
            return error("invalid", "Nothing to save.", 422)
        result = actions.run_card(request.user, card)
        if not result["saved"] and result["failed"]:
            return error("not_saved", result["failed"][0][1], 422, failed=[{"title": t, "message": m} for t, m in result["failed"]])
        return Response({"en": result["en"], "hi": result["hi"], "saved": result["saved"],
                         "failed": [{"title": t, "message": m} for t, m in result["failed"]], "undo": bool(result["undo"])},
                        status=201)


class UndoView(APIView):
    """The Undo button (and "undo" said aloud goes the same way): what JARVIS saved a moment ago is taken back."""
    permission_classes = [IsMDOrPA]

    def post(self, request):
        from . import actions

        done = actions.undo_last(request.user)
        return Response(done)


class AlertAckView(APIView):
    permission_classes = [IsMDOrPA]

    def post(self, request, alert_id):
        from django.utils import timezone

        alert = Alert.objects.get(id=alert_id, recipient=request.user)
        told = None
        said = request.data.get("seen") if isinstance(request.data, dict) else None
        # "Shall I tell Sunil you've seen it?" — yes: whoever gave the task or sent the message learns it was seen;
        # no: seen, but nobody is told. "Got it" on something that rings until seen counts as Seen (and tells).
        tell = said in (True, "true", 1, "1") if said is not None else bool(alert.ring)
        if alert.seen_at is None and (alert.ring or tell):  # seen before (an answer, a Start): nothing more to say
            told = services.mark_seen(request.user, alert, tell=tell)
        if not alert.acknowledged_at:
            alert.acknowledged_at = timezone.now()
            alert.save(update_fields=["acknowledged_at"])
        told_to = services.giver_words(told.recipient) if told else ("", "")
        return Response({"acknowledged": str(alert.id), "told": str(told.id) if told else None,
                         "told_to": told_to[0], "told_to_hi": told_to[1]})


class AlertDetailView(APIView):
    """One alert of the person's own, even one already dealt with (the phone opens it from a notification)."""
    permission_classes = [IsMDOrPA]

    def get(self, request, alert_id):
        alert = Alert.objects.select_related("recipient", "sender").get(id=alert_id, recipient=request.user)
        return Response(services.alert_item(alert, lang.current(request.user)))


class AlertSeenView(APIView):
    """"Seen" (the button on the banner or on the ringing notification): the ringing stops on every phone, the
    sender is told, and the answer says what JARVIS asks next ("Shall I tell Sir when you'll do it?") if anything.
    A message or meeting needs nothing more and leaves the lists; work stays until that question is answered."""
    permission_classes = [IsMDOrPA]

    def post(self, request, alert_id):
        from django.utils import timezone

        alert = Alert.objects.select_related("recipient", "sender").get(id=alert_id, recipient=request.user)
        told = services.mark_seen(request.user, alert, tell=True)
        question = services.alert_question(alert)
        if not (question and question.get("kind") == "promise") and not alert.acknowledged_at:
            alert.acknowledged_at = timezone.now()
            alert.save(update_fields=["acknowledged_at"])
        to, to_hi = services.giver_words(told.recipient) if told else ("", "")
        return Response({"seen": True, "told_to": to, "told_to_hi": to_hi, "question": question,
                         "item": services.alert_item(alert, lang.current(request.user))})


class AlertAnswerView(APIView):
    """The words said after JARVIS asked about work between people (by when, can't do it, ring again, give it to
    someone else). handled = False: the words were about something else; the phone treats them as a new sentence."""
    permission_classes = [IsMDOrPA]

    def post(self, request, alert_id):
        from . import promises

        alert = Alert.objects.select_related("recipient", "sender").get(id=alert_id, recipient=request.user)
        text = str((request.data or {}).get("text") or "")[:1000] if isinstance(request.data, dict) else ""
        return Response(promises.answer(request.user, alert, text))


class TaskHistoryView(APIView):
    """A work item's history: who gave it, when it was seen, the time promised, moves, who else got it, done."""
    permission_classes = [IsMDOrPA]

    def get(self, request, task_id):
        task = services.visible_shared_qs(request.user).get(id=task_id)
        return Response({"results": services.task_history(task)})
