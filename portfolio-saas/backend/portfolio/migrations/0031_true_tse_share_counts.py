"""Store true broker share counts for TSE stocks.

Quantities for TSE-priced assets were deliberately kept at 1/10 of the real
broker share count, so that `quantity x rial_price` landed on a Toman value with
no conversion anywhere. See the old note in marketdata/currency.py.

That kept portfolio TOTALS correct and everything else wrong. Share counts were
a tenth of reality everywhere they were displayed (a holder of 26,000,000 KAMA
saw 2,600,000), cost basis and realised P&L inherited the same distortion, and
the moment the add-holding wizard offered more than the one seeded stock, any
user entering their real share count was valued 10x high.

The replacement is the standard boundary: quantities are true, TSE prices stay
Rial exactly as the provider and the broker report them, and the PRODUCT is
divided by ten once, in `currency.holding_value_to_toman`.

x10 here and /10 there are value-preserving in combination, so:
  * every historical `Snapshot` stays correct and is deliberately NOT rewritten;
  * `Price` rows are untouched -- prices were never the wrong unit;
  * `LedgerEntry.price_tomans` is untouched for the same reason;
  * `LedgerEntry.amount_tomans` is ALSO untouched, and that is the subtle one.
    It is a `quantity x price` product, so under the old convention
    (tenth-shares x Rial) it already held the true Toman cash amount and was
    correct. Scaling it here would have turned every historical trade's cash
    figure into Rial -- and since `create_ledger_entry` now converts the same
    product on write, the two together would have been self-consistently wrong,
    which is exactly the shape of bug that passes its own tests. Cash balances
    and TWR cash-flow boundaries read this column as Toman.

Because `amount_tomans` does not move, `Account.cash_balance_tomans` stays
valid and no projection rebuild is needed; `Holding.quantity` and
`LedgerEntry.quantity` move together, so replay still reconciles.

Scoped by `tse_symbol`, not by asset_class: a manual stock has no TSE feed and
is priced by an operator in Toman, so scaling it would inflate it tenfold.
"""
from django.db import migrations
from django.db.models import F


def to_true_share_counts(apps, schema_editor):
    Asset = apps.get_model("portfolio", "Asset")
    Holding = apps.get_model("portfolio", "Holding")
    LedgerEntry = apps.get_model("portfolio", "LedgerEntry")

    tse_ids = list(
        Asset.objects.exclude(tse_symbol="").values_list("id", flat=True)
    )
    if not tse_ids:
        return
    Holding.objects.filter(asset_id__in=tse_ids).update(quantity=F("quantity") * 10)
    LedgerEntry.objects.filter(
        asset_id__in=tse_ids, quantity__isnull=False
    ).update(quantity=F("quantity") * 10)


def to_tenth_share_counts(apps, schema_editor):
    Asset = apps.get_model("portfolio", "Asset")
    Holding = apps.get_model("portfolio", "Holding")
    LedgerEntry = apps.get_model("portfolio", "LedgerEntry")

    tse_ids = list(
        Asset.objects.exclude(tse_symbol="").values_list("id", flat=True)
    )
    if not tse_ids:
        return
    Holding.objects.filter(asset_id__in=tse_ids).update(quantity=F("quantity") / 10)
    LedgerEntry.objects.filter(
        asset_id__in=tse_ids, quantity__isnull=False
    ).update(quantity=F("quantity") / 10)


class Migration(migrations.Migration):

    dependencies = [("portfolio", "0030_owned_assets_visibility")]

    operations = [migrations.RunPython(to_true_share_counts, to_tenth_share_counts)]
