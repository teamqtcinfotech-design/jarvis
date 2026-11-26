"""seed_tasks (26 Nov 2026): demo work from his task export, laid over the test days to named staff and to the MD."""
import json
import os
import tempfile
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from core import timeutils as tu
from core.models import IntegrationCommand, Kind, PrivateTask, SharedTask, Status
from core.tests import BaseCase

SUBJECTS = ["GST no. ke CA sir se update leni hai", "Tour & travels ka letter head banvana hai", "MG hector ka ek challan bharna hai",
            "Partner removal agreement check karna hai panel pe", "Zoom ka link franchise panel pe show hona chahiye",
            "SBI ka OTP mail ke through receive hona chahiye", "5 Cr loan ke liye salary slip ki baat karni hai",
            "FIM ki list ready karke franchise partners ko bhejni hai", "Appointment letter bhijwana hai", "Demat format update karna hai",
            "Software point ki update daily milni chahiye", "Pan drive data save karna hai", "demo", "test meeting", "short",
            "Done already once", "Customer care ke email replies", "Rohtash sir ka task bar banvana hai"]


class SeedTasksTests(BaseCase):
    def setUp(self):
        super().setUp()
        rows = [{"subject": s, "status": "Completed" if s == "Done already once" else "Not Started",
                 "priority": ["Low", "Medium", "High", "Urgent", "Extreme"][i % 5], "estimated_hours": "2.00" if i % 3 == 0 else None}
                for i, s in enumerate(SUBJECTS)]
        export = [{"type": "header", "version": "5.2"}, {"type": "database", "name": "tms"}, {"type": "table", "name": "tasks", "data": rows}]
        fd, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(export, fh, ensure_ascii=False)

    def tearDown(self):
        os.unlink(self.path)
        super().tearDown()

    def run_seed(self, *args):
        out = StringIO()
        call_command("seed_tasks", self.path, *args, stdout=out)
        return out.getvalue()

    def test_the_plan_only_writes_nothing(self):
        before = (PrivateTask.objects.count(), SharedTask.objects.count())
        out = self.run_seed("--staff", "Ramesh Kumar:2,Anil Verma:2", "--me", "3", "--dry-run")
        self.assertIn("(plan only)", out)
        self.assertIn("Ramesh Kumar", out)
        self.assertIn("LATE", out)  # yesterday's item
        self.assertEqual((PrivateTask.objects.count(), SharedTask.objects.count()), before)

    def test_work_lands_on_the_right_days_people_and_list(self):
        today = tu.today_local()
        out = self.run_seed("--staff", "Ramesh Kumar:3,Anil Verma:2", "--me", "4")
        self.assertIn("5 for staff", out)
        staff = SharedTask.objects.filter(assignee_type=SharedTask.Assignee.STAFF).select_related("staff")
        self.assertEqual(staff.count(), 5)
        self.assertEqual(sorted(t.staff.name for t in staff), ["Anil Verma", "Anil Verma", "Ramesh Kumar", "Ramesh Kumar", "Ramesh Kumar"])
        ramesh = sorted(t.due_date for t in staff if t.staff.name == "Ramesh Kumar")
        self.assertEqual(ramesh, [today - timedelta(days=1), today, today + timedelta(days=1)])  # yesterday (late), today, tomorrow
        self.assertTrue(all(t.status == Status.OPEN for t in staff))
        self.assertTrue(all(IntegrationCommand.objects.filter(task=t).exists() for t in staff))  # through the gateway
        mine = PrivateTask.objects.filter(owner=self.md).exclude(id=self.private.id)
        tasks = [t for t in mine if t.kind == Kind.TASK]
        meetings = [t for t in mine if t.kind == Kind.MEETING]
        self.assertEqual(len(tasks), 4)
        self.assertEqual(sorted(t.due_date for t in tasks), [today - timedelta(days=1), today, today + timedelta(days=1), today + timedelta(days=2)])
        self.assertEqual(len(meetings), 6)
        self.assertTrue(all(m.title.startswith("Meeting: ") and m.due_at is not None for m in meetings))
        self.assertTrue(all(m.due_at > tu.now_local() for m in meetings))  # none born already over
        self.assertTrue(any(m.due_date == today + timedelta(days=30) for m in meetings))  # next month
        titles = {t.title for t in list(staff) + tasks}
        self.assertEqual(len(titles), 9)  # every title once
        for bad in ("demo", "test meeting", "short", "Done already once"):
            self.assertNotIn(bad, titles)

    def test_an_unknown_or_ambiguous_name_is_refused_with_the_directory(self):
        with self.assertRaises(CommandError) as err:
            self.run_seed("--staff", "Nobody:2")
        self.assertIn("The directory has:", str(err.exception))
        with self.assertRaises(CommandError):
            self.run_seed("--staff", "Ramesh:2")  # fits Ramesh Kumar and Ramesh Yadav
        self.assertFalse(SharedTask.objects.filter(assignee_type=SharedTask.Assignee.STAFF).exists())
