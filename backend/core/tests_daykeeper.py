"""The day keeper (Nov 2026): when someone is free now and has late or pending work, JARVIS speaks first, once per free
stretch, in one short line with one question. A yes lays the work into that free time (deadlines and meetings
untouched, undo for a minute); a no leaves the day alone; the same stretch is never asked about twice."""
from datetime import time, timedelta
from unittest import mock

from core import actions, followups, scheduling, services
from core import timeutils as tu
from core.models import Alert, Kind, PrivateTask, Priority
from core.tests import BaseCase, _cmd

ALL_DAY = {"officeStart": "00:00", "officeEnd": "23:59", "weeklyOff": [], "routineBlocks": []}


class DayKeeperTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.private.delete()  # the fixture's meeting in an hour: this class lays out its own day
        today = tu.today_local()
        self.now = tu.combine_local(today, time(11, 0))  # the clock the keeper is given: 11 in the morning
        # A fixed meeting from 2 to 3: the free stretch is 11 to 2.
        self.meeting = services.create_private_task(self.md, {"title": "Meeting with architect", "kind": Kind.MEETING,
                                                              "due_date": today, "due_time": time(14, 0), "duration_minutes": 60})
        # Late work: urgent before high before normal, whatever the age.
        self.a = services.create_private_task(self.md, {"title": "Renew the trade licence", "due_date": today - timedelta(days=10)})
        self.b = services.create_private_task(self.md, {"title": "Call the insurance agent", "due_date": today - timedelta(days=3),
                                                        "priority": Priority.URGENT, "kind": Kind.CALL})
        self.c = services.create_private_task(self.md, {"title": "Send the bank statement", "due_date": today - timedelta(days=1),
                                                        "priority": Priority.HIGH})

    def keeper(self):
        with mock.patch("core.followups.get_prefs", return_value=dict(ALL_DAY)):
            return followups.free_time(now=self.now)

    def suggestion(self):
        return Alert.objects.get(recipient=self.md, ref_type="followup", ref_id__startswith="free:")

    def test_one_short_suggestion_with_the_plan_urgent_first(self):
        self.assertEqual(self.keeper(), 1)
        alert = self.suggestion()
        q = alert.question
        self.assertEqual(q["kind"], "place")
        # The call keeps its kind (never moved by the keeper); the two tasks are laid out urgent / high / normal.
        self.assertEqual([p["title"] for p in q["plan"]], ["Send the bank statement", "Renew the trade licence"])
        self.assertEqual([p["start"][11:16] for p in q["plan"]], ["11:00", "11:30"])
        self.assertIn("free till 2 PM", alert.spoken_text)
        self.assertIn("2 late items", alert.spoken_text)
        self.assertTrue(alert.spoken_text.endswith("Shall I put them in?"), alert.spoken_text)
        self.assertEqual(alert.spoken_text.count("Sir"), 1)
        self.assertIn("रख दूँ?", alert.spoken_text_hi)
        # Never a title in the spoken line; the banner's body carries them.
        for title in ("Send the bank statement", "Renew the trade licence"):
            self.assertNotIn(title, alert.spoken_text)
            self.assertNotIn(title, alert.spoken_text_hi)
            self.assertIn(title, alert.body)
        # The same stretch is not asked about again.
        self.assertEqual(self.keeper(), 0)

    def test_yes_lays_the_work_into_the_free_time_and_undo_takes_it_out(self):
        self.keeper()
        alert = self.suggestion()
        en, hi = services.answer_place(self.md, alert, True)
        self.assertTrue(en.startswith("Done: Send the bank statement at 11 AM, Renew the trade licence at 11:30 AM."), en)
        self.assertTrue(hi.startswith("हो गया:"), hi)
        c, a = PrivateTask.objects.get(id=self.c.id), PrivateTask.objects.get(id=self.a.id)
        self.assertEqual((tu.to_local(c.scheduled_start).hour, tu.to_local(c.scheduled_start).minute), (11, 0))
        self.assertEqual((tu.to_local(a.scheduled_start).hour, tu.to_local(a.scheduled_start).minute), (11, 30))
        self.assertEqual(a.due_date, tu.today_local() - timedelta(days=10))  # the deadline is not touched
        self.assertIsNotNone(Alert.objects.get(id=alert.id).acknowledged_at)
        # The placed work is busy time now: nothing else is suggested for it, and undo clears the slots.
        self.assertEqual(self.keeper(), 0)
        actions.undo_last(self.md)
        self.assertIsNone(PrivateTask.objects.get(id=self.c.id).scheduled_start)
        self.assertIsNone(PrivateTask.objects.get(id=self.a.id).scheduled_start)

    def test_no_leaves_the_day_alone(self):
        self.keeper()
        alert = self.suggestion()
        en, _hi = services.answer_place(self.md, alert, False)
        self.assertEqual(en, "Okay, leaving it.")
        self.assertIsNone(PrivateTask.objects.get(id=self.c.id).scheduled_start)
        self.assertIsNotNone(Alert.objects.get(id=alert.id).acknowledged_at)
        self.assertEqual(self.keeper(), 0)

    def test_busy_now_means_no_suggestion(self):
        services.create_private_task(self.md, {"title": "Board call", "kind": Kind.CALL, "due_date": tu.today_local(),
                                               "due_time": time(11, 0), "duration_minutes": 60})
        self.assertEqual(self.keeper(), 0)

    def test_the_planner_skips_fixed_and_timed_items(self):
        today = tu.today_local().isoformat()
        items = [
            {"id": "m", "scope": "private", "title": "Meeting", "kind": Kind.MEETING, "status": "OPEN", "overdue": True, "priority": "NORMAL"},
            {"id": "t", "scope": "private", "title": "Timed later", "kind": Kind.TASK, "status": "OPEN", "overdue": False, "priority": "URGENT",
             "due_date": today, "due_at": (self.now + timedelta(hours=5)).isoformat()},
            {"id": "p", "scope": "private", "title": "Already placed", "kind": Kind.TASK, "status": "OPEN", "overdue": True, "priority": "URGENT",
             "scheduled_start": (self.now + timedelta(hours=1)).isoformat()},
            {"id": "u", "scope": "private", "title": "Undated", "kind": Kind.TASK, "status": "OPEN", "overdue": False, "priority": "LOW"},
            {"id": "o", "scope": "private", "title": "Late payment", "kind": Kind.PAYMENT, "status": "OPEN", "overdue": True, "priority": "LOW",
             "due_date": "2026-11-01", "estimated_minutes": 20},
            {"id": "d", "scope": "private", "title": "Done one", "kind": Kind.TASK, "status": "COMPLETED", "overdue": True, "priority": "URGENT"},
        ]
        plan = scheduling.fit_into_free_time(items, self.now, self.now + timedelta(minutes=45), self.now)
        self.assertEqual([p["id"] for p in plan["placed"]], ["o"])          # the late payment fits (20 min), overdue first
        self.assertEqual(plan["left"], ["Undated"])                          # the undated task did not fit the 25 minutes left
        self.assertEqual(plan["placed"][0]["minutes"], 20)

    def test_a_yes_by_voice_with_the_app_closed(self):
        self.keeper()
        alert = self.suggestion()
        q = alert.question
        r = self.md_client.post("/api/v1/assistant/turn/", {"text": "", "event": "ask",
                                                            "about": {"kind": "alert", "id": str(alert.id), "title": alert.title, "question": q["en"]}},
                                format="json")
        self.assertEqual(r.status_code, 200, r.content)
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": "haan rakh do"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["show"].startswith("Done:"), r.json())
        self.assertIsNotNone(PrivateTask.objects.get(id=self.c.id).scheduled_start)

    def test_kaun_se_by_voice_names_them_and_keeps_the_question_open(self):
        self.keeper()
        alert = self.suggestion()
        r = self.md_client.post("/api/v1/assistant/turn/", {"text": "", "event": "ask",
                                                            "about": {"kind": "alert", "id": str(alert.id), "title": alert.title, "question": alert.question["en"]}},
                                format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.md_client.post("/api/v1/assistant/turn/", {"text": "kaun se?"}, format="json")  # no model call needed
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["show"], "Send the bank statement, Renew the trade licence.")
        self.assertIsNone(PrivateTask.objects.get(id=self.c.id).scheduled_start)  # nothing placed by asking
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="confirm_pending")):
            r = self.md_client.post("/api/v1/assistant/turn/", {"text": "haan"}, format="json")
        self.assertTrue(r.json()["show"].startswith("Done:"), r.json())
        self.assertIsNotNone(PrivateTask.objects.get(id=self.c.id).scheduled_start)

    def test_a_yes_from_the_banner_goes_through_the_place_door(self):
        self.keeper()
        alert = self.suggestion()
        r = self.md_client.post(f"/api/v1/alerts/{alert.id}/place/", {"yes": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["show"].startswith("Done:"))
        self.assertIn("हो गया", r.json()["say_hi"])
        self.assertIsNotNone(PrivateTask.objects.get(id=self.a.id).scheduled_start)
        # The PA cannot answer the MD's suggestion.
        r = self.pa_client.post(f"/api/v1/alerts/{alert.id}/place/", {"yes": True}, format="json")
        self.assertIn(r.status_code, (403, 404))
