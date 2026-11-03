"""Durable command delivery (guide §7.3, §15.2).

- The command row (with frozen payload) is committed before any network call.
- A lease prevents two workers sending the same command at once.
- Timeouts leave the outcome UNKNOWN: we ask TaskPanel for the receipt, then retry the SAME command_id.
- "Assigned" is only ever shown after TaskPanel returns a committed receipt.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Alert, IntegrationCommand, Priority, SharedTask, TaskPanelLink
from .client import TaskPanelError, get_client
from .contracts import apply_snapshot

log = logging.getLogger("jarvis.taskpanel")

BACKOFF_SECONDS = [2, 5, 15, 60, 300, 900]
MAX_ATTEMPTS = 8
LEASE = timedelta(seconds=45)
REJECT_STATUSES = {400, 401, 403, 404, 409, 410, 422}


def _claim(command_id) -> bool:
    now = timezone.now()
    return bool(
        IntegrationCommand.objects.filter(
            id=command_id,
            state__in=[IntegrationCommand.State.QUEUED, IntegrationCommand.State.OUTCOME_UNKNOWN],
        )
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
        .update(lease_until=now + LEASE, state=IntegrationCommand.State.SENDING)
    )


def _succeed(cmd: IntegrationCommand, result: dict):
    with transaction.atomic():
        cmd.state = IntegrationCommand.State.SUCCEEDED
        cmd.result = result
        cmd.error_code = cmd.error_message = ""
        cmd.lease_until = None
        cmd.save()
        if cmd.task_id:
            task = SharedTask.objects.select_for_update().get(id=cmd.task_id)
            apply_snapshot(task, result.get("task", {}))
            task.sync_state = SharedTask.Sync.SYNCED
            task.sync_error = ""
            task.last_synced_at = timezone.now()
            task.save()


def _reject(cmd: IntegrationCommand, err: TaskPanelError):
    with transaction.atomic():
        cmd.state = IntegrationCommand.State.REJECTED
        cmd.error_code, cmd.error_message = err.code[:60], err.message[:300]
        cmd.lease_until = None
        cmd.save()
        if cmd.task_id:
            SharedTask.objects.filter(id=cmd.task_id).update(
                sync_state=SharedTask.Sync.FAILED, sync_error=err.message[:300], updated_at=timezone.now()
            )


def _defer(cmd: IntegrationCommand, err: TaskPanelError):
    cmd.state = IntegrationCommand.State.OUTCOME_UNKNOWN if err.outcome_unknown else IntegrationCommand.State.QUEUED
    cmd.error_code, cmd.error_message = err.code[:60], err.message[:300]
    cmd.lease_until = None
    if cmd.attempts >= MAX_ATTEMPTS:
        cmd.next_attempt_at = None  # parked: needs a person to look
        if cmd.task_id:
            SharedTask.objects.filter(id=cmd.task_id).update(
                sync_error="TaskPanel has not confirmed this task yet. It will not be re-sent automatically.",
                updated_at=timezone.now(),
            )
        Alert.objects.create(
            recipient=cmd.actor, kind=Alert.Kind.INFO, priority=Priority.HIGH,
            title="TaskPanel has not confirmed a task",
            body=(cmd.payload.get("data", {}).get("title") or "")[:200],
            spoken_text="TaskPanel has not confirmed one of the tasks you assigned. Please check it in the app.",
            ref_type="command", ref_id=str(cmd.id),
        )
    else:
        delay = BACKOFF_SECONDS[min(cmd.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        cmd.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    cmd.save()


def dispatch(command_id) -> IntegrationCommand:
    """Deliver one command if it is due and not leased. Safe to call from the web request or the worker."""
    if not _claim(command_id):
        return IntegrationCommand.objects.get(id=command_id)
    cmd = IntegrationCommand.objects.select_related("actor").get(id=command_id)
    cmd.attempts += 1
    cmd.save(update_fields=["attempts", "updated_at"])
    link = TaskPanelLink.objects.filter(user=cmd.actor, active=True).first()
    client = get_client()
    try:
        if cmd.attempts > 1:
            # A previous attempt may have committed: resolve by receipt before re-sending.
            try:
                receipt = client.command_status(link, str(cmd.id))
                if receipt.get("state") == "succeeded":
                    _succeed(cmd, receipt)
                    return cmd
            except TaskPanelError as err:
                if err.status not in (404,):
                    raise
        _status, result = client.command(link, cmd.payload)
        if result.get("state") != "succeeded":
            raise TaskPanelError(502, "unexpected_state", "TaskPanel did not confirm the command.",
                                 retryable=True, outcome_unknown=True)
        _succeed(cmd, result)
    except TaskPanelError as err:
        if err.status in REJECT_STATUSES and not err.retryable:
            log.info("TaskPanel rejected command %s: %s", cmd.id, err.code)
            _reject(cmd, err)
        else:
            log.warning("TaskPanel command %s deferred (%s), attempt %s", cmd.id, err.code, cmd.attempts)
            _defer(cmd, err)
    cmd.refresh_from_db()
    return cmd


def dispatch_due(limit: int = 20) -> int:
    now = timezone.now()
    due = IntegrationCommand.objects.filter(
        state__in=[IntegrationCommand.State.QUEUED, IntegrationCommand.State.OUTCOME_UNKNOWN],
        next_attempt_at__isnull=False, next_attempt_at__lte=now,
    ).order_by("next_attempt_at").values_list("id", flat=True)[:limit]
    count = 0
    for command_id in list(due):
        dispatch(command_id)
        count += 1
    # Recover commands whose worker died mid-send (lease expired while SENDING).
    IntegrationCommand.objects.filter(state=IntegrationCommand.State.SENDING, lease_until__lt=now).update(
        state=IntegrationCommand.State.OUTCOME_UNKNOWN, lease_until=None, next_attempt_at=now
    )
    return count
