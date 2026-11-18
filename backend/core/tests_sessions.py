"""Sign in by phone, one session per phone, sign a phone out from anywhere (Nov 2026)."""
import json
from unittest import mock

from django.test import override_settings
from rest_framework.test import APIClient

from core import services, sessions
from core.models import Alert, PushDevice, Session, User
from core.tests import BaseCase


def fake_proof(phone):
    """What Firebase's verify_id_token returns for a phone sign-in."""
    return {"phone_number": phone, "uid": "fb-" + phone, "firebase": {"sign_in_provider": "phone"}}


class PhoneNumbersTests(BaseCase):
    def test_numbers_are_kept_in_one_form_and_never_shared(self):
        for raw, want in (("98765 43210", "+919876543210"), ("098765-43210", "+919876543210"), ("+91 98765 43210", "+919876543210"),
                          ("+1 415 555 0100", "+14155550100"), ("12345", ""), ("", "")):
            self.assertEqual(sessions.normalize_phone(raw), want, raw)
        self.assertEqual(sessions.phone_label("+919876543210"), "+91 98765 43210")
        nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1", "phone": "98765 43210"})
        self.assertEqual(nikki.phone, "+919876543210")
        with self.assertRaises(services.ServiceError):
            services.team_add(self.md, {"name": "Seema", "title": "SMM", "username": "seema", "password": "seema1", "phone": "9876543210"})
        with self.assertRaises(services.ServiceError):
            services.team_update(self.md, self.pa.id, {"phone": "98765 43210"})
        services.team_update(self.md, self.pa.id, {"phone": "91234 56789"})
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.phone, "+919123456789")
        # The MD sees numbers on the Team page; a team member sees only their own.
        r = self.md_client.get("/api/v1/team/").json()["results"]
        self.assertIn("+91 98765 43210", json.dumps(r))
        r = self.pa_client.get("/api/v1/team/").json()["results"]
        self.assertNotIn("98765", json.dumps(r))
        self.assertIn("+91 91234 56789", json.dumps([p for p in r if p.get("you")]))


class PhoneSignInTests(BaseCase):
    def setUp(self):
        super().setUp()
        services.team_update(self.md, self.pa.id, {"phone": "91234 56789"})
        self.anon = APIClient()
        # Many sign-ins from one address in a few seconds: the sign-in rate limit would stop the tests, not people.
        patcher = mock.patch("rest_framework.throttling.ScopedRateThrottle.allow_request", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self, phone, device="Xiaomi M2101K7BI", device_id="dev-1"):
        with mock.patch("core.sessions.verify_firebase_phone", return_value=sessions.normalize_phone(phone)):
            return self.anon.post("/api/v1/auth/phone/", {"id_token": "proof", "device_name": device, "device_id": device_id}, format="json")

    def test_the_proof_is_checked_and_the_number_must_be_on_the_team(self):
        r = self.anon.post("/api/v1/auth/phone/", {"id_token": "", "device_name": "x"}, format="json")
        self.assertEqual(r.status_code, 422)
        with mock.patch("core.push.firebase_app", return_value=object()), \
                mock.patch("firebase_admin.auth.verify_id_token", side_effect=ValueError("expired")):
            r = self.anon.post("/api/v1/auth/phone/", {"id_token": "bad"}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "invalid_proof")
        with mock.patch("core.push.firebase_app", return_value=object()), \
                mock.patch("firebase_admin.auth.verify_id_token", return_value=fake_proof("+919999999999")):
            r = self.anon.post("/api/v1/auth/phone/", {"id_token": "ok"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "not_on_team")
        with mock.patch("core.push.firebase_app", return_value=object()), \
                mock.patch("firebase_admin.auth.verify_id_token", return_value={"phone_number": "+919123456789", "firebase": {"sign_in_provider": "google.com"}}):
            r = self.anon.post("/api/v1/auth/phone/", {"id_token": "ok"}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_signing_in_starts_a_session_the_tokens_carry_it_and_sign_out_ends_it_at_once(self):
        r = self.sign_in("91234 56789")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["user"]["username"], "pa")
        self.assertEqual(body["user"]["phone"], "+91 91234 56789")
        session = Session.objects.get(id=body["session_id"])
        self.assertEqual((session.user_id, session.name, session.how, session.device_id), (self.pa.id, "Xiaomi M2101K7BI", "phone", "dev-1"))
        phone = APIClient()
        phone.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        me = phone.get("/api/v1/me/").json()
        self.assertEqual(me["session_id"], str(session.id))
        # The phone registers its push token: it belongs to this session.
        phone.post("/api/v1/me/push-token/", {"token": "fcm-token-1"}, format="json")
        self.assertEqual(PushDevice.objects.get(token="fcm-token-1").session_id, session.id)
        # Sign out: the session ends, the push token goes, the same access token is refused from the next request.
        r = phone.post("/api/v1/auth/logout/", {"refresh": body["refresh"]}, format="json")
        self.assertEqual(r.status_code, 204)
        self.assertIsNotNone(Session.objects.get(id=session.id).revoked_at)
        self.assertFalse(PushDevice.objects.filter(token="fcm-token-1").exists())
        r = phone.get("/api/v1/me/")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "signed_out")
        r = self.anon.post("/api/v1/auth/refresh/", {"refresh": body["refresh"]}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_other_phones_are_told_and_can_sign_the_new_one_out(self):
        first = self.sign_in("91234 56789", device="Old Samsung", device_id="dev-old").json()
        self.assertFalse(Alert.objects.filter(recipient=self.pa, ref_type="session").exists())  # the only phone: nothing to tell
        second = self.sign_in("91234 56789", device="Xiaomi M2101K7BI", device_id="dev-new").json()
        note = Alert.objects.get(recipient=self.pa, ref_type="session")
        self.assertEqual(note.title, "Signed in on Xiaomi M2101K7BI")
        self.assertEqual(note.question["kind"], "signout")
        self.assertEqual(note.question["session"], second["session_id"])
        old = APIClient()
        old.credentials(HTTP_AUTHORIZATION=f"Bearer {first['access']}")
        r = old.get("/api/v1/auth/sessions/").json()
        self.assertEqual([s["name"] for s in r["results"]], ["Old Samsung", "Xiaomi M2101K7BI"])
        self.assertTrue(r["results"][0]["this_phone"])
        # "Not you? Sign it out" from the old phone: the new one is cut off at once.
        r = old.post(f"/api/v1/auth/sessions/{second['session_id']}/revoke/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["signed_out"])
        new = APIClient()
        new.credentials(HTTP_AUTHORIZATION=f"Bearer {second['access']}")
        self.assertEqual(new.get("/api/v1/me/").status_code, 401)
        self.assertEqual(old.get("/api/v1/me/").status_code, 200)
        # A phone cannot sign itself out this way (that is Sign out).
        self.assertEqual(old.post(f"/api/v1/auth/sessions/{first['session_id']}/revoke/", {}, format="json").status_code, 422)
        # Sign out everywhere else.
        third = self.sign_in("91234 56789", device="Tablet", device_id="dev-tab").json()
        r = old.post("/api/v1/auth/sessions/", {}, format="json")
        self.assertEqual(r.json()["signed_out"], 1)
        tab = APIClient()
        tab.credentials(HTTP_AUTHORIZATION=f"Bearer {third['access']}")
        self.assertEqual(tab.get("/api/v1/me/").status_code, 401)

    def test_the_same_phone_signing_in_again_replaces_its_session(self):
        a = self.sign_in("91234 56789", device_id="dev-1").json()
        b = self.sign_in("91234 56789", device_id="dev-1").json()
        self.assertEqual(Session.objects.get(id=a["session_id"]).revoked_why, "replaced")
        self.assertIsNone(Session.objects.get(id=b["session_id"]).revoked_at)
        self.assertFalse(Alert.objects.filter(recipient=self.pa, ref_type="session").exists())  # not a new phone

    def test_the_md_sees_and_signs_out_anyones_phone_but_nobody_else_can(self):
        pa_login = self.sign_in("91234 56789").json()
        services.team_update(self.md, self.md.id, {"phone": "90000 00000"})
        r = self.md_client.get(f"/api/v1/team/{self.pa.id}/sessions/").json()
        self.assertEqual(r["results"][0]["name"], "Xiaomi M2101K7BI")
        self.assertEqual(self.pa_client.get(f"/api/v1/team/{self.pa.id}/sessions/").status_code, 403)
        # Someone else on the team cannot touch the PA's session.
        nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        nc = APIClient()
        nc.force_authenticate(nikki)
        self.assertEqual(nc.post(f"/api/v1/auth/sessions/{pa_login['session_id']}/revoke/", {}, format="json").status_code, 404)
        # The MD can, and the person is told.
        r = self.md_client.post(f"/api/v1/auth/sessions/{pa_login['session_id']}/revoke/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Session.objects.get(id=pa_login["session_id"]).revoked_why, "md")
        self.assertTrue(Alert.objects.filter(recipient=self.pa, title__startswith="Signed out of").exists())

    def test_the_background_service_shares_the_phones_session(self):
        login = self.sign_in("91234 56789").json()
        phone = APIClient()
        phone.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']}")
        svc = phone.post("/api/v1/auth/device-token/", {}, format="json").json()
        from rest_framework_simplejwt.tokens import AccessToken
        self.assertEqual(AccessToken(svc["access"])["sid"], login["session_id"])
        sessions.revoke(Session.objects.get(id=login["session_id"]), self.pa, "other_phone")
        bg = APIClient()
        bg.credentials(HTTP_AUTHORIZATION=f"Bearer {svc['access']}")
        self.assertEqual(bg.get("/api/v1/reminders/").status_code, 401)

    def test_an_older_token_without_a_session_gets_one_on_refresh(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        old = RefreshToken.for_user(self.pa)  # as issued before sessions existed
        r = self.anon.post("/api/v1/auth/refresh/", {"refresh": str(old)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        sid = r.json()["session_id"]
        self.assertEqual(Session.objects.get(id=sid).name, "Earlier sign-in")
        phone = APIClient()
        phone.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['access']}")
        self.assertEqual(phone.get("/api/v1/me/").json()["session_id"], sid)

    @override_settings(JARVIS_PASSWORD_LOGIN=False)
    def test_password_sign_in_can_be_switched_off(self):
        r = self.anon.post("/api/v1/auth/login/", {"username": "pa", "password": "pa-pass-12345"}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertIn("phone number", json.dumps(r.json()))

    def test_password_sign_in_starts_a_session_too_while_it_is_on(self):
        r = self.anon.post("/api/v1/auth/login/", {"username": "pa", "password": "pa-pass-12345", "device_name": "Old app"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        s = Session.objects.get(id=r.json()["session_id"])
        self.assertEqual((s.how, s.name), ("password", "Old app"))

    def test_the_walkie_line_is_refused_for_a_signed_out_phone(self):
        from asgiref.sync import async_to_sync
        from core import walkie
        login = self.sign_in("91234 56789").json()
        self.assertEqual(async_to_sync(walkie.user_for_token)(login["access"]).id, self.pa.id)
        sessions.revoke(Session.objects.get(id=login["session_id"]), self.pa, "other_phone")
        self.assertIsNone(async_to_sync(walkie.user_for_token)(login["access"]))
