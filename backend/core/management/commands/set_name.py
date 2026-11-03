"""Set the name JARVIS uses for the MD or PA (spoken and shown).

    python manage.py set_name pa "Neha"
    python manage.py set_name md "Sir"
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Set the display/spoken name of a JARVIS user."

    def add_arguments(self, parser):
        parser.add_argument("username", help="md or pa (the login username)")
        parser.add_argument("name", help="Name JARVIS should use, e.g. Neha")

    def handle(self, *args, **opts):
        user = User.objects.filter(username=opts["username"]).first()
        if not user:
            raise CommandError(f"No user '{opts['username']}'.")
        name = opts["name"].strip()
        if not name or len(name) > 80:
            raise CommandError("Name must be 1–80 characters.")
        user.display_name = name
        if user.is_pa:
            user.first_name = name
        user.save(update_fields=["display_name", "first_name"])
        self.stdout.write(self.style.SUCCESS(f"{user.username} will now be called “{name}”."))
