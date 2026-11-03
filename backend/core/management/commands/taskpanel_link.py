"""Link a JARVIS user (md / pa) to their TaskPanel identity.

    python manage.py taskpanel_link md --actor-key <TaskPanel user_key> --actor-name "Managing Director"
    (the grant is read from a prompt so it never lands in shell history)

The grant comes from TaskPanel's bin/integration-grant.php (run by the TaskPanel admin). It is stored
encrypted and never sent to the phone. Use --revoke to unlink.
"""
import getpass
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import TaskPanelLink, User
from core.timeutils import IST


class Command(BaseCommand):
    help = "Link or revoke a user's TaskPanel delegated grant."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--actor-key", default="")
        parser.add_argument("--actor-name", default="")
        parser.add_argument("--scopes", default="directory.read tasks.read tasks.create")
        parser.add_argument("--expires", default="", help="YYYY-MM-DD (India date)")
        parser.add_argument("--grant", default="", help="Avoid: prefer the interactive prompt.")
        parser.add_argument("--revoke", action="store_true")

    def handle(self, *args, **opts):
        user = User.objects.filter(username=opts["username"]).first()
        if not user:
            raise CommandError(f"No JARVIS user '{opts['username']}'.")
        if opts["revoke"]:
            updated = TaskPanelLink.objects.filter(user=user).update(active=False, grant="")
            self.stdout.write(self.style.WARNING(f"Revoked {updated} link(s) for {user.username}."))
            return
        if not opts["actor_key"]:
            raise CommandError("--actor-key is required (the person's TaskPanel user_key).")
        grant = opts["grant"] or getpass.getpass("Paste the TaskPanel grant (input hidden): ").strip()
        if len(grant) < 20:
            raise CommandError("That grant looks too short.")
        expires = None
        if opts["expires"]:
            expires = datetime.fromisoformat(opts["expires"]).replace(hour=23, minute=59, tzinfo=IST)
        TaskPanelLink.objects.update_or_create(
            user=user,
            defaults={"actor_key": opts["actor_key"], "actor_name": opts["actor_name"], "grant": grant,
                      "scopes": opts["scopes"], "expires_at": expires, "active": True},
        )
        self.stdout.write(self.style.SUCCESS(
            f"Linked {user.username} -> TaskPanel {opts['actor_key']} (scopes: {opts['scopes']}; "
            f"expires: {expires.isoformat() if expires else 'not set'}). Linked at {timezone.now():%Y-%m-%d %H:%M}."
        ))
