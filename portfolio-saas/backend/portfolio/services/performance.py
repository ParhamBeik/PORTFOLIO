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
    """Resolve the Toman-per-unit rate for a USD/USDT basis, AS OF a date.

    Mirrors `valuation.value_as_of`'s resolution order so the segment-boundary
    valuation and the cash flow that crosses it are never priced at two
    different rates.

    `usd_denominated` used to ignore `as_of` entirely and return
    `get_latest_prices()["usd_cash"]` -- today's live tick -- while
    `usdt_denominated` beside it correctly read the warehouse at the date it was
    handed. Both callers pass a real date: `_flow_amount` passes
    `entry.timestamp`, which is when the deposit actually happened. So in
    `account_performance`'s TWR loop, `before_value` came from
    `value_as_of(as_of=entry.timestamp)` at the historical rate and `flow` came
    back at today's, and `segment_start = before_value + signed_flow` added two
    dollar figures measured with two different rulers. Against a currency that
    has lost most of its value over the tracked period, a deposit made two years
    ago was divided by a rate several times too large, so the flow was
    understated by that factor and both TWR and XIRR came out wrong -- silently,
    with no `quality_status` to show for it.

    The live map stays as the last resort for the USD basis only, which is where
    it was already the only source: it is what `_current_value` needs before the
    day's gold/currency row has been ingested.
    """
    from marketdata.models import GoldCurrencyHistory
    from .returns import to_jalali_str

    if basis not in ("usd_denominated", "usdt_denominated"):
        return None

    jalali = to_jalali_str(as_of)
    row = None
    if basis == "usdt_denominated":
        row = (
            GoldCurrencyHistory.objects.filter(symbol="USDT_IRT", date__lte=jalali)
            .order_by("-date").first()
        )
    if not row or row.close_price <= 0:
        row = (
            GoldCurrencyHistory.objects.filter(symbol="USD", date__lte=jalali)
            .order_by("-date").first()
        )
    if row and row.close_price > 0:
        return Decimal(str(row.close_price))

    if basis == "usd_denominated":
        rate = Decimal(str(get_latest_prices().get("usd_cash", 0) or 0))
        return rate if rate > 0 else None
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


def _house_position(asset, entries, valuation_items, label=None) -> dict:
    """What a property cost and what it is worth, as one row.

    Everything about a house is measured per square meter, so both prices on
    this row are per square meter and the "quantity" is the area. Reusing the
    unit-count shape would print a quantity of 24 for a flat bought at 24
    million a meter, and the reader has no way to tell that from 24 coins.

    Marks REPLACE (see `ledger.HOUSE_MARK_KINDS`), so the current mark is the
    last one and the purchase price is the last one that declared it -- a later
    correction wins, which is the only way a typo is fixable.
    """
    from ..models import HOUSE_AREA_SQM, HOUSE_PRICE_SCALE
    from .ledger import HOUSE_MARK_KINDS

    reversed_ids = {e.reversal_of_id for e in entries if e.reversal_of_id}
    live = [
        e for e in entries
        if not e.reversal_of_id
        and e.pk not in reversed_ids
        and e.kind in HOUSE_MARK_KINDS
    ]
    area = HOUSE_AREA_SQM
    basis_per_sqm_million = None
    for entry in live:
        if entry.area_sqm is not None:
            area = Decimal(entry.area_sqm)
        if entry.cost_basis_tomans is not None:
            basis_per_sqm_million = Decimal(entry.cost_basis_tomans)

    item = valuation_items.get(asset.key)
    current_value = (
        Decimal(str(item["value"]))
        if item and item.get("value") is not None
        else None
    )
    known = basis_per_sqm_million is not None and basis_per_sqm_million > 0
    cost_per_sqm = (
        basis_per_sqm_million * HOUSE_PRICE_SCALE if known else None
    )
    total_basis = cost_per_sqm * area if known else None
    return {
        "asset_key": asset.key,
        # A property is minted per owner and the catalog row has nothing but
        # the asset class to put in `name`, so without the holder's own name
        # for it every house on this table reads "Real Estate". Same rule as
        # `models.owner_display_names`.
        "asset_name": label or asset.name_fa or asset.name,
        "quantity": str(area),
        # Square meters divide; the step is what the holdings table would use
        # for a divisible unit, not the "1" a countable asset gets.
        "quantity_step": "any",
        "quantity_unit": "sqm",
        "cost_basis_known": known,
        "average_cost_tomans": str(cost_per_sqm) if known else None,
        "average_cost_currency": "toman",
        # Says the price above is per square meter, so the table can label it
        # rather than leave a per-meter figure next to per-unit ones.
        "average_cost_unit": "sqm",
        "total_cost_basis_tomans": str(total_basis) if known else None,
        # A property is not part-sold here; a sale is a delete, and the ledger
        # reverses every mark with it.
        "realized_pnl_tomans": "0" if known else None,
        "unrealized_pnl_tomans": (
            str(current_value - total_basis)
            if known and current_value is not None
            else None
        ),
        "current_value_tomans": (
            str(current_value) if current_value is not None else None
        ),
    }


def _position_metrics(account) -> dict:
    from .visibility import hidden_asset_ids

    hidden_ids = hidden_asset_ids([account])
    version = account.transactions.aggregate(
        count=Count("id"), max_id=Max("id"), id_sum=Sum("id"),
        # An EDIT moves none of the three above -- same rows, same ids -- so
        # correcting a quantity or declaring a purchase price left this table
        # answering from cache for the next hour.
        touched=Max("updated_at"),
    )
    cache_key = (
        f"position-metrics:{account.id}:{version['count']}:"
        f"{version['max_id'] or 0}:{version['id_sum'] or 0}:"
        f"{version['touched'].timestamp() if version['touched'] else 0}:"
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
    # The holder's own name for each asset, so a property is "Tehran flat" and
    # not "Real Estate". Read once rather than per row.
    labels = {
        holding.asset_id: holding.label
        for holding in account.holdings.select_related("asset")
    }
    entries_by_asset = {}
    for entry in account.transactions.select_related("asset").order_by(
        "timestamp", "pk"
    ):
        if entry.asset_id and entry.asset_id not in hidden_ids:
            entries_by_asset.setdefault(entry.asset_id, []).append(entry)
    for entries in entries_by_asset.values():
        asset = entries[0].asset
        if asset.is_house:
            # A property is a series of dated marks, not a position, and the
            # running total below would add every revaluation to the last: the
            # "quantity" it accumulates is a price per square meter, so a house
            # marked three times reported a quantity of 119 and a current price
            # of zero (real estate has no feed to look one up in). It gets its
            # own arithmetic.
            result[asset.key] = _house_position(
                asset, entries, valuation_items, labels.get(asset.id)
            )
            continue
        quantity = Decimal("0")
        average_cost = Decimal("0")
        realized = Decimal("0")
        unknown_basis = False
        # A reversal pair nets to nothing: skip the reversal row AND the row it
        # reverses, or the cost basis keeps an event the holdings no longer have.
        reversed_ids = {
            entry.reversal_of_id for entry in entries if entry.reversal_of_id
        }
        live = [
            entry for entry in entries
            if not entry.reversal_of_id and entry.pk not in reversed_ids
        ]
        if not live:
            # EVERY entry was reversed, so this asset was never really held here.
            # The loop below would fall through untouched and emit a row with
            # `cost_basis_known` True and a basis of zero -- a position the
            # account does not have, on the P&L table, claiming to have cost
            # nothing. Distinct from a position bought and then sold, which has
            # live entries and a realized P&L worth showing.
            continue
        for entry in live:
            qty = Decimal(entry.quantity or 0)
            price = Decimal(entry.price_tomans or 0)
            if entry.kind == LedgerEntry.Kind.OPENING_POSITION:
                # An opening says "I already own this". Whether that voids the
                # cost basis depends on one thing only: whether the owner said
                # what they paid. `cost_basis_tomans` is that declaration, in
                # the same unit as every other price on the row (Rial for TSE),
                # and it absorbs exactly like a purchase at that price so a
                # later buy averages against it. Absent it, the basis is
                # genuinely unknown and everything downstream stays blank --
                # which is what every opening used to mean, unconditionally.
                declared = Decimal(entry.cost_basis_tomans or 0)
                if declared > 0 and not unknown_basis:
                    if quantity + qty > 0:
                        average_cost = (
                            average_cost * quantity + declared * qty
                        ) / (quantity + qty)
                    quantity += qty
                else:
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
            # Countable things have no unit to name. Only a property does, and
            # `_house_position` is where that row comes from -- but the key is
            # present on every row so the client never has to test for it.
            "quantity_unit": None,
            "average_cost_unit": None,
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
        if asset.is_house:
            # A property with no ledger history at all: still measured in square
            # meters, and `holding.quantity` is a price per meter, not a count.
            result[asset.key] = _house_position(
                asset, [], valuation_items, holding.label
            )
            result[asset.key]["quantity"] = str(holding.area_sqm)
            continue
        result[asset.key] = {
            "asset_key": asset.key,
            "asset_name": asset.tse_symbol or asset.name_fa or asset.name,
            "quantity": str(holding.quantity),
            "quantity_step": asset.quantity_step,
            "quantity_unit": None,
            "average_cost_unit": None,
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
