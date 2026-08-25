"""User-owned assets, per-holding nicknames, the visibility tick, and opt-in cash.

Four additive columns and one data backfill. Nothing here is a repair of a past
mistake, so unlike the squashed history it is safe to keep indefinitely.

`Account.track_cash` is backfilled True for every portfolio that already behaves
as though it tracks cash -- a non-zero balance, or any cash-kind ledger entry.
Those accounts keep settling trades against their balance exactly as before; only
portfolios that never recorded cash get the new "a buy funds itself" behaviour.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def set_track_cash(apps, schema_editor):
    Account = apps.get_model("portfolio", "Account")
    LedgerEntry = apps.get_model("portfolio", "LedgerEntry")
    cash_kinds = ["opening_cash", "deposit", "withdrawal"]
    with_cash_entries = set(
        LedgerEntry.objects.filter(kind__in=cash_kinds).values_list(
            "account_id", flat=True
        )
    )
    Account.objects.filter(
        models.Q(cash_balance_tomans__gt=0) | models.Q(pk__in=with_cash_entries)
    ).update(track_cash=True)


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0029_snapshot_session_close"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="owned_assets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="holding",
            name="display_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="holding",
            name="is_hidden",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="account",
            name="track_cash",
            field=models.BooleanField(
                default=False,
                help_text="True when this portfolio records cash movements, so trades settle against a balance.",
            ),
        ),
        migrations.RunPython(set_track_cash, migrations.RunPython.noop),
    ]
