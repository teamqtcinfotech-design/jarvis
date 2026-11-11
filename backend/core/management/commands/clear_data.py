"""Wipe the MD's and PA's working data: every task, request, alert, reminder mark, memory, contact, saved
conversation, AI-usage row and audit line. Accounts, passwords, TaskPanel links, the staff directory, settings
and registered phones stay, so both can sign in and carry on with a clean slate.

    python manage.py clear_data --yes
    python manage.py clear_data --yes --prefs     # also reset both users' settings to the defaults
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import (
    AiUsage, Alert, AuditLog, CommandReceipt, Contact, ConversationState, EventInbox, IntegrationCommand, Memory,
    PrivateTask, ReminderAck, ReminderTold, SharedTask, TaskRequest, UserPrefs,
)

# Children before parents, so nothing is left pointing at a deleted row.
DATA_MODELS = (IntegrationCommand, CommandReceipt, EventInbox, TaskRequest, Alert, SharedTask, PrivateTask,
               ReminderAck, ReminderTold, Memory, Contact, ConversationState, AiUsage, AuditLog)


class Command(BaseCommand):
    help = "Delete all tasks, requests, alerts and other working data; keep the accounts and settings."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Really delete (there is no undo).")
        parser.add_argument("--prefs", action="store_true", help="Also reset both users' settings to the defaults.")

    def handle(self, *args, **opts):
        if not opts["yes"]:
            raise CommandError("This deletes every task, request and alert. Run again with --yes if you mean it.")
        counts = []
        with transaction.atomic():
            for model in DATA_MODELS:
                n = model.objects.count()
                model.objects.all().delete()
                counts.append((model.__name__, n))
            if opts["prefs"]:
                n = UserPrefs.objects.count()
                UserPrefs.objects.all().delete()
                counts.append(("UserPrefs", n))
        for name, n in counts:
            self.stdout.write(f"  {name}: {n} deleted")
        self.stdout.write(self.style.SUCCESS("Cleared. Accounts, TaskPanel links, staff directory and phones kept."))
