"""App updates over the air: JARVIS serves the Expo Updates protocol (version 1) itself. No APK for app-code changes.

How it fits together
- On a push that changes the app, GitHub builds the app's code (`npx expo export`) and hands it to
  `manage.py app_update publish`. It reaches the test phones only (channel "preview").
- `app_update promote` gives it to everyone; `app_update rollback` takes the newest one back.
- Every phone asks GET /api/v1/updates/manifest when JARVIS opens. The answer is the newest update for its native
  build (the runtime version) and channel, signed with the private key; the app holds the certificate and refuses
  anything not signed with it. Files are stored and served by their content hash, so a URL never changes content.
- A phone only switches to an update NEWER than the one it runs. So a rollback publishes the older code again as a
  new update (same files, new id and time), and the bad one is withdrawn: it is never served again.
- Native changes (wake word, permissions) need a new APK: `app_update apk` offers it, and the app shows
  "New version available" (one tap on each phone).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from datetime import timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AppApk, AppDevice, AppUpdate, Channel

log = logging.getLogger("jarvis.updates")

PLATFORM = "android"
KEY_ID = "main"
ALG = "rsa-v1_5-sha256"

CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".svg": "image/svg+xml", ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff",
    ".woff2": "font/woff2", ".json": "application/json", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".mp4": "video/mp4", ".bundle": "application/javascript", ".js": "application/javascript",
}
SAFE_FILE = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]{1,10}$")
SAFE_APK = re.compile(r"^JARVIS-\d{1,3}\.\d{1,3}\.\d{1,3}\.apk$")
DEVICE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
MAX_APK_BYTES = 300 * 1024 * 1024


class UpdateError(Exception):
    """Something an operator must fix (said in plain words by the command)."""


# ---------------------------------------------------------------------------------------------------- the store
def store() -> Path:
    return Path(settings.JARVIS_UPDATES_DIR)


def _dir(name: str) -> Path:
    d = store() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_path(name: str) -> Path | None:
    """The stored file for a public asset name, or None (bad name, or not there)."""
    if not SAFE_FILE.match(name or ""):
        return None
    p = store() / "assets" / name
    return p if p.is_file() else None


def apk_path(name: str) -> Path | None:
    if not SAFE_APK.match(name or ""):
        return None
    p = store() / "apk" / name
    return p if p.is_file() else None


def content_type_of(name: str) -> str:
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _b64url(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _now_ms():
    """Now, to the millisecond (the manifest's createdAt), and always after the newest update's time."""
    now = timezone.now()
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)
    newest = AppUpdate.objects.order_by("-created_at").values_list("created_at", flat=True).first()
    if newest and now <= newest:
        now = newest + timedelta(milliseconds=1)
    return now


def iso(dt) -> str:
    return dt.astimezone(dt_timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------------- publish
def _unpack(source: Path, into: Path) -> Path:
    """The exported folder: `source` itself, or a .tgz of it unpacked safely (no links, nothing outside)."""
    if source.is_dir():
        return source
    if not source.is_file():
        raise UpdateError(f"Not found: {source}")
    with tarfile.open(source, "r:*") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if m.issym() or m.islnk() or m.isdev() or m.name.startswith(("/", "\\")) or ".." in parts:
                raise UpdateError(f"Refusing an unsafe entry in the archive: {m.name}")
        try:
            tar.extractall(into, filter="data")
        except TypeError:  # Python without extraction filters: every entry was checked above
            tar.extractall(into)
    return into


def _runtime_of(expo: dict) -> str:
    rv = expo.get("runtimeVersion")
    if isinstance(rv, str) and rv.strip():
        return rv.strip()
    if isinstance(rv, dict) and rv.get("policy") == "appVersion" and expo.get("version"):
        return str(expo["version"])
    raise UpdateError("The app config has no plain runtimeVersion (app.config.js sets it); nothing was published.")


def _take(root: Path, rel: str, ext: str, *, launch: bool = False) -> dict:
    """Copy one exported file into the store under its content hash; return its manifest facts."""
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise UpdateError(f"A file points outside the export: {rel}")
    if not path.is_file():
        raise UpdateError(f"A file listed in metadata.json is missing: {rel}")
    data = path.read_bytes()
    sha = hashlib.sha256(data)
    ext = ext.lower()
    name = sha.hexdigest() + ext
    dest = _dir("assets") / name
    if not dest.exists():
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(data)
        part.replace(dest)
    item = {"file": name, "hash": _b64url(sha.digest()), "key": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            "contentType": "application/javascript" if launch else content_type_of(name)}
    if not launch:
        item["fileExtension"] = ext
    return item


def _fingerprint(launch: dict, assets: list[dict], expo: dict) -> str:
    body = json.dumps({"launch": launch["hash"], "assets": sorted(a["hash"] for a in assets), "config": expo},
                      sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()


def publish(source: Path, *, commit: str = "", message: str = "") -> tuple[AppUpdate, bool]:
    """Publish an `npx expo export --platform android` folder (or a .tgz of it) that also holds expoConfig.json.

    It goes to the test phones only. Returns (update, created); created is False when the code and files are
    identical to the newest update already published for that native build (nothing new to send)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _unpack(Path(source), Path(tmp))
        try:
            meta = json.loads((root / "metadata.json").read_text("utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise UpdateError(f"metadata.json is missing or unreadable ({exc}); was this made by `npx expo export`?") from exc
        files = (meta.get("fileMetadata") or {}).get(PLATFORM) or {}
        if not files.get("bundle"):
            raise UpdateError("metadata.json has no Android bundle (export with --platform android).")
        try:
            expo = json.loads((root / "expoConfig.json").read_text("utf-8-sig"))  # a Windows shell may add a BOM
        except (OSError, ValueError) as exc:
            raise UpdateError("expoConfig.json is missing (npx expo config --type public --json > dist/expoConfig.json).") from exc
        expo = expo.get("exp", expo) if isinstance(expo, dict) else {}
        runtime = _runtime_of(expo)
        launch = _take(root, files["bundle"], ".bundle", launch=True)
        assets = [_take(root, a["path"], "." + str(a.get("ext") or "bin").lstrip(".")) for a in files.get("assets", [])]
    fingerprint = _fingerprint(launch, assets, expo)
    with transaction.atomic():
        newest = AppUpdate.objects.select_for_update().filter(runtime_version=runtime, platform=PLATFORM, withdrawn=False).first()
        if newest and newest.fingerprint == fingerprint:
            return newest, False
        update = AppUpdate.objects.create(
            runtime_version=runtime, platform=PLATFORM, channel=Channel.PREVIEW, created_at=_now_ms(),
            launch_asset=launch, assets=assets, expo_client=expo, fingerprint=fingerprint,
            commit=(commit or "")[:40], message=(message or "")[:200])
    return update, True


def _republish(src: AppUpdate, channel: str, message: str) -> AppUpdate:
    """The same code again as a NEW update (new id, newest time), so phones on a newer one take it."""
    return AppUpdate.objects.create(
        runtime_version=src.runtime_version, platform=src.platform, channel=channel, created_at=_now_ms(),
        launch_asset=src.launch_asset, assets=src.assets, expo_client=src.expo_client, fingerprint=src.fingerprint,
        commit=src.commit, message=message[:200], republished_from=src,
        promoted_at=timezone.now() if channel == Channel.PRODUCTION else None)


def label(u: AppUpdate | None) -> str:
    if u is None:
        return "the APK's own code"
    what = u.message or (f"commit {u.commit[:7]}" if u.commit else "")
    return f"{str(u.id)[:8]} · {timezone.localtime(u.created_at):%d %b %H:%M}" + (f" · {what}" if what else "")


# ---------------------------------------------------------------------------------------------------- promote / rollback
def promote() -> list[str]:
    """The newest test update (and the newest test APK) go to everyone. Returns what was done, in words."""
    done = []
    with transaction.atomic():
        live_qs = AppUpdate.objects.select_for_update().filter(platform=PLATFORM, withdrawn=False)
        for runtime in sorted(set(live_qs.values_list("runtime_version", flat=True))):
            # Only the NEWEST update of each native build: an older test update that was never sent is superseded.
            newest = live_qs.filter(runtime_version=runtime).order_by("-created_at").first()
            if newest is None or newest.channel != Channel.PREVIEW:
                continue
            live = live_qs.filter(runtime_version=runtime, channel=Channel.PRODUCTION).order_by("-created_at").first()
            if live and live.fingerprint == newest.fingerprint:
                done.append(f"[{runtime}] The test phones run the same code as everyone: nothing new to send.")
                continue
            newest.channel, newest.promoted_at = Channel.PRODUCTION, timezone.now()
            newest.save(update_fields=["channel", "promoted_at"])
            done.append(f"[{runtime}] Sent to everyone: {label(newest)}")
        apk = AppApk.objects.select_for_update().filter(withdrawn=False, channel=Channel.PREVIEW).first()
        if apk:
            apk.channel = Channel.PRODUCTION
            apk.save(update_fields=["channel"])
            done.append(f"APK {apk.version} offered to everyone.")
    return done or ["Nothing on the test phones to send: everyone already has the newest version."]


def rollback(*, everyone: bool = False) -> list[str]:
    """Take the newest update back. Still only on the test phones: they return to everyone's code. Already with
    everyone (or `everyone`): everyone returns to the version before it (or to the APK's own code if none)."""
    done = []
    with transaction.atomic():
        live_qs = AppUpdate.objects.select_for_update().filter(platform=PLATFORM, withdrawn=False)
        bad = (live_qs.filter(channel=Channel.PRODUCTION) if everyone else live_qs).first()
        if not bad:
            raise UpdateError("Nothing to roll back: no update is being served.")
        others = AppUpdate.objects.filter(runtime_version=bad.runtime_version, platform=PLATFORM, withdrawn=False,
                                          channel=Channel.PRODUCTION).exclude(pk=bad.pk).exclude(fingerprint=bad.fingerprint)
        if bad.channel == Channel.PREVIEW:
            back_to = others.first()
            newer_tests = AppUpdate.objects.filter(runtime_version=bad.runtime_version, platform=PLATFORM, withdrawn=False,
                                                   channel=Channel.PREVIEW)
            if back_to:
                newer_tests = newer_tests.filter(created_at__gt=back_to.created_at)
            n = newer_tests.update(withdrawn=True)
            done.append(f"Withdrawn from the test phones: {label(bad)}" + (f" (and {n - 1} older test update(s))" if n > 1 else ""))
            if back_to:
                copy = _republish(back_to, Channel.PREVIEW, f"Back to {label(back_to)}")
                done.append(f"Test phones go back to everyone's code ({label(copy)}).")
            else:
                done.append("Test phones go back to the APK's own code.")
        else:
            back_to = others.filter(created_at__lt=bad.created_at).first()
            bad.withdrawn = True
            bad.save(update_fields=["withdrawn"])
            done.append(f"Withdrawn from everyone: {label(bad)}")
            if back_to:
                copy = _republish(back_to, Channel.PRODUCTION, f"Back to {label(back_to)}")
                done.append(f"Everyone goes back to {label(back_to)} (sent as {str(copy.id)[:8]}).")
            else:
                done.append("Everyone goes back to the APK's own code.")
    return done


# ---------------------------------------------------------------------------------------------------- what a phone gets
def parse_extra_params(header: str) -> dict[str, str]:
    """The `Expo-Extra-Params` header (a structured-field dictionary of strings): device="ab12…"."""
    out = {}
    for key, value in re.findall(r'([a-z*][a-z0-9_\-.*]*)\s*=\s*"((?:[^"\\]|\\.)*)"', header or ""):
        out[key] = value.replace('\\"', '"').replace("\\\\", "\\")
    return out


def note_device(device_id: str, *, runtime: str = "", update_id: str | None = None, user=None, model: str = "",
                app_version: str = "") -> AppDevice | None:
    """Remember a phone (random id made by the app). Unknown or malformed ids are ignored."""
    if not DEVICE_ID.match(device_id or ""):
        return None
    dev, _ = AppDevice.objects.get_or_create(device_id=device_id)
    if runtime:
        dev.runtime_version = runtime[:40]
    if update_id is not None:
        dev.update_id = update_id[:40]
    if user is not None:
        dev.user = user
    if model:
        dev.model = model[:80]
    if app_version:
        dev.app_version = app_version[:40]
    dev.save()
    return dev


def current_for(runtime: str, *, tester: bool) -> AppUpdate | None:
    qs = AppUpdate.objects.filter(runtime_version=runtime, platform=PLATFORM, withdrawn=False)
    if not tester:
        qs = qs.filter(channel=Channel.PRODUCTION)
    return qs.order_by("-created_at").first()


def manifest_of(update: AppUpdate, base_url: str) -> dict:
    def asset(a: dict, launch: bool = False) -> dict:
        out = {"hash": a["hash"], "key": a["key"], "contentType": a["contentType"],
               "url": f"{base_url}/api/v1/updates/assets/{a['file']}"}
        if not launch and a.get("fileExtension"):
            out["fileExtension"] = a["fileExtension"]
        return out

    return {
        "id": str(update.id), "createdAt": iso(update.created_at), "runtimeVersion": update.runtime_version,
        "launchAsset": asset(update.launch_asset, launch=True), "assets": [asset(a) for a in update.assets],
        "metadata": {}, "extra": {"expoClient": update.expo_client},
    }


def _version_tuple(v: str) -> tuple[int, ...]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def apk_for(runtime: str, *, tester: bool) -> AppApk | None:
    """A newer APK this phone should install (its native build is older), or None."""
    qs = AppApk.objects.filter(withdrawn=False)
    if not tester:
        qs = qs.filter(channel=Channel.PRODUCTION)
    mine = _version_tuple(runtime)
    best = None
    for apk in qs:
        if _version_tuple(apk.version) > mine and (best is None or _version_tuple(apk.version) > _version_tuple(best.version)):
            best = apk
    return best


def apk_version(path: Path) -> tuple[str, int]:
    """(versionName, versionCode) read from the APK's own AndroidManifest.xml (Android's binary XML format), so an
    offer can never carry a different version from the file: a phone that installed it would be offered it again."""
    import struct
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            data = z.read("AndroidManifest.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"Cannot read this APK ({exc}); is it the right file?") from exc

    def u16(o):
        return struct.unpack_from("<H", data, o)[0]

    def u32(o):
        return struct.unpack_from("<I", data, o)[0]

    want = {"versionName": 0x0101021C, "versionCode": 0x0101021B}  # android:versionName / android:versionCode
    strings: list[str] = []
    res_ids: list[int] = []
    try:
        pos = u16(2)
        while pos + 8 <= len(data):
            ctype, hsize, csize = u16(pos), u16(pos + 2), u32(pos + 4)
            if csize < 8:
                break
            if ctype == 0x0001:  # string pool
                count, flags, start = u32(pos + 8), u32(pos + 16), u32(pos + 20)
                for i in range(count):
                    o = pos + start + u32(pos + hsize + 4 * i)
                    if flags & 0x100:  # UTF-8: the UTF-16 length, then the byte length (1 or 2 bytes each)
                        o += 2 if data[o] & 0x80 else 1
                        n = data[o]
                        if n & 0x80:
                            n, o = ((n & 0x7F) << 8) | data[o + 1], o + 2
                        else:
                            o += 1
                        strings.append(data[o:o + n].decode("utf-8", "replace"))
                    else:
                        n, o = u16(o), o + 2
                        if n & 0x8000:
                            n, o = ((n & 0x7FFF) << 16) | u16(o), o + 2
                        strings.append(data[o:o + 2 * n].decode("utf-16-le", "replace"))
            elif ctype == 0x0180:  # the resource ids of attribute names
                res_ids = [u32(pos + 8 + 4 * i) for i in range((csize - 8) // 4)]
            elif ctype == 0x0102:  # an element starts
                ext = pos + hsize
                if strings[u32(ext + 4)] == "manifest":
                    attr_start, attr_size, count = u16(ext + 8), u16(ext + 10), u16(ext + 12)
                    found = {}
                    for i in range(count):
                        a = ext + attr_start + i * attr_size
                        ni, raw, dtype, val = u32(a + 4), u32(a + 8), data[a + 15], u32(a + 16)
                        for key, rid in want.items():
                            if (ni < len(res_ids) and res_ids[ni] == rid) or (ni < len(strings) and strings[ni] == key):
                                found[key] = strings[raw] if raw != 0xFFFFFFFF else (strings[val] if dtype == 0x03 else val)
                    return str(found.get("versionName", "")), int(found.get("versionCode", 0) or 0)
            pos += csize
    except (IndexError, struct.error, ValueError) as exc:
        raise UpdateError(f"Cannot read the version inside this APK ({exc}).") from exc
    raise UpdateError("This APK has no version in its manifest.")


def add_apk(source: Path, version: str = "", notes: str = "", replace: bool = False) -> AppApk:
    """Offer a new APK (test phones first; `promote` gives it to everyone). The version is read from the APK itself;
    a `version` given by hand must match it."""
    source = Path(source)
    if not source.is_file() or source.suffix.lower() != ".apk":
        raise UpdateError(f"Not an APK file: {source}")
    inside, _code = apk_version(source)
    if version and version != inside:
        raise UpdateError(f"This APK is version {inside}, not {version}. Nothing was offered.")
    version = inside
    if not VERSION.match(version or ""):
        raise UpdateError(f"The version inside this APK ({version or 'none'}) does not look like 2.6.0.")
    size = source.stat().st_size
    if size > MAX_APK_BYTES:
        raise UpdateError("That APK is larger than 300 MB; is it the right file?")
    name = f"JARVIS-{version}.apk"
    dest = _dir("apk") / name
    if dest.exists() and not replace:
        raise UpdateError(f"APK {version} is already offered (add --replace to swap the file).")
    digest = hashlib.sha256()
    part = dest.with_name(name + ".part")
    with source.open("rb") as src, part.open("wb") as out:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
            out.write(chunk)
    part.replace(dest)
    AppApk.objects.filter(version=version).update(withdrawn=True)
    return AppApk.objects.create(version=version, file=name, size=size, sha256=digest.hexdigest(), notes=notes[:200])


# ---------------------------------------------------------------------------------------------------- signing
_KEY_CACHE: dict = {}


def key_file() -> Path:
    return Path(settings.JARVIS_UPDATES_KEY_FILE)


def certificate_file() -> Path:
    return key_file().with_name("updates-certificate.pem")


def _private_key():
    from cryptography.hazmat.primitives import serialization

    path = key_file()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    if _KEY_CACHE.get("stamp") != (str(path), stamp):
        _KEY_CACHE["key"] = serialization.load_pem_private_key(path.read_bytes(), password=None)
        _KEY_CACHE["stamp"] = (str(path), stamp)
    return _KEY_CACHE["key"]


def can_sign() -> bool:
    return _private_key() is not None


def signature_header(body: bytes) -> str:
    """The `expo-signature` value for a manifest or directive body: RSA (PKCS#1 v1.5) with SHA-256, base64."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key = _private_key()
    if key is None:
        raise UpdateError("No signing key on this server (run: manage.py app_update keygen).")
    sig = key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return f'sig="{base64.b64encode(sig).decode()}", keyid="{KEY_ID}"'


def certificate_fingerprint(pem: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    digest = x509.load_pem_x509_certificate(pem).fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def keygen(force: bool = False, group: str = "") -> tuple[Path, str]:
    """Make the signing key (stays on this server, readable by its owner only) and its certificate (public: it goes
    into the app). Refuses to replace an existing key: installed apps would refuse every update until a new APK.
    `group`: the server runs as another user (on the VPS: jarvis), so that group may read the key too (like .env)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    kf, cf = key_file(), certificate_file()
    if kf.exists() and not force:
        raise UpdateError(f"A signing key already exists at {kf}. Replacing it makes every installed app refuse updates "
                          "until a new APK is installed; add --force only if that is what you want.")
    kf.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(kf.parent, 0o700)
    except OSError:
        pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS app updates")])
    now = timezone.now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    tmp = kf.with_name(kf.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key_pem)
    os.replace(tmp, kf)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cf.write_bytes(cert_pem)
    if group:
        import grp  # Linux only: the VPS

        gid = grp.getgrnam(group).gr_gid
        for path, mode in ((kf.parent, 0o750), (kf, 0o640), (cf, 0o644)):
            os.chown(path, -1, gid)
            os.chmod(path, mode)
    _KEY_CACHE.clear()
    return cf, certificate_fingerprint(cert_pem)


def remove_store_for_tests() -> None:  # pragma: no cover - used by tests only
    shutil.rmtree(store(), ignore_errors=True)
