"""The walkie-talkie relay (core/walkie.py): lines, the floor, forwarding, waking, quiet, the log."""
import json

from asgiref.sync import async_to_sync
from django.test import override_settings
from rest_framework.test import APIClient

from core import services, walkie
from core.models import Alert, TalkSession, WalkiePresence
from core.tests import BaseCase


class FakeWs:
    """A phone's line, for the tests: everything sent to it is kept."""

    def __init__(self):
        self.text: list[dict] = []
        self.data: list[bytes] = []
        self.closed = False

    async def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self.data.append(bytes(payload))
        else:
            self.text.append(json.loads(payload))

    def last(self, kind: str) -> dict | None:
        return next((m for m in reversed(self.text) if m.get("t") == kind), None)

    def kinds(self) -> list[str]:
        return [m["t"] for m in self.text]


@override_settings(JARVIS_WALKIE_URL="ws://localhost:8001/ws/walkie/")
class WalkieTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.seema = services.team_add(self.md, {"name": "Seema", "title": "Social Media Manager", "username": "seema", "password": "seema1"})
        for u in (self.md, self.pa, self.nikki, self.seema):
            services.set_prefs(u, {"walkieEnabled": True})
        self.hub = walkie.Hub()
        self.pushed = []
        self.noted = []

        async def pusher(user, data):
            self.pushed.append((user.username, data))

        async def noter(user, caller, talk_id):
            self.noted.append((user.username, caller.username))
        self.hub.pusher, self.hub.noter = pusher, noter

    def line(self, user) -> tuple[walkie.Conn, FakeWs]:
        ws = FakeWs()
        conn = walkie.Conn(ws, user)
        async_to_sync(self.hub.join)(conn)
        return conn, ws

    def test_lines_presence_and_the_people_list(self):
        md, md_ws = self.line(self.md)
        ready = md_ws.last("ready")
        self.assertEqual(ready["you"], self.md.id)
        by_name = {p["name"]: p for p in ready["people"]}
        self.assertEqual(by_name["Nikki"]["status"], "reach")  # on, but her line is down
        nikki, nikki_ws = self.line(self.nikki)
        self.assertEqual({p["name"]: p["status"] for p in md_ws.last("presence")["people"]}["Nikki"], "available")
        services.set_prefs(self.seema, {"walkieEnabled": False})
        async_to_sync(self.hub.broadcast_presence)()
        self.assertEqual({p["name"]: p["status"] for p in md_ws.last("presence")["people"]}["Seema"], "off")
        self.assertTrue(WalkiePresence.objects.get(user=self.nikki).connected)
        async_to_sync(self.hub.leave)(nikki)
        self.assertFalse(WalkiePresence.objects.get(user=self.nikki).connected)
        self.assertEqual({p["name"]: p["status"] for p in md_ws.last("presence")["people"]}["Nikki"], "reach")
        del md

    def test_talk_floor_forwarding_reply_and_the_log(self):
        md, md_ws = self.line(self.md)
        nikki, nikki_ws = self.line(self.nikki)
        seema, seema_ws = self.line(self.seema)
        # Sir holds the button for Nikki and Seema: the sound flows to both, not back to him.
        async_to_sync(self.hub.talk)(md, {"t": "talk", "to": [self.nikki.id, self.seema.id], "codec": "amrwb"})
        floor = md_ws.last("floor")
        self.assertEqual(sorted(floor["listening"]), sorted([self.nikki.id, self.seema.id]))
        talk_id = floor["talk"]
        self.assertEqual(nikki_ws.last("incoming")["from"]["name"], "Sir")
        self.assertEqual(seema_ws.last("incoming")["codec"], "amrwb")
        async_to_sync(self.hub.audio)(md, b"\x01\x02\x03")
        async_to_sync(self.hub.audio)(md, b"\x04\x05")
        self.assertEqual(nikki_ws.data, [b"\x01\x02\x03", b"\x04\x05"])
        self.assertEqual(seema_ws.data, [b"\x01\x02\x03", b"\x04\x05"])
        self.assertEqual(md_ws.data, [])
        # Sound from someone without the floor is dropped.
        async_to_sync(self.hub.audio)(nikki, b"\x09")
        self.assertEqual(md_ws.data, [])
        # Nikki cannot talk over him; she waits.
        async_to_sync(self.hub.talk)(nikki, {"t": "talk", "talk": talk_id})
        self.assertEqual(nikki_ws.last("busy")["who"], "Sir")
        # He lets go: "done" to the others; Nikki replies to the same talk; Sir and Seema hear her.
        async_to_sync(self.hub.end)(md)
        self.assertEqual(nikki_ws.last("done")["talk"], talk_id)
        async_to_sync(self.hub.talk)(nikki, {"t": "talk", "talk": talk_id})
        self.assertEqual(nikki_ws.last("floor")["talk"], talk_id)
        async_to_sync(self.hub.audio)(nikki, b"\x07")
        self.assertEqual(md_ws.data, [b"\x07"])
        self.assertEqual(seema_ws.data[-1], b"\x07")
        self.assertEqual(md_ws.last("incoming")["from"]["name"], "Nikki")
        async_to_sync(self.hub.end)(nikki)
        session = TalkSession.objects.get(id=talk_id)
        self.assertEqual((session.started_by, sorted(session.members), session.turns), (self.md, sorted([self.md.id, self.nikki.id, self.seema.id]), 2))
        self.assertEqual(session.last_speaker, self.nikki)
        del seema

    def test_waking_a_sleeping_phone_quiet_people_and_who_may_start(self):
        md, md_ws = self.line(self.md)
        services.set_pref_quiet(self.seema, "walkieQuietUntil", (walkie._now() + walkie.timedelta(minutes=30)).isoformat())
        async_to_sync(self.hub.talk)(md, {"t": "talk", "to": [self.nikki.id, self.seema.id]})
        floor = md_ws.last("floor")
        self.assertEqual((floor["waking"], floor["quiet"], floor["listening"]), (["Nikki"], ["Seema"], []))
        self.assertEqual(self.pushed[0][0], "nikki")
        self.assertEqual(self.pushed[0][1]["type"], "walkie")
        self.assertEqual(self.noted, [("seema", "md")])
        # Nikki's phone wakes and connects while he is still talking: she gets "incoming" at once.
        nikki, nikki_ws = self.line(self.nikki)
        self.assertEqual(nikki_ws.last("incoming")["talk"], floor["talk"])
        async_to_sync(self.hub.audio)(md, b"\x01")
        self.assertEqual(nikki_ws.data, [b"\x01"])
        async_to_sync(self.hub.end)(md)
        # Only the MD may start, when he says so.
        services.set_prefs(self.md, {"walkieAnyoneStarts": False})
        async_to_sync(self.hub.talk)(nikki, {"t": "talk", "to": [self.md.id]})
        self.assertIn("only Sir can start", nikki_ws.last("refused")["why"])
        services.set_prefs(self.md, {"walkieAnyoneStarts": True})
        async_to_sync(self.hub.talk)(nikki, {"t": "talk", "to": [self.md.id]})
        self.assertIsNotNone(nikki_ws.last("floor"))
        async_to_sync(self.hub.end)(nikki)

    def test_quiet_by_the_line_and_by_voice_and_the_address_for_phones(self):
        nikki, nikki_ws = self.line(self.nikki)
        async_to_sync(self.hub.quiet)(nikki, 30)
        self.assertTrue(nikki_ws.last("quiet")["until"])
        self.assertTrue(services.get_prefs(self.nikki)["walkieQuietUntil"])
        async_to_sync(self.hub.quiet)(nikki, 0)
        self.assertEqual(services.get_prefs(self.nikki)["walkieQuietUntil"], "")
        # By voice: "walkie quiet for an hour" is a setting; "walkie band karo" too.
        services.set_prefs(self.nikki, {"walkieQuiet": "60"})
        self.assertTrue(services.get_prefs(self.nikki)["walkieQuietUntil"])
        services.set_prefs(self.nikki, {"walkieQuiet": "off"})
        self.assertEqual(services.get_prefs(self.nikki)["walkieQuietUntil"], "")
        self.assertEqual(self.md_client.get("/api/v1/me/").json()["walkie_url"], "ws://localhost:8001/ws/walkie/")
        # Live: nginx hides the port from Django, so the address comes from the public URL the phones already use.
        with self.settings(JARVIS_WALKIE_URL="", JARVIS_PUBLIC_URL="http://1.2.3.4:8088"):
            self.assertEqual(self.md_client.get("/api/v1/me/").json()["walkie_url"], "ws://1.2.3.4:8088/ws/walkie/")
        with self.settings(JARVIS_WALKIE_URL="", JARVIS_PUBLIC_URL=""):
            self.assertEqual(self.md_client.get("/api/v1/me/").json()["walkie_url"], "ws://testserver/ws/walkie/")
        # A switched-off person is not in a talk at all, and a busy one is "busy".
        services.set_prefs(self.seema, {"walkieEnabled": False})
        md, md_ws = self.line(self.md)
        async_to_sync(self.hub.talk)(md, {"t": "talk", "to": [self.seema.id, self.nikki.id]})
        self.assertEqual(md_ws.last("floor")["listening"], [self.nikki.id])
        pa, pa_ws = self.line(self.pa)
        async_to_sync(self.hub.talk)(pa, {"t": "talk", "to": [self.nikki.id]})
        self.assertEqual(pa_ws.last("busy")["who"], "Nikki")
        async_to_sync(self.hub.end)(md)
        del nikki, pa

    def test_a_dead_token_is_refused(self):
        self.assertIsNone(async_to_sync(walkie.user_for_token)("not-a-token"))
        client = APIClient()
        r = client.post("/api/v1/auth/login/", {"username": "nikki", "password": "nikki1"}, format="json")
        self.assertEqual(r.status_code, 200)
        user = async_to_sync(walkie.user_for_token)(r.json()["access"])
        self.assertEqual(user, self.nikki)
        self.assertFalse(Alert.objects.filter(ref_type="walkie").exists())
