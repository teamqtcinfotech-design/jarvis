"""Background worker for the TaskPanel connection. Run in its own terminal:

    python manage.py taskpanel_worker

- rings work and messages again until they are seen (core/ringing.py), and pushes reminders at their moment
- every 10 minutes: the next occurrence of each repeating item (core/recurrence.py) and the follow-up nudges
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

from core import followups, recurrence, reminderpush, ringing, services
from core import timeutils as tu
from core.taskpanel import dispatcher, sync

log = logging.getLogger("jarvis.taskpanel")
FOLLOWUP_EVERY_SECONDS = 10 * 60
FREE_EVERY_SECONDS = 60  # the free-time suggestion comes within a minute of his becoming free (a meeting ends, is cancelled)


class Command(BaseCommand):
    help = "Deliver TaskPanel commands and sync TaskPanel changes."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one cycle and exit (for cron / testing).")

    def handle(self, *args, **opts):
        stop = {"flag": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
        self.stdout.write(self.style.SUCCESS(
            f"TaskPanel worker started ({settings.TASKPANEL_MODE} mode). Ctrl+C to stop."))
        # Work for the MD goes straight onto his list now: a request still waiting from before is moved there.
        try:
            moved = services.settle_pending_requests()
            if moved:
                self.stdout.write(f"{tu.now_local():%H:%M:%S}  {moved} waiting request(s) put on the MD's list")
        except Exception:  # noqa: BLE001 — the worker's other duties must start regardless
            log.exception("settling the waiting requests failed")
        last_pull = last_dir = 0.0
        last_follow = -1e9  # first cycle runs the follow-up check straight away
        last_free = -1e9
        cleaned_on = None   # the local date the alert cleanup last ran (once a day, in the small hours)
        while not stop["flag"]:
            close_old_connections()
            now = time.monotonic()
            try:
                # Alerts older than a week go, once a day between 3 and 4 in the morning (tasks are never touched).
                local = tu.now_local()
                if cleaned_on != local.date() and local.hour == 3:
                    cleaned_on = local.date()
                    gone = services.cleanup_alerts()
                    if gone:
                        self.stdout.write(f"{local:%H:%M:%S}  alert cleanup: {gone} old entr{'y' if gone == 1 else 'ies'} deleted")
                # Reminders at their exact moment: pushed to the phones as soon as they come due (every cycle).
                pushed = reminderpush.run()
                if pushed:
                    self.stdout.write(f"{tu.now_local():%H:%M:%S}  pushed {pushed} reminder(s)")
                # Ring until seen: work and messages ring again until the person taps Seen (core/ringing.py).
                rang = ringing.run()
                if any(rang.values()):
                    self.stdout.write(f"{tu.now_local():%H:%M:%S}  rang {rang['rung']}, stopped {rang['stopped']}, "
                                      f"not seen {rang['given_up']}")
                if now - last_follow >= FOLLOWUP_EVERY_SECONDS:
                    # Repeating items: the next occurrence of each one whose turn has come (core/recurrence.py).
                    made = recurrence.run()
                    if made:
                        self.stdout.write(f"{tu.now_local():%H:%M:%S}  made {made} repeat occurrence(s)")
                    raised = followups.run()
                    last_follow = now
                    if raised:
                        self.stdout.write(f"{tu.now_local():%H:%M:%S}  raised {raised} follow-up(s)")
                elif now - last_free >= FREE_EVERY_SECONDS and tu.now_local().hour in followups.ACTIVE_HOURS:
                    # Between the 10-minute passes: the moment a free stretch begins (a meeting ended, was cancelled),
                    # the suggestion for it; the same stretch is never raised twice.
                    free = followups.free_time()
                    last_free = now
                    if free:
                        self.stdout.write(f"{tu.now_local():%H:%M:%S}  free-time suggestion(s): {free}")
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
