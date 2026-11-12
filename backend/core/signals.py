"""Things that happen on their own when data changes."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from . import push
from .models import CLOSED_STATUSES, Alert, SharedTask


@receiver(post_save, sender=Alert)
def _alert_created(sender, instance: Alert, created: bool, **kwargs):
    """Every new alert is pushed to the recipient's phones the moment it is safely saved."""
    if created and not (instance.snoozed_until and instance.snoozed_until > timezone.now()):
        alert_id = instance.id
        transaction.on_commit(lambda: push.notify_alert(alert_id))


@receiver(post_save, sender=SharedTask)
def _task_closed(sender, instance: SharedTask, **kwargs):
    """Finished or cancelled (by anyone, including the staff app): what was said about it while it was open ("new
    task for you", "started", "on hold") has nothing left to announce. The news that it is finished, raised right
    after this moment, is kept: that one he still needs to hear."""
    if instance.status in CLOSED_STATUSES:
        closed_at = instance.completed_at or timezone.now()
        Alert.objects.filter(ref_type="shared_task", ref_id=str(instance.id), acknowledged_at__isnull=True,
                             created_at__lte=closed_at).update(acknowledged_at=timezone.now())
