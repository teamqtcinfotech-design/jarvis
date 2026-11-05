# Phones registered for live push (Firebase Cloud Messaging).
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_alert_long_text'),
    ]

    operations = [
        migrations.CreateModel(
            name='PushDevice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(max_length=512, unique=True)),
                ('platform', models.CharField(default='android', max_length=16)),
                ('failures', models.PositiveSmallIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='push_devices', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
