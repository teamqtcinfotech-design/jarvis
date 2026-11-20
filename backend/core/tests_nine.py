"""Day-to-day task work (Nov 2026): repeating items, checklists, updates on a task, the hand-on chain, approvals with
an amount, chasing, a slot free for several people, and files on a task."""
import json
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core import filestore, followups, livevoice, recurrence, services
from core import timeutils as tu
from core.models import Alert, Kind, PrivateTask, SharedTask, Status, TaskEvent, TaskFile, User
from core.tests import BaseCase, _cmd, _workday


class NineCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.nikki_client = APIClient()
        self.nikki_client.force_authenticate(self.nikki)
        self.mansi = User.objects.get(username="manager")  # the manager account the base case makes: "Mansi"
        self.mansi_client = APIClient()
        self.mansi_client.force_authenticate(self.mansi)

    def interpret(self, client, text, cmd, **extra):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text, **extra}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()


# ------------------------------------------------------------------------------------------------ 1 recurring
class RepeatWordsTests(BaseCase):
    def test_the_words_become_a_rule(self):
        anchor = date(2026, 11, 20)  # a Friday
        for words, rule in (
            ("har mahine 10 tarikh ko", "monthly:10"), ("every month on the 25th", "monthly:25"), ("हर महीने १० तारीख़ को", "monthly:10"),
            ("roz", "daily"), ("har din subah", "daily"), ("every day", "daily"), ("रोज़ सुबह", "daily"),
            ("har Monday", "weekly:0"), ("every Monday and Thursday", "weekly:0,3"), ("har somvar", "weekly:0"),
            ("weekly", "weekly:4"), ("har hafte", "weekly:4"), ("weekdays", "weekly:0,1,2,3,4"),
            ("har saal", "yearly:11-20"), ("mahine ke last day", "monthly:last"), ("", ""), ("kal 4 baje", ""),
        ):
            self.assertEqual(recurrence.parse_words(words, anchor), rule, words)

    def test_next_dates_and_month_ends(self):
        self.assertEqual(recurrence.next_date("monthly:31", date(2026, 1, 31)), date(2026, 2, 28))
        self.assertEqual(recurrence.next_date("monthly:31", date(2026, 2, 28)), date(2026, 3, 31))
        self.assertEqual(recurrence.next_date("monthly:last", date(2026, 2, 10)), date(2026, 2, 28))
        self.assertEqual(recurrence.next_date("monthly:10", date(2026, 11, 10)), date(2026, 12, 10))
        self.assertEqual(recurrence.next_date("weekly:0,3", date(2026, 11, 20)), date(2026, 11, 23))  # Fri -> Mon
        self.assertEqual(recurrence.next_date("weekly:0,3", date(2026, 11, 23)), date(2026, 11, 26))  # Mon -> Thu
        self.assertEqual(recurrence.next_date("daily", date(2026, 12, 31)), date(2027, 1, 1))
        self.assertEqual(recurrence.next_date("yearly:03-31", date(2026, 3, 31)), date(2027, 3, 31))
        self.assertIsNone(recurrence.next_date("", date(2026, 1, 1)))
        self.assertEqual(recurrence.describe("monthly:10"), ("every month on the 10th", "हर महीने 10 तारीख़ को"))
        self.assertEqual(recurrence.describe("weekly:0,1,2,3,4")[0], "every working day")
        self.assertTrue(recurrence.valid("weekly:2"))
        self.assertFalse(recurrence.valid("fortnightly"))


class RepeatingTasksTests(NineCase):
    def test_a_monthly_task_given_to_nikki_comes_back_every_month_once(self):
        today = tu.today_local()
        first = today + timedelta(days=3)
        d = self.interpret(self.md_client, "Nikki ko bolo har mahine GST file karo", _cmd(
            intent="assign_to_person", staff_name="Nikki", recipients=["Nikki"], title="File GST", kind="TASK",
            due_date=first.isoformat(), repeat_words="har mahine", spoken_reply="Ji Sir."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIn("every month", d["spoken_reply"])
        task = SharedTask.objects.get(title="File GST")
        self.assertEqual((task.repeat, task.due_date, task.assignee_user), (f"monthly:{first.day}", first, self.nikki))
        item = services.shared_item(task)
        self.assertEqual(item["repeat_label"], f"every month on the {first.day}{'st' if first.day in (1, 21, 31) else 'nd' if first.day in (2, 22) else 'rd' if first.day in (3, 23) else 'th'}")
        # Still open and not yet due: nothing is made.
        self.assertEqual(recurrence.run(), 0)
        # Nikki finishes it: the next occurrence is made once, on the next date the rule names, given as the first was.
        r = self.nikki_client.post(f"/api/v1/pa-tasks/{task.id}/status/", {"status": "COMPLETED", "expected_version": task.version}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        Alert.objects.all().delete()
        self.assertEqual(recurrence.run(), 1)
        self.assertEqual(recurrence.run(), 0)
        nxt = SharedTask.objects.get(repeat_of=task)
        self.assertEqual((nxt.title, nxt.assignee_user, nxt.created_by, nxt.repeat, nxt.status),
                         ("File GST", self.nikki, self.md, task.repeat, Status.OPEN))
        self.assertEqual(nxt.due_date, recurrence.next_date(task.repeat, first))
        self.assertTrue(Alert.objects.filter(recipient=self.nikki, kind="TASK_ASSIGNED", ref_id=str(nxt.id), ring=True).exists())
        self.assertIn("repeats every month", " ".join(h["text"] for h in services.task_history(nxt)))
        # "Band kar do": the series ends; nothing more comes.
        services.recurrence.stop_series(nxt)
        nxt.status = Status.COMPLETED
        nxt.save()
        self.assertEqual(recurrence.run(), 0)

    def test_a_daily_reminder_whose_time_has_passed_starts_tomorrow_and_rolls_on(self):
        now = tu.now_local()
        gone = (now - timedelta(hours=2)).time().replace(second=0, microsecond=0)
        if now.hour < 3:
            gone = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)  # a small-hours run: no past time today
        task = services.create_private_task(self.md, {"title": "Blood pressure tablet", "kind": Kind.TASK, "due_date": now.date(),
                                                       "due_time": gone, "repeat": "daily"})
        expected = now.date() + timedelta(days=1) if gone < now.time() else now.date()
        self.assertEqual(task.due_date, expected)
        self.assertEqual(tu.to_local(task.due_at).time(), gone)
        # Its day passes without a tick: the next one is still made (never two for the same series).
        SharedTask.objects.all().delete()
        PrivateTask.objects.filter(id=task.id).update(due_date=now.date() - timedelta(days=1), due_at=task.due_at - timedelta(days=2))
        self.assertEqual(recurrence.run(), 1)
        self.assertEqual(recurrence.run(), 0)
        nxt = PrivateTask.objects.get(repeat_of=task)
        self.assertGreaterEqual(nxt.due_date, now.date())
        self.assertEqual(nxt.owner, self.md)
        self.assertEqual(nxt.title, "Blood pressure tablet")

    def test_a_repeat_with_an_end_date_stops_there(self):
        today = tu.today_local()
        task = services.create_own_task(self.nikki, {"title": "Daily cash count", "kind": Kind.TASK, "repeat": "daily",
                                                      "due_date": today, "repeat_until": today})
        task.status = Status.COMPLETED
        task.save()
        self.assertEqual(recurrence.run(), 0)  # tomorrow is past repeat_until
        self.assertFalse(SharedTask.objects.filter(repeat_of=task).exists())

    def test_the_live_form_carries_the_repeat_and_the_api_accepts_the_rule(self):
        cmd = livevoice.cmd_for(self.md, "give_work", {"items": [{"to": "Nikki", "what": "GST filing", "when_words": "",
                                                                  "repeat_words": "har mahine 10 tarikh ko", "steps": ["Collect bills", "File"],
                                                                  "amount": 0}], "said": "s"})
        self.assertEqual((cmd["repeat_words"], cmd["steps"]), ("har mahine 10 tarikh ko", ["Collect bills", "File"]))
        r = self.md_client.post("/api/v1/private-tasks/", {"title": "Water the plants", "repeat": "weekly:0,3"}, format="json", HTTP_IDEMPOTENCY_KEY="p1")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["repeat_label"], "every Monday and Thursday")
        self.assertIsNotNone(r.json()["due_date"])  # the first date the rule names
        r = self.md_client.post("/api/v1/private-tasks/", {"title": "Nonsense", "repeat": "fortnightly"}, format="json", HTTP_IDEMPOTENCY_KEY="p2")
        self.assertEqual(r.status_code, 422)


# ------------------------------------------------------------------------------------------------ 3 checklists
class ChecklistTests(NineCase):
    def test_steps_are_saved_ticked_by_the_doer_and_the_last_one_offers_done(self):
        d = self.interpret(self.md_client, "Nikki ko bolo Pune trip ke liye hotel, flights aur car book karo", _cmd(
            intent="assign_to_person", staff_name="Nikki", recipients=["Nikki"], title="Pune Trip Bookings",
            steps=["Hotel", "Flights", "Car"], spoken_reply="Ji Sir."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        task = SharedTask.objects.get(title="Pune Trip Bookings")
        self.assertEqual([s["text"] for s in task.steps], ["Hotel", "Flights", "Car"])
        item = services.shared_item(task)
        self.assertEqual((item["steps_done"], item["steps_total"]), (0, 3))
        # Nikki ticks one on screen: it counts as seen, the giver is not pestered with a "seen".
        r = self.nikki_client.post(f"/api/v1/shared-tasks/{task.id}/steps/", {"index": 0, "done": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["task"]["steps_done"], r.json()["all_done"]), (1, False))
        task.refresh_from_db()
        self.assertIsNotNone(task.seen_at)
        # By voice: "flights ho gayi" ticks the step; the last one asks whether the task itself is done.
        d = self.interpret(self.nikki_client, "flights book ho gayi", _cmd(intent="tick_step", title="flights", target_refs=[str(task.id)]))
        self.assertIn("2 of 3", d["spoken_reply"])
        d = self.interpret(self.nikki_client, "car bhi ho gayi", _cmd(intent="tick_step", title="car"))
        self.assertEqual(d["action"], "complete")
        self.assertIn("last step", d["spoken_reply"])
        self.assertEqual(d["target"]["id"], str(task.id))
        # Mansi has nothing to do with it: not even found.
        r = self.mansi_client.post(f"/api/v1/shared-tasks/{task.id}/steps/", {"index": 0, "done": False}, format="json")
        self.assertEqual(r.status_code, 404)
        # The MD rewrites the list; ticks already made are kept by their words.
        r = self.md_client.post(f"/api/v1/shared-tasks/{task.id}/steps/", {"steps": ["Hotel", "Flights", "Car", "Driver"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["task"]["steps_done"], 3)
        self.assertEqual(r.json()["task"]["steps_total"], 4)
        # Nothing of the checklist is readable in the database.
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT steps_json FROM core_sharedtask WHERE id = %s", [str(task.id)])
            raw = cur.fetchone()[0]
        self.assertTrue(raw.startswith("enc1:"), raw[:12])
        self.assertNotIn("Hotel", raw)

    def test_a_personal_checklist_is_the_owners_alone(self):
        r = self.md_client.post("/api/v1/private-tasks/", {"title": "Weekend", "steps": ["Gym", "Groceries"]}, format="json", HTTP_IDEMPOTENCY_KEY="w1")
        self.assertEqual(r.status_code, 201, r.content)
        pid = r.json()["id"]
        r = self.pa_client.post(f"/api/v1/private-tasks/{pid}/steps/", {"index": 0}, format="json")
        self.assertEqual(r.status_code, 404)
        r = self.md_client.post(f"/api/v1/private-tasks/{pid}/steps/", {"index": 1}, format="json")
        self.assertEqual(r.json()["task"]["steps"][1]["done"], True)


# ------------------------------------------------------------------------------------------------ 4 updates
class TaskUpdateTests(NineCase):
    def test_an_update_goes_into_the_history_and_the_other_side_hears_it(self):
        task = services.create_person_task(self.md, self.nikki, {"title": "Bank NEFT for vendor", "kind": Kind.TASK}, compose_note=False)
        Alert.objects.all().delete()
        r = self.nikki_client.post(f"/api/v1/shared-tasks/{task.id}/notes/", {"text": "Bank was closed today, will do it tomorrow morning"},
                                   format="json", HTTP_IDEMPOTENCY_KEY="n1")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["told"], ["Sir"])
        ev = TaskEvent.objects.get(task=task, kind=TaskEvent.Kind.NOTE)
        self.assertEqual(ev.note, "Bank was closed today, will do it tomorrow morning")
        self.assertEqual(ev.data, {})  # the words are in the encrypted field only
        alert = Alert.objects.get(recipient=self.md)
        self.assertIn("Nikki on Bank NEFT for vendor: Bank was closed today", alert.spoken_text)
        self.assertIn("Nikki: Bank was closed today", " | ".join(h["text"] for h in services.task_history(task)))
        task.refresh_from_db()
        self.assertIsNotNone(task.seen_at)  # an update from the doer is seeing
        # The giver answers on the same task: Nikki hears it. A stranger cannot even find it.
        d = self.interpret(self.md_client, "Nikki ke task pe note daalo: pehle salary wala karo", _cmd(
            intent="update_task", target_refs=[str(task.id)], note_text="Do the salary one first"))
        self.assertIn("told Nikki", d["spoken_reply"])
        self.assertTrue(Alert.objects.filter(recipient=self.nikki, body="Do the salary one first").exists())
        r = self.mansi_client.post(f"/api/v1/shared-tasks/{task.id}/notes/", {"text": "hi"}, format="json", HTTP_IDEMPOTENCY_KEY="n2")
        self.assertEqual(r.status_code, 404)
        # Movement: the "no update" clock starts again.
        self.assertGreater(task.updated_at, task.created_at)

    def test_by_voice_without_words_jarvis_asks_what_to_write(self):
        task = services.create_person_task(self.md, self.nikki, {"title": "Vendor call", "kind": Kind.TASK}, compose_note=False)
        d = self.interpret(self.nikki_client, "vendor call pe update", _cmd(intent="update_task", target_refs=[str(task.id)]))
        self.assertTrue(d["needs_clarification"])
        self.assertIn("what should the update", d["spoken_reply"])


# ------------------------------------------------------------------------------------------------ 5 chain
class HandOnChainTests(NineCase):
    def test_the_chain_shows_only_once_it_was_handed_on(self):
        task = services.create_person_task(self.md, self.mansi, {"title": "Audit file", "kind": Kind.TASK}, compose_note=False)
        self.assertEqual(services.shared_item(task)["chain"], [])
        Alert.objects.all().delete()
        # Mansi, who has it, hands it on herself: Sir (who gave it) hears where it went; Nikki is rung as for new work.
        services.reassign_task(self.mansi, task.id, self.nikki)
        task.refresh_from_db()
        self.assertEqual(task.handed, [self.md.id, self.mansi.id, self.nikki.id])
        self.assertEqual(services.shared_item(task)["chain"], ["Sir", "Mansi", "Nikki"])
        self.assertIn("Mansi has handed Audit file on to Nikki", Alert.objects.get(recipient=self.md).spoken_text)
        self.assertTrue(Alert.objects.filter(recipient=self.nikki, kind="TASK_ASSIGNED", ring=True).exists())
        self.assertFalse(Alert.objects.filter(recipient=self.mansi).exists())
        # Nikki, a stranger to the task until now, could not have taken it; Mansi no longer holds it either.
        with self.assertRaises(services.ServiceError):
            services.reassign_task(self.mansi, task.id, self.pa)
        # An older task with no record starts its chain when it is first handed on.
        old = SharedTask.objects.create(title="Old one", assignee_type="USER", assignee_user=self.mansi, origin="GIVEN", created_by=self.md)
        services.reassign_task(self.md, old.id, self.nikki)
        old.refresh_from_db()
        self.assertEqual(services.shared_item(old)["chain"], ["Sir", "Mansi", "Nikki"])


# ------------------------------------------------------------------------------------------------ 9 approvals
class ApprovalTests(NineCase):
    def ask(self, amount="1.5 lakh", title="Vendor Advance"):
        d = self.interpret(self.nikki_client, f"Sir se {amount} ka approval chahiye {title} ke liye", _cmd(
            intent="request_for_md", kind="APPROVAL", title=title, amount=services.clean_amount(amount), spoken_reply="Ji."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        return SharedTask.objects.get(title=title)

    def test_rupees_are_read_and_written_the_indian_way(self):
        self.assertEqual(services.clean_amount("1.5 lakh"), Decimal("150000.00"))
        self.assertEqual(services.clean_amount("50 hazaar"), Decimal("50000.00"))
        self.assertEqual(services.clean_amount("₹2,50,000"), Decimal("250000.00"))
        self.assertEqual(services.clean_amount("2 crore"), Decimal("20000000.00"))
        self.assertEqual(services.clean_amount(50000), Decimal("50000.00"))
        self.assertIsNone(services.clean_amount("nothing"))
        self.assertIsNone(services.clean_amount(0))
        self.assertEqual(services.rupees(Decimal("150000")), "₹1,50,000")
        self.assertEqual(services.rupees(Decimal("20000000")), "₹2,00,00,000")
        self.assertEqual(services.rupees(Decimal("999.5")), "₹999.50")

    def test_an_approval_rings_sir_asks_approve_and_tells_nikki_the_decision(self):
        task = self.ask()
        self.assertEqual((task.kind, task.amount, task.assignee_user), ("APPROVAL", Decimal("150000.00"), self.md))
        alert = Alert.objects.get(recipient=self.md, kind="TASK_ASSIGNED", ref_id=str(task.id))
        self.assertTrue(alert.ring)
        self.assertIn("asks your approval for ₹1,50,000: Vendor Advance", alert.spoken_text)
        q = services.alert_question(alert)
        self.assertEqual((q["kind"], q["en"], q["task"]), ("approve", "Approve ₹1,50,000?", str(task.id)))
        self.assertEqual(services.shared_item(task)["amount_label"], "₹1,50,000")
        # Sir approves on the banner: approved, Nikki told with the amount, the ringing over, the history says so.
        r = self.md_client.post(f"/api/v1/shared-tasks/{task.id}/approve/", {"approve": True, "note": "Pay after the GRN"},
                                format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(r.status_code, 200, r.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Status.COMPLETED)
        told = Alert.objects.get(recipient=self.nikki, sender=self.md)
        self.assertIn("has approved ₹1,50,000 for Vendor Advance. Note: Pay after the GRN", told.spoken_text)
        self.assertEqual(TaskEvent.objects.get(task=task, kind=TaskEvent.Kind.APPROVED).note, "Pay after the GRN")  # encrypted: compare here
        self.assertIsNone(services.alert_question(Alert.objects.get(id=alert.id)))
        self.assertEqual(r.status_code, 200)
        # Nobody else can decide it; deciding twice is refused.
        other = self.ask(amount="20 hazaar", title="Office Chairs")
        r = self.mansi_client.post(f"/api/v1/shared-tasks/{other.id}/approve/", {"approve": True}, format="json", HTTP_IDEMPOTENCY_KEY="k2")
        self.assertEqual(r.status_code, 403)
        # By voice Sir declines, with a reason; Nikki hears it.
        d = self.interpret(self.md_client, "chairs wala mana kar do, abhi budget nahi", _cmd(
            intent="approve_item", approve=False, target_refs=[str(other.id)], note_text="No budget this month"))
        self.assertIn("Declined", d["spoken_reply"])
        other.refresh_from_db()
        self.assertEqual(other.status, Status.CANCELLED)
        self.assertIn("has declined ₹20,000 for Office Chairs. Note: No budget this month",
                      Alert.objects.filter(recipient=self.nikki, sender=self.md).latest("created_at").spoken_text)
        r = self.md_client.post(f"/api/v1/shared-tasks/{other.id}/approve/", {"approve": True}, format="json", HTTP_IDEMPOTENCY_KEY="k3")
        self.assertEqual(r.status_code, 409)
        # The approvals card: what waits, what was decided, the money added up; Mansi sees none of it.
        third = self.ask(amount="10000", title="Printer Toner")
        r = self.md_client.get("/api/v1/views/approvals/")
        self.assertEqual(r.status_code, 200, r.content)
        view = r.json()
        stats = {s["label"]: s["value"] for s in view["stats"]}
        self.assertEqual((stats["Waiting"], stats["Approved"], stats["Declined"], stats["Approved ₹"]), (1, 1, 1, "₹1,50,000"))
        self.assertIn("1 approval waiting, first Printer Toner (₹10,000)", view["summary"])
        self.assertEqual(view["rows"][0]["who"], "Nikki → You")
        # Someone with no part in it (not the PA or the manager, who run Sir's diary) sees none of it; Nikki sees hers.
        rahul = services.team_add(self.md, {"name": "Rahul", "title": "Farm Manager", "username": "rahul", "password": "rahul1"})
        rahul_client = APIClient()
        rahul_client.force_authenticate(rahul)
        self.assertEqual(rahul_client.get("/api/v1/views/approvals/").json()["rows"], [])
        self.assertIn(str(third.id), json.dumps(self.nikki_client.get("/api/v1/views/approvals/").json()["rows"]))

    def test_an_approval_said_without_a_name_goes_to_sir(self):
        from core.assistant import _route_intent
        self.assertEqual(_route_intent(self.nikki, _cmd(intent="create_task", kind="APPROVAL", title="Laptop", amount=60000), "60 hazaar ka approval"),
                         "request_for_md")
        d = self.interpret(self.nikki_client, "60 hazaar ka approval chahiye laptop ke liye", _cmd(
            intent="create_task", kind="APPROVAL", title="New Laptop", amount=60000, spoken_reply="Ji."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertIn("asked Sir's approval for ₹60,000", d["spoken_reply"])


# ------------------------------------------------------------------------------------------------ 8 chasing
class ChasingTests(NineCase):
    def test_a_promise_gone_by_is_chased_once_and_the_giver_hears_at_the_digest(self):
        day = tu.now_local().replace(hour=11, minute=0, second=0, microsecond=0)
        task = services.create_person_task(self.md, self.nikki, {"title": "Vendor payment", "kind": Kind.TASK}, compose_note=False)
        yesterday = day.date() - timedelta(days=1)
        SharedTask.objects.filter(id=task.id).update(promise_date=yesterday, seen_at=day - timedelta(days=2), created_at=day - timedelta(days=2))
        unseen = services.create_person_task(self.md, self.mansi, {"title": "Sign the lease", "kind": Kind.TASK}, compose_note=False)
        SharedTask.objects.filter(id=unseen.id).update(created_at=day - timedelta(hours=5))
        Alert.objects.all().delete()
        followups.run(now=day)
        nudge = Alert.objects.get(recipient=self.nikki, ref_type="followup", ref_id__startswith="promise:")
        self.assertIn("you said you'd do Sir's task 'Vendor payment' by", nudge.spoken_text)
        self.assertEqual((nudge.question["kind"], nudge.question["task"]), ("promise", str(task.id)))
        self.assertEqual(followups.run(now=day + timedelta(minutes=10)), 0)
        # Her new time goes back to Sir through the usual promise door (the nudge carries the task).
        from core import promises
        with mock.patch("core.promises._read_ai", return_value=None):
            out = promises.answer(self.nikki, nudge, "kal shaam tak")
        self.assertTrue(out["handled"])
        task.refresh_from_db()
        self.assertEqual(task.promise_date, tu.today_local() + timedelta(days=1))
        # Nothing for the giver before his summary time; at it, one line with everything that has not moved.
        self.assertFalse(Alert.objects.filter(recipient=self.md, ref_id__startswith="chase:").exists())
        evening = day.replace(hour=18, minute=35)
        followups.run(now=evening)
        chase = Alert.objects.get(recipient=self.md, ref_id__startswith="chase:")
        self.assertIn("Sign the lease (Mansi, not seen yet)", chase.spoken_text)
        self.assertNotIn("Vendor payment", chase.spoken_text)  # she has a fresh promise: it has moved
        self.assertEqual(followups.run(now=evening + timedelta(minutes=10)), 0)


# ------------------------------------------------------------------------------------------------ 6 a slot for all
class GroupSlotTests(NineCase):
    def test_a_meeting_with_two_people_looks_at_every_diary(self):
        day = _workday()
        # Mansi has a meeting at 4; Nikki and Sir are free then; everyone is free at 5.
        services.create_own_task(self.mansi, {"title": "Vendor review", "kind": Kind.MEETING, "due_date": day, "due_time": time(16, 0),
                                              "duration_minutes": 60})
        d = self.interpret(self.md_client, "Nikki aur Mansi ke saath 4 baje meeting", _cmd(
            intent="meet_with_person", staff_name="Nikki", recipients=["Nikki", "Mansi"], title="Planning", kind="MEETING",
            due_date=day.isoformat(), due_time="16:00", spoken_reply="Ji Sir."))
        self.assertFalse(d.get("booked"), d["spoken_reply"])
        sched = d["schedule"]
        self.assertEqual(sched["for"], "group")
        self.assertEqual([c["who"] for c in sched["clashes"]], ["Mansi"])
        self.assertIn("Mansi has Vendor review then", d["spoken_reply"])
        self.assertEqual(sched["suggested"]["due_time"], "17:00")
        self.assertIn("when all of you are free", d["spoken_reply"])
        # Yes takes the time free for all; both are told.
        d2 = self.interpret(self.md_client, "haan", _cmd(intent="confirm_pending"), previous="Nikki aur Mansi ke saath 4 baje meeting",
                            awaiting="Meeting with Nikki and Mansi", pending=d)
        self.assertEqual(d2["overrides"], {"due_date": day.isoformat(), "due_time": "17:00"})
        # A free time: no question, booked at once for both.
        d = self.interpret(self.md_client, "Nikki aur Mansi ke saath 11 baje meeting", _cmd(
            intent="meet_with_person", staff_name="Nikki", recipients=["Nikki", "Mansi"], title="Standup", kind="MEETING",
            due_date=day.isoformat(), due_time="11:00", spoken_reply="Ji Sir."))
        self.assertTrue(d["booked"], d["spoken_reply"])
        self.assertEqual(SharedTask.objects.filter(title="Standup").count(), 2)


# ------------------------------------------------------------------------------------------------ 2 files
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


class FilesOnATaskTests(NineCase):
    def test_a_photo_is_kept_encrypted_and_seen_only_by_the_tasks_people(self):
        import tempfile
        with tempfile.TemporaryDirectory() as store, override_settings(JARVIS_FILES_DIR=store):
            task = services.create_person_task(self.md, self.nikki, {"title": "Fix the signboard", "kind": Kind.TASK}, compose_note=False)
            r = self.md_client.post(f"/api/v1/shared-tasks/{task.id}/files/",
                                    {"file": SimpleUploadedFile("board.png", PNG, content_type="image/png")}, format="multipart")
            self.assertEqual(r.status_code, 201, r.content)
            fid = r.json()["id"]
            self.assertEqual((r.json()["kind"], r.json()["name"], r.json()["size"]), ("photo", "board.png", len(PNG)))
            row = TaskFile.objects.get(id=fid)
            on_disk = (filestore.files_dir() / row.stored).read_bytes()
            self.assertNotIn(PNG[:16], on_disk)          # not the picture itself
            self.assertEqual(filestore.read_file(row), PNG)
            self.assertEqual(services.shared_item(task)["files_count"], 1)
            # Nikki (it is her task) sees and downloads it; Mansi finds nothing.
            r = self.nikki_client.get(f"/api/v1/shared-tasks/{task.id}/files/")
            self.assertEqual([f["id"] for f in r.json()["results"]], [fid])
            r = self.nikki_client.get(f"/api/v1/files/{fid}/")
            self.assertEqual((r.status_code, r["Content-Type"], r.content), (200, "image/png", PNG))
            self.assertEqual(self.mansi_client.get(f"/api/v1/files/{fid}/").status_code, 404)
            self.assertEqual(self.mansi_client.get(f"/api/v1/shared-tasks/{task.id}/files/").status_code, 404)
            # A kind that is not kept, and an empty file, are refused.
            r = self.md_client.post(f"/api/v1/shared-tasks/{task.id}/files/",
                                    {"file": SimpleUploadedFile("tool.exe", b"MZ....", content_type="application/x-msdownload")}, format="multipart")
            self.assertEqual(r.status_code, 422)
            # Nikki adds a voice note; Mansi may not remove it; Nikki removes hers and the disk copy goes too.
            r = self.nikki_client.post(f"/api/v1/shared-tasks/{task.id}/files/",
                                       {"file": SimpleUploadedFile("note.m4a", b"\x00\x00\x00\x18ftypM4A " + b"a" * 100, content_type="audio/mp4")},
                                       format="multipart")
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(r.json()["kind"], "voice")
            vid = r.json()["id"]
            self.assertEqual(self.mansi_client.delete(f"/api/v1/files/{vid}/").status_code, 404)
            stored = TaskFile.objects.get(id=vid).stored
            self.assertEqual(self.nikki_client.delete(f"/api/v1/files/{vid}/").status_code, 204)
            self.assertFalse((filestore.files_dir() / stored).exists())
            self.assertEqual(TaskFile.objects.filter(shared_task=task).count(), 1)

    def test_a_personal_items_files_are_the_owners_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as store, override_settings(JARVIS_FILES_DIR=store):
            r = self.md_client.post("/api/v1/private-tasks/", {"title": "Passport renewal"}, format="json", HTTP_IDEMPOTENCY_KEY="f1")
            pid = r.json()["id"]
            r = self.md_client.post(f"/api/v1/private-tasks/{pid}/files/",
                                    {"file": SimpleUploadedFile("passport.pdf", b"%PDF-1.4 " + b"x" * 50, content_type="application/pdf")},
                                    format="multipart")
            self.assertEqual(r.status_code, 201, r.content)
            fid = r.json()["id"]
            self.assertEqual(self.pa_client.get(f"/api/v1/files/{fid}/").status_code, 404)
            self.assertEqual(self.pa_client.get(f"/api/v1/private-tasks/{pid}/files/").status_code, 404)
            self.assertEqual(self.md_client.get(f"/api/v1/files/{fid}/").status_code, 200)


# ------------------------------------------------------------------------------------------------ the voice forms
class LiveFormsTests(NineCase):
    def test_the_new_forms_become_their_intents(self):
        cmd = livevoice.cmd_for(self.nikki, "update_task", {"which_id": "abc", "which_words": "", "text": "Bank band tha", "said": "s"})
        self.assertEqual((cmd["intent"], cmd["target_refs"], cmd["note_text"]), ("update_task", ["abc"], "Bank band tha"))
        cmd = livevoice.cmd_for(self.nikki, "tick_step", {"which_id": "abc", "step_words": "hotel", "said": "s"})
        self.assertEqual((cmd["intent"], cmd["title"]), ("tick_step", "hotel"))
        cmd = livevoice.cmd_for(self.md, "approve", {"which_id": "abc", "approve": True, "note": "ok", "said": "s"})
        self.assertEqual((cmd["intent"], cmd["approve"], cmd["note_text"]), ("approve_item", True, "ok"))
        cmd = livevoice.cmd_for(self.nikki, "give_work", {"items": [{"to": "Sir", "what": "Vendor advance", "kind": "APPROVAL", "amount": 150000,
                                                                     "when_words": ""}], "said": "s"})
        self.assertEqual((cmd["intent"], cmd["kind"], cmd["amount"]), ("assign_to_person", "APPROVAL", 150000.0))
        show = next(t for t in livevoice.TOOLS if t.get("name") == "show")
        self.assertIn("approvals", show["parameters"]["properties"]["what"]["enum"])
        text = livevoice.instructions_for(self.md, services.ai_context(self.md))
        self.assertIn("update_task", text)
        self.assertIn("APPROVAL", text)
