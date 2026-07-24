from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0005_snapshot_is_estimated"),
    ]

    operations = [
        migrations.AlterField(
            model_name="snapshot",
            name="timestamp",
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
            ),
        ),
    ]
