# The day's conversation with the brain (core/act.py, 25 Nov 2026): one encrypted thread per person per day.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.crypto


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_staffmember_level_role_open_tasks'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BrainThread',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField()),
                ('messages_json', core.crypto.EncryptedTextField(blank=True, default='[]')),
                ('turns', models.PositiveIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='brain_threads', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('user', 'day'), name='unique_brain_thread_day')],
            },
        ),
    ]
