from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0039_remove_asset_currency")]

    operations = [
        migrations.AddField(
            model_name="snapshot", name="day",
            field=models.DateField(db_index=True, null=True),
        ),
        migrations.RunSQL(
            sql="""
                UPDATE portfolio_snapshot
                SET day = (timestamp AT TIME ZONE 'Asia/Tehran')::date;
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY user_id, account_id, day
                        ORDER BY is_session_close DESC, is_estimated ASC,
                                 timestamp DESC, id DESC
                    ) AS priority
                    FROM portfolio_snapshot
                )
                DELETE FROM portfolio_snapshot AS snapshot
                USING ranked
                WHERE snapshot.id = ranked.id AND ranked.priority > 1;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="snapshot", name="day",
            field=models.DateField(db_index=True),
        ),
        migrations.AlterField(
            model_name="snapshot", name="is_estimated",
            field=models.BooleanField(
                default=False,
                help_text="Legacy estimated observation; new daily closes are always real.",
            ),
        ),
        migrations.AddConstraint(
            model_name="snapshot",
            constraint=models.UniqueConstraint(
                fields=("user", "account", "day"),
                condition=Q(account__isnull=False),
                name="uniq_snapshot_account_day",
            ),
        ),
        migrations.AddConstraint(
            model_name="snapshot",
            constraint=models.UniqueConstraint(
                fields=("user", "day"),
                condition=Q(account__isnull=True),
                name="uniq_snapshot_user_day",
            ),
        ),
    ]
