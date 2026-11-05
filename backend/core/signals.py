"""Things that happen on their own when data changes."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import push
from .models import Alert


@receiver(post_save, sender=Alert)
def _alert_created(sender, instance: Alert, created: bool, **kwargs):
    """Every new alert is pushed to the recipient's phones the moment it is safely saved."""
    if created:
        alert_id = instance.id
        transaction.on_commit(lambda: push.notify_alert(alert_id))
