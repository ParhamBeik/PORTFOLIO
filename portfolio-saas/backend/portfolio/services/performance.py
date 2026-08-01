"""Ledger-aware, account-scoped investment performance."""
import datetime as dt
from decimal import Decimal

from django.utils import timezone

from ..models import LedgerEntry
from .timeline import xirr
from .valuation import get_latest_prices, value_account, value_as_of


def normalize_basis(value: str | None) -> str:
    aliases = {
        None: "nominal_toman",
        "nominal": "nominal_toman",
        "nominal_toman": "nominal_toman",
        "usd_real": "usd_denominated",
        "usd_denominated": "usd_denominated",
    }
    if value not in aliases:
        raise ValueError("basis must be nominal_toman or usd_denominated")
    return aliases[value]


def _current_value(account, basis: str) -> Decimal | None:
    total = Decimal(value_account(account)["total"]) + account.cash_balance_tomans
    if basis == "usd_denominated":
        rate = Decimal(str(get_latest_prices().get("usd_cash", 0) or 0))
        return total / rate if rate > 0 else None
    return total


def _flow_amount(entry, basis: str) -> Decimal | None:
    amount = Decimal(entry.amount_tomans or 0)
    if basis == "nominal_toman":
        return amount
    from marketdata.models import GoldCurrencyHistory
    from .returns import to_jalali_str

    row = GoldCurrencyHistory.objects.filter(
        symbol="USD", date__lte=to_jalali_str(entry.timestamp)
    ).order_by("-date").first()
    if not row or row.close_price <= 0:
        return None
    return amount / Decimal(str(row.close_price))


def _position_metrics(account) -> dict:
    prices = get_latest_prices()
    result = {}
    for asset in {e.asset for e in account.transactions.select_related("asset") if e.asset_id}:
        quantity = Decimal("0")
        average_cost = Decimal("0")
        realized = Decimal("0")
        unknown_basis = False
        entries = account.transactions.filter(asset=asset).order_by("timestamp", "pk")
        for entry in entries:
            if entry.reversal_of_id:
                continue
            qty = Decimal(entry.quantity or 0)
            price = Decimal(entry.price_tomans or 0)
            if entry.kind == LedgerEntry.Kind.OPENING_POSITION:
                quantity += qty
                unknown_basis = True
            elif entry.kind == LedgerEntry.Kind.BUY:
                known_quantity = Decimal("0") if unknown_basis else quantity
                if not unknown_basis and quantity + qty > 0:
                    average_cost = (average_cost * known_quantity + price * qty) / (quantity + qty)
                quantity += qty
            elif entry.kind == LedgerEntry.Kind.SELL:
                if not unknown_basis:
                    realized += qty * (price - average_cost)
                quantity -= qty
        current_price = Decimal(str(prices.get(asset.key, 0) or 0))
        result[asset.key] = {
            "asset_name": asset.name,
            "quantity": str(quantity),
            "cost_basis_known": not unknown_basis,
            "average_cost_tomans": str(average_cost) if not unknown_basis else None,
            "total_cost_basis_tomans": str(average_cost * quantity) if not unknown_basis else None,
            "realized_pnl_tomans": str(realized) if not unknown_basis else None,
            "unrealized_pnl_tomans": str((current_price - average_cost) * quantity) if not unknown_basis else None,
        }
    return result


def account_performance(account, *, basis=None) -> dict:
    basis = normalize_basis(basis)
    if not account.ledger_complete or not account.tracking_started_at:
        return {
            "performance_available": False,
            "detail": "Complete an opening baseline before calculating performance.",
        }

    start = account.tracking_started_at
    start_payload = value_as_of(
        account.user, account=account, as_of=start, basis=basis
    )
    if start_payload.get("quality_status") != "complete":
        return {
            "performance_available": False,
            "detail": "Opening valuation is incomplete.",
            "excluded": start_payload.get("excluded", []),
        }
    start_value = Decimal(str(start_payload["total"]))
    current_value = _current_value(account, basis)
    if current_value is None:
        return {
            "performance_available": False,
            "detail": "USD rate is unavailable.",
        }

    flows = list(account.transactions.filter(
        kind__in=[LedgerEntry.Kind.DEPOSIT, LedgerEntry.Kind.WITHDRAWAL],
        reversal_of__isnull=True,
    ).order_by("timestamp", "pk"))
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
                "detail": "A cash-flow boundary valuation is incomplete.",
                "excluded": before.get("excluded", []),
            }
        before_value = Decimal(str(before["total"]))
        if segment_start > 0:
            factor *= before_value / segment_start
        flow = _flow_amount(entry, basis)
        if flow is None:
            return {"performance_available": False, "detail": "USD rate is unavailable."}
        signed_flow = flow if entry.kind == LedgerEntry.Kind.DEPOSIT else -flow
        segment_start = before_value + signed_flow
        investor_cashflows.append((entry.timestamp.date(), -signed_flow))

    if segment_start > 0:
        factor *= current_value / segment_start
    investor_cashflows.append((timezone.now().date(), current_value))

    return {
        "performance_available": True,
        "basis": basis,
        "tracking_started_at": start.isoformat(),
        "current_value_tomans": str(
            Decimal(value_account(account)["total"]) + account.cash_balance_tomans
        ),
        "current_value": str(current_value),
        "external_flow_count": len(flows),
        "twr": float(factor - Decimal("1")),
        "xirr": float(xirr(investor_cashflows)),
        "assets": _position_metrics(account),
        "methodology": "cash-flow-boundary TWR; investor XIRR; trades are internal",
    }
