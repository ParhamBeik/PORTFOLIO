"""Ledger-aware, account-scoped investment performance."""
import datetime as dt
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Max, Sum
from django.utils import timezone

from marketdata.currency import holding_value_to_toman, is_tse_priced

from ..models import LedgerEntry
from .deflator import cpi_for_date, normalize_basis
from .timeline import xirr
from .valuation import get_latest_prices, value_account, value_as_of

# Below this many tracked days, an annualized return (XIRR) is noise, not signal.
MIN_TRACKING_DAYS_FOR_ANNUALIZED = 90


def _conversion_rate(basis: str, as_of) -> Decimal | None:
    """Resolve the Toman-per-unit rate for a USD/USDT basis.

    Mirrors valuation.py's resolution order (`value_as_of` ~L572-586) so the
    opening valuation and the current value are never priced in different units.
    """
    if basis == "usd_denominated":
        rate = Decimal(str(get_latest_prices().get("usd_cash", 0) or 0))
        return rate if rate > 0 else None
    if basis == "usdt_denominated":
        from marketdata.models import GoldCurrencyHistory
        from .returns import to_jalali_str

        jalali = to_jalali_str(as_of)
        row = (
            GoldCurrencyHistory.objects.filter(symbol="USDT_IRT", date__lte=jalali)
            .order_by("-date").first()
        )
        if not row or row.close_price <= 0:
            row = (
                GoldCurrencyHistory.objects.filter(symbol="USD", date__lte=jalali)
                .order_by("-date").first()
            )
        if not row or row.close_price <= 0:
            return None
        return Decimal(str(row.close_price))
    return None


def _current_value(account, basis: str) -> Decimal | None:
    total = Decimal(value_account(account)["total"]) + account.cash_balance_tomans
    if basis in ("usd_denominated", "usdt_denominated"):
        rate = _conversion_rate(basis, timezone.now())
        return total / rate if rate else None
    if basis == "real_toman":
        return total / Decimal(str(cpi_for_date(timezone.now()))) * Decimal("100")
    return total


def _flow_amount(entry, basis: str) -> Decimal | None:
    amount = Decimal(entry.amount_tomans or 0)
    if basis == "nominal_toman":
        return amount
    if basis == "real_toman":
        return amount / Decimal(str(cpi_for_date(entry.timestamp))) * Decimal("100")
    rate = _conversion_rate(basis, entry.timestamp)
    return amount / rate if rate else None


def _position_metrics(account) -> dict:
    from .visibility import hidden_asset_ids

    hidden_ids = hidden_asset_ids([account])
    version = account.transactions.aggregate(
        count=Count("id"), max_id=Max("id"), id_sum=Sum("id")
    )
    cache_key = (
        f"position-metrics:{account.id}:{version['count']}:"
        f"{version['max_id'] or 0}:{version['id_sum'] or 0}:"
        # Ticking an asset off changes this result without touching a single
        # ledger row, so the visibility set has to be part of the key or the
        # cached answer outlives the toggle for an hour.
        f"{'-'.join(str(i) for i in sorted(hidden_ids))}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    prices = get_latest_prices()
    valuation_items = {
        item["key"]: item
        for item in value_account(account).get("items", [])
    }
    result = {}
    entries_by_asset = {}
    for entry in account.transactions.select_related("asset").order_by(
        "timestamp", "pk"
    ):
        if entry.asset_id and entry.asset_id not in hidden_ids:
            entries_by_asset.setdefault(entry.asset_id, []).append(entry)
    for entries in entries_by_asset.values():
        asset = entries[0].asset
        quantity = Decimal("0")
        average_cost = Decimal("0")
        realized = Decimal("0")
        unknown_basis = False
        # A reversal pair nets to nothing: skip the reversal row AND the row it
        # reverses, or the cost basis keeps an event the holdings no longer have.
        reversed_ids = {
            entry.reversal_of_id for entry in entries if entry.reversal_of_id
        }
        for entry in entries:
            if entry.reversal_of_id or entry.pk in reversed_ids:
                continue
            qty = Decimal(entry.quantity or 0)
            price = Decimal(entry.price_tomans or 0)
            if entry.kind == LedgerEntry.Kind.OPENING_POSITION:
                quantity += qty
                unknown_basis = True
            elif entry.kind == LedgerEntry.Kind.RIGHTS_ISSUE:
                # Free shares: the money already spent now buys more of them, so
                # the average cost falls and the basis stays KNOWN. Treating this
                # as an opening would void the basis of every purchase before it,
                # and treating it as a zero-price buy would trip the "no price
                # recorded" sentinel to the same effect.
                if not unknown_basis and quantity + qty > 0:
                    average_cost = average_cost * quantity / (quantity + qty)
                quantity += qty
            elif entry.kind == LedgerEntry.Kind.BUY:
                if price <= 0:
                    # price_tomans == 0/NULL is the model's sentinel for "no
                    # price recorded yet" (see LedgerEntry docstring), not a
                    # free buy — treat cost basis as unknown from here on.
                    unknown_basis = True
                known_quantity = Decimal("0") if unknown_basis else quantity
                if not unknown_basis and quantity + qty > 0:
                    average_cost = (average_cost * known_quantity + price * qty) / (quantity + qty)
                quantity += qty
            elif entry.kind == LedgerEntry.Kind.SELL:
                if price <= 0:
                    unknown_basis = True
                if not unknown_basis:
                    realized += qty * (price - average_cost)
                quantity -= qty
        current_price = Decimal(str(prices.get(asset.key, 0) or 0))
        # `average_cost` is a UNIT price and stays in the asset's own quote unit
        # (Rial for TSE), matching what the UI shows next to the live price.
        # Everything below it is money, so each one is a quantity x price
        # product and converts exactly once -- see currency.holding_value_to_toman.
        result[asset.key] = {
            "asset_key": asset.key,
            # Same naming rule as `Holding.label`: a TSE position is known by its
            # ticker, not by the registered company name.
            "asset_name": asset.tse_symbol or asset.name_fa or asset.name,
            "quantity": str(quantity),
            # Same declaration the valuation rows carry, so this table and the
            # holdings table print the same count to the same precision.
            "quantity_step": asset.quantity_step,
            "cost_basis_known": not unknown_basis,
            "average_cost_tomans": str(average_cost) if not unknown_basis else None,
            # ...which, despite the field name, is Rial for a TSE share. The
            # three money fields under it are products and have been divided;
            # this one is a unit price and has not. Without the unit travelling
            # beside it the client can only guess, and guessing "Toman" prints a
            # 46,348-Rial average cost as "46,348 T" -- ten times what was paid,
            # one column away from a cost basis that IS Toman and therefore does
            # not reconcile against it. Same declaration the holdings rows carry.
            "average_cost_currency": "rial" if is_tse_priced(asset) else "toman",
            "total_cost_basis_tomans": str(
                holding_value_to_toman(asset, average_cost * quantity)
            ) if not unknown_basis else None,
            "realized_pnl_tomans": str(
                holding_value_to_toman(asset, realized)
            ) if not unknown_basis else None,
            "unrealized_pnl_tomans": str(
                holding_value_to_toman(asset, (current_price - average_cost) * quantity)
            ) if not unknown_basis else None,
            "current_value_tomans": (
                str(valuation_items[asset.key]["value"])
                if asset.key in valuation_items
                and valuation_items[asset.key].get("value") is not None
                else None
            ),
        }
    for holding in account.holdings.select_related("asset").filter(is_hidden=False):
        asset = holding.asset
        if asset.key in result:
            continue
        item = valuation_items.get(asset.key)
        result[asset.key] = {
            "asset_key": asset.key,
            "asset_name": asset.tse_symbol or asset.name_fa or asset.name,
            "quantity": str(holding.quantity),
            "quantity_step": asset.quantity_step,
            "cost_basis_known": False,
            "average_cost_tomans": None,
            "average_cost_currency": "rial" if is_tse_priced(asset) else "toman",
            "total_cost_basis_tomans": None,
            "realized_pnl_tomans": None,
            "unrealized_pnl_tomans": None,
            "current_value_tomans": (
                str(item["value"])
                if item and item.get("value") is not None
                else None
            ),
        }
    cache.set(cache_key, result, timeout=3600)
    return result


def account_performance(account, *, basis=None) -> dict:
    basis = normalize_basis(basis)
    if not account.ledger_complete or not account.tracking_started_at:
        return {
            "performance_available": False,
            "reason": "opening_baseline_missing",
            "detail": "Complete an opening baseline before calculating performance.",
        }

    start = account.tracking_started_at
    days_tracked = (timezone.now() - start).days
    if days_tracked < MIN_TRACKING_DAYS_FOR_ANNUALIZED:
        remaining = MIN_TRACKING_DAYS_FOR_ANNUALIZED - days_tracked
        return {
            "performance_available": False,
            "reason": "insufficient_history",
            "detail": f"Performance available after {remaining} more day(s) of tracking.",
            "tracking_started_at": start.isoformat(),
            "days_tracked": days_tracked,
            # TWR and XIRR are the only things the 90-day bar protects: they are
            # annualized, so they are noise before then. Cost basis and P&L are
            # not annualized and not time-weighted -- they are just the recorded
            # trades against today's price, and they are correct from the first
            # buy. Withholding them left the panel with nothing on it for the
            # first three months of every account's life.
            "assets": _position_metrics(account),
        }

    start_payload = value_as_of(
        account.user, account=account, as_of=start, basis=basis
    )
    if start_payload.get("quality_status") != "complete":
        return {
            "performance_available": False,
            "reason": "incomplete_opening_valuation",
            "detail": "Opening valuation is incomplete.",
            "excluded": start_payload.get("excluded", []),
        }
    start_value = Decimal(str(start_payload["total"]))
    current_value = _current_value(account, basis)
    if current_value is None:
        return {
            "performance_available": False,
            "reason": "conversion_rate_unavailable",
            "detail": "USD rate is unavailable.",
        }

    # Dividends are portfolio-generated return, not external investor capital.
    # Reversal pairs net to nothing, or a reversed deposit shows up as a real
    # cash flow into TWR/XIRR -- `ledger.active_entries` owns that rule.
    from .ledger import active_entries

    flows = active_entries(
        account, kinds=[LedgerEntry.Kind.DEPOSIT, LedgerEntry.Kind.WITHDRAWAL]
    )
    factor = Decimal("1")
    segment_start = start_value
    investor_cashflows = [(start.date(), -start_value)]
    for entry in flows:
        before = value_as_of(
            account.user,
            account=account,
            as_of=entry.timestamp - dt.timedelta(microseconds=1),
            basis=basis,
        )
        if before.get("quality_status") != "complete":
            return {
                "performance_available": False,
                "reason": "incomplete_opening_valuation",
                "detail": "A cash-flow boundary valuation is incomplete.",
                "excluded": before.get("excluded", []),
            }
        before_value = Decimal(str(before["total"]))
        if segment_start <= 0:
            return {
                "performance_available": False,
                "reason": "non_positive_segment",
                "detail": "A TWR segment start value was zero or negative.",
            }
        factor *= before_value / segment_start
        flow = _flow_amount(entry, basis)
        if flow is None:
            return {
                "performance_available": False,
                "reason": "conversion_rate_unavailable",
                "detail": "USD rate is unavailable.",
            }
        signed_flow = flow if entry.kind == LedgerEntry.Kind.DEPOSIT else -flow
        segment_start = before_value + signed_flow
        investor_cashflows.append((entry.timestamp.date(), -signed_flow))

    if segment_start <= 0:
        return {
            "performance_available": False,
            "reason": "non_positive_segment",
            "detail": "A TWR segment start value was zero or negative.",
        }
    factor *= current_value / segment_start
    investor_cashflows.append((timezone.now().date(), current_value))

    xirr_value = xirr(investor_cashflows)
    if xirr_value is None:
        return {
            "performance_available": False,
            "reason": "xirr_did_not_converge",
            "detail": "The money-weighted return (XIRR) calculation did not converge.",
        }

    return {
        "performance_available": True,
        "basis": basis,
        "tracking_started_at": start.isoformat(),
        "days_tracked": days_tracked,
        "current_value_tomans": str(
            Decimal(value_account(account)["total"]) + account.cash_balance_tomans
        ),
        "current_value": str(current_value),
        "external_flow_count": len(flows),
        "twr": float(factor - Decimal("1")),
        "xirr": xirr_value,
        "assets": _position_metrics(account),
        "methodology": "cash-flow-boundary TWR; investor XIRR; trades are internal",
    }
