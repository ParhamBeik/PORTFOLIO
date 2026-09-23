from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0010_quota_evidence")]

    operations = [
        migrations.AddField(
            model_name="apirequestquota", name="provider_observed_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="apirequestquota", name="provider_observation_source",
            field=models.CharField(max_length=12, blank=True, default=""),
        ),
    ]
