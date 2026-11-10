# Spoken replies (Grok voice) are counted per user per day, in characters.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_push_device'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aiusage',
            name='kind',
            field=models.CharField(choices=[('COMMAND', 'Understanding a command'), ('ANSWER', 'Answering a question'), ('BRIEFING', 'Briefing'), ('VOICE', 'Spoken reply (Grok voice); input_tokens holds characters')], max_length=10),
        ),
    ]
