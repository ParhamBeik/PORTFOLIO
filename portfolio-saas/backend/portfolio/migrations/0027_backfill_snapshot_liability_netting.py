"""Make the net-worth chart one measure instead of two.

`_flush_user_snapshots` subtracts each account's liabilities from the snapshot it
writes ("Match value_account: net mortgage / other liabilities"). That was not
always true: snapshots written before that change are GROSS, so the series the
chart draws switches definition partway through and shows a cliff no portfolio
ever experienced. On the demo household, account 1 stepped from ~1.56B to ~280M
in a single interval -- exactly its 1.6B of mortgage rows becoming visible, with
no trade and no price move behind it.

The chart is the product, so history is rewritten to the CURRENT definition (net
of liabilities) rather than annotating the discontinuity.

Two things this deliberately does NOT do:

* It does not guess a cutoff date. The changeover is detected per account from
  the data: a drop between consecutive snapshots of at least 90% of that
  account's liability total. A 1.6B step down inside one two-minute interval
  cannot come from prices on a ~1.9B book, so the signature is unambiguous, and
  an account whose series never shows one is already consistent and left alone.

* It does not destroy the old numbers. Every row it changes is copied to
  `portfolio_snapshot_pre_netting_backup` first, so `backwards` can put them
  back exactly. Rewriting financial history without an undo is not a migration,
  it is data loss.

Net worth may legitimately go negative where liabilities exceeded assets; that is
a real state and is not clamped. The migration reports how many rows landed
there.

Liability carries no effective date -- only the amount and when the row was
entered -- so the current total is applied to the whole pre-changeover stretch.
That is the most truthful reading available: it assumes the debt existed while
the assets did, which is why the mortgage rows were entered in the first place.
"""
from decimal import Decimal

from django.db import migrations

BACKUP_TABLE = "portfolio_snapshot_pre_netting_backup"
# Share of the liability total a single inter-snapshot drop must clear to count
# as the netting changeover rather than market movement.
DROP_SIGNATURE = Decimal("0.9")


def _create_backup(cursor):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
            snapshot_id bigint PRIMARY KEY,
            total_value_tomans numeric(24, 4) NOT NULL
        )
        """
    )


def forwards(apps, schema_editor):
    Account = apps.get_model("portfolio", "Account")
    Liability = apps.get_model("portfolio", "Liability")
    Snapshot = apps.get_model("portfolio", "Snapshot")

    # Liability totals per account, and per user for the account_id IS NULL rows
    # (which the writer builds by summing already-netted account totals).
    per_account: dict[int, Decimal] = {}
    for row in Liability.objects.values("account_id", "amount_tomans"):
        per_account[row["account_id"]] = (
            per_account.get(row["account_id"], Decimal("0"))
            + Decimal(str(row["amount_tomans"]))
        )
    if not per_account:
        return

    account_user = dict(
        Account.objects.filter(id__in=per_account).values_list("id", "user_id")
    )
    per_user: dict[int, Decimal] = {}
    for account_id, total in per_account.items():
        user_id = account_user.get(account_id)
        if user_id is not None:
            per_user[user_id] = per_user.get(user_id, Decimal("0")) + total

    def changeover(rows, liability_total):
        """Timestamp of the first snapshot already net of liabilities, or None."""
        threshold = liability_total * DROP_SIGNATURE
        previous = None
        for stamp, value in rows:
            if previous is not None and (previous - value) >= threshold:
                return stamp
            previous = value
        return None

    with schema_editor.connection.cursor() as cursor:
        _create_backup(cursor)

    adjusted = negative = 0
    account_boundaries: dict[int, object] = {}

    # Per-account series first. Detect each account's boundary from its
    # AS-SEEDED rows and record it before mutating anything -- the user-level
    # pass below cannot re-detect from these same rows once they are netted,
    # because an already-netted series has no cliff left to find.
    for account_id, liability_total in per_account.items():
        rows = list(
            Snapshot.objects.filter(account_id=account_id)
            .order_by("timestamp", "id")
            .values_list("timestamp", "total_value_tomans")
        )
        boundary = changeover(rows, liability_total)
        account_boundaries[account_id] = boundary
        if boundary is None:
            continue
        stale = Snapshot.objects.filter(
            account_id=account_id, timestamp__lt=boundary
        )
        adjusted += _apply(schema_editor, stale, liability_total)
        negative += stale.filter(total_value_tomans__lt=0).count()

    # Then the user-level rollups, using the earliest changeover seen across that
    # user's accounts -- one deploy flipped them all, so the earliest is the one.
    for user_id, liability_total in per_user.items():
        boundaries = [
            account_boundaries[account_id]
            for account_id in per_account
            if account_user.get(account_id) == user_id
            and account_boundaries.get(account_id) is not None
        ]
        if not boundaries:
            continue
        stale = Snapshot.objects.filter(
            user_id=user_id, account__isnull=True, timestamp__lt=min(boundaries)
        )
        adjusted += _apply(schema_editor, stale, liability_total)
        negative += stale.filter(total_value_tomans__lt=0).count()

    print(
        f"  snapshot netting: rewrote {adjusted} row(s); "
        f"{negative} now show negative net worth (liabilities exceeded assets)"
    )


def _apply(schema_editor, queryset, liability_total):
    """Back the rows up, then subtract. Returns the number changed."""
    from django.db.models import F

    ids = list(queryset.values_list("id", flat=True))
    if not ids:
        return 0
    with schema_editor.connection.cursor() as cursor:
        # ON CONFLICT DO NOTHING keeps the FIRST backed-up value if this is ever
        # re-run, so the original is never overwritten by an already-netted one.
        cursor.execute(
            f"""
            INSERT INTO {BACKUP_TABLE} (snapshot_id, total_value_tomans)
            SELECT id, total_value_tomans FROM portfolio_snapshot
            WHERE id = ANY(%s)
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            [ids],
        )
    queryset.update(total_value_tomans=F("total_value_tomans") - liability_total)
    return len(ids)


def backwards(apps, schema_editor):
    """Restore the exact pre-netting values from the backup table."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass(%s) IS NOT NULL", [f"public.{BACKUP_TABLE}"]
        )
        if not cursor.fetchone()[0]:
            return
        cursor.execute(
            f"""
            UPDATE portfolio_snapshot s
            SET total_value_tomans = b.total_value_tomans
            FROM {BACKUP_TABLE} b
            WHERE s.id = b.snapshot_id
            """
        )
        cursor.execute(f"DROP TABLE {BACKUP_TABLE}")


class Migration(migrations.Migration):

    atomic = True  # one consistent flip of the series, or none of it

    dependencies = [("portfolio", "0026_asset_proxy_key")]

    operations = [migrations.RunPython(forwards, backwards)]
