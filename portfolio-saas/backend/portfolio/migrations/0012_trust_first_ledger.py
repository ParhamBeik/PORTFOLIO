from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


def populate_trade_amounts(apps, schema_editor):
    LedgerEntry = apps.get_model("portfolio", "LedgerEntry")
    for entry in LedgerEntry.objects.filter(kind__in=["buy", "sell"]).iterator():
        entry.amount_tomans = (entry.quantity or Decimal("0")) * (
            entry.price_tomans or Decimal("0")
        )
        if entry.source == "imported":
            entry.source = "csv"
        elif entry.source == "inferred":
            entry.source = "system"
        entry.save(update_fields=["amount_tomans", "source"])


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0011_backtestuserquota_and_more")]

    operations = [
        migrations.AddField(
            model_name="account",
            name="cash_balance_tomans",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=24),
        ),
        migrations.AddField(
            model_name="account",
            name="ledger_complete",
            field=models.BooleanField(
                default=False,
                help_text="True when opening balances and subsequent cash flows are complete.",
            ),
        ),
        migrations.AddField(
            model_name="account",
            name="tracking_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="ImportBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file_hash", models.CharField(max_length=64)),
                ("row_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="import_batches", to="portfolio.account")),
            ],
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.UniqueConstraint(fields=("account", "file_hash"), name="uniq_import_file_per_account"),
        ),
        migrations.RenameModel(old_name="Transaction", new_name="LedgerEntry"),
        migrations.RenameField(model_name="ledgerentry", old_name="side", new_name="kind"),
        migrations.AlterField(
            model_name="ledgerentry",
            name="kind",
            field=models.CharField(
                choices=[
                    ("opening_position", "Opening position"),
                    ("opening_cash", "Opening cash"),
                    ("deposit", "Deposit"),
                    ("withdrawal", "Withdrawal"),
                    ("buy", "Buy"),
                    ("sell", "Sell"),
                    ("dividend", "Dividend"),
                    ("fee", "Fee"),
                ],
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="ledgerentry",
            name="asset",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="transactions", to="portfolio.asset"),
        ),
        migrations.AlterField(
            model_name="ledgerentry",
            name="quantity",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True),
        ),
        migrations.AlterField(
            model_name="ledgerentry",
            name="price_tomans",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AlterField(
            model_name="ledgerentry",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "manual"),
                    ("csv", "csv"),
                    ("system", "system"),
                    ("imported", "imported (legacy)"),
                    ("inferred", "inferred (legacy)"),
                ],
                default="manual",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="amount_tomans",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=24, null=True),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="external_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="import_batch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="entries", to="portfolio.importbatch"),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="reversal_of",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversed_by", to="portfolio.ledgerentry"),
        ),
        migrations.RunPython(populate_trade_amounts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                check=(
                    ~models.Q(kind__in=["opening_position", "buy", "sell", "dividend"])
                    | (models.Q(asset__isnull=False) & models.Q(quantity__gt=0))
                ),
                name="ledger_asset_event_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                check=(
                    ~models.Q(kind__in=["opening_cash", "deposit", "withdrawal", "dividend", "fee"])
                    | models.Q(amount_tomans__gt=0)
                ),
                name="ledger_cash_event_amount",
            ),
        ),
    ]
