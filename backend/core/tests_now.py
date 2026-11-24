""""Abhi" (Nov 2026): on a task or a message it means right away (never "you are not free", never "already
passed"); on a meeting it means this very moment; "Nikki ko bolo Sharmaji wali meeting abhi arrange karwa de" asks
Nikki and moves the meeting only when she confirms."""
import json
from datetime import timedelta
from unittest import mock

from rest_framework.test import APIClient

from core import livevoice, services
from core import timeutils as tu
from core.models import Alert, Kind, PrivateTask, SharedTask
from core.tests import BaseCase, _cmd


class NowCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.nikki_client = APIClient()
        self.nikki_client.force_authenticate(self.nikki)
        # It is "12:10": Aman's meeting ran from 70 minutes ago to 10 minutes ago; Sharmaji's is in 50 minutes, for an hour.
        self.now = tu.now_local().replace(second=0, microsecond=0)
        self.aman = self.meeting("Meeting with Aman", self.now - timedelta(minutes=70))
        self.sharma = self.meeting("Meeting with Sharmaji", self.now + timedelta(minutes=50))

    def meeting(self, title, start):
        return services.create_private_task(self.md, {"title": title, "kind": Kind.MEETING, "due_date": start.date(),
                                                      "due_time": start.time(), "duration_minutes": 60})

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def hhmm(self, dt):
        return tu.to_local(dt).strftime("%H:%M")


class AbhiOnWorkTests(NowCase):
    def test_work_said_abhi_is_given_at_once_without_a_clock_time(self):
        # The model wrote the current clock time for "abhi": it is not a deadline, and nothing has "already passed".
        d = self.interpret(self.md_client, "Nikki ko bolo papers abhi mere office mein bhej de", _cmd(
            intent="assign_to_person", staff_name="Nikki", recipients=["Nikki"], title="Send the papers to Sir's office",
            due_date=self.now.date().isoformat(), due_time=self.hhmm(self.now), spoken_reply="Ji Sir."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertNotIn("passed", d["spoken_reply"])
        self.assertNotIn("busy", d["spoken_reply"].lower())
        task = SharedTask.objects.get(title="Send the papers to Sir's office")
        self.assertEqual((task.assignee_user, task.priority, task.due_at, task.due_date), (self.nikki, "HIGH", None, None))

    def test_a_quick_ask_as_a_message_goes_now(self):
        d = self.interpret(self.md_client, "Nikki ko bolo abhi mere office aaye", _cmd(
            intent="send_alert", staff_name="Nikki", recipients=["Nikki"], title="Come to Sir's office now",
            notes="Sir ke office mein abhi aa jaiye.", due_date=self.now.date().isoformat(), due_time=self.hhmm(self.now)))
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIn("gone to Nikki", d["spoken_reply"])
        alert = Alert.objects.get(recipient=self.nikki, ref_type="message")
        self.assertIsNone(alert.snoozed_until)  # not held for later: now
        self.assertTrue(alert.ring)

    def test_my_own_task_during_a_meeting_is_saved_without_a_diary_check(self):
        # Aman's slot, Sharmaji's slot: neither matters for a task on my list.
        d = self.interpret(self.md_client, "mujhe kal quotation bhejni hai, meri list mein add karo", _cmd(
            intent="create_task", title="Send the quotation", due_date=(self.now.date() + timedelta(days=1)).isoformat(),
            spoken_reply="Ji Sir."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIsNone(d.get("schedule"))
        self.assertTrue(PrivateTask.objects.filter(owner=self.md, due_date=self.now.date() + timedelta(days=1)).exists())


class MoveTheMeetingNowTests(NowCase):
    def test_sharmaji_wali_meeting_abhi_kar_do_moves_that_meeting_to_this_minute(self):
        d = self.interpret(self.md_client, "Sharmaji wali meeting abhi kar do", _cmd(
            intent="change_task", target_refs=[str(self.sharma.id)], changed_fields=[], spoken_reply="Ji Sir."))
        self.assertEqual(d["action"], "change_task")
        self.assertEqual(d["changes"], {"due_date": self.now.date().isoformat(), "due_time": self.hhmm(self.now)})
        self.assertFalse(d["needs_clarification"], d["spoken_reply"])
        self.assertEqual(d["schedule"]["clashes"], [])          # Aman's slot has elapsed; its own old slot does not count
        self.assertFalse(d["schedule"]["question"])
        # The yes: the app applies the change to the same meeting.
        r = self.md_client.patch(f"/api/v1/private-tasks/{self.sharma.id}/", {"expected_version": self.sharma.version, **d["changes"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.sharma.refresh_from_db()
        self.assertEqual(self.hhmm(self.sharma.due_at), self.hhmm(self.now))
        self.assertEqual(self.sharma.scheduled_end - self.sharma.scheduled_start, timedelta(minutes=60))  # the hour is kept
        self.aman.refresh_from_db()
        self.assertEqual(self.aman.due_at, self.now - timedelta(minutes=70))                                 # Aman untouched
        # Its reminder is the new moment's; the old 1 PM one is gone.
        keys = json.dumps(self.md_client.get("/api/v1/reminders/").json())
        self.assertNotIn(f"t{int((self.now + timedelta(minutes=50)).timestamp())}", keys)

    def test_two_sharmaji_meetings_ask_which_one(self):
        self.meeting("Meeting with Sharmaji – GST", self.now + timedelta(hours=3))
        d = self.interpret(self.md_client, "Sharmaji wali meeting abhi kar do", _cmd(intent="change_task", title="Sharmaji", changed_fields=[]))
        self.assertTrue(d["needs_clarification"])
        self.assertEqual(len(d["target_candidates"]), 2)

    def test_the_live_form_carries_now_and_the_person_asked(self):
        cmd = livevoice.cmd_for(self.md, "change", {"which_id": str(self.sharma.id), "which_words": "Sharmaji wali meeting", "when_words": "",
                                                    "now": True, "ask_person": "Nikki", "said": "Nikki ko bolo Sharmaji wali meeting arrange karwa de"})
        self.assertEqual((cmd["intent"], cmd["recipients"], cmd["due_in_minutes"]), ("change_task", ["Nikki"], 1))
        self.assertIn("abhi", cmd["_said"])
        self.assertEqual(cmd["changed_fields"], ["due_date", "due_time"])


class AskNikkiToArrangeTests(NowCase):
    def ask(self, text="Nikki ko bolo Sharmaji wali meeting abhi arrange karwa de", meeting=None):
        return self.interpret(self.md_client, text, _cmd(
            intent="change_task", target_refs=[str((meeting or self.sharma).id)], staff_name="Nikki", recipients=["Nikki"],
            changed_fields=[], spoken_reply="Ji Sir."))

    def test_nikki_is_asked_and_the_meeting_waits_for_her_confirmation(self):
        Alert.objects.all().delete()
        d = self.ask()
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIn("asked Nikki to move your Meeting with Sharmaji", d["spoken_reply"])
        self.assertIn(f"to today at {tu.fmt_time(self.now)}", d["spoken_reply"])
        self.assertIn("stays as it is until they confirm", d["spoken_reply"])  # Nikki's gender was not set: neutral
        ask = SharedTask.objects.get(assignee_user=self.nikki)
        self.assertEqual((str(ask.about_id), ask.about_scope, ask.arrange["due_time"], ask.arrange["applied"]), (str(self.sharma.id), "private", self.hhmm(self.now), False))
        self.assertIn("Move Sir's Meeting with Sharmaji to today at", ask.title)
        ring = Alert.objects.get(recipient=self.nikki, ref_type="shared_task", ref_id=str(ask.id))
        self.assertTrue(ring.ring)
        self.assertIn("Sir asks you to move his Meeting with Sharmaji from today at", ring.spoken_text)
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.due_at, self.now + timedelta(minutes=50))  # still at 1 PM
        # Nikki sees which meeting, its time now and the change asked for.
        item = self.nikki_client.get(f"/api/v1/shared-tasks/{ask.id}/").json()
        self.assertEqual(item["arrangement"]["about"]["title"], "Meeting with Sharmaji")
        self.assertIn("today at", item["arrangement"]["about"]["when"])
        self.assertIn(tu.fmt_time(self.now), item["arrangement"]["to"])
        # The same command again within moments (heard twice): asked once, and nothing is said a second time.
        d2 = self.ask()
        self.assertEqual(SharedTask.objects.filter(assignee_user=self.nikki).count(), 1)
        self.assertEqual(d2["spoken_reply"], "", d2)
        with mock.patch("core.assistant.ECHO_SECONDS", -1):  # said again after a while: told it is already asked
            d3 = self.ask()
        self.assertEqual(SharedTask.objects.filter(assignee_user=self.nikki).count(), 1)
        self.assertIn("already", d3["spoken_reply"])
        # Nikki confirms: the meeting moves, keeping its hour; Sir hears it from her; a second confirm changes nothing.
        Alert.objects.filter(recipient=self.md).delete()
        r = self.nikki_client.post(f"/api/v1/pa-tasks/{ask.id}/status/", {"status": "COMPLETED", "expected_version": ask.version}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.sharma.refresh_from_db()
        self.assertEqual(self.hhmm(self.sharma.due_at), self.hhmm(self.now))
        self.assertEqual(self.sharma.scheduled_end - self.sharma.scheduled_start, timedelta(minutes=60))
        told = Alert.objects.get(recipient=self.md)
        self.assertIn(f"Nikki has moved your Meeting with Sharmaji to today at {tu.fmt_time(self.now)}", told.spoken_text)
        ask.refresh_from_db()
        self.assertTrue(ask.arrange["applied"])
        r = self.nikki_client.post(f"/api/v1/pa-tasks/{ask.id}/status/", {"status": "COMPLETED", "expected_version": ask.version - 1}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Alert.objects.filter(recipient=self.md).count(), 1)

    def test_a_clash_at_the_new_time_is_asked_first(self):
        # Something of Sir's already sits at "now": JARVIS names it and offers the next free time; nothing is asked of Nikki yet.
        self.meeting("Review with Priya", self.now - timedelta(minutes=5))
        d = self.ask()
        self.assertFalse(d.get("booked"))
        self.assertIn("you already have Review with Priya", d["spoken_reply"])
        self.assertEqual(d["action"], "arrange_request")
        self.assertFalse(SharedTask.objects.filter(assignee_user=self.nikki).exists())

    def test_hold_from_nikki_leaves_the_meeting_and_tells_sir(self):
        self.ask()
        ask = SharedTask.objects.get(assignee_user=self.nikki)
        Alert.objects.filter(recipient=self.md).delete()
        r = self.nikki_client.post(f"/api/v1/pa-tasks/{ask.id}/status/", {"status": "BLOCKED", "expected_version": ask.version, "note": "Sharmaji is not reachable"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.sharma.refresh_from_db()
        self.assertEqual(self.sharma.due_at, self.now + timedelta(minutes=50))
        self.assertIn("on hold. Reason: Sharmaji is not reachable", Alert.objects.get(recipient=self.md).spoken_text)


class QuietRepliesPrefTests(NowCase):
    def test_silent_replies_in_a_meeting_is_a_setting(self):
        r = self.md_client.get("/api/v1/me/prefs/")
        self.assertTrue(r.json()["meetingQuietReplies"])
        r = self.md_client.patch("/api/v1/me/prefs/", {"meetingQuietReplies": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["meetingQuietReplies"])
        d = self.interpret(self.md_client, "meeting mein chup raho", _cmd(intent="set_setting", setting_key="meetingQuietReplies", setting_value="on"))
        self.assertEqual(d["setting_patch"], {"meetingQuietReplies": True})
        self.assertIn("silent replies in a meeting on", d["spoken_reply"])
