from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketdata", "0022_operationalmetricsnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="table_bytes",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="archive",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="quota",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="queues",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="codal_status",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="workflow_15m",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="workers",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operationalmetricsnapshot",
            name="disk",
            field=models.JSONField(default=dict),
        ),
    ]
