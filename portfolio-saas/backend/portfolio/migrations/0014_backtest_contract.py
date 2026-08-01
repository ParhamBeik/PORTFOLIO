from django.db import migrations, models
import django.db.models.deletion


def canonicalize_basis(apps, schema_editor):
    BacktestRun = apps.get_model("portfolio", "BacktestRun")
    BacktestRun.objects.filter(basis="nominal").update(basis="nominal_toman")
    BacktestRun.objects.filter(basis="usd_real").update(basis="usd_denominated")


class Migration(migrations.Migration):
    dependencies = [("portfolio", "0013_ledger_constraints")]

    operations = [
        migrations.AddField(
            model_name="backtestrun",
            name="account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="backtest_runs",
                to="portfolio.account",
            ),
        ),
        migrations.AddField(
            model_name="backtestrun",
            name="completed_years",
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddField(
            model_name="backtestrun",
            name="manifest",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="backtestrun",
            name="universe_mode",
            field=models.CharField(
                choices=[("portfolio", "portfolio"), ("verified_market", "verified_market")],
                default="portfolio",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="backtestrun",
            name="basis",
            field=models.CharField(
                choices=[
                    ("nominal_toman", "nominal_toman"),
                    ("usd_denominated", "usd_denominated"),
                    ("nominal", "nominal (deprecated)"),
                    ("usd_real", "usd_real (deprecated)"),
                ],
                default="nominal_toman",
                max_length=20,
            ),
        ),
        migrations.RunPython(canonicalize_basis, migrations.RunPython.noop),
    ]
