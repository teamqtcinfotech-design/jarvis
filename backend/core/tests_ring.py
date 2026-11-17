"""Phase 2 of the team plan (17 Nov 2026): ring until seen.

Work given and messages sent ring the receiver's phone every few minutes until they tap Seen; the giver hears it was
seen; JARVIS asks "Shall I tell Sir when you'll do it?" and the answer ("kal shaam tak", "nahi ho payega, ...") goes
back to the giver; after 30 minutes unseen the giver is asked "ring again, or give it to someone else?"; every step is
kept in the task's history.
"""
import json
from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from core import conversation, promises, ringing, services
from core import timeutils as tu
from core.models import Alert, Kind, Priority, SharedTask, Status, TaskEvent
from core.tests import SECRET, BaseCase


class RingUntilSeenTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.sunil = services.team_add(self.md, {"name": "Sunil", "title": "Accountant", "department": "Accounts",
                                                 "gender": "male", "username": "sunil", "password": "sunil1"})
        self.kiran = services.team_add(self.md, {"name": "Kiran", "title": "Farm Manager", "department": "Farm",
                                                 "username": "kiran", "password": "kiran1"})
        # Time-of-day independent: nobody is in quiet hours while the tests run (one test sets them on purpose).
        # Rings every 5 minutes here (the default is 10), so a 30-minute story fits in six rings.
        for u in (self.md, self.pa, self.sunil, self.kiran):
            services.set_prefs(u, {"quietHoursEnabled": False, "ringEveryMinutes": 5})
        self.sunil_client = APIClient()
        self.sunil_client.force_authenticate(self.sunil)
        self.kiran_client = APIClient()
        self.kiran_client.force_authenticate(self.kiran)
        self.sent = []  # (username, data) for every push

        def fake_send(user, data, urgent=False):
            self.sent.append((user.username, dict(data)))
            return {"sent": 1, "removed": 0, "devices": 1}

        for p in (mock.patch("core.push.configured", return_value=True),
                  mock.patch("core.push.send_to_user", side_effect=fake_send),
                  mock.patch("core.push.notify_alert")):
            p.start()
            self.addCleanup(p.stop)

    def give(self, title="Send the GST file", priority=Priority.NORMAL, to=None, by=None, **extra):
        task = services.create_person_task(by or self.md, to or self.sunil,
                                           {"title": title, "kind": Kind.TASK, "priority": priority, **extra},
                                           compose_note=False)
        alert = Alert.objects.get(ref_type="shared_task", ref_id=str(task.id), kind=Alert.Kind.TASK_ASSIGNED,
                                  acknowledged_at__isnull=True)
        return task, alert

    def pushes(self, kind, to=None):
        return [d for u, d in self.sent if d.get("type") == kind and (to is None or u == to)]

    # ---------------------------------------------------------------- ringing
    def test_given_work_rings_every_ten_minutes_urgent_every_minute_and_low_not_at_all(self):
        self.assertEqual(services.pref_defaults()["ringEveryMinutes"], 10)
        _task, alert = self.give()
        self.assertTrue(alert.ring)
        self.assertEqual(alert.rings, 1)  # the alert itself is the first ring
        self.assertAlmostEqual((alert.next_ring_at - alert.ring_started_at).total_seconds(), 300, delta=2)  # 5 here (setUp)
        _t, urgent = self.give("Pay the electricity bill", Priority.URGENT)
        self.assertAlmostEqual((urgent.next_ring_at - urgent.ring_started_at).total_seconds(), 60, delta=2)
        _t, low = self.give("Sort the old files", Priority.LOW)
        self.assertFalse(low.ring)
        # The push of the alert itself carries the ring and the Seen button.
        from core import push
        data = push.payload_for(alert)
        self.assertEqual((data["ring"], data["seen_label"]), ("1", "Seen"))
        # Someone who switched ringing off gets it once, as before.
        services.set_prefs(self.kiran, {"ringUntilSeen": False})
        _t, quiet = self.give("Count the cattle", to=self.kiran)
        self.assertFalse(quiet.ring)

    def test_it_rings_again_until_seen_then_the_giver_is_asked(self):
        task, alert = self.give()
        for n in range(2, 7):  # rings 2..6 at 5, 10, 15, 20, 25 minutes
            at = alert.ring_started_at + timedelta(minutes=5 * (n - 1), seconds=1)
            out = ringing.run(at)
            self.assertEqual(out["rung"], 1, n)
            alert.refresh_from_db()
            self.assertEqual(alert.rings, n)
        rings = self.pushes("ring", "sunil")
        self.assertEqual(len(rings), 5)
        self.assertEqual(rings[-1]["count"], "1")
        self.assertEqual(json.loads(rings[-1]["items"])[0]["ring_no"], 6)
        self.assertIn("Send the GST file", rings[-1]["body"])
        # 30 minutes: it stops and the MD is asked.
        out = ringing.run(alert.ring_started_at + timedelta(minutes=30, seconds=2))
        self.assertEqual(out["given_up"], 1)
        alert.refresh_from_db()
        self.assertIsNone(alert.next_ring_at)
        self.assertEqual(self.pushes("ring_stop", "sunil")[-1]["id"], str(alert.id))
        unseen = Alert.objects.get(recipient=self.md, ref_type="unseen", ref_id=str(alert.id))
        self.assertIn("Sunil hasn't seen it yet: Send the GST file", unseen.spoken_text)
        self.assertEqual(services.alert_question(unseen)["kind"], "unseen")
        self.assertIn("give it to someone else", services.alert_question(unseen)["en"])
        self.assertTrue(TaskEvent.objects.filter(task=task, kind="NOT_SEEN").exists())

    def test_everything_waiting_rings_together_as_one_ting(self):
        t1, a1 = self.give("Send the GST file")
        t2, a2 = self.give("Call the auditor", Priority.HIGH, by=self.kiran)
        self.sent.clear()
        # a1 is due; a2 (a minute younger, not yet due) rings with it: one push, both counted, clocks aligned.
        Alert.objects.filter(id=a2.id).update(next_ring_at=a1.next_ring_at + timedelta(minutes=1))
        out = ringing.run(a1.next_ring_at + timedelta(seconds=1))
        self.assertEqual((out["rung"], out["pushes"]), (2, 1))
        push_ = self.pushes("ring", "sunil")[-1]
        items = json.loads(push_["items"])
        self.assertEqual(push_["count"], "2")
        self.assertEqual([i["title"] for i in items][0], "New task from Kiran")  # the HIGH one first
        self.assertIn("2 waiting for you", push_["title"])
        self.assertIn("Sir: ", push_["body"])
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertEqual((a1.rings, a2.rings), (2, 2))
        self.assertEqual(a1.next_ring_at, a2.next_ring_at)
        # An urgent item among them: every minute for all of it; once it is seen, back to the usual interval.
        _t3, a3 = self.give("Server is down", Priority.URGENT, by=self.kiran)
        out = ringing.run(a1.next_ring_at + timedelta(seconds=1))
        self.assertEqual(out["pushes"], 1)
        a1.refresh_from_db()
        a3.refresh_from_db()
        self.assertEqual(a1.next_ring_at, a3.next_ring_at)
        at = a1.next_ring_at - timedelta(minutes=1)  # the pass that just ran
        self.assertAlmostEqual((a3.next_ring_at - at).total_seconds(), 60, delta=2)
        services.mark_seen(self.sunil, Alert.objects.get(id=a3.id))
        at = a1.next_ring_at + timedelta(seconds=1)
        out = ringing.run(at)
        self.assertEqual(out["stopped"], 1)  # a3's ringing stopped on his phones
        a1.refresh_from_db()
        self.assertAlmostEqual((a1.next_ring_at - at).total_seconds(), 300, delta=2)
        self.assertTrue(TaskEvent.objects.filter(task=t1).exists() and TaskEvent.objects.filter(task=t2).exists())

    def test_quiet_hours_hold_a_non_urgent_ring_but_not_an_urgent_one(self):
        hour = tu.now_local().hour
        services.set_prefs(self.sunil, {"quietHoursEnabled": True, "quietStartHour": hour, "quietEndHour": (hour + 2) % 24})
        _t, alert = self.give()
        self.assertEqual(alert.rings, 0)  # delivered once, quietly; no ring until the quiet hours end
        self.assertEqual(tu.to_local(alert.next_ring_at).hour, (hour + 2) % 24)
        from core import push
        self.assertEqual(push.payload_for(alert)["ring"], "")
        _t, urgent = self.give("Server is down", Priority.URGENT)
        self.assertEqual(urgent.rings, 1)

    # ---------------------------------------------------------------- seen
    def test_seen_stops_the_ringing_tells_the_giver_and_asks_by_when(self):
        task, alert = self.give()
        r = self.sunil_client.post(f"/api/v1/alerts/{alert.id}/seen/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["question"]["kind"], "promise")
        self.assertIn("Shall I tell Sir when you'll do it", body["question"]["en"])
        alert.refresh_from_db()
        task.refresh_from_db()
        self.assertIsNotNone(alert.seen_at)
        self.assertIsNone(alert.acknowledged_at)  # stays until "by when" is answered
        self.assertIsNotNone(task.seen_at)
        note = Alert.objects.get(recipient=self.md, ref_type="seen")
        self.assertEqual(note.title, "Sunil has seen it")
        # The next pass takes the ringing notification off his phones.
        out = ringing.run()
        self.assertEqual(out["stopped"], 1)
        self.assertEqual(self.pushes("ring_stop", "sunil")[-1]["id"], str(alert.id))
        self.assertEqual(ringing.run(alert.next_ring_at or timezone.now() + timedelta(hours=1))["rung"], 0)
        # Seen twice: nothing more.
        self.sunil_client.post(f"/api/v1/alerts/{alert.id}/seen/", {}, format="json")
        self.assertEqual(Alert.objects.filter(recipient=self.md, ref_type="seen").count(), 1)
        # The phone's list says so.
        listed = next(a for a in self.sunil_client.get("/api/v1/alerts/").json()["results"] if a["id"] == str(alert.id))
        self.assertEqual((listed["ring"], listed["ringing"]), (False, False))
        self.assertIsNotNone(listed["seen_at"])

    def test_got_it_on_a_ringing_message_is_seen_and_no_means_nobody_is_told(self):
        services.send_message_to(self.md, [self.sunil, self.kiran], "Office closed tomorrow")
        a1 = Alert.objects.get(recipient=self.sunil, ref_type="message")
        a2 = Alert.objects.get(recipient=self.kiran, ref_type="message")
        self.assertTrue(a1.ring and a2.ring)
        self.sunil_client.post(f"/api/v1/alerts/{a1.id}/ack/", {}, format="json")  # "Got it"
        self.kiran_client.post(f"/api/v1/alerts/{a2.id}/ack/", {"seen": False}, format="json")  # "no, don't tell"
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertIsNotNone(a1.seen_at)
        self.assertIsNotNone(a2.seen_at)
        told = Alert.objects.filter(recipient=self.md, ref_type="seen")
        self.assertEqual([t.ref_id for t in told], [str(a1.id)])
        self.assertIsNone(services.alert_question(a1))  # seen: no more "shall I tell Sir you've seen it?"

    def test_starting_the_work_counts_as_seen(self):
        task, alert = self.give()
        r = self.sunil_client.post(f"/api/v1/pa-tasks/{task.id}/status/", {"status": "IN_PROGRESS", "expected_version": task.version},
                                   format="json")
        self.assertEqual(r.status_code, 200, r.content)
        alert.refresh_from_db()
        self.assertIsNotNone(alert.seen_at)
        self.assertEqual(ringing.run()["stopped"], 1)
        kinds = [e["kind"] for e in services.task_history(SharedTask.objects.get(id=task.id))]
        self.assertEqual(kinds, ["GIVEN", "SEEN", "STARTED"])

    def test_done_or_withdrawn_stops_the_ringing(self):
        task, alert = self.give()
        services.update_shared_md_task(self.md, task.id, {"status": Status.CANCELLED}, task.version)
        self.assertEqual(ringing.run()["stopped"], 1)
        self.assertEqual(self.pushes("ring_stop", "sunil")[-1]["id"], str(alert.id))

    # ---------------------------------------------------------------- by when / can't do it
    def answer(self, client, alert, text):
        r = client.post(f"/api/v1/alerts/{alert.id}/answer/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_kal_shaam_tak_is_kept_on_the_task_and_the_giver_hears_it(self):
        task, alert = self.give()
        out = self.answer(self.sunil_client, alert, "kal shaam tak kar dunga")
        self.assertTrue(out["handled"])
        self.assertIn("I've told Sir: by tomorrow at 6 PM", out["show"])
        task.refresh_from_db()
        tomorrow = tu.today_local() + timedelta(days=1)
        self.assertEqual(task.promise_date, tomorrow)
        self.assertEqual(tu.to_local(task.promise_at).hour, 18)
        self.assertEqual((task.due_kind, task.due_date), ("TIMED", tomorrow))  # it had no deadline: now it has
        self.assertIsNotNone(task.seen_at)
        alert.refresh_from_db()
        self.assertIsNotNone(alert.acknowledged_at)
        note = Alert.objects.get(recipient=self.md, title__startswith="Sunil will do it")
        self.assertIn("Sunil has seen Send the GST file and will do it by tomorrow at 6 PM", note.spoken_text)
        self.assertEqual(note.priority, Priority.NORMAL)  # spoken
        self.assertFalse(Alert.objects.filter(recipient=self.md, ref_type="seen").exists())  # one line, not two
        item = services.shared_item(task)
        self.assertEqual(item["promise_label"], "tomorrow at 6 PM")
        kinds = [e["kind"] for e in self.md_client.get(f"/api/v1/shared-tasks/{task.id}/history/").json()["results"]]
        self.assertEqual(kinds, ["GIVEN", "SEEN", "PROMISED"])
        self.assertIsNone(services.alert_question(alert))  # promised: nothing more to ask

    def test_a_bare_yes_asks_by_when(self):
        task, alert = self.give()
        out = self.answer(self.sunil_client, alert, "haan")
        self.assertEqual(out["ask"]["en"], "By when?")
        self.assertTrue(Alert.objects.filter(recipient=self.md, ref_type="seen").exists())  # seen, told
        out = self.answer(self.sunil_client, alert, "2 ghante mein")
        self.assertTrue(out["handled"])
        self.assertIsNone(out["ask"])
        task.refresh_from_db()
        self.assertIsNotNone(task.promise_at)
        self.assertGreaterEqual(task.promise_at, timezone.now() + timedelta(minutes=115))
        note = Alert.objects.get(recipient=self.md, title__startswith="Sunil will do it")
        self.assertIn("Sunil will do Send the GST file by", note.spoken_text)

    def test_cant_do_it_goes_back_with_the_reason_and_the_giver_gives_it_to_someone_else(self):
        task, alert = self.give()
        out = self.answer(self.sunil_client, alert, "nahi ho payega kyunki bank band hai")
        self.assertIn("I've told Sir you can't do it", out["show"])
        task.refresh_from_db()
        self.assertEqual((task.status, task.hold_reason), (Status.BLOCKED, "bank band hai"))
        back = Alert.objects.get(recipient=self.md, title="Sunil can't do it")
        self.assertIn("Reason: bank band hai", back.spoken_text)
        self.assertEqual(services.alert_question(back)["kind"], "reassign")
        # Sir: "Kiran ko de do".
        out = self.answer(self.md_client, back, "haan Kiran ko de do")
        self.assertIn("given to Kiran", out["show"])
        task.refresh_from_db()
        self.assertEqual((task.assignee_user, task.status, task.seen_at, task.promise_date), (self.kiran, Status.OPEN, None, None))
        new = Alert.objects.get(recipient=self.kiran, kind=Alert.Kind.TASK_ASSIGNED, ref_id=str(task.id))
        self.assertTrue(new.ring and new.rings == 1)
        self.assertTrue(Alert.objects.filter(recipient=self.sunil, ref_type="reassigned", ref_id=str(task.id)).exists())
        back.refresh_from_db()
        self.assertIsNotNone(back.acknowledged_at)
        # Sunil no longer sees it; Kiran does; the history keeps it all.
        self.assertEqual(self.sunil_client.get(f"/api/v1/shared-tasks/{task.id}/history/").status_code, 404)
        lines = [e["text"] for e in self.kiran_client.get(f"/api/v1/shared-tasks/{task.id}/history/").json()["results"]]
        self.assertEqual(lines[0], "Given by Sir to Sunil")
        self.assertIn("Sunil can't do it: bank band hai", lines)
        self.assertIn("Sir gave it to Kiran instead of Sunil", lines)

    def test_no_to_someone_else_leaves_it_and_who_asks_again(self):
        task, alert = self.give()
        self.answer(self.sunil_client, alert, "nahi kar paunga")
        back = Alert.objects.get(recipient=self.md, title="Sunil can't do it")
        out = self.answer(self.md_client, back, "haan")
        self.assertEqual(out["ask"]["en"], "Who should get it?")
        out = self.answer(self.md_client, back, "Zubin ko de do")
        self.assertIn("i don't know anyone called zubin", out["show"].lower())
        self.assertEqual(out["ask"]["en"], "Who should get it?")
        out = self.answer(self.md_client, back, "rehne do")
        self.assertIn("it stays with Sunil", out["show"])
        task.refresh_from_db()
        self.assertEqual(task.assignee_user, self.sunil)

    def test_unseen_ring_again_or_give_it_to_someone_else(self):
        task, alert = self.give()
        ringing.run(alert.ring_started_at + timedelta(minutes=31))
        unseen = Alert.objects.get(recipient=self.md, ref_type="unseen")
        out = self.answer(self.md_client, unseen, "haan phir se ring karo")
        self.assertIn("ringing Sunil again", out["show"])
        alert.refresh_from_db()
        self.assertIsNone(alert.ring_started_at)
        before = alert.rings
        self.assertEqual(ringing.run()["rung"], 1)
        alert.refresh_from_db()
        self.assertEqual(alert.rings, before + 1)
        self.assertTrue(TaskEvent.objects.filter(task=task, kind="RING_AGAIN").exists())
        # Unseen again: this time Kiran gets it.
        ringing.run(alert.ring_started_at + timedelta(minutes=31))
        unseen = Alert.objects.filter(recipient=self.md, ref_type="unseen", acknowledged_at__isnull=True).get()
        out = self.answer(self.md_client, unseen, "Kiran ko de do")
        self.assertIn("given to Kiran", out["show"])
        task.refresh_from_db()
        self.assertEqual(task.assignee_user, self.kiran)
        # Sunil sees it later: nothing rings for him any more, and Sir hears nothing about it.
        alert.refresh_from_db()
        self.assertIsNotNone(alert.acknowledged_at)

    def test_seen_late_after_not_seen_is_spoken_to_the_giver(self):
        task, alert = self.give()
        ringing.run(alert.ring_started_at + timedelta(minutes=31))
        self.sunil_client.post(f"/api/v1/alerts/{alert.id}/seen/", {}, format="json")
        note = Alert.objects.get(recipient=self.md, ref_type="seen")
        self.assertEqual(note.priority, Priority.NORMAL)
        self.assertIn("Sunil has now seen: Send the GST file", note.spoken_text)
        unseen = Alert.objects.get(recipient=self.md, ref_type="unseen")
        self.assertIsNotNone(unseen.acknowledged_at)  # "not seen yet" is out of date

    def test_something_else_said_is_not_taken_as_an_answer(self):
        task, alert = self.give()
        with mock.patch("core.promises._read_ai", return_value=None):
            out = self.answer(self.sunil_client, alert, "what is the weather today")
        self.assertFalse(out["handled"])
        alert.refresh_from_db()
        self.assertIsNotNone(alert.seen_at)  # he heard it, though

    def test_the_closed_app_conversation_takes_the_answer(self):
        task, alert = self.give()
        about = {"kind": "alert", "id": str(alert.id), "title": alert.title, "question": "Shall I tell Sir when you'll do it?"}
        conversation.turn(self.sunil, "", event="ask", about=about)
        resp = conversation.turn(self.sunil, "parso tak")
        self.assertEqual(resp["action"], "saved")
        self.assertIn("I've told Sir", resp["show"])
        task.refresh_from_db()
        self.assertEqual(task.promise_date, tu.today_local() + timedelta(days=2))
        # A bare yes keeps JARVIS listening for the time.
        task2, alert2 = self.give("Book the hall")
        conversation.turn(self.sunil, "", event="ask", about={**about, "id": str(alert2.id)})
        resp = conversation.turn(self.sunil, "haan")
        self.assertEqual((resp["show"], resp["action"]), ("By when?", "asking"))
        resp = conversation.turn(self.sunil, "kal subah")
        self.assertIn("I've told Sir", resp["show"])
        task2.refresh_from_db()
        self.assertEqual(tu.to_local(task2.promise_at).hour, 10)

    def test_later_on_a_ringing_alert_counts_as_seen(self):
        _task, alert = self.give()
        services.snooze_alert(self.sunil, alert.id, timezone.now() + timedelta(minutes=15))
        alert.refresh_from_db()
        self.assertIsNotNone(alert.seen_at)
        self.assertTrue(Alert.objects.filter(recipient=self.md, ref_type="seen").exists())

    # ---------------------------------------------------------------- history and privacy
    def test_history_of_older_work_and_who_may_read_it(self):
        task, _alert = self.give(by=self.sunil, to=self.kiran)
        TaskEvent.objects.filter(task=task).delete()  # as if it were made before the history existed
        services.update_task_status(self.kiran, task.id, Status.COMPLETED, task.version)
        lines = self.md_client.get(f"/api/v1/shared-tasks/{task.id}/history/").json()["results"]
        self.assertEqual([e["kind"] for e in lines], ["GIVEN", "SEEN", "DONE"])
        self.assertEqual(lines[0]["text"], "Given by Sunil to Kiran")
        self.assertEqual(lines[0]["text_hi"], "Sunil ने Kiran को दिया")
        # The PA neither gave it nor does it: not even its history.
        self.assertEqual(self.pa_client.get(f"/api/v1/shared-tasks/{task.id}/history/").status_code, 404)
        self.assertNotIn(SECRET, json.dumps(lines, ensure_ascii=False))

    def test_moving_a_task_is_in_its_history(self):
        task, _alert = self.give()
        day = tu.today_local() + timedelta(days=3)
        services.update_shared_md_task(self.md, task.id, {"due_date": day}, task.version)
        last = services.task_history(SharedTask.objects.get(id=task.id))[-1]
        self.assertEqual(last["kind"], "MOVED")
        self.assertTrue(last["text"].startswith("Moved to ") and last["text"].endswith("by Sir"))


class AnswerReadingTests(BaseCase):
    """The rule reading (the AI is off in tests): what people actually say."""

    def read(self, text, kind="promise"):
        return promises._read_rules(text, kind, self.md)

    def test_times(self):
        r = self.read("kal shaam tak")
        self.assertEqual((r["answer"], r["due_date"], r["due_time"]),
                         ("time", (tu.today_local() + timedelta(days=1)).isoformat(), "18:00"))
        self.assertEqual(self.read("aaj tak kar dungi")["due_time"], "19:00")
        self.assertEqual(self.read("2 ghante mein")["in_minutes"], 120)
        self.assertEqual(self.read("aadhe ghante me")["in_minutes"], 30)
        self.assertEqual(self.read("abhi karta hoon")["in_minutes"], 15)
        self.assertEqual(self.read("Friday tak")["answer"], "time")

    def test_cannot_and_reasons(self):
        r = self.read("nahi ho payega because the bank is closed")
        self.assertEqual((r["answer"], r["reason"]), ("cannot", "the bank is closed"))
        self.assertEqual(self.read("I can't do it")["answer"], "cannot")
        self.assertEqual(self.read("mere se nahi hoga")["answer"], "cannot")

    def test_yes_no_and_names(self):
        self.assertEqual(self.read("haan bata do")["answer"], "yes")
        self.assertEqual(self.read("nahi")["answer"], "no")
        self.assertEqual(self.read("haan", "unseen")["answer"], "yes")
        self.assertEqual(self.read("phir se ring karo", "unseen")["answer"], "yes")
        self.assertEqual(self.read("rehne do", "reassign")["answer"], "no")
        self.assertEqual(self.read("leave it", "reassign")["answer"], "no")
        r = self.read("haan PA ko de do", "reassign")
        self.assertEqual(r["answer"], "give")
        self.assertEqual(services.find_people(r["person"])["people"], [self.pa])

    def test_the_when_of_an_answer(self):
        now = tu.now_local()
        past = (now - timedelta(hours=1)).strftime("%H:%M")
        day, at = promises._when({"due_date": now.date().isoformat(), "due_time": past, "in_minutes": -1})
        if now.hour >= 1:  # "11 baje tak" said at noon means tomorrow
            self.assertEqual(day, now.date() + timedelta(days=1))
        self.assertIsNone(promises._when({"due_date": "", "due_time": "", "in_minutes": -1}))
        day, at = promises._when({"in_minutes": 0})
        self.assertEqual(at.minute % 5, 0)
        self.assertGreaterEqual(at, now + timedelta(minutes=14))
