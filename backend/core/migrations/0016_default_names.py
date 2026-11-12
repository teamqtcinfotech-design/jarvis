"""One-time: the MD is addressed as "Sir" and the PA is Mansi (a woman), as asked on 12 Nov 2026.

Runs once on each server (the laptop and the live one). Afterwards the names and the form of address are changed
from the MD's Settings (Names), and this never runs again.
"""
from django.db import migrations


def set_defaults(apps, schema_editor):
    User = apps.get_model("core", "User")
    UserPrefs = apps.get_model("core", "UserPrefs")
    for pa in User.objects.filter(role="PA", is_active=True):
        pa.display_name = "Mansi"
        pa.first_name = "Mansi"
        pa.save(update_fields=["display_name", "first_name"])
    for md in User.objects.filter(role="MD", is_active=True):
        row, _ = UserPrefs.objects.get_or_create(user=md, defaults={"data": {}})
        data = dict(row.data or {})
        data["addressAs"] = "Sir"
        data["paGender"] = "female"
        row.data = data
        row.save(update_fields=["data"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_phonelog"),
    ]

    operations = [
        migrations.RunPython(set_defaults, migrations.RunPython.noop),
    ]
