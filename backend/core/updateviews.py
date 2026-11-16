"""The app-update endpoints (see appupdates.py). The phone's update library calls the manifest and the files itself,
without a login: what it gets is public app code, signed, and it can only ever receive a newer signed version.
The app's own signed-in "hello" links a phone to its user, so the operator can see whose phone is whose."""
import json
import logging
import uuid

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_safe
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from . import appupdates as au

log = logging.getLogger("jarvis.updates")


def _base_url(request) -> str:
    return settings.JARVIS_PUBLIC_URL or request.build_absolute_uri("/").rstrip("/")


def _multipart(name: str, body: bytes, signature: str | None, protocol: int) -> HttpResponse:
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            "Content-Type: application/json; charset=utf-8\r\n"
            + (f"expo-signature: {signature}\r\n" if signature else "")
            + "\r\n").encode()
    payload = head + body + f"\r\n--{boundary}--\r\n".encode()
    resp = HttpResponse(payload, content_type=f"multipart/mixed; boundary={boundary}")
    return _common(resp, protocol)


def _common(resp: HttpResponse, protocol: int) -> HttpResponse:
    resp["expo-protocol-version"] = str(protocol)
    resp["expo-sfv-version"] = "0"
    resp["Cache-Control"] = "private, max-age=0"
    return resp


def _part(request, name: str, obj: dict, protocol: int) -> HttpResponse:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode()
    signature = None
    if request.headers.get("expo-expect-signature"):
        try:
            signature = au.signature_header(body)
        except au.UpdateError as exc:
            # Phones that require a signature get nothing unsigned: they keep the code they have.
            log.error("App update not sent: %s", exc)
            return JsonResponse({"error": "Updates are not signed on this server yet."}, status=503)
    return _multipart(name, body, signature, protocol)


def _no_update(request, protocol: int) -> HttpResponse:
    if protocol < 1:
        return _common(HttpResponse(status=204), protocol)
    return _part(request, "directive", {"type": "noUpdateAvailable"}, protocol)


@require_safe
def manifest(request):
    try:
        protocol = int(request.headers.get("expo-protocol-version") or "0")
    except ValueError:
        protocol = -1
    if protocol not in (0, 1):
        return JsonResponse({"error": "Unsupported protocol version (expected 0 or 1)."}, status=406)
    platform = request.headers.get("expo-platform") or request.GET.get("platform")
    if platform != au.PLATFORM:
        return JsonResponse({"error": "Unsupported platform (only android)."}, status=400)
    runtime = (request.headers.get("expo-runtime-version") or request.GET.get("runtime-version") or "").strip()
    if not runtime or len(runtime) > 40:
        return JsonResponse({"error": "No runtime version given."}, status=400)

    current = (request.headers.get("expo-current-update-id") or "").strip().lower()
    embedded = (request.headers.get("expo-embedded-update-id") or "").strip().lower()
    params = au.parse_extra_params(request.headers.get("expo-extra-params", ""))
    device = au.note_device(params.get("device", ""), runtime=runtime,
                            update_id="" if (not current or current == embedded) else current)
    update = au.current_for(runtime, tester=bool(device and device.tester))

    if update is None:
        if protocol >= 1 and current and embedded and current != embedded:
            # It runs downloaded code that is no longer offered (rolled back): back to the APK's own code.
            return _part(request, "directive", {"type": "rollBackToEmbedded", "parameters": {"commitTime": au.iso(timezone.now())}}, protocol)
        return _no_update(request, protocol)
    if current and current == str(update.id):
        return _no_update(request, protocol)
    return _part(request, "manifest", au.manifest_of(update, _base_url(request)), protocol)


@require_safe
def asset(request, name: str):
    path = au.asset_path(name)
    if path is None:
        raise Http404("No such file.")
    resp = FileResponse(path.open("rb"), content_type="application/javascript" if name.endswith(".bundle") else au.content_type_of(name))
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@require_safe
def certificate(request):
    """The public half of the signing key (it goes into the app). Safe to share; the private key never leaves."""
    try:
        pem = au.certificate_file().read_bytes()
    except OSError:
        raise Http404("No certificate yet (run: manage.py app_update keygen).")
    resp = HttpResponse(pem, content_type="text/plain; charset=utf-8")
    resp["X-Certificate-SHA256"] = au.certificate_fingerprint(pem)
    return resp


@require_safe
def apk(request, name: str):
    path = au.apk_path(name)
    if path is None:
        raise Http404("No such APK.")
    return FileResponse(path.open("rb"), content_type="application/vnd.android.package-archive", as_attachment=True, filename=name)


class HelloSerializer(serializers.Serializer):
    device = serializers.RegexField(au.DEVICE_ID.pattern)
    runtime = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")
    update_id = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")
    model = serializers.CharField(max_length=80, allow_blank=True, required=False, default="")
    app_version = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")


class UpdateHelloView(APIView):
    """The app says which phone this is and what it runs. Answer: is it a test phone, and is a newer APK waiting."""

    def post(self, request):
        s = HelloSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        dev = au.note_device(d["device"], runtime=d["runtime"], update_id=d["update_id"], user=request.user,
                             model=d["model"], app_version=d["app_version"])
        tester = bool(dev and dev.tester)
        new_apk = au.apk_for(d["runtime"], tester=tester) if d["runtime"] else None
        return Response({
            "tester": tester,
            "apk": {"version": new_apk.version, "size": new_apk.size, "notes": new_apk.notes,
                    "url": f"{_base_url(request)}/api/v1/updates/apk/{new_apk.file}"} if new_apk else None,
        })
