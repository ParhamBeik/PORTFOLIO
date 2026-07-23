"""Trade execution and correction for buying and selling assets.

A trade is three writes that must succeed or fail together, so the whole body
runs in one `transaction.atomic()`:
  1. append a `Transaction` row (the event/ledger);
  2. upsert the derived `Holding.quantity` (running balance);
  3. stamp an immediate per-user `Snapshot` so the net-worth chart steps at the
     trade moment, not at the next 2-min cron tick.

Selling more than is held is rejected (`InsufficientHolding`) — the ledger must
never imply a negative position. The execution price is captured from the latest
price map at call time so the historical event is self-describing. Mistakes can
be undone only when they are the latest trade for that asset, preserving ledger
chronology and the derived holding balance.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from ..models import Account, Asset, Holding, Snapshot, Transaction
from .valuation import get_latest_prices, value_account, value_user


class TradeError(Exception):
    """Base for trade validation failures (mapped to HTTP 400 by the view)."""


class InsufficientHolding(TradeError):
    """Raised when a sell exceeds the current quantity held."""


class ManualAssetTrade(TradeError):
    """Raised when trading a house asset (valued by formula, not quantity)."""


class StaleTradeUndo(TradeError):
    """Raised when undoing a trade would rewrite later history for that asset."""


def _q(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def _stamp_snapshots(user, account: Account) -> dict:
    valuation = value_user(user)
    account_total = value_account(account)["total"]
    Snapshot.objects.bulk_create([
        Snapshot(user=user, account=None, total_value_tomans=valuation["total"]),
        Snapshot(user=user, account=account, total_value_tomans=account_total),
    ])
    return valuation


@transaction.atomic
def execute_trade(
    *,
    account: Account,
    asset: Asset,
    side: str,
    quantity,
    note: str = "",
) -> dict:
    """Record one buy/sell for `asset` in `account`. Returns a summary dict.

    Atomic: ledger row + holding balance + net-worth snapshot commit together.
    `quantity` must be positive; direction comes from `side`.
    """
    if side not in Transaction.Side.values:
        raise TradeError(f"side must be one of {Transaction.Side.values}")
    qty = _q(quantity)
    if qty <= 0:
        raise TradeError("quantity must be positive")
    if asset.is_house:
        # Houses are valued by a formula on `quantity` (price/sqm), not a
        # tradeable count — editing them goes through the holding endpoint.
        raise ManualAssetTrade("house assets are not tradeable; edit the holding directly")

    # Lock the holding row for the duration so concurrent trades on the same
    # asset can't race the balance check (SELECT ... FOR UPDATE).
    holding = (
        Holding.objects.select_for_update()
        .filter(account=account, asset=asset)
        .first()
    )
    current_qty = _q(holding.quantity) if holding else Decimal("0")

    if side == Transaction.Side.SELL and qty > current_qty:
        raise InsufficientHolding(
            f"Cannot sell {qty.normalize():f}; only {current_qty.normalize():f} is held."
        )

    new_qty = current_qty + qty if side == Transaction.Side.BUY else current_qty - qty

    # Capture execution price from the live map (0 if the asset has no price yet).
    price = _q(get_latest_prices().get(asset.key, 0))

    Transaction.objects.create(
        account=account,
        asset=asset,
        side=side,
        quantity=qty,
        price_tomans=price,
        note=note[:200],
    )

    if holding is None:
        holding = Holding.objects.create(account=account, asset=asset, quantity=new_qty)
    elif new_qty == 0:
        # A fully-closed position leaves the ledger intact but drops the balance
        # row, so it stops appearing in valuation.
        holding.delete()
        holding = None
    else:
        holding.quantity = new_qty
        holding.save(update_fields=["quantity", "updated_at"])

    # Immediate snapshots: the whole-user total (account=None, mirrors the cron's
    # whole-portfolio row) AND this account's own total, so a per-account chart
    # also steps at the trade moment instead of waiting for the next cron tick.
    user = account.user
    valuation = _stamp_snapshots(user, account)

    return {
        "asset_key": asset.key,
        "side": side,
        "quantity": str(qty),
        "price_tomans": str(price),
        "holding_quantity": str(new_qty),
        "cash_flow_tomans": str((price * qty).quantize(Decimal("0.0001"))),
        "total_value_tomans": str(valuation["total"]),
    }


@transaction.atomic
def undo_trade(*, user, transaction_id: int) -> None:
    """Remove the latest trade for one asset and reverse its holding effect."""
    trade = (
        Transaction.objects.select_for_update()
        .select_related("account", "asset")
        .get(pk=transaction_id, account__user=user)
    )
    holding = (
        Holding.objects.select_for_update()
        .filter(account=trade.account, asset=trade.asset)
        .first()
    )
    latest_id = (
        Transaction.objects.filter(account=trade.account, asset=trade.asset)
        .order_by("-timestamp", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    if latest_id != trade.pk:
        raise StaleTradeUndo("Only the latest trade for this asset can be undone.")

    current_qty = _q(holding.quantity) if holding else Decimal("0")
    new_qty = current_qty - trade.quantity if trade.side == Transaction.Side.BUY else current_qty + trade.quantity
    if new_qty < 0:
        raise TradeError("Undoing this trade would result in negative holdings.")

    if new_qty == 0:
        if holding:
            holding.delete()
    elif holding:
        holding.quantity = new_qty
        holding.save(update_fields=["quantity", "updated_at"])
    else:
        Holding.objects.create(account=trade.account, asset=trade.asset, quantity=new_qty)

    account = trade.account
    trade.delete()
    _stamp_snapshots(user, account)
