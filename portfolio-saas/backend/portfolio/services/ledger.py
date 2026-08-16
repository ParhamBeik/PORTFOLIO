"""Immutable account-ledger writes and derived projection updates."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import HOUSE_AREA_SQM, Account, Asset, Holding, LedgerEntry


class LedgerError(Exception):
    pass


def _decimal(value, field: str, *, required: bool = False, allow_zero: bool = False) -> Decimal | None:
    if value in (None, ""):
        if required:
            raise LedgerError(f"{field} is required.")
        return None
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        raise LedgerError(f"{field} must be a number.") from None
    if allow_zero:
        if result < 0:
            raise LedgerError(f"{field} must be positive or zero.")
    else:
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
    from portfolio.models import Liability
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
        if (
            asset.is_house
            and not reverse
            and kind in {
                LedgerEntry.Kind.OPENING_POSITION,
                LedgerEntry.Kind.VALUATION_MARK,
            }
        ):
            # A house "quantity" is price-per-sqm, so a new mark REPLACES the
            # old one. Adding would read a revaluation from 90 to 150 as 240.
            new_quantity = quantity
        else:
            new_quantity = current + _holding_delta(kind, quantity, reverse)
        if new_quantity < 0:
            raise LedgerError("Insufficient holding quantity.")
        if new_quantity == 0 and holding:
            holding.delete()
            if asset.is_house:
                Liability.objects.filter(account=account, asset=asset).delete()
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
            if asset.is_house:
                mortgage_val = holding.mortgage_deduction_tomans
                if mortgage_val > 0:
                    Liability.objects.update_or_create(
                        account=account,
                        asset=asset,
                        defaults={
                            "label": f"Mortgage ({asset.name})",
                            "amount_tomans": mortgage_val,
                        }
                    )
                else:
                    Liability.objects.filter(account=account, asset=asset).delete()
        elif new_quantity > 0:
            values = {"account": account, "asset": asset, "quantity": new_quantity}
            if asset.is_house:
                values.update(
                    area_sqm=area_sqm or HOUSE_AREA_SQM,
                    mortgage_deduction_tomans=(
                        mortgage_deduction_tomans or Decimal("0")
                    ),
                )
            new_holding = Holding.objects.create(**values)
            if asset.is_house:
                mortgage_val = new_holding.mortgage_deduction_tomans
                if mortgage_val > 0:
                    Liability.objects.update_or_create(
                        account=account,
                        asset=asset,
                        defaults={
                            "label": f"Mortgage ({asset.name})",
                            "amount_tomans": mortgage_val,
                        }
                    )

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
    unit_price = _decimal(unit_price_tomans, "unit_price_tomans")
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
        mortgage_deduction_tomans, "mortgage_deduction_tomans", allow_zero=True
    )
    if kind in {
        LedgerEntry.Kind.OPENING_POSITION,
        LedgerEntry.Kind.BUY,
        LedgerEntry.Kind.SELL,
        LedgerEntry.Kind.DIVIDEND,
    } and asset is None:
        raise LedgerError("asset_key is required for this entry kind.")
    if kind in {LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL}:
        if unit_price is None:
            try:
                unit_price = resolve_historical_price(asset, occurred_at)
            except PriceResolutionError as exc:
                raise LedgerError(str(exc)) from exc
        amount = (quantity * unit_price).quantize(Decimal("0.0001"))
    if area is not None or mortgage is not None:
        # A revaluation carries the same terms as the opening it supersedes, so
        # both house mark kinds may set them. Anything else still may not.
        if kind not in {
            LedgerEntry.Kind.OPENING_POSITION,
            LedgerEntry.Kind.VALUATION_MARK,
        } or not asset or not asset.is_house:
            raise LedgerError(
                "Real-estate baseline fields require a house opening position."
            )
    if asset and asset.is_house and kind == LedgerEntry.Kind.OPENING_POSITION:
        area = area or HOUSE_AREA_SQM
        # No mortgage supplied means no mortgage. Defaulting to a hard-coded
        # figure here used to invent debt the user never entered (and, since
        # 0017, a phantom Liability row on top of it).
        mortgage = mortgage or Decimal("0")

    account = Account.objects.select_for_update().get(pk=account.pk)
    if kind in {LedgerEntry.Kind.OPENING_CASH, LedgerEntry.Kind.OPENING_POSITION}:
        if account.tracking_started_at and account.tracking_started_at != occurred_at:
            raise LedgerError("Opening entries must share the tracking start timestamp.")
        account.tracking_started_at = occurred_at
        account.ledger_complete = True
        account.save(update_fields=["tracking_started_at", "ledger_complete", "updated_at"])

    entry = LedgerEntry.objects.create(
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
    _commit_projections(account)
    return entry


@transaction.atomic
def reverse_ledger_entry(*, user, account_id: int, entry_id: int) -> LedgerEntry:
    entry = (
        LedgerEntry.objects.select_for_update()
        .get(pk=entry_id, account_id=account_id, account__user=user)
    )
    if LedgerEntry.objects.filter(reversal_of=entry).exists():
        raise LedgerError("Ledger entry has already been reversed.")
    account = Account.objects.select_for_update().get(pk=entry.account_id)
    reversal = LedgerEntry.objects.create(
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
    _commit_projections(account)
    return reversal


@transaction.atomic
def update_ledger_entry(
    *, user, account_id: int, entry_id: int, quantity=None,
    unit_price_tomans=None, amount_tomans=None, occurred_at=None, note=None,
) -> LedgerEntry:
    """Mutate a ledger row in place, then rebuild holdings/cash from the timeline."""
    entry = (
        LedgerEntry.objects.select_for_update()
        .get(pk=entry_id, account_id=account_id, account__user=user)
    )
    if entry.reversal_of_id:
        raise LedgerError("Cannot edit a reversal.")
    if LedgerEntry.objects.filter(reversal_of=entry).exists():
        raise LedgerError("Cannot edit an entry that has already been reversed.")
    if occurred_at is not None:
        if occurred_at > timezone.now():
            raise LedgerError("occurred_at cannot be in the future.")
        entry.timestamp = occurred_at
    if note is not None:
        entry.note = note[:200]
    if quantity is not None:
        entry.quantity = _decimal(quantity, "quantity", required=True)
    if unit_price_tomans is not None:
        entry.price_tomans = _decimal(unit_price_tomans, "unit_price_tomans", required=True)
    if amount_tomans is not None:
        entry.amount_tomans = _decimal(amount_tomans, "amount_tomans", required=True)
    if entry.kind in {LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL}:
        qty = entry.quantity
        price = entry.price_tomans
        if qty is None or price is None:
            raise LedgerError("Buy/sell entries need quantity and unit price.")
        entry.amount_tomans = (qty * price).quantize(Decimal("0.0001"))
    entry.save()
    _commit_projections(entry.account)
    return entry


@transaction.atomic
def delete_ledger_entry(*, user, account_id: int, entry_id: int) -> None:
    """Remove a ledger row (and its reversal, if any), then rebuild projections."""
    entry = (
        LedgerEntry.objects.select_for_update()
        .get(pk=entry_id, account_id=account_id, account__user=user)
    )
    account = Account.objects.select_for_update().get(pk=entry.account_id)
    asset_id = entry.asset_id
    LedgerEntry.objects.filter(reversal_of=entry).delete()
    entry.delete()
    _commit_projections(account)
    if asset_id and not LedgerEntry.objects.filter(
        account=account, asset_id=asset_id
    ).exists():
        Holding.objects.filter(account=account, asset_id=asset_id).delete()


@transaction.atomic
def set_orphan_holding(*, account: Account, asset: Asset, quantity) -> Holding:
    """Set quantity on a holding that has no ledger history."""
    if LedgerEntry.objects.filter(account=account, asset=asset).exists():
        raise LedgerError("This holding has ledger history; edit a buy/sell row instead.")
    qty = _decimal(quantity, "quantity", required=True)
    holding, _ = Holding.objects.update_or_create(
        account=account, asset=asset, defaults={"quantity": qty}
    )
    return holding


@transaction.atomic
def delete_orphan_holding(*, user, account_id: int, holding_id: int) -> None:
    holding = Holding.objects.select_for_update().get(
        pk=holding_id, account_id=account_id, account__user=user
    )
    if LedgerEntry.objects.filter(account_id=account_id, asset_id=holding.asset_id).exists():
        raise LedgerError("This holding has ledger history; delete a buy/sell row instead.")
    holding.delete()


def synthetic_position_rows(accounts, ledger_rows) -> list[dict]:
    """Holdings with no ledger history, shown as editable position rows."""
    covered = {
        (entry.account_id, entry.asset_id)
        for entry in ledger_rows
        if entry.asset_id
    }
    rows = []
    holdings = (
        Holding.objects.filter(account__in=accounts)
        .select_related("asset", "account")
    )
    for holding in holdings:
        if (holding.account_id, holding.asset_id) in covered:
            continue
        rows.append({
            "id": f"h-{holding.id}",
            "kind": "position",
            "is_synthetic": True,
            "holding_id": holding.id,
            "account_id": holding.account_id,
            "account_name": holding.account.name,
            "asset_key": holding.asset.key,
            "asset_name": holding.asset.name,
            "asset_name_fa": holding.asset.name_fa,
            "quantity": str(holding.quantity),
            "unit_price_tomans": None,
            "amount_tomans": None,
            "occurred_at": holding.updated_at.isoformat() if holding.updated_at else None,
            "source": "holding",
            "note": "",
            "external_id": "",
            "reversal_of": None,
            "created_at": holding.created_at.isoformat() if holding.created_at else None,
            "pnl_tomans": None,
            "pnl_kind": None,
        })
    return rows


def _commit_projections(account: Account) -> None:
    """Replay the ledger into holdings/cash, mapping replay errors to write errors."""
    try:
        rebuild_projections(account)
    except LedgerError as exc:
        text = str(exc).lower()
        if "negative cash" in text:
            raise LedgerError("Insufficient cash balance.") from exc
        if "negative holding" in text:
            raise LedgerError("Insufficient holding quantity.") from exc
        raise


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
        is_house_mark = (
            entry.asset_id
            and entry.asset
            and entry.asset.is_house
            and entry.kind in {
                LedgerEntry.Kind.OPENING_POSITION,
                LedgerEntry.Kind.VALUATION_MARK,
            }
        )
        if is_house_mark:
            # A house mark is a price, not a position: successive marks REPLACE
            # each other. Accumulating them would read a revaluation from 90 to
            # 100 million/sqm as a holding of 190. Entries arrive in timestamp
            # order, so the last mark seen is the one in force.
            holdings[entry.asset_id] = qty
        elif entry.kind in {
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
        if is_house_mark:
            # Terms travel with the newest mark for the same reason.
            real_estate[entry.asset_id] = {
                "area_sqm": entry.area_sqm or HOUSE_AREA_SQM,
                "mortgage_deduction_tomans": (
                    entry.mortgage_deduction_tomans or Decimal("0")
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
    from portfolio.models import Liability
    Liability.objects.filter(account=account, asset__isnull=False).delete()
    for asset_id, data in state["real_estate"].items():
        mortgage_val = data.get("mortgage_deduction_tomans", Decimal("0"))
        if mortgage_val > 0:
            a_obj = Asset.objects.get(pk=asset_id)
            Liability.objects.create(
                account=account,
                asset=a_obj,
                label=f"Mortgage ({a_obj.name})",
                amount_tomans=mortgage_val,
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
def update_manual_holding(
    holding: Holding,
    *,
    quantity,
    unit_price_tomans=None,
) -> Holding:
    """Update quantity and optional unit price for a manual (non-house) asset."""
    from portfolio.models import Price
    from portfolio.services.valuation import invalidate_prices_cache

    if not holding.asset.is_manual or holding.asset.is_house:
        raise LedgerError("Only manual non-house assets support this update.")
    qty = _decimal(quantity, "quantity", required=True)
    holding.quantity = qty
    holding.save(update_fields=["quantity", "updated_at"])
    if unit_price_tomans is not None:
        price = _decimal(unit_price_tomans, "unit_price_tomans", required=True)
        Price.objects.create(
            asset=holding.asset,
            price=price,
            source="manual",
            price_unit=Price.Unit.IRT,
            price_unit_verified=True,
        )
        invalidate_prices_cache()
    return holding


@transaction.atomic
def record_house_mark(
    *,
    user,
    account_id: int,
    asset,
    quantity,
    area_sqm=None,
    mortgage_deduction_tomans=None,
    occurred_at=None,
) -> LedgerEntry:
    """Append a dated valuation mark for a house.

    Revaluing real estate used to REPLACE the single opening entry, so the
    house carried one price across all of history: its appreciation never
    reached the net-worth chart, and today's price was baked into the opening
    balance, understating TWR. Each revaluation is now its own dated event, and
    `timeline.house_marks_as_of` reads whichever mark was in force.

    The first mark for an account is still the OPENING_POSITION, so the opening
    baseline keeps its existing meaning.
    """
    account = Account.objects.select_for_update().get(pk=account_id, user=user)
    has_opening = LedgerEntry.objects.filter(
        account=account,
        asset=asset,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        reversal_of__isnull=True,
        reversed_by__isnull=True,
    ).exists()
    kind = (
        LedgerEntry.Kind.VALUATION_MARK
        if has_opening
        else LedgerEntry.Kind.OPENING_POSITION
    )
    when = occurred_at or timezone.now()
    if when > timezone.now():
        raise LedgerError("A valuation mark cannot be dated in the future.")
    return create_ledger_entry(
        account=account,
        kind=kind,
        asset=asset,
        quantity=quantity,
        area_sqm=area_sqm,
        mortgage_deduction_tomans=mortgage_deduction_tomans,
        occurred_at=when,
        source="manual",
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


class PriceResolutionError(Exception):
    def __init__(self, message, field="price_tomans"):
        super().__init__(message)
        self.field = field


def _jalali_date(when) -> str:
    import jdatetime

    try:
        return jdatetime.date.fromgregorian(date=when.date()).strftime("%Y-%m-%d")
    except Exception as exc:
        raise PriceResolutionError(
            "Could not convert to Jalali date.", field="timestamp"
        ) from exc


def _latest_live_price(asset: Asset) -> Decimal | None:
    from portfolio.models import Price

    row = Price.objects.filter(asset=asset).order_by("-fetched_at").first()
    if row and row.price > 0:
        return Decimal(str(row.price))
    return None


def assert_not_before_history(asset: Asset, when) -> None:
    from marketdata.models import GoldCurrencyHistory, MarketCandle

    if asset.is_manual or asset.is_house:
        return
    j_date = _jalali_date(when)
    if asset.asset_class == Asset.AssetClass.STOCK and asset.tse_symbol:
        first = (
            MarketCandle.objects.filter(symbol=asset.tse_symbol, timeframe="1d_unadj")
            .order_by("date_time")
            .first()
        )
        if first and j_date < first.date_time.split(" ")[0]:
            raise PriceResolutionError(
                f"Date is before the earliest available price date ({first.date_time}).",
                field="timestamp",
            )
    elif (
        asset.asset_class
        in (Asset.AssetClass.GOLD, Asset.AssetClass.CASH, Asset.AssetClass.CRYPTO)
        and asset.brs_symbol
    ):
        first = (
            GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol)
            .order_by("date")
            .first()
        )
        if first and j_date < first.date:
            raise PriceResolutionError(
                f"Date is before the earliest available price date ({first.date}).",
                field="timestamp",
            )


def resolve_historical_price(asset: Asset, when) -> Decimal:
    """Warehouse close on `when`, else latest live Price. Raises if none."""
    from marketdata.currency import to_toman
    from marketdata.models import GoldCurrencyHistory, MarketCandle

    assert_not_before_history(asset, when)
    if asset.is_manual or asset.is_house:
        price = _latest_live_price(asset)
        if price is None:
            raise PriceResolutionError(
                "Price omitted and no historical price found for this date."
            )
        return price

    j_date = _jalali_date(when)
    if asset.asset_class == Asset.AssetClass.STOCK and asset.tse_symbol:
        candle = MarketCandle.objects.filter(
            symbol=asset.tse_symbol,
            timeframe="1d_unadj",
            date_time__startswith=j_date,
        ).first()
        if not candle:
            candle = (
                MarketCandle.objects.filter(
                    symbol=asset.tse_symbol,
                    timeframe="1d_unadj",
                    date_time__lte=j_date + " 23:59:59",
                )
                .order_by("-date_time")
                .first()
            )
        if candle and candle.close_price > 0:
            return Decimal(str(candle.close_price))
        price = _latest_live_price(asset)
        if price is None:
            raise PriceResolutionError(
                "Price omitted and no historical price found for this date."
            )
        return price

    if (
        asset.asset_class
        in (Asset.AssetClass.GOLD, Asset.AssetClass.CASH, Asset.AssetClass.CRYPTO)
        and asset.brs_symbol
    ):
        history = GoldCurrencyHistory.objects.filter(
            symbol=asset.brs_symbol, date=j_date
        ).first()
        if not history:
            history = (
                GoldCurrencyHistory.objects.filter(
                    symbol=asset.brs_symbol, date__lte=j_date
                )
                .order_by("-date")
                .first()
            )
        if history and history.close_price > 0:
            usd_row = (
                GoldCurrencyHistory.objects.filter(
                    symbol="USD", date__lte=history.date, close_price__gt=0
                )
                .order_by("-date")
                .first()
            )
            converted = to_toman(
                asset.brs_symbol,
                history.close_price,
                history.unit,
                usd_rate=usd_row.close_price if usd_row else None,
            )
            if converted > 0:
                return Decimal(str(converted))
        price = _latest_live_price(asset)
        if price is None:
            raise PriceResolutionError(
                "Price omitted and no historical price found for this date."
            )
        return price

    price = _latest_live_price(asset)
    if price is None:
        raise PriceResolutionError(
            "Price omitted and no historical price found for this date."
        )
    return price


def entry_pnl_map(entries, prices: dict) -> dict[int, dict]:
    """Map ledger pk -> {pnl_tomans, pnl_kind}. Unknown stays null, never 0."""
    from collections import defaultdict, deque

    result = {
        entry.pk: {"pnl_tomans": None, "pnl_kind": None} for entry in entries
    }
    reversed_ids = {entry.reversal_of_id for entry in entries if entry.reversal_of_id}
    lots: dict[int, deque] = defaultdict(deque)

    ordered = sorted(entries, key=lambda entry: (entry.timestamp, entry.pk))
    for entry in ordered:
        if entry.reversal_of_id or entry.pk in reversed_ids:
            continue
        if not entry.asset_id or (entry.asset and entry.asset.is_house):
            continue
        qty = Decimal(entry.quantity or 0)
        price = entry.price_tomans
        if entry.kind in {
            LedgerEntry.Kind.OPENING_POSITION,
            LedgerEntry.Kind.BUY,
        }:
            lots[entry.asset_id].append({
                "id": entry.pk,
                "key": entry.asset.key,
                "remaining": qty,
                "price": price,
            })
        elif entry.kind == LedgerEntry.Kind.SELL:
            remaining = qty
            realized = Decimal("0")
            known = price is not None and price > 0
            while remaining > 0 and lots[entry.asset_id]:
                lot = lots[entry.asset_id][0]
                take = min(lot["remaining"], remaining)
                lot_price = lot["price"]
                if lot_price is None or lot_price <= 0:
                    known = False
                elif known:
                    realized += take * (price - lot_price)
                lot["remaining"] -= take
                remaining -= take
                if lot["remaining"] == 0:
                    lots[entry.asset_id].popleft()
            if remaining > 0:
                known = False
            if known:
                result[entry.pk] = {
                    "pnl_tomans": str(realized),
                    "pnl_kind": "realized",
                }

    for queue in lots.values():
        for lot in queue:
            if lot["remaining"] <= 0:
                continue
            lot_price = lot["price"]
            current = Decimal(str(prices.get(lot["key"], 0) or 0))
            if lot_price is None or lot_price <= 0 or current <= 0:
                continue
            result[lot["id"]] = {
                "pnl_tomans": str(lot["remaining"] * (current - lot_price)),
                "pnl_kind": "unrealized",
            }
    return result

