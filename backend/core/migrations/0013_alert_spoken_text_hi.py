from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0012_alert_told_and_reminder_told")]

    operations = [
        migrations.AddField(
            model_name="alert",
            name="spoken_text_hi",
            field=models.TextField(blank=True, default=""),
        ),
    ]
