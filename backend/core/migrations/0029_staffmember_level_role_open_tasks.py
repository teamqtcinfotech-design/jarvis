# The staff directory as TaskPanel's api.php gives it (Nov 2026): seniority, role and open work per person.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_arrange_requests'),
    ]

    operations = [
        migrations.AddField(
            model_name='staffmember',
            name='level',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
        migrations.AddField(
            model_name='staffmember',
            name='role',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='staffmember',
            name='open_tasks',
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
