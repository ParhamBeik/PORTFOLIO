import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from django.utils import timezone
from ..models import LedgerEntry, Holding

def _q(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")

_HOUSE_MARK_KINDS = (
    LedgerEntry.Kind.OPENING_POSITION,
    LedgerEntry.Kind.VALUATION_MARK,
)


def load_house_marks(account):
    """All live house marks for `account`, oldest first. One query."""
    return list(
        LedgerEntry.objects.filter(
            account=account,
            asset__is_house=True,
            kind__in=_HOUSE_MARK_KINDS,
            reversal_of__isnull=True,
            reversed_by__isnull=True,
        )
        .select_related("asset")
        .order_by("timestamp", "pk")
    )


def house_state_as_of(marks, target) -> tuple:
    """Price-per-sqm and area in force at `target`, from a preloaded mark list."""
    qty: Dict[str, Decimal] = {}
    area: Dict[str, Decimal] = {}
    for mark in marks:
        if mark.timestamp <= target:
            qty[mark.asset.key] = _q(mark.quantity)
            if mark.area_sqm is not None:
                area[mark.asset.key] = _q(mark.area_sqm)
    return qty, area


def house_marks_as_of(account, target) -> Dict[str, Decimal]:
    """Price-per-sqm in force for each house asset at `target`.

    Real-estate marks REPLACE each other; they do not accumulate the way buys
    and sells do. Running them through the additive walk in `holdings_as_of`
    would subtract the opening mark and value the house at zero, so houses are
    resolved here instead: take the most recent non-reversed mark at or before
    the target date.
    """
    qty, _area = house_state_as_of(load_house_marks(account), target)
    return qty


def house_area_as_of(account, target) -> Dict[str, Decimal]:
    """Area travelling with the mark in force, so history is not re-measured."""
    _qty, area = house_state_as_of(load_house_marks(account), target)
    return area


def holdings_as_of(user, account, date) -> Dict[str, Decimal]:
    """Return asset quantities held in `account` as of the end of `date`.

    Walks the transaction ledger backwards from the current holding quantities.
    qty(t) = qty_now - sum(buys after t) + sum(sells after t)

    Houses are excluded from that walk and resolved by `house_marks_as_of`,
    because their "quantity" is a price mark rather than a position size.
    """
    if account.user_id != user.id:
        return {}

    holdings = Holding.objects.filter(account=account).select_related("asset")
    current_qty = {
        h.asset.key: _q(h.quantity) for h in holdings if not h.asset.is_house
    }

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
        # Houses are marks, not positions: unwinding them additively would drive
        # the price-per-sqm to zero. house_marks_as_of resolves them below.
        if txn.asset.is_house:
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

    # Houses carry the mark in force on that date, not a walked-back position.
    for key, price_per_sqm in house_marks_as_of(account, target).items():
        if price_per_sqm > Decimal("0"):
            result[key] = price_per_sqm

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

def xirr(cashflows: List[tuple[datetime.date, Decimal]]) -> Optional[float]:
    """Calculate the Money-Weighted Return (XIRR).

    Returns None on failure to converge — never a disguised 0.0, since a failed
    solve is not the same thing as a genuine 0% return.
    """
    import scipy.optimize
    if not cashflows:
        return None

    d0 = cashflows[0][0]

    def npv(r):
        return sum(float(cf) / ((1.0 + r) ** ((d - d0).days / 365.0)) for d, cf in cashflows)

    # Scan a wide, sane rate range for a sign change, then root-find inside
    # that bracket with brentq (guaranteed convergence within a valid bracket).
    # An unbracketed Newton solve from a fixed guess can silently diverge or
    # land on a spurious root, which is exactly what produced the disguised 0.0.
    lo, hi, steps = -0.9999, 100.0, 400
    prev_r, prev_v = lo, npv(lo)
    for i in range(1, steps + 1):
        r = lo + (hi - lo) * i / steps
        v = npv(r)
        if (prev_v < 0) != (v < 0):
            try:
                return scipy.optimize.brentq(npv, prev_r, r, maxiter=200)
            except (RuntimeError, ValueError):
                return None
        prev_r, prev_v = r, v
    return None
