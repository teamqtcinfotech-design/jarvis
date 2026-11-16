"""Over-the-air app updates (appupdates.py, updateviews.py, manage.py app_update)."""
import base64
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core import appupdates as au
from core.models import AppDevice, AppUpdate, Channel, User

RUNTIME = "9.9.0"
DEVICE = "a1b2c3d4e5f60718"


def make_export(root: Path, bundle: bytes = b"bundle-v1", image: bytes = b"png-bytes", runtime: str = RUNTIME) -> Path:
    dist = Path(tempfile.mkdtemp(dir=root)) / "dist"
    (dist / "_expo/static/js/android").mkdir(parents=True)
    (dist / "assets").mkdir()
    (dist / "_expo/static/js/android/entry-abc.hbc").write_bytes(bundle)
    md5 = hashlib.md5(image, usedforsecurity=False).hexdigest()
    (dist / "assets" / md5).write_bytes(image)
    (dist / "metadata.json").write_text(json.dumps({"version": 0, "bundler": "metro", "fileMetadata": {"android": {
        "bundle": "_expo/static/js/android/entry-abc.hbc", "assets": [{"path": f"assets/{md5}", "ext": "png"}]}}}))
    (dist / "expoConfig.json").write_text(json.dumps({"name": "JARVIS", "slug": "jarvis", "version": runtime, "runtimeVersion": runtime}))
    return dist


def make_apk(path: Path, version: str, code: int) -> Path:
    """A minimal APK: a zip whose AndroidManifest.xml is Android's binary XML with versionName / versionCode."""
    import struct
    import zipfile

    strings = ["versionCode", "versionName", "manifest", version]
    blob, offsets = b"", []
    for s in strings:
        offsets.append(len(blob))
        blob += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
    blob += b"\0" * (-len(blob) % 4)
    start = 28 + 4 * len(strings)
    pool = struct.pack("<HHIIIIII", 0x0001, 28, start + len(blob), len(strings), 0, 0, start, 0)
    pool += b"".join(struct.pack("<I", o) for o in offsets) + blob
    resmap = struct.pack("<HHI", 0x0180, 8, 16) + struct.pack("<II", 0x0101021B, 0x0101021C)
    none = 0xFFFFFFFF
    attrs = struct.pack("<IIIHBBI", none, 0, none, 8, 0, 0x10, code) + struct.pack("<IIIHBBI", none, 1, 3, 8, 0, 0x03, 3)
    element = struct.pack("<HHIII", 0x0102, 16, 16 + 20 + len(attrs), 1, none)
    element += struct.pack("<IIHHHHHH", none, 2, 20, 20, 2, 0, 0, 0) + attrs
    body = pool + resmap + element
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body)
    return path


def parse_multipart(resp):
    """{part name: (headers dict, body bytes)} of a multipart/mixed response."""
    boundary = resp["Content-Type"].split("boundary=")[1].encode()
    parts = {}
    for chunk in resp.content.split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, body = chunk.partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        name = headers["content-disposition"].split('name="')[1].split('"')[0]
        parts[name] = (headers, body)
    return parts


class UpdatesBase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.override = override_settings(JARVIS_UPDATES_DIR=self.tmp / "store",
                                          JARVIS_UPDATES_KEY_FILE=self.tmp / "keys" / "updates-private.pem",
                                          JARVIS_PUBLIC_URL="http://jarvis.test:8088")
        self.override.enable()
        self.addCleanup(self.override.disable)
        au._KEY_CACHE.clear()
        self.cert_path, _ = au.keygen()
        self.cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        self.client = APIClient()

    def ask(self, device: str = DEVICE, current: str = "", embedded: str = "emb-1", runtime: str = RUNTIME, sign: bool = True):
        headers = {"HTTP_EXPO_PROTOCOL_VERSION": "1", "HTTP_EXPO_PLATFORM": "android", "HTTP_EXPO_RUNTIME_VERSION": runtime,
                   "HTTP_EXPO_EMBEDDED_UPDATE_ID": embedded, "HTTP_EXPO_CURRENT_UPDATE_ID": current or embedded,
                   "HTTP_ACCEPT": "multipart/mixed,application/expo+json,application/json"}
        if device:
            headers["HTTP_EXPO_EXTRA_PARAMS"] = f'device="{device}"'
        if sign:
            headers["HTTP_EXPO_EXPECT_SIGNATURE"] = 'sig, keyid="main", alg="rsa-v1_5-sha256"'
        return self.client.get("/api/v1/updates/manifest", **headers)

    def body_of(self, resp) -> tuple[str, dict, dict]:
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        parts = parse_multipart(resp)
        self.assertEqual(len(parts), 1)
        name, (headers, body) = next(iter(parts.items()))
        sig = headers.get("expo-signature", "")
        if sig:  # the phone's check: RSA PKCS#1 v1.5 + SHA-256 over the part body, with the embedded certificate
            raw = base64.b64decode(sig.split('sig="')[1].split('"')[0])
            self.cert.public_key().verify(raw, body, padding.PKCS1v15(), hashes.SHA256())
            self.assertIn('keyid="main"', sig)
        return name, json.loads(body), headers

    def publish(self, **kw):
        return au.publish(make_export(self.tmp, **kw), commit="abc1234", message="test")


class PublishAndServeTests(UpdatesBase):
    def test_signed_manifest_for_a_test_phone_and_files_match_their_hashes(self):
        update, created = self.publish()
        self.assertTrue(created)
        self.assertEqual(update.channel, Channel.PREVIEW)
        AppDevice.objects.create(device_id=DEVICE, tester=True)
        name, manifest, headers = self.body_of(self.ask())
        self.assertEqual(name, "manifest")
        self.assertTrue(headers.get("expo-signature"))
        self.assertEqual(manifest["id"], str(update.id))
        self.assertEqual(manifest["runtimeVersion"], RUNTIME)
        self.assertTrue(manifest["createdAt"].endswith("Z"))
        self.assertEqual(manifest["extra"]["expoClient"]["version"], RUNTIME)
        self.assertNotIn("fileExtension", manifest["launchAsset"])
        self.assertEqual(manifest["assets"][0]["fileExtension"], ".png")
        for item in [manifest["launchAsset"], *manifest["assets"]]:
            self.assertTrue(item["url"].startswith("http://jarvis.test:8088/api/v1/updates/assets/"))
            r = self.client.get(item["url"].replace("http://jarvis.test:8088", ""))
            self.assertEqual(r.status_code, 200)
            data = b"".join(r.streaming_content)
            self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), item["hash"])
            self.assertEqual(r["Content-Type"], item["contentType"])
            self.assertIn("immutable", r["Cache-Control"])
        self.assertEqual(manifest["assets"][0]["key"], hashlib.md5(b"png-bytes", usedforsecurity=False).hexdigest())

    def test_everyone_else_gets_nothing_until_promote(self):
        update, _ = self.publish()
        name, body, _ = self.body_of(self.ask(device="ffffffff00000000"))
        self.assertEqual((name, body), ("directive", {"type": "noUpdateAvailable"}))
        self.body_of(self.ask(device=""))  # a phone that has not said its id yet: also nothing
        au.promote()
        update.refresh_from_db()
        self.assertEqual(update.channel, Channel.PRODUCTION)
        name, body, _ = self.body_of(self.ask(device="ffffffff00000000"))
        self.assertEqual((name, body["id"]), ("manifest", str(update.id)))

    def test_the_update_it_already_runs_is_not_sent_again(self):
        update, _ = self.publish()
        au.promote()
        name, body, _ = self.body_of(self.ask(current=str(update.id)))
        self.assertEqual((name, body), ("directive", {"type": "noUpdateAvailable"}))

    def test_identical_publish_is_skipped_and_a_change_is_new(self):
        first, _ = self.publish()
        again, created = self.publish()
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        second, created = self.publish(bundle=b"bundle-v2")
        self.assertTrue(created)
        self.assertGreater(second.created_at, first.created_at)

    def test_other_native_builds_get_nothing(self):
        self.publish()
        au.promote()
        name, body, _ = self.body_of(self.ask(runtime="9.8.0"))
        self.assertEqual(body, {"type": "noUpdateAvailable"})

    def test_unsigned_request_is_answered_unsigned_and_no_key_means_no_update(self):
        self.publish()
        au.promote()
        _, _, headers = self.body_of(self.ask(sign=False))
        self.assertNotIn("expo-signature", headers)
        au.key_file().unlink()
        au._KEY_CACHE.clear()
        self.assertEqual(self.ask().status_code, 503)  # never an unsigned answer to a phone that wants a signature

    def test_bad_requests(self):
        r = self.client.get("/api/v1/updates/manifest", HTTP_EXPO_PROTOCOL_VERSION="1", HTTP_EXPO_PLATFORM="ios",
                            HTTP_EXPO_RUNTIME_VERSION=RUNTIME)
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/v1/updates/manifest", HTTP_EXPO_PROTOCOL_VERSION="7", HTTP_EXPO_PLATFORM="android",
                            HTTP_EXPO_RUNTIME_VERSION=RUNTIME)
        self.assertEqual(r.status_code, 406)
        for name in ["../../keys/updates-private.pem", "a" * 64 + ".png/..", "notahash.png", "updates-private.pem"]:
            self.assertEqual(self.client.get(f"/api/v1/updates/assets/{name}").status_code, 404, name)
        self.assertEqual(self.client.get("/api/v1/updates/apk/..%2Fkeys%2Fupdates-private.pem").status_code, 404)

    def test_certificate_is_public_and_the_key_is_not(self):
        r = self.client.get("/api/v1/updates/certificate.pem")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"BEGIN CERTIFICATE", r.content)
        self.assertNotIn(b"PRIVATE", r.content)
        self.assertEqual(r["X-Certificate-SHA256"], au.certificate_fingerprint(r.content))
        ext = self.cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        self.assertTrue(ext.critical)
        self.assertIn(x509.oid.ExtendedKeyUsageOID.CODE_SIGNING, ext.value)
        self.assertTrue(self.cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature)

    def test_keygen_never_replaces_a_key_by_accident(self):
        with self.assertRaises(au.UpdateError):
            au.keygen()
        before = au.key_file().read_bytes()
        au.keygen(force=True)
        self.assertNotEqual(au.key_file().read_bytes(), before)

    def test_unsafe_archive_is_refused_and_a_good_one_publishes(self):
        bad = self.tmp / "bad.tgz"
        with tarfile.open(bad, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(au.UpdateError):
            au.publish(bad)
        good = self.tmp / "good.tgz"
        with tarfile.open(good, "w:gz") as tar:
            tar.add(make_export(self.tmp), arcname=".")
        update, created = au.publish(good)
        self.assertTrue(created)


class PromoteAndRollbackTests(UpdatesBase):
    def setUp(self):
        super().setUp()
        AppDevice.objects.create(device_id=DEVICE, tester=True)
        self.everyone = "eeeeeeee00000000"

    def served(self, device):
        name, body, _ = self.body_of(self.ask(device=device))
        return body.get("id") if name == "manifest" else body.get("type")

    def test_bad_test_version_goes_back_only_on_test_phones(self):
        v1, _ = self.publish(bundle=b"v1")
        au.promote()
        v2, _ = self.publish(bundle=b"v2")
        self.assertEqual(self.served(DEVICE), str(v2.id))
        self.assertEqual(self.served(self.everyone), str(v1.id))
        au.rollback()
        v2.refresh_from_db()
        self.assertTrue(v2.withdrawn)
        copy = AppUpdate.objects.filter(withdrawn=False).first()
        self.assertEqual(copy.fingerprint, v1.fingerprint)  # the same code as everyone's…
        self.assertGreater(copy.created_at, v2.created_at)  # …as a newer update, so the test phone takes it
        self.assertEqual(self.served(DEVICE), str(copy.id))
        self.assertEqual(self.served(self.everyone), str(v1.id))  # everyone untouched
        self.assertIn("nothing new", " ".join(au.promote()))

    def test_bad_version_with_everyone_goes_back_for_everyone(self):
        v1, _ = self.publish(bundle=b"v1")
        au.promote()
        v2, _ = self.publish(bundle=b"v2")
        au.promote()
        self.assertEqual(self.served(self.everyone), str(v2.id))
        au.rollback()
        copy = AppUpdate.objects.filter(withdrawn=False).first()
        self.assertEqual((copy.fingerprint, copy.channel), (v1.fingerprint, Channel.PRODUCTION))
        self.assertEqual(self.served(self.everyone), str(copy.id))
        self.assertEqual(self.served(DEVICE), str(copy.id))

    def test_rolling_back_the_only_version_returns_phones_to_the_apk(self):
        v1, _ = self.publish(bundle=b"v1")
        au.promote()
        au.rollback()
        name, body, _ = self.body_of(self.ask(device=self.everyone, current=str(v1.id)))
        self.assertEqual((name, body["type"]), ("directive", "rollBackToEmbedded"))
        name, body, _ = self.body_of(self.ask(device=self.everyone))  # a phone on the APK's code: nothing to do
        self.assertEqual(body, {"type": "noUpdateAvailable"})

    def test_promote_never_sends_an_old_superseded_test_version(self):
        self.publish(bundle=b"v1")
        v2, _ = self.publish(bundle=b"v2")
        au.promote()
        self.assertEqual(self.served(self.everyone), str(v2.id))
        self.assertIn("Nothing on the test phones", " ".join(au.promote()))
        self.assertEqual(self.served(self.everyone), str(v2.id))

    def test_rollback_everyone_while_a_test_version_is_newer(self):
        v1, _ = self.publish(bundle=b"v1")
        au.promote()
        v2, _ = self.publish(bundle=b"v2")
        au.promote()
        self.publish(bundle=b"v3")  # on the test phones only
        au.rollback(everyone=True)
        v2.refresh_from_db()
        self.assertTrue(v2.withdrawn)
        copy = AppUpdate.objects.filter(withdrawn=False, channel=Channel.PRODUCTION).first()
        self.assertEqual(copy.fingerprint, v1.fingerprint)
        self.assertEqual(self.served(self.everyone), str(copy.id))


@override_settings(JARVIS_AI_ENABLED=False)
class HelloAndApkTests(UpdatesBase):
    def test_hello_links_the_phone_and_offers_a_newer_apk_to_test_phones_first(self):
        user = User.objects.create_user(username="nikki", password="x" * 12, role=User.Role.TM)
        api = APIClient()
        api.force_authenticate(user)
        body = {"device": DEVICE, "runtime": "2.5.0", "update_id": "", "model": "Xiaomi 13", "app_version": "2.5.0"}
        r = api.post("/api/v1/updates/hello/", body, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"tester": False, "apk": None})
        dev = AppDevice.objects.get(device_id=DEVICE)
        self.assertEqual((dev.user, dev.model), (user, "Xiaomi 13"))

        apk_file = make_apk(self.tmp / "app.apk", "2.6.0", 12)
        self.assertEqual(au.apk_version(apk_file), ("2.6.0", 12))
        with self.assertRaises(au.UpdateError):  # a label that is not the APK's own version is refused
            au.add_apk(apk_file, "2.6.1")
        au.add_apk(apk_file, notes="Wake word fix")  # the version comes from the file
        self.assertIsNone(api.post("/api/v1/updates/hello/", body, format="json").json()["apk"])  # test phones first
        call_command("app_update", "tester", DEVICE[:8], stdout=io.StringIO())
        offer = api.post("/api/v1/updates/hello/", body, format="json").json()
        self.assertTrue(offer["tester"])
        self.assertEqual(offer["apk"]["version"], "2.6.0")
        r = self.client.get(offer["apk"]["url"].replace("http://jarvis.test:8088", ""))
        self.assertEqual((r.status_code, b"".join(r.streaming_content)), (200, apk_file.read_bytes()))
        same = {**body, "runtime": "2.6.0"}
        self.assertIsNone(api.post("/api/v1/updates/hello/", same, format="json").json()["apk"])  # already on it
        self.assertEqual(APIClient().post("/api/v1/updates/hello/", body, format="json").status_code, 401)

    def test_a_phone_can_be_made_a_tester_before_its_first_check(self):
        full = "0123456789abcdef0123456789abcdef"
        call_command("app_update", "tester", full, stdout=io.StringIO())
        self.assertTrue(AppDevice.objects.get(device_id=full).tester)
        self.publish()
        name, body, _ = self.body_of(self.ask(device=full))  # its very first check already gets the test version
        self.assertEqual(name, "manifest")
        with self.assertRaises(Exception):  # a short prefix of an unknown phone is still refused
            call_command("app_update", "tester", "abcdef12", stdout=io.StringIO())

    def test_bad_device_ids_are_ignored(self):
        self.assertIsNone(au.note_device("x;drop"))
        self.assertIsNone(au.note_device(""))
        self.assertEqual(au.parse_extra_params('device="abc12345", other="x\\"y"'), {"device": "abc12345", "other": 'x"y'})

    def test_command_round_trip(self):
        out = io.StringIO()
        call_command("app_update", "publish", str(make_export(self.tmp)), "--commit", "abc1234", stdout=out)
        self.assertIn("Published to the test phones", out.getvalue())
        call_command("app_update", "promote", stdout=out)
        call_command("app_update", "status", stdout=out)
        self.assertIn("Everyone:", out.getvalue())
        with self.assertRaises(InvalidSignature):  # a manifest changed in transit fails the phone's check
            body = b'{"id":"x"}'
            sig = base64.b64decode(au.signature_header(body).split('sig="')[1].split('"')[0])
            self.cert.public_key().verify(sig, body + b" ", padding.PKCS1v15(), hashes.SHA256())
