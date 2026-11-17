"""The walkie-talkie relay (see core/walkie.py). Run in its own terminal / PM2 app:

    python manage.py walkie_server            # port from JARVIS_WALKIE_PORT (8001)
    python manage.py walkie_server --port 8011

On the VPS nginx passes /ws/ to it (proxy_http_version 1.1, Upgrade / Connection headers); on the laptop the phone
reaches it through `adb reverse tcp:8001 tcp:8001`.
"""
import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand

from core import walkie


class Command(BaseCommand):
    help = "Run the walkie-talkie relay (one open line per phone, one speaker at a time)."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=settings.JARVIS_WALKIE_PORT)

    def handle(self, *args, **opts):
        self.stdout.write(self.style.SUCCESS(f"Walkie-talkie relay on port {opts['port']}. Ctrl+C to stop."))
        try:
            asyncio.run(walkie.serve(opts["port"]))
        except KeyboardInterrupt:
            self.stdout.write("Walkie-talkie relay stopped.")
