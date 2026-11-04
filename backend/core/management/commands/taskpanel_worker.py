"""Background worker for the TaskPanel connection. Run in its own terminal:

    python manage.py taskpanel_worker

- delivers queued / unconfirmed task commands (same command id on every retry)
- pulls TaskPanel's change feed: every 5 s in working hours, every 30 s otherwise
- refreshes the staff directory every 60 s
"""
import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core import followups
from core import timeutils as tu
from core.taskpanel import dispatcher, sync

log = logging.getLogger("jarvis.taskpanel")
FOLLOWUP_EVERY_SECONDS = 10 * 60


class Command(BaseCommand):
    help = "Deliver TaskPanel commands and sync TaskPanel changes."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one cycle and exit (for cron / testing).")

    def handle(self, *args, **opts):
        stop = {"flag": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
        self.stdout.write(self.style.SUCCESS(
            f"TaskPanel worker started ({settings.TASKPANEL_MODE} mode). Ctrl+C to stop."))
        last_pull = last_dir = 0.0
        last_follow = -1e9  # first cycle runs the follow-up check straight away
        while not stop["flag"]:
            close_old_connections()
            now = time.monotonic()
            try:
                if now - last_follow >= FOLLOWUP_EVERY_SECONDS:
                    raised = followups.run()
                    last_follow = now
                    if raised:
                        self.stdout.write(f"{tu.now_local():%H:%M:%S}  raised {raised} follow-up(s)")
                sent = dispatcher.dispatch_due()
                if sent:
                    self.stdout.write(f"{tu.now_local():%H:%M:%S}  delivered {sent} command(s)")
                hour = tu.now_local().hour
                interval = settings.TASKPANEL_POLL_ACTIVE_SECONDS if 8 <= hour < 21 else settings.TASKPANEL_POLL_IDLE_SECONDS
                if now - last_pull >= interval:
                    applied = sync.pull_all()
                    last_pull = now
                    if applied:
                        self.stdout.write(f"{tu.now_local():%H:%M:%S}  applied {applied} TaskPanel change(s)")
                if now - last_dir >= settings.TASKPANEL_DIRECTORY_TTL_SECONDS:
                    sync.ensure_directory()
                    last_dir = now
            except Exception:  # keep the worker alive; details go to the log
                log.exception("TaskPanel worker cycle failed")
            if opts["once"]:
                break
            time.sleep(2)
        self.stdout.write("TaskPanel worker stopped.")
