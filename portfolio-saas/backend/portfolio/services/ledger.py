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
    amount: Decimal | None, area_sqm: Decimal | None = None,
    mortgage_deduction_tomans: Decimal | None = None, reverse: bool = False
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
            fields = ["quantity", "updated_at"]
            if asset.is_house and not reverse:
                holding.area_sqm = area_sqm or holding.area_sqm
                holding.mortgage_deduction_tomans = (
                    mortgage_deduction_tomans or holding.mortgage_deduction_tomans
                )
                fields += ["area_sqm", "mortgage_deduction_tomans"]
            holding.save(update_fields=fields)
        elif new_quantity > 0:
            values = {"account": account, "asset": asset, "quantity": new_quantity}
            if asset.is_house:
                values.update(
                    area_sqm=area_sqm or Decimal("90.2"),
                    mortgage_deduction_tomans=(
                        mortgage_deduction_tomans or Decimal("400000000")
                    ),
                )
            Holding.objects.create(**values)

    account.cash_balance_tomans = new_cash
    account.save(update_fields=["cash_balance_tomans", "updated_at"])


@transaction.atomic
def create_ledger_entry(
    *, account: Account, kind: str, occurred_at=None, asset: Asset | None = None,
    quantity=None, unit_price_tomans=None, amount_tomans=None,
    area_sqm=None, mortgage_deduction_tomans=None,
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
    area = _decimal(area_sqm, "area_sqm")
    mortgage = _decimal(
        mortgage_deduction_tomans, "mortgage_deduction_tomans"
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
    if area is not None or mortgage is not None:
        if kind != LedgerEntry.Kind.OPENING_POSITION or not asset or not asset.is_house:
            raise LedgerError(
                "Real-estate baseline fields require a house opening position."
            )
    if asset and asset.is_house and kind == LedgerEntry.Kind.OPENING_POSITION:
        area = area or Decimal("90.2")
        mortgage = mortgage or Decimal("400000000")

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
        area_sqm=area,
        mortgage_deduction_tomans=mortgage,
    )
    return LedgerEntry.objects.create(
        account=account,
        asset=asset,
        kind=kind,
        quantity=quantity,
        price_tomans=unit_price,
        amount_tomans=amount,
        area_sqm=area,
        mortgage_deduction_tomans=mortgage,
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
        area_sqm=entry.area_sqm,
        mortgage_deduction_tomans=entry.mortgage_deduction_tomans,
        reverse=True,
    )
    return LedgerEntry.objects.create(
        account=account,
        asset=entry.asset,
        kind=entry.kind,
        quantity=entry.quantity,
        price_tomans=entry.price_tomans,
        amount_tomans=entry.amount_tomans,
        area_sqm=entry.area_sqm,
        mortgage_deduction_tomans=entry.mortgage_deduction_tomans,
        timestamp=timezone.now(),
        source="system",
        note=f"Reversal of ledger entry {entry.pk}",
        reversal_of=entry,
    )


def _active_entries(account: Account) -> list[LedgerEntry]:
    """Ledger rows that still affect projections (reversal pairs net out)."""
    entries = list(
        LedgerEntry.objects.filter(account=account)
        .select_related("asset")
        .order_by("timestamp", "pk")
    )
    reversed_ids = {entry.reversal_of_id for entry in entries if entry.reversal_of_id}
    return [
        entry
        for entry in entries
        if entry.reversal_of_id is None and entry.pk not in reversed_ids
    ]


def _projection_state(account: Account) -> dict:
    holdings: dict[int, Decimal] = {}
    real_estate: dict[int, dict[str, Decimal]] = {}
    cash = Decimal("0")
    entries = _active_entries(account)
    for entry in entries:
        qty = entry.quantity or Decimal("0")
        amount = entry.amount_tomans or Decimal("0")
        if entry.kind in {
            LedgerEntry.Kind.OPENING_POSITION,
            LedgerEntry.Kind.BUY,
        } and entry.asset_id:
            holdings[entry.asset_id] = holdings.get(entry.asset_id, Decimal("0")) + qty
        elif entry.kind == LedgerEntry.Kind.SELL and entry.asset_id:
            holdings[entry.asset_id] = holdings.get(entry.asset_id, Decimal("0")) - qty
        cash += _cash_delta(entry.kind, amount, reverse=False)
        if cash < 0:
            raise LedgerError(f"Ledger replay produced negative cash at entry {entry.pk}.")
        if entry.asset_id and holdings.get(entry.asset_id, Decimal("0")) < 0:
            raise LedgerError(
                f"Ledger replay produced negative holding quantity at entry {entry.pk}."
            )
        if entry.asset and entry.asset.is_house and entry.kind == LedgerEntry.Kind.OPENING_POSITION:
            real_estate[entry.asset_id] = {
                "area_sqm": entry.area_sqm or Decimal("90.2"),
                "mortgage_deduction_tomans": (
                    entry.mortgage_deduction_tomans or Decimal("400000000")
                ),
            }
    return {
        "holdings": {
            asset_id: qty for asset_id, qty in holdings.items() if qty != 0
        },
        "cash": cash,
        "real_estate": real_estate,
        "ledger_complete": any(
            entry.kind in {
                LedgerEntry.Kind.OPENING_CASH,
                LedgerEntry.Kind.OPENING_POSITION,
            }
            for entry in entries
        ),
    }


def compute_projections(account: Account) -> tuple[dict[int, Decimal], Decimal]:
    """Derive holding quantities and cash from the immutable ledger."""
    state = _projection_state(account)
    return state["holdings"], state["cash"]


@transaction.atomic
def rebuild_projections(account: Account) -> dict:
    """Rewrite Holding rows and cash_balance_tomans from the ledger."""
    account = Account.objects.select_for_update().get(pk=account.pk)
    state = _projection_state(account)
    expected_holdings = state["holdings"]
    expected_cash = state["cash"]
    managed_asset_ids = set(
        LedgerEntry.objects.filter(account=account, asset__isnull=False)
        .values_list("asset_id", flat=True)
    )

    Holding.objects.filter(account=account, asset_id__in=managed_asset_ids).exclude(
        asset_id__in=expected_holdings
    ).delete()
    for asset_id, quantity in expected_holdings.items():
        defaults = {"quantity": quantity}
        defaults.update(state["real_estate"].get(asset_id, {}))
        Holding.objects.update_or_create(
            account=account, asset_id=asset_id, defaults=defaults
        )

    account.cash_balance_tomans = expected_cash
    account.ledger_complete = state["ledger_complete"]
    account.save(
        update_fields=["cash_balance_tomans", "ledger_complete", "updated_at"]
    )
    return {
        "account_id": account.id,
        "cash_balance_tomans": str(expected_cash),
        "holdings": {
            str(asset_id): str(qty) for asset_id, qty in expected_holdings.items()
        },
    }


def projection_drift(account: Account) -> list[dict]:
    """Return differences between stored projections and a ledger replay."""
    state = _projection_state(account)
    expected_holdings = state["holdings"]
    expected_cash = state["cash"]
    drifts: list[dict] = []

    stored_cash = account.cash_balance_tomans or Decimal("0")
    if round(stored_cash, 6) != round(expected_cash, 6):
        drifts.append({
            "kind": "cash",
            "stored": str(stored_cash),
            "ledger": str(expected_cash),
        })

    managed_asset_ids = set(
        LedgerEntry.objects.filter(account=account, asset__isnull=False)
        .values_list("asset_id", flat=True)
    )
    stored_rows = {
        h.asset_id: h
        for h in Holding.objects.filter(account=account, asset_id__in=managed_asset_ids)
    }
    stored = {asset_id: holding.quantity for asset_id, holding in stored_rows.items()}
    for asset_id in set(stored) | set(expected_holdings):
        s_qty = stored.get(asset_id, Decimal("0"))
        e_qty = expected_holdings.get(asset_id, Decimal("0"))
        if round(s_qty, 6) != round(e_qty, 6):
            drifts.append({
                "kind": "holding",
                "asset_id": asset_id,
                "stored": str(s_qty),
                "ledger": str(e_qty),
            })
    for asset_id, terms in state["real_estate"].items():
        holding = stored_rows.get(asset_id)
        if not holding:
            continue
        for field, expected in terms.items():
            stored_value = getattr(holding, field)
            if stored_value != expected:
                drifts.append({
                    "kind": field,
                    "asset_id": asset_id,
                    "stored": str(stored_value),
                    "ledger": str(expected),
                })
    if account.ledger_complete != state["ledger_complete"]:
        drifts.append({
            "kind": "ledger_complete",
            "stored": account.ledger_complete,
            "ledger": state["ledger_complete"],
        })
    return drifts


@transaction.atomic
def replace_ledger_entry(
    *, user, account_id: int, entry_id: int, quantity,
    area_sqm=None, mortgage_deduction_tomans=None
) -> LedgerEntry:
    original = (
        LedgerEntry.objects.select_for_update()
        .get(pk=entry_id, account_id=account_id, account__user=user)
    )
    if original.kind != LedgerEntry.Kind.OPENING_POSITION:
        raise LedgerError("Only opening positions can be replaced.")
    reverse_ledger_entry(
        user=user, account_id=account_id, entry_id=original.pk
    )
    return create_ledger_entry(
        account=original.account,
        kind=original.kind,
        asset=original.asset,
        quantity=quantity,
        area_sqm=area_sqm,
        mortgage_deduction_tomans=mortgage_deduction_tomans,
        occurred_at=original.timestamp,
        source="system",
        note=f"Replacement for ledger entry {original.pk}",
    )


@transaction.atomic
def house_ledger_entry(holding: Holding) -> LedgerEntry:
    entry = (
        LedgerEntry.objects.filter(
            account=holding.account,
            asset=holding.asset,
            kind=LedgerEntry.Kind.OPENING_POSITION,
            reversal_of__isnull=True,
            reversed_by__isnull=True,
        )
        .order_by("timestamp", "pk")
        .first()
    )
    if entry:
        return entry
    account = Account.objects.select_for_update().get(pk=holding.account_id)
    values = {
        "asset": holding.asset,
        "quantity": holding.quantity,
        "area_sqm": holding.area_sqm,
        "mortgage_deduction_tomans": holding.mortgage_deduction_tomans,
    }
    holding.delete()
    return create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        occurred_at=account.tracking_started_at or timezone.now(),
        source="system",
        note="Real-estate baseline migrated to ledger",
        **values,
    )
