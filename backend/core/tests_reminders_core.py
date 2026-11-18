"""Reminders, snooze and reschedule as separate things (Nov 2026): a snooze moves the reminder and never the
deadline, the third "later" earns a suggestion, a stale snooze cannot wake a finished task, and a reopened task
reminds afresh."""
from datetime import timedelta

from django.utils import timezone

from core import services
from core import timeutils as tu
from core.models import ReminderSnooze, SharedTask, Status, TaskEvent
from core.reminders import reminders_for
from core.tests import BaseCase


class SnoozeIsNotRescheduleTests(BaseCase):
    def setUp(self):
        super().setUp()
        # Work Sir gave the PA for later today at a fixed time (ahead of now, inside the reminder window).
        self.due = (timezone.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
        self.task = SharedTask.objects.create(title="Send the GST working", assignee_type=SharedTask.Assignee.USER,
                                              assignee_user=self.pa, origin=SharedTask.Origin.GIVEN, created_by=self.md,
                                              due_kind="TIMED", due_at=self.due, due_date=tu.to_local(self.due).date(),
                                              original_due_at=self.due, original_due_date=tu.to_local(self.due).date())

    def reminder_key(self, label="due"):
        items = reminders_for(self.pa, hours=6)["reminders"]
        return next(r["key"] for r in items if r["ref"]["id"] == str(self.task.id) and r["label"] == label)

    def test_a_snooze_moves_the_reminder_and_says_when_it_passes_the_deadline(self):
        key = self.reminder_key()
        # 20 minutes: still before the deadline, nothing to add.
        r = self.pa_client.post("/api/v1/reminders/snooze/", {"key": key, "minutes": 20}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["past_deadline"])
        self.assertEqual(r.json()["note"], "")
        self.task.refresh_from_db()
        self.assertEqual(self.task.due_at, self.due)                       # the deadline did not move
        self.assertEqual(self.task.events.filter(kind=TaskEvent.Kind.MOVED).count(), 0)
        # Past the deadline: said plainly, the deadline still stays.
        late = (self.due + timedelta(hours=3)).isoformat()
        r = self.pa_client.post("/api/v1/reminders/snooze/", {"key": key, "until": late}, format="json").json()
        self.assertTrue(r["past_deadline"])
        self.assertIn("after its deadline", r["note"])
        self.assertIn("the deadline itself stays", r["note"])
        self.assertIn("डेडलाइन वही रहेगी", r["note_hi"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.due_at, self.due)
        # The reminder itself now fires at the snoozed moment, under a fresh key.
        items = reminders_for(self.pa, hours=12)["reminders"]
        mine = [i for i in items if i["ref"]["id"] == str(self.task.id) and i["label"] == "due"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["fire_at"], late.replace("+00:00", "+00:00"))
        self.assertTrue(mine[0]["key"].startswith(key))

    def test_the_third_later_earns_a_suggestion_not_a_silent_move(self):
        key = self.reminder_key()
        notes = []
        for n in (1, 2, 3):
            r = self.pa_client.post("/api/v1/reminders/snooze/", {"key": f"{key}:s{n}", "minutes": 10 * n}, format="json").json()
            notes.append((r["snoozes"], r["note"]))
        self.assertEqual([s for s, _ in notes], [1, 2, 3])
        self.assertEqual(notes[0][1], "")
        self.assertEqual(notes[1][1], "")
        self.assertIn("put this off 3 times", notes[2][1])
        self.assertIn("another day, or mark it blocked", notes[2][1])
        self.task.refresh_from_db()
        self.assertEqual(self.task.due_at, self.due)

    def test_a_stale_snooze_cannot_wake_a_finished_task(self):
        key = self.reminder_key()
        # She finishes it; a "later" sent a moment too late (a slow phone) changes nothing and reminds nobody.
        r = self.pa_client.post(f"/api/v1/shared-tasks/{self.task.id}/complete/", {"expected_version": self.task.version}, format="json")
        self.assertIn(r.status_code, (200, 202), r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Status.COMPLETED)
        r = self.pa_client.post("/api/v1/reminders/snooze/", {"key": key, "minutes": 5}, format="json")
        self.assertEqual(r.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Status.COMPLETED)
        keys = [i["key"] for i in reminders_for(self.pa, hours=48)["reminders"]]
        self.assertFalse(any(str(self.task.id) in k for k in keys))
        # A completion is never undone by a snooze; the history says done, once.
        self.assertEqual(self.task.events.filter(kind=TaskEvent.Kind.DONE).count(), 1)

    def test_reopening_gives_a_fresh_reminder_and_a_history_step(self):
        self.pa_client.post(f"/api/v1/shared-tasks/{self.task.id}/complete/", {"expected_version": self.task.version}, format="json")
        self.task.refresh_from_db()
        r = self.pa_client.post(f"/api/v1/pa-tasks/{self.task.id}/status/",
                                {"status": "OPEN", "expected_version": self.task.version}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Status.OPEN)
        self.assertIsNone(self.task.completed_at)
        self.assertEqual([e.kind for e in self.task.events.order_by("at")][-2:], [TaskEvent.Kind.DONE, TaskEvent.Kind.REOPENED])
        keys = [i["key"] for i in reminders_for(self.pa, hours=6)["reminders"]]
        self.assertTrue(any(str(self.task.id) in k for k in keys))          # it reminds again at its time
        # The Done step kept the deadline it was finished against.
        done = self.task.events.filter(kind=TaskEvent.Kind.DONE).first()
        self.assertEqual(done.data.get("due"), self.due.isoformat())

    def test_a_reschedule_is_a_history_step_and_the_old_reminder_retires(self):
        before = reminders_for(self.pa, hours=48)["reminders"]
        old_keys = {i["key"] for i in before if i["ref"]["id"] == str(self.task.id)}
        self.assertTrue(old_keys)
        new_due = self.due + timedelta(days=1)
        r = self.md_client.patch(f"/api/v1/shared-tasks/{self.task.id}/",
                                 {"expected_version": self.task.version, "due_date": tu.to_local(new_due).date().isoformat(),
                                  "due_time": tu.to_local(new_due).time().strftime("%H:%M")}, format="json", HTTP_IDEMPOTENCY_KEY="move-1")
        self.assertIn(r.status_code, (200, 202), r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.original_due_at, self.due)               # the first date is kept
        self.assertTrue(self.task.events.filter(kind=TaskEvent.Kind.MOVED).exists())
        after = {i["key"] for i in reminders_for(self.pa, hours=48)["reminders"] if i["ref"]["id"] == str(self.task.id)}
        self.assertFalse(old_keys & after)                                   # the old moment's reminders are gone
        self.assertTrue(after)                                               # the new moment's are there
        self.assertEqual(ReminderSnooze.objects.count(), 0)
