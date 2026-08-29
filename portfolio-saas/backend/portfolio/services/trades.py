"""Trade compatibility wrappers over the immutable ledger service.

`/trades/` remains for one release as a thin caller of `create_ledger_entry` /
`reverse_ledger_entry`. Corrections append reversal rows; ledger rows are never
deleted. Holdings and cash stay derived projections updated by the ledger path.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import Account, Asset, Holding, LedgerEntry, Snapshot, Transaction
from .ledger import (
    LedgerError,
    PriceResolutionError,
    create_ledger_entry,
    resolve_historical_price,
    reverse_ledger_entry,
)
from .valuation import get_latest_prices, value_account, value_user


class TradeError(Exception):
    """Base for trade validation failures (mapped to HTTP 400 by the view)."""


class InsufficientHolding(TradeError):
    """Raised when a sell exceeds the current quantity held."""


class ManualAssetTrade(TradeError):
    """Raised when trading a house asset (valued by formula, not quantity)."""


_SOURCE_MAP = {
    "manual": "manual",
    "imported": "csv",
    "inferred": "system",
    "csv": "csv",
    "system": "system",
}


def _q(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def _stamp_snapshots(user, account: Account) -> dict:
    # Snapshots record everything owned, hidden holdings included, matching the
    # cron writer in portfolio.tasks. The stored series has to keep one meaning
    # across its whole length: if the writer started omitting whatever was
    # switched off today, the chart would show a cliff on the day someone ticked
    # a box. The read path subtracts hidden assets across the entire window
    # instead -- see SnapshotListView / valuation.hidden_value_series.
    valuation = value_user(user, include_hidden=True)
    account_total = value_account(account, include_hidden=True)["total"]
    Snapshot.objects.bulk_create([
        Snapshot(user=user, account=None, total_value_tomans=valuation["total"]),
        Snapshot(user=user, account=account, total_value_tomans=account_total),
    ])
    return valuation


def _map_ledger_error(exc: LedgerError) -> TradeError:
    message = str(exc)
    lower = message.lower()
    if "holding" in lower:
        return InsufficientHolding(message)
    return TradeError(message)


def provision_asset(symbol_or_key: str) -> Asset:
    """Create an Asset on demand from MarketInstrument metadata if not exists.

    Validates through Asset.full_clean() to ensure eligibility.
    """
    asset = Asset.objects.filter(key=symbol_or_key).first()
    if asset:
        return asset

    asset = (
        Asset.objects.filter(tse_symbol=symbol_or_key).first()
        or Asset.objects.filter(brs_symbol=symbol_or_key).first()
    )
    if asset:
        return asset

    from marketdata.models import MarketInstrument
    mi = (
        MarketInstrument.objects.filter(symbol=symbol_or_key).first()
        or MarketInstrument.objects.filter(symbol__iexact=symbol_or_key).first()
    )
    if not mi and "_" in symbol_or_key:
        sym = symbol_or_key.split("_")[0]
        mi = MarketInstrument.objects.filter(symbol__iexact=sym).first()

    if not mi:
        raise TradeError(
            f"No matching eligible MarketInstrument found for symbol: {symbol_or_key}"
        )

    # The same classifier the picker uses, not a second copy of the rules. This
    # branch had its own hard-coded FX list and fell through to GOLD for anything
    # else, so a Bitcoin position created by symbol became a Gold asset while the
    # identical instrument added from the wizard became Crypto.
    from .catalog import _class_for

    asset_class = _class_for(mi)

    import re
    clean_sym = re.sub(r"[^a-zA-Z0-9_]", "", mi.symbol).lower()
    key = f"{clean_sym}_stock" if asset_class == Asset.AssetClass.STOCK else clean_sym

    existing = Asset.objects.filter(key=key).first()
    if existing:
        return existing

    asset = Asset(
        key=key,
        name=mi.name or mi.symbol,
        asset_class=asset_class,
        currency=Asset.Currency.IRT,
        tse_symbol=mi.symbol if mi.source == MarketInstrument.Source.TSETMC else "",
        brs_symbol=mi.symbol if mi.source == MarketInstrument.Source.BRS else "",
        is_active=True,
    )
    asset.full_clean()
    asset.save()
    return asset


@transaction.atomic
def execute_trade(
    *,
    account: Account,
    asset: Asset | str,
    side: str,
    quantity,
    price_tomans: Decimal | None = None,
    timestamp=None,
    source: str = "manual",
    skip_snapshots: bool = False,
    note: str = "",
) -> dict:
    """Record one buy/sell via the ledger. Returns a summary dict.

    Atomic: ledger row + holding/cash projections + optional net-worth snapshot.
    """
    if isinstance(asset, str):
        asset = provision_asset(asset)
    if side not in Transaction.Side.values:
        raise TradeError(f"side must be one of {Transaction.Side.values}")
    qty = _q(quantity)
    if qty <= 0:
        raise TradeError("quantity must be positive")
    if asset.is_house:
        raise ManualAssetTrade(
            "house assets are not tradeable; edit the holding directly"
        )
    occurred_at = timestamp or timezone.now()
    if occurred_at > timezone.now():
        raise TradeError("Transaction timestamp cannot be in the future.")
    if source not in _SOURCE_MAP:
        raise TradeError("Invalid transaction source.")
    ledger_source = _SOURCE_MAP[source]

    if price_tomans is not None:
        price = _q(price_tomans)
        if price <= 0:
            raise TradeError("Price must be positive.")
    else:
        try:
            price = resolve_historical_price(asset, occurred_at)
        except PriceResolutionError as exc:
            raise TradeError("No valid execution price is available.") from exc
        if price <= 0:
            raise TradeError("No valid execution price is available.")

    try:
        entry = create_ledger_entry(
            account=account,
            kind=side,
            asset=asset,
            quantity=qty,
            unit_price_tomans=price,
            occurred_at=occurred_at,
            source=ledger_source,
            note=note,
        )
    except LedgerError as exc:
        raise _map_ledger_error(exc) from exc

    holding = Holding.objects.filter(account=account, asset=asset).first()
    new_qty = _q(holding.quantity) if holding else Decimal("0")

    if not skip_snapshots:
        _stamp_snapshots(account.user, account)
        # What the caller is told is what the caller can see. The snapshot writer
        # stores the hidden-inclusive total on purpose (see `_stamp_snapshots`),
        # but reusing that figure here reported a portfolio 46,961,000,000 T
        # larger than the dashboard on an account with two switched-off houses.
        # `include_hidden=True` blanks the per-item `is_hidden` flag, so the
        # visible total cannot be subtracted back out of it and has to be its own
        # pass -- cheap, because `get_latest_prices()` is already cached.
        total_value = str(value_user(account.user)["total"])
    else:
        total_value = "0"

    return {
        "asset_key": asset.key,
        "side": side,
        "quantity": str(qty),
        "price_tomans": str(price),
        "holding_quantity": format(new_qty.normalize(), "f"),
        "cash_flow_tomans": str(entry.amount_tomans or Decimal("0")),
        "total_value_tomans": total_value,
        "ledger_entry_id": entry.id,
    }


@transaction.atomic
def undo_trade(*, user, transaction_id: int) -> None:
    """Append a reversal for a ledger entry; rows are never deleted."""
    trade = LedgerEntry.objects.select_for_update().get(
        pk=transaction_id, account__user=user
    )
    try:
        reverse_ledger_entry(
            user=user, account_id=trade.account_id, entry_id=trade.id
        )
    except LedgerError as exc:
        raise _map_ledger_error(exc) from exc
    _stamp_snapshots(user, trade.account)
