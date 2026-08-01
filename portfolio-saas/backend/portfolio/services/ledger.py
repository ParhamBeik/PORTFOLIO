"""Immutable account-ledger writes and derived projection updates."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import Account, Asset, Holding, LedgerEntry


class LedgerError(Exception):
    pass


def _decimal(value, field: str, *, required: bool = False) -> Decimal | None:
    if value in (None, ""):
        if required:
            raise LedgerError(f"{field} is required.")
        return None
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        raise LedgerError(f"{field} must be a number.") from None
    if result <= 0:
        raise LedgerError(f"{field} must be positive.")
    return result


def _holding_delta(kind: str, quantity: Decimal, reverse: bool) -> Decimal:
    direction = Decimal("-1") if reverse else Decimal("1")
    if kind in {LedgerEntry.Kind.OPENING_POSITION, LedgerEntry.Kind.BUY}:
        return quantity * direction
    if kind == LedgerEntry.Kind.SELL:
        return -quantity * direction
    return Decimal("0")


def _cash_delta(kind: str, amount: Decimal, reverse: bool) -> Decimal:
    direction = Decimal("-1") if reverse else Decimal("1")
    if kind in {
        LedgerEntry.Kind.OPENING_CASH,
        LedgerEntry.Kind.DEPOSIT,
        LedgerEntry.Kind.SELL,
        LedgerEntry.Kind.DIVIDEND,
    }:
        return amount * direction
    if kind in {
        LedgerEntry.Kind.WITHDRAWAL,
        LedgerEntry.Kind.BUY,
        LedgerEntry.Kind.FEE,
    }:
        return -amount * direction
    return Decimal("0")


def _apply_projection(
    *, account: Account, kind: str, asset: Asset | None, quantity: Decimal | None,
    amount: Decimal | None, reverse: bool = False
) -> None:
    cash_delta = _cash_delta(kind, amount or Decimal("0"), reverse)
    new_cash = account.cash_balance_tomans + cash_delta
    if new_cash < 0:
        raise LedgerError("Insufficient cash balance.")

    if asset is not None and quantity is not None:
        holding = (
            Holding.objects.select_for_update()
            .filter(account=account, asset=asset)
            .first()
        )
        current = holding.quantity if holding else Decimal("0")
        new_quantity = current + _holding_delta(kind, quantity, reverse)
        if new_quantity < 0:
            raise LedgerError("Insufficient holding quantity.")
        if new_quantity == 0 and holding:
            holding.delete()
        elif holding:
            holding.quantity = new_quantity
            holding.save(update_fields=["quantity", "updated_at"])
        elif new_quantity > 0:
            Holding.objects.create(account=account, asset=asset, quantity=new_quantity)

    account.cash_balance_tomans = new_cash
    account.save(update_fields=["cash_balance_tomans", "updated_at"])


@transaction.atomic
def create_ledger_entry(
    *, account: Account, kind: str, occurred_at=None, asset: Asset | None = None,
    quantity=None, unit_price_tomans=None, amount_tomans=None,
    source: str = "manual", note: str = "", external_id: str = "",
    import_batch=None,
) -> LedgerEntry:
    if kind not in LedgerEntry.Kind.values:
        raise LedgerError("Invalid ledger entry kind.")
    if source not in {"manual", "csv", "system"}:
        raise LedgerError("Invalid ledger entry source.")
    occurred_at = occurred_at or timezone.now()
    if occurred_at > timezone.now():
        raise LedgerError("occurred_at cannot be in the future.")

    quantity = _decimal(quantity, "quantity", required=kind in {
        LedgerEntry.Kind.OPENING_POSITION, LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL
    })
    unit_price = _decimal(
        unit_price_tomans,
        "unit_price_tomans",
        required=kind in {LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL},
    )
    amount = _decimal(
        amount_tomans,
        "amount_tomans",
        required=kind in {
            LedgerEntry.Kind.OPENING_CASH,
            LedgerEntry.Kind.DEPOSIT,
            LedgerEntry.Kind.WITHDRAWAL,
            LedgerEntry.Kind.DIVIDEND,
            LedgerEntry.Kind.FEE,
        },
    )
    if kind in {LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL}:
        amount = (quantity * unit_price).quantize(Decimal("0.0001"))
    if kind in {
        LedgerEntry.Kind.OPENING_POSITION,
        LedgerEntry.Kind.BUY,
        LedgerEntry.Kind.SELL,
        LedgerEntry.Kind.DIVIDEND,
    } and asset is None:
        raise LedgerError("asset_key is required for this entry kind.")

    account = Account.objects.select_for_update().get(pk=account.pk)
    if kind in {LedgerEntry.Kind.OPENING_CASH, LedgerEntry.Kind.OPENING_POSITION}:
        if account.tracking_started_at and account.tracking_started_at != occurred_at:
            raise LedgerError("Opening entries must share the tracking start timestamp.")
        account.tracking_started_at = occurred_at
        account.ledger_complete = True
        account.save(update_fields=["tracking_started_at", "ledger_complete", "updated_at"])

    _apply_projection(
        account=account,
        kind=kind,
        asset=asset,
        quantity=quantity,
        amount=amount,
    )
    return LedgerEntry.objects.create(
        account=account,
        asset=asset,
        kind=kind,
        quantity=quantity,
        price_tomans=unit_price,
        amount_tomans=amount,
        timestamp=occurred_at,
        source=source,
        note=note[:200],
        external_id=external_id[:120],
        import_batch=import_batch,
    )


@transaction.atomic
def reverse_ledger_entry(*, user, account_id: int, entry_id: int) -> LedgerEntry:
    entry = (
        LedgerEntry.objects.select_for_update()
        .get(pk=entry_id, account_id=account_id, account__user=user)
    )
    if LedgerEntry.objects.filter(reversal_of=entry).exists():
        raise LedgerError("Ledger entry has already been reversed.")
    account = Account.objects.select_for_update().get(pk=entry.account_id)
    _apply_projection(
        account=account,
        kind=entry.kind,
        asset=entry.asset,
        quantity=entry.quantity,
        amount=entry.amount_tomans,
        reverse=True,
    )
    return LedgerEntry.objects.create(
        account=account,
        asset=entry.asset,
        kind=entry.kind,
        quantity=entry.quantity,
        price_tomans=entry.price_tomans,
        amount_tomans=entry.amount_tomans,
        timestamp=timezone.now(),
        source="system",
        note=f"Reversal of ledger entry {entry.pk}",
        reversal_of=entry,
    )
