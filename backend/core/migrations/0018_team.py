"""The team (13 Nov 2026): JARVIS grows from three fixed people to any number of accounts (accountant, farm manager,
director, ...). Anyone gives anyone work or a message; personal items stay with their owner; the MD sees all work.

Schema: a job title, department and gender on each person; the person a task is for (assignee_user); the sender of
an alert. Data: the PA's and the manager's lists become ordinary team lists (USER), and the pair-by-pair origins
(MD_TO_PA, MG_TO_PA, PA_SELF, MD_MEET_PA ...) become GIVEN / SELF / MEET. Nothing is deleted or lost.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

_GIVEN = {"MD_TO_PA", "MD_TO_MG", "MG_TO_PA", "PA_TO_MG"}
_SELF = {"PA_SELF", "MG_SELF"}


def forwards(apps, schema_editor):
    User = apps.get_model("core", "User")
    UserPrefs = apps.get_model("core", "UserPrefs")
    SharedTask = apps.get_model("core", "SharedTask")
    md = User.objects.filter(role="MD").order_by("id").first()
    pa = User.objects.filter(role="PA").order_by("-is_active", "id").first()
    mg = User.objects.filter(role="MG").order_by("-is_active", "id").first()

    # Job titles for the three who were there before (only where none is set).
    for user, title in ((md, "Managing Director"), (pa, "PA"), (mg, "Manager")):
        if user is not None and not user.job_title:
            user.job_title = title
            user.save(update_fields=["job_title"])
    # The PA's gender was an MD setting ("paGender"); it now lives on the PA's own account.
    if md is not None and pa is not None and not pa.gender:
        row = UserPrefs.objects.filter(user=md).first()
        said = str(((row.data or {}) if row else {}).get("paGender") or "")
        if said in ("male", "female"):
            pa.gender = said
            pa.save(update_fields=["gender"])

    for task in SharedTask.objects.all().iterator():
        changed = []
        if task.assignee_type == "MD" and md is not None and task.assignee_user_id is None:
            task.assignee_user_id = md.id
            changed.append("assignee_user")
        elif task.assignee_type in ("PA", "MG"):
            person = pa if task.assignee_type == "PA" else mg
            if person is not None:
                task.assignee_type = "USER"
                task.assignee_user_id = person.id
                changed += ["assignee_type", "assignee_user"]
        if task.origin in _GIVEN:
            task.origin = "GIVEN"
            changed.append("origin")
        elif task.origin in _SELF:
            task.origin = "SELF"
            changed.append("origin")
        elif task.origin == "MD_MEET_PA":
            task.origin = "MEET"
            changed.append("origin")
        if changed:
            task.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_manager_role"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="user", name="role",
            field=models.CharField(choices=[("MD", "Managing Director"), ("PA", "Personal Assistant"), ("MG", "Manager"),
                                            ("TM", "Team member")], default="PA", max_length=2),
        ),
        migrations.AddField(model_name="user", name="job_title", field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(model_name="user", name="department", field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(model_name="user", name="gender", field=models.CharField(blank=True, default="", max_length=6)),
        migrations.AddField(
            model_name="sharedtask", name="assignee_user",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="assigned_tasks", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="sharedtask", name="assignee_type",
            field=models.CharField(choices=[("MD", "MD"), ("USER", "Team member"), ("STAFF", "Staff"), ("PA", "PA"),
                                            ("MG", "Manager")], max_length=5),
        ),
        migrations.AlterField(
            model_name="sharedtask", name="origin",
            field=models.CharField(choices=[
                ("PA_REQUEST", "Requested of the MD (accepted)"), ("GIVEN", "Given by one person to another"),
                ("SELF", "The person's own work"), ("MEET", "Meeting between the giver and this person"),
                ("MD_DELEGATION", "Delegated by MD"), ("PA_DELEGATION", "Delegated by PA"),
                ("TASKPANEL_WEB", "Assigned in TaskPanel"),
                ("MD_TO_PA", "Assigned by MD to PA"), ("MD_MEET_PA", "Meeting between MD and PA"),
                ("PA_SELF", "PA's own task"), ("MG_SELF", "Manager's own task"),
                ("MD_TO_MG", "Assigned by MD to the manager"), ("MG_TO_PA", "Assigned by the manager to the PA"),
                ("PA_TO_MG", "Assigned by the PA to the manager"),
            ], max_length=13),
        ),
        migrations.AddIndex(
            model_name="sharedtask",
            index=models.Index(fields=["assignee_user", "status"], name="core_shared_assigne_team_idx"),
        ),
        migrations.AddField(
            model_name="alert", name="sender",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
