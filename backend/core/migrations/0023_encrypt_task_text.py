"""Every word of a work task is encrypted at rest (Nov 2026).

Title in the deterministic form (exact lookups keep working), notes / instruction / announcement randomised; see
core/crypto.py. Rows written before this are encrypted here, once, in place. Nothing else about a task changes.
"""
from django.db import migrations

import core.crypto


def encrypt_existing(apps, schema_editor):
    SharedTask = apps.get_model("core", "SharedTask")
    for task in SharedTask.objects.all().iterator(chunk_size=500):
        # Read back plain (no prefix yet), written encrypted by the fields' own get_prep_value.
        task.save(update_fields=["title", "notes", "description", "announcement"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_walkie"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sharedtask",
            name="title",
            field=core.crypto.DeterministicEncryptedTextField(),
        ),
        migrations.AlterField(
            model_name="sharedtask",
            name="notes",
            field=core.crypto.EncryptedTextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="sharedtask",
            name="description",
            field=core.crypto.EncryptedTextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="sharedtask",
            name="announcement",
            field=core.crypto.EncryptedTextField(blank=True, default=""),
        ),
        migrations.RunPython(encrypt_existing, migrations.RunPython.noop),
    ]
