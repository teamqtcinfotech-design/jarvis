"""Messages kept for good (Nov 2026): Message + MessageReceipt. The alerts that carried earlier messages become
their news: each existing message alert gets its own kept record, with what is known of when it was seen."""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.crypto


def keep_existing_messages(apps, schema_editor):
    Alert = apps.get_model("core", "Alert")
    Message = apps.get_model("core", "Message")
    MessageReceipt = apps.get_model("core", "MessageReceipt")
    for alert in Alert.objects.filter(ref_type="message").exclude(sender__isnull=True).order_by("created_at").iterator():
        m = Message.objects.create(sender_id=alert.sender_id, text=alert.body or alert.title, priority=alert.priority,
                                   deliver_at=alert.snoozed_until)
        Message.objects.filter(id=m.id).update(sent_at=alert.created_at)
        MessageReceipt.objects.create(message=m, recipient_id=alert.recipient_id, delivered_at=alert.delivered_at, seen_at=alert.seen_at)
        Alert.objects.filter(id=alert.id).update(ref_id=str(m.id))


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_encrypt_task_text"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Message",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("text", core.crypto.EncryptedTextField()),
                ("priority", models.CharField(choices=[("URGENT", "Urgent"), ("HIGH", "High"), ("NORMAL", "Normal"), ("LOW", "Low")], default="NORMAL", max_length=6)),
                ("kind", models.CharField(default="message", max_length=8)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("deliver_at", models.DateTimeField(blank=True, null=True)),
                ("sender", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="messages_sent", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-sent_at"]},
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["sender", "sent_at"], name="core_messag_sender__5afc37_idx"),
        ),
        migrations.CreateModel(
            name="MessageReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("seen_at", models.DateTimeField(blank=True, null=True)),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="receipts", to="core.message")),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages_received", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(
            model_name="messagereceipt",
            index=models.Index(fields=["recipient", "seen_at"], name="core_messag_recipie_0db88a_idx"),
        ),
        migrations.AddConstraint(
            model_name="messagereceipt",
            constraint=models.UniqueConstraint(fields=("message", "recipient"), name="unique_message_receipt"),
        ),
        migrations.RunPython(keep_existing_messages, migrations.RunPython.noop),
    ]
