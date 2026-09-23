from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0037_correct_derived_liability_backfill")]

    operations = [
        # The current result for a key is the newest observation, with pk as a
        # deterministic tie-breaker. Rehearse on a backed-up staging copy first.
        migrations.RunSQL(
            sql="""
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY account_id, scenario, basis, window_days
                        ORDER BY created_at DESC, id DESC
                    ) AS rank
                    FROM portfolio_optimizationsnapshot
                )
                DELETE FROM portfolio_optimizationsnapshot AS snapshot
                USING ranked
                WHERE snapshot.id = ranked.id AND ranked.rank > 1
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddConstraint(
            model_name="optimizationsnapshot",
            constraint=models.UniqueConstraint(
                fields=("account", "scenario", "basis", "window_days"),
                condition=Q(account__isnull=False),
                name="uniq_current_account_optimization",
            ),
        ),
        migrations.AddConstraint(
            model_name="optimizationsnapshot",
            constraint=models.UniqueConstraint(
                fields=("scenario", "basis", "window_days"),
                condition=Q(account__isnull=True),
                name="uniq_current_global_optimization",
            ),
        ),
    ]
