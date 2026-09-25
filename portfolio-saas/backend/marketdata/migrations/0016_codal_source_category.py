from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0015_widen_gold_currency_price_precision")]

    operations = [
        migrations.AddField(
            model_name="codalannouncement", name="source_category",
            field=models.IntegerField(blank=True, null=True, choices=[
                (1, "General Disclosures"), (2, "Periodic Financial Statements"),
                (3, "Monthly Production & Sales"), (4, "Board of Directors Report"),
                (5, "Auditor Notes & Opinion"), (6, "General Assembly Decision"),
                (7, "Capital Increase Announcement"), (8, "Monthly Investment Portfolio"),
                (9, "Corporate Governance"), (10, "Subsidiary Financial Statements"),
                (11, "IPO & Bond Prospectus"),
            ]),
        ),
        migrations.AddField(
            model_name="codalannouncement", name="source_category_title",
            field=models.CharField(max_length=120, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="codalannouncement", name="source_is_audited",
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
