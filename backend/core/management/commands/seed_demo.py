"""Create the MD and PA accounts plus synthetic demo data (never real client data).

    python manage.py seed_demo                 # first time: creates users, prints passwords, adds demo data
    python manage.py seed_demo --reset-tasks   # wipe demo tasks and recreate them (a full, realistic week)
    python manage.py seed_demo --accounts-only # live server: accounts only, no demo data

The demo data covers every screen and report: a day with meetings and gaps, late items, finished items,
Mansi's tasks in each state, requests waiting / accepted / declined, and staff work across departments
(in TaskPanel test mode the staff tasks move on by themselves: Working after 2 minutes, Done after 5).
"""
import secrets
import string
from datetime import time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core import services
from core import timeutils as tu
from core.models import (
    Alert, CommandReceipt, EventInbox, IntegrationCommand, Kind, Priority, PrivateTask, ReminderAck, SharedTask,
    StaffMember, Status, SyncCursor, TaskPanelLink, TaskRequest, User,
)
from core.taskpanel.client import TaskPanelError


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(14))


class Command(BaseCommand):
    help = "Create MD/PA accounts and synthetic demo tasks."

    def add_arguments(self, parser):
        parser.add_argument("--reset-tasks", action="store_true", help="Delete all tasks/requests/alerts first.")
        parser.add_argument("--accounts-only", action="store_true",
                            help="Create the MD and PA accounts only (no demo tasks). Use on the live server.")
        parser.add_argument("--pa-name", default="Mansi")
        parser.add_argument("--md-password", default="")
        parser.add_argument("--pa-password", default="")

    @transaction.atomic
    def handle(self, *args, **opts):
        created = []
        md = User.objects.filter(role=User.Role.MD).first()
        if not md:
            pw = opts["md_password"] or _password()
            md = User.objects.create_user(username="md", password=pw, role=User.Role.MD, display_name="Sir")
            created.append(("md", pw))
        pa = User.objects.filter(role=User.Role.PA).first()
        if not pa:
            pw = opts["pa_password"] or _password()
            pa = User.objects.create_user(username="pa", password=pw, role=User.Role.PA,
                                          display_name=opts["pa_name"], first_name=opts["pa_name"])
            created.append(("pa", pw))

        if opts["reset_tasks"]:
            for model in (Alert, TaskRequest, IntegrationCommand, EventInbox, SharedTask, PrivateTask, ReminderAck,
                          CommandReceipt):
                model.objects.all().delete()
            SyncCursor.objects.all().delete()

        mock = settings.TASKPANEL_MODE != "live"
        if mock:
            # Test-mode TaskPanel identities (real ones are linked with `manage.py taskpanel_link`).
            for user, key, name in ((md, "staff_001", "Managing Director"), (pa, "staff_002", opts["pa_name"])):
                TaskPanelLink.objects.get_or_create(
                    user=user, defaults={"actor_key": key, "actor_name": name, "grant": "mock-" + secrets.token_urlsafe(24)}
                )
        try:
            services.sync_staff(md)
        except TaskPanelError as err:
            self.stdout.write(self.style.WARNING(f"  TaskPanel directory not loaded: {err.message}"))

        if opts["accounts_only"]:
            self.stdout.write(self.style.SUCCESS("Accounts ready (no demo tasks created)."))
            for username, pw in created:
                self.stdout.write(self.style.WARNING(f"  login: {username}   password: {pw}   (shown once — store it safely)"))
            return

        if not PrivateTask.objects.exists():
            self._private(md)
        if not SharedTask.objects.filter(assignee_type=SharedTask.Assignee.PA).exists():
            self._pa_work(md, pa)
        if not TaskRequest.objects.exists():
            self._requests(md, pa)
        if mock and not SharedTask.objects.filter(assignee_type=SharedTask.Assignee.STAFF).exists():
            self._staff(md, pa)
        self._routine(md, pa)
        # Alerts raised while seeding are history, not news: nobody needs them read out.
        Alert.objects.filter(acknowledged_at__isnull=True).update(acknowledged_at=timezone.now())

        self.stdout.write(self.style.SUCCESS("Demo data ready (synthetic)."))
        for username, pw in created:
            self.stdout.write(self.style.WARNING(f"  login: {username}   password: {pw}   (shown once — store it safely)"))
        if not created:
            self.stdout.write("  Existing MD/PA accounts kept. Change passwords in /admin if needed.")

    # ------------------------------------------------------------------ Sir's own diary
    def _private(self, md):
        now = tu.now_local()
        today = now.date()
        soon = (now + timedelta(minutes=45)).replace(second=0, microsecond=0)
        later = (now + timedelta(hours=2, minutes=15)).replace(second=0, microsecond=0)

        def add(title, kind, priority, due_date=None, due_time=None, minutes=None, done_at=None, notes=""):
            data = {"title": title, "kind": kind, "priority": priority, "notes": notes}
            if due_date:
                data["due_date"] = due_date
            if due_time:
                data["due_time"] = due_time
            if minutes:
                data["duration_minutes"] = minutes
            t = services.create_private_task(md, data)
            if done_at:
                PrivateTask.objects.filter(id=t.id).update(status=Status.COMPLETED, completed_at=done_at)
            return t

        # Today: a real day with meetings, calls, a payment and gaps.
        add("Sharma Franchise quarterly review", Kind.MEETING, Priority.URGENT, soon.date(), soon.time(), 45,
            notes="Pune and Nagpur numbers; renewal terms.")
        add("Call Mehta ji – Pune Franchise", Kind.CALL, Priority.HIGH, later.date(), later.time(), 20)
        add("Pay GST advance instalment (reminder only)", Kind.PAYMENT, Priority.HIGH, today, time(17, 0))
        add("Sign Indore vendor agreement", Kind.TASK, Priority.NORMAL, today)
        # Coming up
        add("Doctor appointment – Dr Joshi", Kind.MEETING, Priority.NORMAL, today + timedelta(days=1), time(9, 30), 30)
        add("Call Verma ji – Nagpur dealership", Kind.CALL, Priority.NORMAL, today + timedelta(days=1), time(11, 30))
        add("Approve office interiors quote", Kind.TASK, Priority.NORMAL, today + timedelta(days=2))
        add("Board meeting – Q4 plan", Kind.MEETING, Priority.HIGH, today + timedelta(days=4), time(11, 0), 120)
        add("Bank RM call – FD renewal", Kind.CALL, Priority.NORMAL, today + timedelta(days=6), time(12, 0))
        add("Draft message for Diwali staff bonus", Kind.TASK, Priority.LOW)
        # Late
        add("Renew club membership", Kind.TASK, Priority.LOW, today - timedelta(days=1))
        add("Send gift to Kapoor ji", Kind.TASK, Priority.NORMAL, today - timedelta(days=3))
        # Finished
        add("Call CA about the audit", Kind.CALL, Priority.NORMAL, today, time(10, 0),
            done_at=tu.combine_local(today, time(10, 5)))
        add("Approve Diwali bonus list", Kind.TASK, Priority.HIGH, today - timedelta(days=1),
            done_at=tu.combine_local(today - timedelta(days=1), time(16, 10)))
        add("Review franchise brochure", Kind.TASK, Priority.NORMAL, today - timedelta(days=3),
            done_at=tu.combine_local(today - timedelta(days=3), time(12, 40)))

    # ------------------------------------------------------------------ work given to Mansi
    def _pa_work(self, md, pa):
        today = tu.today_local()
        name = pa.spoken_name

        def give(title, priority, due_date=None, due_time=None, status=None, done_at=None, hold="", desc=""):
            data = {"title": title, "kind": Kind.TASK, "priority": priority, "description": desc or title}
            if due_date:
                data["due_date"] = due_date
            if due_time:
                data["due_time"] = due_time
            t = services.create_pa_task(md, data)
            patch = {}
            if status:
                patch["status"] = status
            if done_at:
                patch["completed_at"] = done_at
            if hold:
                patch["hold_reason"] = hold
            if patch:
                SharedTask.objects.filter(id=t.id).update(**patch)
            return t

        give("Book Pune flights for Friday", Priority.HIGH, today, status=Status.COMPLETED,
             done_at=tu.combine_local(today, time(11, 20)), desc=f"{name} ko bolo Friday ki Pune flights book kare")
        give("Courier the signed vendor contract", Priority.NORMAL, today, time(15, 0), status=Status.IN_PROGRESS)
        give("Collect quotations for office interiors", Priority.NORMAL, today + timedelta(days=2), status=Status.BLOCKED,
             hold="Waiting for two vendors to reply")
        give("Arrange Sharma ji's lunch meeting at Marriott", Priority.NORMAL, today + timedelta(days=1), time(13, 0))
        give("Prepare Q4 travel calendar", Priority.LOW, today + timedelta(days=5))
        give("Renew car insurance", Priority.NORMAL, today - timedelta(days=2), status=Status.COMPLETED,
             done_at=tu.combine_local(today - timedelta(days=2), time(17, 30)))
        services.create_pa_task(md, {"title": "Weekly review", "kind": Kind.MEETING, "priority": Priority.NORMAL,
                                     "due_date": today + timedelta(days=1), "due_time": time(10, 0),
                                     "duration_minutes": 30, "description": f"{name} ke saath weekly review"},
                                meeting_with_pa=True)

    # ------------------------------------------------------------------ requests from Mansi to Sir
    def _requests(self, md, pa):
        today = tu.today_local()
        services.create_md_request(pa, {
            "title": "Sign vendor agreement for Indore outlet", "kind": Kind.TASK, "priority": Priority.HIGH,
            "due_date": today + timedelta(days=1), "due_time": time(12, 0),
            "notes": "Legal has cleared the final draft.", "reason": "Vendor needs it before Friday.",
        })
        services.create_md_request(pa, {
            "title": "Meeting with Kavita – franchise expansion", "kind": Kind.MEETING, "priority": Priority.NORMAL,
            "due_date": today + timedelta(days=2), "due_time": time(11, 0), "duration_minutes": 45,
            "reason": "She has three new city proposals ready.",
        })
        accepted = services.create_md_request(pa, {
            "title": "Approve updated HR leave policy", "kind": Kind.TASK, "priority": Priority.NORMAL,
            "due_date": today + timedelta(days=3), "reason": "HR wants it out before the month end.",
        })
        services.accept_request(md, accepted.id)
        declined = services.create_md_request(pa, {
            "title": "Attend vendor fair in Mumbai", "kind": Kind.MEETING, "priority": Priority.LOW,
            "due_date": today + timedelta(days=9), "reason": "Invitation from the organisers.",
        })
        services.decline_request(md, declined.id, note="Send Anil instead.")

    # ------------------------------------------------------------------ staff work (TaskPanel test mode)
    def _staff(self, md, pa):
        today = tu.today_local()
        now = timezone.now()
        by_name = {s.name: s for s in StaffMember.objects.filter(active=True)}

        def give(user, person, title, priority, category, due_date=None, due_time=None, age_minutes=0, desc=""):
            staff = by_name.get(person)
            if not staff:
                return None
            data = {"title": title, "kind": Kind.TASK, "priority": priority}
            if due_date:
                data["due_date"] = due_date
            if due_time:
                data["due_time"] = due_time
            try:
                task, _cmd = services.delegate_to_staff(user, data, staff, category_key=category, description=desc or title)
            except Exception as err:  # noqa: BLE001 — demo data must never stop the seeding
                self.stdout.write(self.style.WARNING(f"  skipped staff task '{title}': {err}"))
                return None
            if age_minutes:
                # Older tasks: test-mode TaskPanel reports them Working after 2 minutes and Done after 5.
                SharedTask.objects.filter(id=task.id).update(created_at=now - timedelta(minutes=age_minutes))
            return task

        give(md, "Ramesh Kumar", "Submit October GST reconciliation", Priority.HIGH, "accounts",
             today + timedelta(days=1), time(16, 0), desc="Ramesh Kumar ko bolo kal 4 baje tak October ka GST reconciliation submit kare.")
        give(md, "Ramesh Kumar", "Reconcile vendor payments for September", Priority.NORMAL, "accounts",
             today - timedelta(days=3), age_minutes=2 * 24 * 60)   # finished late
        give(md, "Anil Verma", "Send Q4 sales projections", Priority.HIGH, "sales", today - timedelta(days=2))  # late, not done
        give(md, "Suresh Patel", "Fix dispatch delays at Nagpur hub", Priority.URGENT, "general",
             today, time(18, 0), age_minutes=6 * 60)              # done today, on time
        give(md, "Kavita Singh", "Prepare franchise expansion deck", Priority.NORMAL, "franchise", today + timedelta(days=5))
        give(md, "QTC Developer", "Fix login issue on TaskPanel", Priority.HIGH, "login", today + timedelta(days=1), time(12, 0))
        give(md, "Pooja Mehra", "Call back 12 pending customer complaints", Priority.NORMAL, "profile_change",
             today, time(17, 30), age_minutes=3 * 24 * 60)        # done days ago
        give(pa, "Neha Gupta", "Shortlist candidates for Indore store manager", Priority.NORMAL, "hr",
             today + timedelta(days=3), desc="Neha ko bolo Indore store manager ke liye candidates shortlist kare.")
        give(pa, "Kavita Singh", "Book venue for the annual franchise meet", Priority.NORMAL, "franchise",
             today + timedelta(days=12), age_minutes=26 * 60)     # done
        give(pa, "Anil Verma", "Share dealer list for Nagpur", Priority.LOW, "sales", today - timedelta(days=1),
             age_minutes=3 * 24 * 60)                              # finished late
        # Let test-mode TaskPanel report the progress of the older ones straight away.
        for user in (md, pa):
            try:
                services.refresh_delegations(user)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ routine (for free-slot suggestions)
    def _routine(self, md, pa):
        prefs = services.get_prefs(md)
        if not prefs.get("routineBlocks"):
            try:
                services.set_prefs(md, {"routineAdd": "Gym 07:00-08:00 daily"})
                services.set_prefs(md, {"routineAdd": "Lunch 13:00-13:45 weekdays"})
            except Exception:  # noqa: BLE001
                pass
