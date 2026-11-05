# The morning briefing is a full spoken paragraph, longer than the 400 characters alerts used to allow.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_user_prefs'),
    ]

    operations = [
        migrations.AlterField(
            model_name='alert',
            name='body',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AlterField(
            model_name='alert',
            name='spoken_text',
            field=models.TextField(blank=True, default=''),
        ),
    ]
