from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0009_retire_ime_derivative_kinds")]

    operations = [
        migrations.AlterField(
            model_name="apirequestquota",
            name="plan",
            field=models.CharField(default="aio", max_length=16),
        ),
        migrations.AddField(
            model_name="apirequestquota",
            name="local_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="apirequestquota",
            name="successful_requests",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="apirequestquota",
            name="provider_used",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
