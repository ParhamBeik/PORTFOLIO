"""Narrow 0035's backfill to the rows the replay actually recreates.

0035 flagged a liability as `derived` on its shape -- named `Mortgage (...)`,
secured on an asset, carrying no schedule -- which was meant to be the mark
path's signature. It is not specific enough. Production had four rows matching
it on assets that are gold coins and a stock, seeded rather than derived, and
`rebuild_projections` would have deleted all four without recreating any: it
only rebuilds a mortgage for an asset the account holds a HOUSE MARK for.

`derived` means "the replay owns this and will put it back". The honest test is
therefore whether such a mark exists, not what the label looks like, so this
re-derives the flag from the ledger and clears it everywhere else. The old
`asset__isnull=False` reap would have eaten those same four rows too, so this
is not a regression being fixed -- it is a stale claim being withdrawn before
anything relies on it.

Reverse is a no-op: 0035's classification is not worth restoring, and dropping
the column (its own reverse) removes the flag entirely anyway.
"""
from django.db import migrations

# `LedgerEntry.Kind.OPENING_POSITION` / `VALUATION_MARK`, and the pair
# `services.ledger.HOUSE_MARK_KINDS` holds. Inlined because a migration must
# not import a live enum that may be renamed under it.
HOUSE_MARK_KINDS = ("opening_position", "valuation_mark")


def rederive_from_the_ledger(apps, schema_editor):
    Liability = apps.get_model("portfolio", "Liability")
    LedgerEntry = apps.get_model("portfolio", "LedgerEntry")

    # The same three conditions `_projection_state.is_house_mark` applies: the
    # asset is a house, the kind is a mark, and a reversal pair nets to nothing
    # (dropping only the reversal would leave the original still counted).
    #
    # The mortgage amount is NOT filtered in the query: a reversal need not
    # carry one, and excluding it there would drop it from `reversed_ids` and
    # leave the row it cancels looking live. Pair first, then read the amount.
    marks = list(
        LedgerEntry.objects.filter(
            kind__in=HOUSE_MARK_KINDS,
            asset__isnull=False,
            asset__is_house=True,
        )
        .order_by("timestamp", "id")
        .values_list(
            "id", "reversal_of_id", "account_id", "asset_id",
            "mortgage_deduction_tomans",
        )
    )
    reversed_ids = {reversal_of for _, reversal_of, _, _, _ in marks if reversal_of}

    # Marks REPLACE rather than accumulate, so the newest live one for an
    # (account, asset) is the one in force -- exactly as the replay reads it.
    # A later mark that drops the mortgage to zero therefore un-owns the row.
    in_force = {}
    for pk, reversal_of, account_id, asset_id, mortgage in marks:
        if reversal_of is not None or pk in reversed_ids:
            continue
        in_force[(account_id, asset_id)] = mortgage or 0

    owned = {key for key, mortgage in in_force.items() if mortgage > 0}

    for liability in Liability.objects.filter(asset__isnull=False):
        should_be = (liability.account_id, liability.asset_id) in owned
        if liability.derived != should_be:
            liability.derived = should_be
            liability.save(update_fields=["derived"])

    # Nothing without an asset can have come from the mark path.
    Liability.objects.filter(asset__isnull=True, derived=True).update(derived=False)


class Migration(migrations.Migration):

    dependencies = [
        ("portfolio", "0036_alter_liability_started_on"),
    ]

    operations = [
        migrations.RunPython(rederive_from_the_ledger, migrations.RunPython.noop),
    ]
