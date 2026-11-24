"""Notifications by voice (Nov 2026): "abhi 10 minute mein kya aaya", "sirf messages", "jo maine dekh liye",
"Mansi ne kya bheja", "inme se kitne meetings ke liye", "show me only what I approved" — one card, sorted into piles,
narrowed as asked, with the counts said from the real numbers."""
from datetime import time, timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from core import datacards, livevoice, services
from core.models import Alert, Kind, Priority, SharedTask, Status, User
from core.tests import BaseCase, _cmd, _workday


class NotificationsCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.nikki_client = APIClient()
        self.nikki_client.force_authenticate(self.nikki)
        self.mansi = User.objects.get(username="manager")  # the base case's manager: "Mansi"
        Alert.objects.all().delete()

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def fill(self):
        """What reaches Sir: a task and a meeting from Nikki, a message from Mansi, an approval from Nikki, and one
        old task from two hours ago."""
        day = _workday()
        old = services.create_person_task(self.nikki, self.md, {"title": "Old papers", "kind": Kind.TASK}, compose_note=False)
        Alert.objects.filter(ref_id=str(old.id)).update(created_at=timezone.now() - timedelta(hours=2))
        task = services.create_person_task(self.nikki, self.md, {"title": "Sign the GST file", "kind": Kind.TASK, "priority": Priority.URGENT},
                                           compose_note=False)
        meeting = services.create_person_task(self.nikki, self.md, {"title": "Sharmaji meeting", "kind": Kind.MEETING, "due_date": day,
                                                                    "due_time": time(16, 0), "duration_minutes": 30}, compose_note=False)
        services.send_message_to(self.mansi, [self.md], "Vendor ka call aaya tha")
        approval = services.create_person_task(self.nikki, self.md, {"title": "Vendor Advance", "kind": Kind.APPROVAL, "amount": 150000},
                                               compose_note=False)
        return old, task, meeting, approval

    def stats(self, view) -> dict:
        return {s["label"]: s["value"] for s in view["stats"]}


class WindowTests(NotificationsCase):
    def test_the_last_ten_minutes_leave_out_what_came_two_hours_ago(self):
        old, task, meeting, approval = self.fill()
        view = datacards.build(self.md, "alerts", params={"minutes": 10})
        titles = [r["title"] for r in view["rows"]]
        self.assertNotIn("Old papers", titles)
        self.assertEqual(set(titles), {"Sign the GST file", "Sharmaji meeting", "Vendor ka call aaya tha", "Vendor Advance"})
        self.assertEqual(view["subtitle"], "4 in the last 10 minutes · 4 still waiting for you")
        self.assertTrue(view["summary"].startswith("Sir, in the last 10 minutes: "), view["summary"])
        # Counts by pile only, never a title or a message (his rule): the card below carries them.
        self.assertEqual(view["summary"], "Sir, in the last 10 minutes: 1 approval, 1 meeting, 1 task, 1 message. 4 still waiting for you.")
        self.assertNotIn("Vendor Advance", view["summary"])
        self.assertIn("Vendor Advance", [r["title"] for r in view["rows"]])
        self.assertIn("पिछले 10 मिनट में", view["summary_hi"])
        # The piles are the tiles; the request keeps the span, so "inme se sirf meetings" starts from the same window.
        self.assertEqual(self.stats(view), {"Approvals": 1, "Meetings": 1, "Tasks": 1, "Messages": 1, "Waiting for you": 4})
        self.assertEqual(view["request"], {"kind": "alerts", "minutes": 10})
        # Today (nothing said) includes the old one too; a day with nothing says so.
        view = datacards.build(self.md, "alerts", params={})
        self.assertEqual(len(view["rows"]), 5)
        self.assertIn("5 today", view["subtitle"])
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        view = datacards.build(self.md, "alerts", params={"from": yesterday, "to": yesterday})
        self.assertEqual(view["rows"], [])
        self.assertEqual(view["summary"], "Sir, nothing came yesterday.")

    def test_by_voice_the_span_rides_in_due_in_minutes_and_the_card_comes_back(self):
        self.fill()
        d = self.interpret(self.md_client, "abhi 10 minute mein jitne messages ya notification aaye wo batao", _cmd(
            intent="query", view_kind="alerts", due_in_minutes=10))
        self.assertEqual(d["view"]["kind"], "alerts")
        self.assertEqual(len(d["view"]["rows"]), 4)
        self.assertIn("in the last 10 minutes", d["spoken_reply"])
        self.assertNotIn("Old papers", d["spoken_reply"])
        # "Inme se kitne meetings ke liye hain": the model drops the span; the server keeps the ten minutes of the list
        # just answered (from the card on screen, or the list remembered), and the count is for that window only.
        d = self.interpret(self.md_client, "inme se kitne meetings ke liye hain", _cmd(intent="query", view_kind="alerts", view_types=["MEETING"]),
                           screen={"kind": "alerts", "title": "Notifications", "items": [], "filters": {"kind": "alerts", "minutes": 10}})
        self.assertEqual(d["view"]["request"], {"kind": "alerts", "types": ["MEETING"], "minutes": 10})
        self.assertEqual(d["spoken_reply"], "Sir, in the last 10 minutes: 1 meeting. 1 still waiting for you.")  # counts only, never a title
        d = self.interpret(self.md_client, "inme se Mansi ki taraf se kitne hain", _cmd(intent="query", view_kind="alerts", view_who="Mansi"))
        self.assertEqual(d["view"]["request"]["minutes"], 10)  # from the list remembered a moment ago
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Vendor ka call aaya tha"])
        # A fresh question about a day is not narrowed to the ten minutes.
        d = self.interpret(self.md_client, "aaj ke saare notifications", _cmd(intent="query", view_kind="alerts", view_from=timezone.localdate().isoformat(),
                                                                             view_to=timezone.localdate().isoformat()))
        self.assertEqual(len(d["view"]["rows"]), 5)
        # The live line's "show" form carries the span the same way.
        cmd = livevoice.cmd_for(self.md, "show", {"what": "alerts", "minutes": 10, "types": ["MESSAGE", "MEETING"], "filter": "unseen", "said": "s"})
        self.assertEqual((cmd["intent"], cmd["view_kind"], cmd["due_in_minutes"], cmd["view_types"], cmd["view_filter"]),
                         ("query", "alerts", 10, ["MESSAGE", "MEETING"], "unseen"))
        # Without the model, the words alone find the card.
        self.assertEqual(datacards.guess_kind("abhi kya kya notification aaya hai"), "alerts")
        self.assertEqual(datacards.guess_kind("show me only what I approved"), "approvals")


class PilesTests(NotificationsCase):
    def test_one_pile_one_sender_one_state_one_priority(self):
        old, task, meeting, approval = self.fill()
        # "Sirf messages": one pile.
        view = datacards.build(self.md, "alerts", params={"types": ["MESSAGE"]})
        self.assertEqual([r["title"] for r in view["rows"]], ["Vendor ka call aaya tha"])
        self.assertEqual(view["title"], "Notifications · messages")
        self.assertEqual(view["summary"], "Sir, today: 1 message. 1 still waiting for you.")  # counts only, never the message itself
        # "Inme se meetings aur approvals": two piles, counted apart.
        view = datacards.build(self.md, "alerts", params={"types": ["MEETING", "APPROVAL"]})
        self.assertEqual(self.stats(view), {"Approvals": 1, "Meetings": 1, "Waiting for you": 2})
        # "Mansi ki taraf se": one sender; a stranger's name is nobody.
        view = datacards.build(self.md, "alerts", params={"who": "Mansi"})
        self.assertEqual([r["who"] for r in view["rows"]], ["Mansi"])
        self.assertEqual(view["title"], "Notifications · from Mansi")
        self.assertEqual(datacards.build(self.md, "alerts", params={"who": "Zubin"})["rows"], [])
        empty = datacards.build(self.md, "alerts", params={"who": "Mansi", "types": ["MEETING"], "minutes": 10})
        self.assertEqual(empty["summary"], "Sir, no meetings from Mansi came in the last 10 minutes.")
        self.assertEqual(empty["summary_hi"], "सर, पिछले 10 मिनट में Mansi की तरफ़ से कोई मीटिंग नहीं आई।")
        # "Jo maine dekh liye" / "unseen wale": Sir taps Seen on the GST task.
        gst = Alert.objects.get(recipient=self.md, ref_id=str(task.id))
        r = self.md_client.post(f"/api/v1/alerts/{gst.id}/seen/", HTTP_IDEMPOTENCY_KEY="s1")
        self.assertIn(r.status_code, (200, 201), r.content)
        seen = datacards.build(self.md, "alerts", params={"filter": "seen"})
        self.assertEqual([r["title"] for r in seen["rows"]], ["Sign the GST file"])
        self.assertEqual(seen["rows"][0]["status"], "Seen")
        unseen = datacards.build(self.md, "alerts", params={"filter": "unseen"})
        self.assertNotIn("Sign the GST file", [r["title"] for r in unseen["rows"]])
        self.assertEqual(len(unseen["rows"]), 3)  # the message, the meeting, the old task; the approval is "waiting", not unseen
        # "Urgent kitne": priority.
        view = datacards.build(self.md, "alerts", params={"priority": "urgent"})
        self.assertEqual([r["title"] for r in view["rows"]], ["Sign the GST file"])
        # Nikki asks the same question and sees only what reached HER (the "done" news and such), never Sir's feed.
        self.assertNotIn("Vendor ka call aaya tha", [r["title"] for r in datacards.build(self.nikki, "alerts", params={})["rows"]])

    def test_the_request_endpoint_takes_the_same_filters(self):
        self.fill()
        r = self.md_client.get("/api/v1/views/alerts/?minutes=10&types=MEETING,APPROVAL")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stats(r.json()), {"Approvals": 1, "Meetings": 1, "Waiting for you": 2})


class ApprovedOnlyTests(NotificationsCase):
    def test_only_what_i_approved_is_not_what_is_waiting(self):
        old, task, meeting, approval = self.fill()
        second = services.create_person_task(self.nikki, self.md, {"title": "Office Chairs", "kind": Kind.APPROVAL, "amount": 20000}, compose_note=False)
        services.create_person_task(self.nikki, self.md, {"title": "Printer Toner", "kind": Kind.APPROVAL, "amount": 10000}, compose_note=False)
        services.approve_task(self.md, approval.id, True, note="")
        services.approve_task(self.md, second.id, False, note="No budget")
        d = self.interpret(self.md_client, "show me only what I approved", _cmd(intent="query", view_kind="approvals", view_filter="approved"))
        view = d["view"]
        self.assertEqual([r["title"] for r in view["rows"]], ["Vendor Advance"])
        self.assertEqual(view["title"], "Approvals · approved")
        self.assertEqual(self.stats(view), {"Approved": 1, "Approved ₹": "₹1,50,000"})
        self.assertEqual(d["spoken_reply"], "Sir, 1 approval approved in the last 30 days: Vendor Advance (₹1,50,000).")
        self.assertNotIn("waiting", d["spoken_reply"])
        # Declined, and what still waits.
        view = datacards.build(self.md, "approvals", params={"filter": "declined"})
        self.assertEqual([r["title"] for r in view["rows"]], ["Office Chairs"])
        view = datacards.build(self.md, "approvals", params={"filter": "waiting"})
        self.assertEqual([r["title"] for r in view["rows"]], ["Printer Toner"])
        self.assertEqual(view["summary"], "Sir, 1 approval waiting for you in the last 30 days: Printer Toner (₹10,000).")
        # In the notifications card the approval pile says the state of each.
        view = datacards.build(self.md, "alerts", params={"types": ["APPROVAL"]})
        states = {r["title"]: r["status"] for r in view["rows"]}
        self.assertEqual(states, {"Vendor Advance": "Approved", "Office Chairs": "Declined", "Printer Toner": "Waiting"})
        self.assertEqual(self.stats(view)["Waiting for you"], 1)
        approved = datacards.build(self.md, "alerts", params={"types": ["APPROVAL"], "filter": "approved"})
        self.assertEqual([r["title"] for r in approved["rows"]], ["Vendor Advance"])
        self.assertEqual(SharedTask.objects.get(id=approval.id).status, Status.COMPLETED)
