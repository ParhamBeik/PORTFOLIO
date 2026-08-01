import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from django.utils import timezone
from ..models import Account, LedgerEntry, Transaction, Holding

def _q(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")

def holdings_as_of(user, account, date) -> Dict[str, Decimal]:
    """Return asset quantities held in `account` as of the end of `date`.
    
    Walks the transaction ledger backwards from the current holding quantities.
    qty(t) = qty_now - sum(buys after t) + sum(sells after t)
    """
    if account.user_id != user.id:
        return {}

    holdings = Holding.objects.filter(account=account).select_related("asset")
    current_qty = {h.asset.key: _q(h.quantity) for h in holdings}

    if isinstance(date, datetime.datetime):
        target = date
    else:
        target = datetime.datetime.combine(date, datetime.time.max)
        target = timezone.make_aware(target, timezone.get_current_timezone())
        
    transactions = LedgerEntry.objects.filter(
        account=account,
        timestamp__gt=target
    ).select_related("asset")

    for txn in transactions:
        if txn.asset_id is None or txn.kind not in {
            LedgerEntry.Kind.OPENING_POSITION,
            LedgerEntry.Kind.BUY,
            LedgerEntry.Kind.SELL,
        }:
            continue
        asset_key = txn.asset.key
        qty = _q(txn.quantity)
        reversed_effect = txn.reversal_of_id is not None
        
        if asset_key not in current_qty:
            current_qty[asset_key] = Decimal("0")
            
        adds_position = txn.kind in {
            LedgerEntry.Kind.OPENING_POSITION,
            LedgerEntry.Kind.BUY,
        }
        if adds_position != reversed_effect:
            current_qty[asset_key] -= qty
        else:
            current_qty[asset_key] += qty
            
    result = {}
    for k, v in current_qty.items():
        if v > Decimal("0"):
            result[k] = v
            
    return result


def cash_as_of(user, account, at) -> Decimal:
    """Return the derived account cash balance at an exact timestamp."""
    if account.user_id != user.id:
        return Decimal("0")
    if isinstance(at, datetime.datetime):
        target = at
    else:
        target = timezone.make_aware(
            datetime.datetime.combine(at, datetime.time.max),
            timezone.get_current_timezone(),
        )
    cash = _q(account.cash_balance_tomans)
    positive = {
        LedgerEntry.Kind.OPENING_CASH,
        LedgerEntry.Kind.DEPOSIT,
        LedgerEntry.Kind.SELL,
        LedgerEntry.Kind.DIVIDEND,
    }
    negative = {
        LedgerEntry.Kind.WITHDRAWAL,
        LedgerEntry.Kind.BUY,
        LedgerEntry.Kind.FEE,
    }
    for entry in LedgerEntry.objects.filter(account=account, timestamp__gt=target):
        amount = _q(entry.amount_tomans)
        effect = amount if entry.kind in positive else -amount if entry.kind in negative else Decimal("0")
        if entry.reversal_of_id is not None:
            effect = -effect
        cash -= effect
    return cash

def xirr(cashflows: List[tuple[datetime.date, Decimal]]) -> float:
    """Calculate the Money-Weighted Return (XIRR)."""
    import scipy.optimize
    if not cashflows: return 0.0
    
    d0 = cashflows[0][0]
    
    def npv(r):
        return sum(float(cf) / ((1.0 + r) ** ((d - d0).days / 365.0)) for d, cf in cashflows)
        
    try:
        return scipy.optimize.newton(npv, 0.0, maxiter=100)
    except (RuntimeError, ValueError):
        return 0.0

def twr(periods: List[tuple[Decimal, Decimal, Decimal]]) -> Decimal:
    """Calculate Time-Weighted Return (TWR) given (start_value, end_value, net_cashflow) per period."""
    product = Decimal("1.0")
    for start, end, cf in periods:
        adj_start = start + cf
        if adj_start > Decimal("0"):
            product *= (end / adj_start)
    return product - Decimal("1.0")

def asset_metrics(transactions: List[Transaction], current_price: Decimal) -> dict:
    """Calculate weighted-average cost basis, realized/unrealized P&L."""
    # simplified implementation
    qty = Decimal("0")
    cost_basis = Decimal("0")
    realized_pnl = Decimal("0")
    
    for txn in sorted(transactions, key=lambda x: x.timestamp):
        t_qty = _q(txn.quantity)
        t_price = _q(txn.price_tomans)
        
        if txn.side == Transaction.Side.BUY:
            cost_basis = (cost_basis * qty + t_qty * t_price) / (qty + t_qty) if (qty + t_qty) > 0 else t_price
            qty += t_qty
        else:
            if qty > 0:
                realized_pnl += t_qty * (t_price - cost_basis)
                qty -= t_qty
                
    unrealized_pnl = (current_price - cost_basis) * qty if qty > 0 else Decimal("0")
    
    return {
        "cost_basis": cost_basis,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "quantity": qty
    }
