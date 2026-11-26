# Memory and corrections (26 Nov 2026): what JARVIS learns by itself (kind, source), the night's reading mark on the
# day's thread, and the bank of corrections replayed against the real model.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import core.crypto


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_brain_thread'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='memory',
            name='kind',
            field=models.CharField(choices=[('note', 'Note'), ('person', 'Person'), ('preference', 'Preference'), ('habit', 'Habit'), ('correction', 'Correction')], default='note', max_length=12),
        ),
        migrations.AddField(
            model_name='memory',
            name='source',
            field=models.CharField(choices=[('said', 'Said by the speaker'), ('learned', 'Learned by JARVIS')], default='said', max_length=8),
        ),
        migrations.AddField(
            model_name='memory',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='brainthread',
            name='learned',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='BankCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField()),
                ('said', core.crypto.EncryptedTextField()),
                ('before_json', core.crypto.EncryptedTextField(blank=True, default='[]')),
                ('turn_json', core.crypto.EncryptedTextField(blank=True, default='{}')),
                ('expected_json', core.crypto.EncryptedTextField(blank=True, default='{}')),
                ('learned', core.crypto.EncryptedTextField(blank=True, default='')),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bank_cases', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
