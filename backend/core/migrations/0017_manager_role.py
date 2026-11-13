"""The manager: a third person (Mansi) beside the MD and the PA (Himanshu), as asked on 13 Nov 2026.

Adds the role and the task kinds between the three, and sets up the accounts once on each server: a login
"manager" (password "manager") for Mansi; the PA, if still called Mansi, becomes Himanshu (a man).
"""
from django.contrib.auth.hashers import make_password
from django.db import migrations, models


def accounts(apps, schema_editor):
    User = apps.get_model("core", "User")
    UserPrefs = apps.get_model("core", "UserPrefs")
    if not User.objects.filter(username="manager").exists():
        User.objects.create(username="manager", password=make_password("manager"), role="MG",
                            display_name="Mansi", first_name="Mansi", is_active=True)
    renamed = False
    for pa in User.objects.filter(role="PA", is_active=True):
        if (pa.display_name or pa.first_name or "").strip().lower() == "mansi":
            pa.display_name = pa.first_name = "Himanshu"
            pa.save(update_fields=["display_name", "first_name"])
            renamed = True
    pa_names = {(u.display_name or u.first_name or "").strip().lower() for u in User.objects.filter(role="PA", is_active=True)}
    if renamed or "himanshu" in pa_names:
        for md in User.objects.filter(role="MD", is_active=True):
            row, _ = UserPrefs.objects.get_or_create(user=md, defaults={"data": {}})
            data = dict(row.data or {})
            data["paGender"] = "male"
            row.data = data
            row.save(update_fields=["data"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_default_names"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user", name="role",
            field=models.CharField(choices=[("MD", "Managing Director"), ("PA", "Personal Assistant"), ("MG", "Manager")],
                                   default="PA", max_length=2),
        ),
        migrations.AlterField(
            model_name="sharedtask", name="assignee_type",
            field=models.CharField(choices=[("MD", "MD"), ("PA", "PA"), ("MG", "Manager"), ("STAFF", "Staff")], max_length=5),
        ),
        migrations.AlterField(
            model_name="sharedtask", name="origin",
            field=models.CharField(choices=[
                ("PA_REQUEST", "Requested by PA"), ("MD_TO_PA", "Assigned by MD to PA"),
                ("MD_MEET_PA", "Meeting between MD and PA"), ("PA_SELF", "PA's own task"),
                ("MG_SELF", "Manager's own task"), ("MD_TO_MG", "Assigned by MD to the manager"),
                ("MG_TO_PA", "Assigned by the manager to the PA"), ("PA_TO_MG", "Assigned by the PA to the manager"),
                ("MD_DELEGATION", "Delegated by MD"), ("PA_DELEGATION", "Delegated by PA"),
                ("TASKPANEL_WEB", "Assigned in TaskPanel"),
            ], max_length=13),
        ),
        migrations.RunPython(accounts, migrations.RunPython.noop),
    ]
