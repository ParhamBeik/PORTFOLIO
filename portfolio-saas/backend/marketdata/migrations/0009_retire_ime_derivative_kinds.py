"""Drop `ime_future`/`ime_option` from the two `choices` lists that offered them.

Metadata only -- `choices` is not enforced by Postgres and nothing is rewritten,
so the `ime_*` rows already in `DerivativeContract`/`MarketDailyBar` stay exactly
as they are and stay readable. They are history: the endpoints that produced them
were retired on 2026-09-06 because every row they returned failed validation on
ingest while billing the TSETMC plan. Deleting the rows was deliberately not done
here -- nothing reads them, they cost little, and a migration that destroys data
to tidy a dropdown is a bad trade.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketdata', '0008_scoped_fingerprint_indexes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='derivativecontract',
            name='kind',
            field=models.CharField(choices=[('tse_option', 'TSE option')], max_length=16),
        ),
        migrations.AlterField(
            model_name='marketdailybar',
            name='asset_class',
            field=models.CharField(choices=[('crypto', 'Crypto'), ('commodity', 'Commodity'), ('etf_nav', 'ETF NAV'), ('index', 'Market index'), ('tse_option', 'TSE option')], db_index=True, max_length=16),
        ),
    ]
