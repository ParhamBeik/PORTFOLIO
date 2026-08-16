from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [("marketdata", "0033_timescale_model_state")]

    operations = [
        migrations.CreateModel(
            name="DerivativeContract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("tse_option", "TSE option"), ("ime_option", "IME option"), ("ime_future", "IME future")], max_length=16)),
                ("contract_code", models.CharField(max_length=96)),
                ("underlying_code", models.CharField(blank=True, default="", max_length=96)),
                ("expiry_date", models.CharField(blank=True, default="", max_length=10)),
                ("contract_size", models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
                ("active", models.BooleanField(default=True)),
                ("provider_payload", models.JSONField(default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="DerivativeSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("observed_at", models.DateTimeField(db_index=True)),
                ("last_price", models.DecimalField(blank=True, decimal_places=4, max_digits=24, null=True)),
                ("bid_price", models.DecimalField(blank=True, decimal_places=4, max_digits=24, null=True)),
                ("ask_price", models.DecimalField(blank=True, decimal_places=4, max_digits=24, null=True)),
                ("volume", models.BigIntegerField(blank=True, null=True)),
                ("open_interest", models.BigIntegerField(blank=True, null=True)),
                ("provider_payload", models.JSONField(default=dict)),
                ("contract", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="snapshots", to="marketdata.derivativecontract")),
            ],
        ),
        migrations.AddConstraint(
            model_name="derivativecontract",
            constraint=models.UniqueConstraint(fields=("kind", "contract_code"), name="uniq_derivative_contract_kind_code"),
        ),
        migrations.AddIndex(
            model_name="derivativecontract",
            index=models.Index(fields=["kind", "expiry_date"], name="marketdata__kind_0a4bd7_idx"),
        ),
        migrations.AddIndex(
            model_name="derivativesnapshot",
            index=models.Index(fields=["contract", "-observed_at"], name="marketdata__contrac_1eb881_idx"),
        ),
    ]
