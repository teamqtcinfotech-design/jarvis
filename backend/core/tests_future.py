"""The future, however far (Nov 2026): a meeting next December is "coming up", not "no date yet"; "meri kitni
meetings schedule hain" counts everything ahead; a named month or year is that period; the past is the ordinary
list for that period. Messages: who wrote, which ones were replied to, which are still unread."""
from datetime import time, timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from core import services
from core.models import Kind, Message
from core.tests import BaseCase, _cmd


class FarFutureTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.today = services.tu.today_local()
        far = self.today + timedelta(days=45)        # well past "next 7 days"
        soon = self.today + timedelta(days=2)
        self.aman = services.create_private_task(self.md, {"title": "Meeting with Aman", "kind": Kind.MEETING, "due_date": far,
                                                           "due_time": time(11, 0), "duration_minutes": 30})
        self.soon = services.create_private_task(self.md, {"title": "Dentist", "kind": Kind.TASK, "due_date": soon})
        self.undated = services.create_private_task(self.md, {"title": "Read the farm report", "kind": Kind.TASK})
        self.far, self.soon_day = far, soon

    def ask(self, text, **fields):
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", **fields)):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_a_meeting_weeks_away_is_coming_up_not_undated(self):
        dash = services.dashboard(self.md)
        self.assertIn("Meeting with Aman", [i["title"] for i in dash["upcoming"]])
        self.assertNotIn("Meeting with Aman", [i["title"] for i in dash["anytime"]])
        self.assertEqual(dash["anytime"], [])  # the undated item given today is today's work; nothing dated is ever "no date yet"
        self.assertIn("Read the farm report", [i["title"] for i in dash["today"]])
        self.assertEqual([i["title"] for i in dash["upcoming"]][:2], ["Dentist", "Meeting with Aman"])  # by date

    def test_future_meetings_and_counts_however_far(self):
        # "Show me my future meetings": only meetings, only ahead; the fixture's own meeting (in an hour) is today's, not ahead.
        d = self.ask("show me my future meetings", view_kind="upcoming", view_types=["MEETING"])
        view = d["view"]
        self.assertEqual([r["title"] for r in view["rows"]], ["Meeting with Aman"])
        self.assertEqual(view["title"], "Coming up · meetings")
        self.assertIn("1 meeting coming up; first Meeting with Aman", d["spoken_reply"])
        self.assertIn("11 AM", d["spoken_reply"])
        stats = {s["label"]: s["value"] for s in view["stats"]}
        self.assertEqual((stats["Meetings"], stats["Tasks"], stats["This week"], stats["Later"]), (1, 0, 0, 1))
        # Everything ahead: the task in two days and the meeting in six weeks, said by week and month.
        d = self.ask("aage kya hai", view_kind="upcoming")
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Dentist", "Meeting with Aman"])
        self.assertIn("2 items coming up (1 meeting, 1 task); first Dentist", d["spoken_reply"])
        self.assertIn("1 this week, 1 in " + self.far.strftime("%B"), d["spoken_reply"])
        self.assertNotIn("7 days", d["spoken_reply"])

    def test_a_named_month_and_the_past(self):
        month_start = self.far.replace(day=1)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        d = self.ask("us mahine ki meetings", view_kind="upcoming", view_types=["MEETING"], view_from=month_start.isoformat(), view_to=month_end.isoformat())
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Meeting with Aman"])
        self.assertIn(self.far.strftime("%B"), d["view"]["title"])
        self.assertIn("1 meeting coming up " + self.far.strftime("%B"), d["spoken_reply"])
        # A month with nothing in it says so, for meetings.
        later_start = (month_end + timedelta(days=1))
        later_end = (later_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        d = self.ask("uske agle mahine", view_kind="upcoming", view_types=["MEETING"], view_from=later_start.isoformat(), view_to=later_end.isoformat())
        self.assertEqual(d["view"]["rows"], [])
        self.assertIn("no meetings are coming up", d["spoken_reply"])
        # The past is never "coming up": last month is the ordinary list for that period (what got done, what slipped).
        done = services.create_private_task(self.md, {"title": "Paid the electricity bill", "kind": Kind.TASK, "due_date": self.today - timedelta(days=20)})
        r = self.md_client.post(f"/api/v1/private-tasks/{done.id}/complete/", {"expected_version": done.version}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        last_end = self.today.replace(day=1) - timedelta(days=1)
        last_start = last_end.replace(day=1)
        d = self.ask("last month ke tasks dikhao", view_kind="upcoming", view_from=last_start.isoformat(), view_to=last_end.isoformat())
        self.assertNotEqual(d["view"]["kind"], "upcoming")
        self.assertNotIn("coming up", d["spoken_reply"])
        if last_start <= self.today - timedelta(days=20) <= last_end:
            self.assertIn("Paid the electricity bill", [r["title"] for r in d["view"]["rows"]])


class WhichOneTests(BaseCase):
    """Two items with one name: the question names their days, "dono" takes both, and the same meeting at the same
    moment is never saved twice."""

    def setUp(self):
        super().setUp()
        self.today = services.tu.today_local()
        day1, day2 = self.today + timedelta(days=3), self.today + timedelta(days=24)
        self.first = services.create_private_task(self.md, {"title": "Meeting with Aman", "kind": Kind.MEETING, "due_date": day1,
                                                            "due_time": time(11, 0), "duration_minutes": 30})
        self.second = services.create_private_task(self.md, {"title": "Meeting with Aman", "kind": Kind.MEETING, "due_date": day2,
                                                             "due_time": time(11, 0), "duration_minutes": 30})
        self.day1, self.day2 = day1, day2

    def interpret(self, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_question_names_the_days_and_dono_deletes_both(self):
        d = self.interpret("Meeting with Aman delete karo", _cmd(intent="delete_task", title="Meeting with Aman", spoken_reply="Ji."))
        self.assertTrue(d["needs_clarification"], d["spoken_reply"])
        self.assertEqual({c["id"] for c in d["target_candidates"]}, {str(self.first.id), str(self.second.id)})
        self.assertIn(f"Meeting with Aman ({services.tu.fmt_day(self.day1)} at 11 AM)", d["spoken_reply"])
        self.assertIn(f"Meeting with Aman ({services.tu.fmt_day(self.day2)} at 11 AM)", d["spoken_reply"])
        self.assertIn("Or all of them?", d["spoken_reply"])
        # "Dono delete kar do": the model made nothing of it (no target_refs); the server takes every one offered and
        # cancels them at once (no "shall I?"); "undo" brings them back.
        choices = {"intent": "delete_task", "items": [{"id": c["id"], "title": c["title"]} for c in d["target_candidates"]]}
        d = self.interpret("dono delete kar do", _cmd(intent="unknown", spoken_reply="Samajh nahi aaya."), choices=choices,
                           awaiting="JARVIS asked which one to delete")
        self.assertFalse(d.get("needs_clarification"), d["spoken_reply"])
        self.assertTrue(d["booked"] and d["card_done"] and d["undo"], d)
        self.assertEqual({t["id"] for t in d["targets"]}, {str(self.first.id), str(self.second.id)})
        self.assertIn("Deleted 2, Sir", d["spoken_reply"])
        self.assertIn(services.tu.fmt_day(self.day2), d["spoken_reply"])
        self.assertEqual({t.status for t in services.private_qs(self.md).filter(id__in=[self.first.id, self.second.id])}, {"CANCELLED"})
        d = self.interpret("undo", _cmd(intent="undo", spoken_reply="Ji."))
        self.assertIn("Undone", d["spoken_reply"])
        self.assertEqual({t.status for t in services.private_qs(self.md).filter(id__in=[self.first.id, self.second.id])}, {"OPEN"})

    def test_delete_it_means_the_one_thing_on_the_screen(self):
        # "December ki meetings dikhao" showed one meeting; "delete it" is that one, at once, even though the model
        # named nothing and the same title exists on another day too.
        screen = {"kind": "upcoming", "title": "Coming up · meetings", "items": [{"id": str(self.second.id), "title": "Meeting with Aman"}],
                  "filters": {"kind": "upcoming", "types": ["MEETING"]}}
        d = self.interpret("isko hata do", _cmd(intent="delete_task", title="", spoken_reply="Ji."), screen=screen)
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIn(f"Deleted Meeting with Aman ({services.tu.fmt_day(self.day2)} at 11 AM), Sir", d["spoken_reply"])
        self.assertEqual(services.private_qs(self.md).get(id=self.second.id).status, "CANCELLED")
        self.assertEqual(services.private_qs(self.md).get(id=self.first.id).status, "OPEN")
        # Two on the card and nothing named: the question lists what is on the card; "dono" takes both.
        third = services.create_private_task(self.md, {"title": "Call the bank", "kind": Kind.TASK, "due_date": self.day1})
        screen = {"kind": "today", "title": "Today", "items": [{"id": str(self.first.id), "title": "Meeting with Aman"}, {"id": str(third.id), "title": "Call the bank"}]}
        d = self.interpret("delete it", _cmd(intent="delete_task", title="", spoken_reply="Ji."), screen=screen)
        self.assertTrue(d["needs_clarification"], d["spoken_reply"])
        self.assertEqual({c["id"] for c in d["target_candidates"]}, {str(self.first.id), str(third.id)})
        d = self.interpret("dono hata do", _cmd(intent="delete_task", title="", spoken_reply="Ji."), screen=screen)
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertEqual({services.private_qs(self.md).get(id=i).status for i in (self.first.id, third.id)}, {"CANCELLED"})

    def test_the_same_meeting_at_the_same_moment_is_not_saved_twice(self):
        # Days later, the same words again: not a second entry, whatever the model drafted.
        d = self.interpret("Aman ke saath meeting rakho", _cmd(intent="create_task", kind="MEETING", title="Meeting with Aman",
                                                            due_date=self.day2.isoformat(), due_time="11:00", spoken_reply="Ji."))
        self.assertTrue(d.get("booked"), d["spoken_reply"])
        self.assertIn("already booked", d["spoken_reply"])
        self.assertEqual(sum(1 for t in services.private_qs(self.md) if t.title == "Meeting with Aman"), 2)
        # A different person at the same time is a clash to settle, never "already booked", and never a "twin".
        from core.assistant import _similar_item
        self.assertEqual(_similar_item(self.md, "Meeting with Akash", self.day2, kinds=(Kind.MEETING, Kind.CALL)), "")
        self.assertEqual(_similar_item(self.md, "Aman ke saath meeting", self.day2, kinds=(Kind.MEETING, Kind.CALL)), "Meeting with Aman")


class MessageRepliesTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.nikki_client = APIClient()
        self.nikki_client.force_authenticate(self.nikki)

    def ask(self, text, **fields):
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", **fields)):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_who_wrote_what_was_replied_to_and_what_is_still_unread(self):
        first = services.send_message_to(self.pa, [self.md], "Sir, vendor ka payment ho gaya")[0]
        Message.objects.filter(id=first.ref_id).update(sent_at=timezone.now() - timedelta(hours=3))
        services.send_message_to(self.md, [self.pa], "Theek hai, receipt bhej do")          # a reply to the PA
        services.send_message_to(self.pa, [self.md], "Receipt bhej di")                    # after the reply: not replied yet
        services.send_message_to(self.nikki, [self.md], "GST file ho gayi")                 # Nikki, never answered
        d = self.ask("kis kis ne message bheja aur kitne ka reply nahi diya", view_kind="messages", view_filter="inbox")
        view = d["view"]
        by_title = {r["title"]: r["status"] for r in view["rows"]}
        self.assertEqual(by_title["Sir, vendor ka payment ho gaya"], "Replied")
        self.assertEqual(by_title["Receipt bhej di"], "New")
        self.assertEqual(by_title["GST file ho gayi"], "New")
        senders = {s["who"]: s for s in view["senders"]}
        self.assertEqual((senders["Priya"]["count"], senders["Priya"]["unreplied"]), (2, 1))
        self.assertEqual((senders["Nikki"]["count"], senders["Nikki"]["unreplied"]), (1, 1))
        self.assertIn("3 messages: 2 from Priya (1 not replied), 1 from Nikki (1 not replied)", d["spoken_reply"])
        self.assertIn("2 not replied to", d["spoken_reply"])
        # "Jinka reply nahi diya": only those; "jo dekhe nahi": only the unread ones.
        d = self.ask("jinka reply nahi diya", view_kind="messages", view_filter="unreplied")
        self.assertEqual({r["title"] for r in d["view"]["rows"]}, {"Receipt bhej di", "GST file ho gayi"})
        self.assertIn("2 messages without a reply from you", d["spoken_reply"])
        self.assertEqual(d["view"]["title"], "Messages not replied to")
        d = self.ask("jo maine dekhe nahi", view_kind="messages", view_filter="unread")
        self.assertEqual(len(d["view"]["rows"]), 3)
        self.assertIn("3 messages not seen yet", d["spoken_reply"])
        # Nikki's own question: her one message to Sir, not answered; nothing of the PA's.
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", view_kind="messages", view_filter="sent")):
            r = self.nikki_client.post("/api/v1/assistant/interpret/", {"text": "maine kya bheja"}, format="json")
        self.assertEqual([x["title"] for x in r.json()["view"]["rows"]], ["GST file ho gayi"])
