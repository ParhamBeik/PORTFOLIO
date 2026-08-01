from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0012_trust_first_ledger")]

    operations = [
        migrations.RemoveConstraint(
            model_name="ledgerentry", name="ledger_asset_event_fields"
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(kind__in=["opening_position", "buy", "sell"])
                    | (
                        models.Q(asset__isnull=False)
                        & models.Q(quantity__isnull=False)
                        & models.Q(quantity__gt=0)
                    )
                ),
                name="ledger_asset_event_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(kind="dividend") | models.Q(asset__isnull=False)
                ),
                name="ledger_dividend_asset",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.UniqueConstraint(
                fields=("account", "external_id"),
                condition=~models.Q(external_id=""),
                name="uniq_ledger_external_id_per_account",
            ),
        ),
    ]
