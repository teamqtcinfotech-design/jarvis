"""The future, however far (Nov 2026): a meeting next December is "coming up", not "no date yet"; "meri kitni
meetings schedule hain" counts everything ahead; a named month or year is that period; the past is the ordinary
list for that period. Messages: who wrote, which ones were replied to, which are still unread."""
from datetime import time, timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from core import livevoice, services
from core import timeutils as tu
from core.models import Kind, Message, Priority, PrivateTask
from core.tests import BaseCase, _cmd, _workday


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
        self.assertIn("Deleted 2:", d["spoken_reply"])
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
        self.assertIn(f"Deleted Meeting with Aman ({services.tu.fmt_day(self.day2)} at 11 AM).", d["spoken_reply"])
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


class BatchClashTests(BaseCase):
    """Several meetings in one breath: the one that lands on a meeting already there is asked about FIRST (its own
    question: keep it short / move the other / put it elsewhere), the rest wait on the same card; an urgent meeting
    in the way is said out loud and never pushed."""

    def setUp(self):
        super().setUp()
        self.day = (services.tu.today_local() + timedelta(days=1))
        self.existing = services.create_private_task(self.md, {"title": "Board review", "kind": Kind.MEETING, "due_date": self.day,
                                                               "due_time": time(16, 0), "duration_minutes": 60})

    def interpret(self, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def three(self, priority="NORMAL"):
        day = self.day.isoformat()
        item = {"notes": "", "kind": "MEETING", "priority": priority, "due_in_minutes": 0, "duration_minutes": 0, "remind_before_minutes": 0,
                "staff_name": "", "team_name": "", "people": [], "reminder_line": ""}
        return _cmd(intent="create_task", title="Meeting with Aman", kind="MEETING", due_date=day, due_time="11:00", spoken_reply="Ji.",
                    more_items=[{**item, "intent": "create_task", "title": "Meeting with Ankur", "due_date": day, "due_time": "16:00"},
                                {**item, "intent": "create_task", "title": "Meeting with Sharma ji", "due_date": day, "due_time": "18:00"}])

    def test_the_clashing_one_is_asked_about_first_and_the_rest_ride_along(self):
        d = self.interpret("kal 11 baje Aman, 4 baje Ankur aur 6 baje Sharma ji ke saath meeting", self.three())
        # Not "3 things, shall I save all?": the Ankur meeting (4 PM, over Board review) is the card, with its question.
        self.assertEqual(d["title"], "Meeting with Ankur", d["spoken_reply"])
        self.assertFalse(d.get("booked"))
        self.assertIn("3 things; one first:", d["spoken_reply"])
        self.assertIn("Board review", d["spoken_reply"])
        self.assertTrue(d["schedule"]["hard"])
        self.assertIn("push", d["schedule"]["options"])
        self.assertEqual([b["title"] for b in d["batch"]], ["Meeting with Aman", "Meeting with Sharma ji"])
        self.assertEqual(sum(1 for t in services.private_qs(self.md) if t.title.startswith("Meeting with")), 0)  # nothing saved yet
        # "Move the other one": Ankur is booked at 4, Board review moves on, and the other two are saved with it.
        d2 = self.interpret("haan, board review aage kar do", _cmd(intent="confirm_pending", setting_value="push", spoken_reply="Ji."),
                            awaiting="JARVIS asked about the clash", pending=d)
        self.assertTrue(d2.get("booked"), d2["spoken_reply"])
        self.assertIn("Meeting with Ankur at 4 PM", d2["spoken_reply"])
        by_title = {t.title: t for t in services.private_qs(self.md)}
        self.assertTrue({"Meeting with Aman", "Meeting with Ankur", "Meeting with Sharma ji", "Board review"} <= set(by_title), set(by_title))
        self.assertIn("The other 2 saved too", d2["spoken_reply"])
        ankur, board = by_title["Meeting with Ankur"], by_title["Board review"]
        self.assertEqual(services.tu.to_local(ankur.due_at).strftime("%H:%M"), "16:00")
        # Board review now starts when Ankur's meeting ends (its usual length), not over it.
        self.assertGreater(board.due_at, ankur.due_at)
        self.assertEqual(board.due_at, ankur.scheduled_end or ankur.due_at + timedelta(minutes=30))

    def test_an_urgent_meeting_in_the_way_is_said_and_never_pushed(self):
        services.update_private_task(self.md, self.existing.id, {"priority": "URGENT"}, self.existing.version)
        d = self.interpret("kal 4 baje Ankur ke saath meeting", _cmd(intent="create_task", title="Meeting with Ankur", kind="MEETING",
                                                                    due_date=self.day.isoformat(), due_time="16:00", spoken_reply="Ji."))
        self.assertIn("URGENT meeting then, Board review at 4 PM; that stays", d["spoken_reply"])
        self.assertNotIn("push", d["schedule"]["options"])
        self.assertTrue(d["schedule"]["suggested"], d["schedule"])  # the way out offered: another time, taken with a plain yes
        self.assertIn("Shall I put Meeting with Ankur at", d["spoken_reply"])
        # A yes with "push" cannot move it: said so, asked again, nothing booked.
        d2 = self.interpret("board review aage kar do", _cmd(intent="confirm_pending", setting_value="push", spoken_reply="Ji."),
                            awaiting="JARVIS asked about the clash", pending=d)
        self.assertFalse(d2.get("booked"))
        self.assertIn("Board review is urgent and stays", d2["spoken_reply"])
        self.assertIn("Shall I put Meeting with Ankur at", d2["spoken_reply"])


class OneBreathTests(BaseCase):
    """The live voice brings the things of one breath one by one, each its own form. The first clash is asked about and
    nothing else is read out until it is answered: a clear item said after it is saved quietly, a second clash waits
    its turn, and the answer settles the card, names what was saved and asks the next question (Nov 2026)."""

    def setUp(self):
        super().setUp()
        self.day = _workday(2)
        self.next = _workday(3) if _workday(3) != self.day else _workday(4)
        self.board = services.create_private_task(self.md, {"title": "Board meeting", "kind": Kind.MEETING, "priority": Priority.URGENT,
                                                            "due_date": self.day, "due_time": time(11, 0), "duration_minutes": 90})
        self.sbi = services.create_private_task(self.md, {"title": "Call with SBI branch manager", "kind": Kind.CALL, "priority": Priority.HIGH,
                                                          "due_date": self.next, "due_time": time(10, 0), "duration_minutes": 30})

    def tool(self, name, args, pending=None):
        with mock.patch("core.ai_brain.interpret", side_effect=AssertionError("no model must be asked")):
            return livevoice.run_tool(self.md, name, args, pending=pending, awaiting="JARVIS asked about a clash" if pending else "")

    def titles(self):
        return {t.title: t for t in PrivateTask.objects.filter(owner=self.md)}

    def test_one_clash_at_a_time_and_the_rest_follows(self):
        day, nxt = self.day.isoformat(), self.next.isoformat()
        # 1. Khanna at 11:30 lands on the urgent board meeting: one question, one proposal (the new one bends).
        d1 = self.tool("set_meeting", {"people": ["Khanna"], "title": "Meeting with Khanna", "kind": "MEETING", "when_words": "",
                                       "due_date": day, "due_time": "11:30", "said": "Monday saadhe gyarah baje Khanna ji ke saath meeting"})
        self.assertTrue(d1["schedule"]["hard"], d1["say"])
        self.assertIn("URGENT meeting then, Board meeting at 11 AM; that stays", d1["spoken_reply"])
        self.assertIn("Shall I put Meeting with Khanna at", d1["spoken_reply"])
        self.assertTrue(d1["say"])  # the Hindi twin, since he spoke Hinglish
        self.assertEqual(d1["schedule"]["default"], "move")
        # 2. A payment the same breath: saved at once, nothing said (the question stands), the card comes back as it was.
        d2 = self.tool("own_item", {"what": "Payment to Rohit", "kind": "PAYMENT", "when_words": "", "due_date": day, "due_time": "14:30",
                                    "said": "Wednesday dhai baje Rohit ko payment karni hai"}, pending=d1)
        self.assertEqual(d2["say"], "")
        self.assertEqual(d2["title"], "Meeting with Khanna")
        self.assertTrue(d2["keep_card"] and d2["saved_now"])
        self.assertEqual(len(d2["quiet"]), 1)
        self.assertIn("Payment to Rohit", self.titles())
        self.assertNotIn("Meeting with Khanna", self.titles())
        # 3. A call over the SBI call: another clash, kept in line behind the open question; still nothing said.
        d3 = self.tool("set_meeting", {"people": ["CA"], "title": "Call with CA", "kind": "CALL", "when_words": "", "due_date": nxt, "due_time": "10:00",
                                       "said": "Thursday subah 10 baje CA ke saath call"}, pending=d2)
        self.assertEqual(d3["say"], "")
        self.assertEqual([q["title"] for q in d3["queue"]], ["Call with CA"])
        self.assertNotIn("Call with CA", self.titles())
        # 4. A clear meeting: saved quietly too.
        d4 = self.tool("set_meeting", {"people": ["Mehra"], "title": "Meeting with Mehra", "kind": "MEETING", "when_words": "", "due_date": day,
                                       "due_time": "17:00", "said": "Saturday shaam 5 baje Mehra ji se milna hai"}, pending=d3)
        self.assertEqual(d4["say"], "")
        self.assertEqual(len(d4["quiet"]), 2)
        self.assertIn("Meeting with Mehra", self.titles())
        # 5. "Haan": Khanna goes where proposed; then the next clash is asked, with the done line first and nothing repeated.
        d5 = self.tool("answer", {"reply": "yes", "said": "haan"}, pending=d4)
        khanna = self.titles()["Meeting with Khanna"]
        self.assertGreaterEqual(khanna.due_at, self.board.scheduled_end)
        self.assertTrue(d5["spoken_reply"].startswith("Booked: Meeting with Khanna"), d5["spoken_reply"])
        self.assertIn("Call with CA", d5["spoken_reply"])
        self.assertIn("Call with SBI branch manager", d5["spoken_reply"])
        self.assertEqual(d5["spoken_reply"].count("Shall I"), 1)
        self.assertTrue(d5["say"])
        self.assertEqual(d5["title"], "Call with CA")
        self.assertTrue(d5["keep_card"] and d5["saved_now"] and not d5.get("card_done"))
        self.assertEqual(len(d5["quiet"]), 2)
        self.assertNotIn("Call with CA", self.titles())
        # 6. "SBI wali ko aage karo": the one already there moves on, the new one stays; the quiet ones are named once.
        d6 = self.tool("answer", {"reply": "yes", "said": "SBI wali ko aage kar do"}, pending=d5)
        self.assertTrue(d6["booked"] and d6["card_done"], d6["say"])
        self.sbi.refresh_from_db()
        self.assertEqual(tu.to_local(self.sbi.due_at).strftime("%H:%M"), "10:15")
        self.assertEqual(tu.to_local(self.titles()["Call with CA"].due_at).strftime("%H:%M"), "10:00")
        self.assertIn("Call with SBI branch manager moved to 10:15 AM", d6["spoken_reply"])
        self.assertIn("The other 2 saved too: Payment to Rohit", d6["spoken_reply"])
        self.assertIn("Meeting with Mehra", d6["spoken_reply"])

    def test_the_answer_heard_as_a_new_meeting_is_still_the_answer(self):
        day = self.day.isoformat()
        d1 = self.tool("set_meeting", {"people": ["Khanna"], "title": "Meeting with Khanna", "kind": "MEETING", "when_words": "",
                                       "due_date": day, "due_time": "11:30", "said": "11:30 Khanna ji ke saath meeting"})
        # "Khanna ji ko 3 baje rakho" read by the voice as another meeting with Khanna: it is the time for the one waiting.
        d2 = self.tool("set_meeting", {"people": ["Khanna"], "title": "Meeting with Khanna", "kind": "MEETING", "when_words": "",
                                       "due_date": day, "due_time": "15:00", "said": "Khanna ji ko 3 baje rakho"}, pending=d1)
        self.assertTrue(d2["booked"] and d2["card_done"], d2["say"])
        khanna = [t for t in PrivateTask.objects.filter(owner=self.md) if t.title == "Meeting with Khanna"]
        self.assertEqual(len(khanna), 1)
        self.assertEqual(tu.to_local(khanna[0].due_at).strftime("%H:%M"), "15:00")

    def test_no_to_the_clash_drops_only_the_new_one(self):
        day = self.day.isoformat()
        d1 = self.tool("set_meeting", {"people": ["Khanna"], "title": "Meeting with Khanna", "kind": "MEETING", "when_words": "",
                                       "due_date": day, "due_time": "11:30", "said": "x"})
        d2 = self.tool("own_item", {"what": "Buy flowers", "when_words": "", "personal": True, "said": "flowers lene hain"}, pending=d1)
        d3 = self.tool("answer", {"reply": "no", "said": "nahi rehne do"}, pending=d2)
        self.assertTrue(d3["card_done"])
        self.assertIn("Dropped Meeting with Khanna", d3["spoken_reply"])
        self.assertIn("The other 1 saved too: Buy flowers", d3["spoken_reply"])
        self.assertNotIn("Meeting with Khanna", self.titles())
        self.assertIn("Buy flowers", self.titles())

    def test_whenever_free_moves_an_item_to_the_next_free_time_at_once(self):
        sharma = services.create_private_task(self.md, {"title": "Meeting with Sharma", "kind": Kind.MEETING, "due_date": self.day,
                                                        "due_time": time(16, 0), "duration_minutes": 60})
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="change_task", title="Sharma", changed_fields=[], spoken_reply="Ji.")):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "Sharma wali meeting jab free ho tab rakh do"}, format="json")
        d = r.json()
        self.assertTrue(d["booked"] and d["card_done"], d["spoken_reply"])
        self.assertIn("Meeting with Sharma moved to", d["spoken_reply"])
        sharma.refresh_from_db()
        self.assertGreater(sharma.due_at, tu.combine_local(self.day, time(16, 0)))
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="undo", spoken_reply="Ji.")):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "undo"}, format="json")
        self.assertIn("Undone", r.json()["spoken_reply"])
        sharma.refresh_from_db()
        self.assertEqual(tu.to_local(sharma.due_at).strftime("%H:%M"), "16:00")


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
