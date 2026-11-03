"""Create the MD and PA accounts plus synthetic demo data (never real client data).

    python manage.py seed_demo                 # first time: creates users, prints passwords
    python manage.py seed_demo --reset-tasks   # wipe demo tasks and recreate them
"""
import secrets
import string
from datetime import time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core import services
from core import timeutils as tu
from core.models import (
    Alert, CommandReceipt, EventInbox, IntegrationCommand, Kind, Priority, PrivateTask, ReminderAck, SharedTask,
    StaffMember, SyncCursor, TaskPanelLink, TaskRequest, User,
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
        parser.add_argument("--pa-name", default="Priya")
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
            now = tu.now_local()
            today = now.date()
            in_45 = (now + timedelta(minutes=45)).replace(second=0, microsecond=0)
            services.create_private_task(md, {
                "title": "Sharma Franchise quarterly review", "kind": Kind.MEETING, "priority": Priority.URGENT,
                "due_date": in_45.date(), "due_time": in_45.time(), "duration_minutes": 45,
                "announcement": "Sir, your quarterly review with Sharma Franchise starts in 15 minutes.",
            })
            services.create_private_task(md, {
                "title": "Pay GST advance instalment (reminder only)", "kind": Kind.PAYMENT,
                "priority": Priority.HIGH, "due_date": today, "due_time": time(17, 0),
            })
            services.create_private_task(md, {
                "title": "Call Mehta ji about the Pune franchise", "kind": Kind.CALL, "priority": Priority.NORMAL,
                "due_date": today + timedelta(days=1), "due_time": time(11, 30),
            })
            services.create_private_task(md, {
                "title": "Review Q3 sales deck", "kind": Kind.TASK, "priority": Priority.NORMAL,
                "due_date": today + timedelta(days=2),
            })
            services.create_private_task(md, {
                "title": "Renew club membership", "kind": Kind.TASK, "priority": Priority.LOW,
                "due_date": today - timedelta(days=1),
            })

        if not TaskRequest.objects.exists():
            services.create_md_request(pa, {
                "title": "Sign vendor agreement for Indore outlet", "kind": Kind.TASK, "priority": Priority.HIGH,
                "due_date": tu.today_local() + timedelta(days=1), "due_time": time(12, 0),
                "notes": "Legal has cleared the final draft.", "reason": "Vendor needs it before Friday.",
            })

        if mock and not SharedTask.objects.filter(assignee_type=SharedTask.Assignee.STAFF).exists():
            ramesh = StaffMember.objects.filter(name="Ramesh Kumar").first()
            neha = StaffMember.objects.filter(name="Neha Gupta").first()
            if ramesh:
                services.delegate_to_staff(md, {
                    "title": "Submit October GST reconciliation", "kind": Kind.TASK, "priority": Priority.HIGH,
                    "due_date": tu.today_local() + timedelta(days=1), "due_time": time(16, 0),
                }, ramesh, category_key="accounts",
                    description="Ramesh Kumar ko bolo kal 4 baje tak October ka GST reconciliation submit kare.")
            if neha:
                services.delegate_to_staff(pa, {
                    "title": "Shortlist candidates for Indore store manager", "kind": Kind.TASK,
                    "priority": Priority.NORMAL, "due_date": tu.today_local() + timedelta(days=3),
                }, neha, category_key="hr",
                    description="Neha ko bolo Indore store manager ke liye candidates shortlist kare.")

        self.stdout.write(self.style.SUCCESS("Demo data ready (synthetic)."))
        for username, pw in created:
            self.stdout.write(self.style.WARNING(f"  login: {username}   password: {pw}   (shown once — store it safely)"))
        if not created:
            self.stdout.write("  Existing MD/PA accounts kept. Change passwords in /admin if needed.")
