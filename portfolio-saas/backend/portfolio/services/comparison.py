"""What the same money would have done somewhere else.

Four questions, one shape of answer: two value curves over one window, plus the
gap between them at the end.

  counterfactual  Replay the purchases you actually made -- real dates, real
                  amounts -- into a different asset.
  holdings        Two things you actually own, side by side.
  benchmark       Your whole portfolio against one asset, both rebased to 100.
  lump_sum        The same total, all of it on the first day, in each asset.

Prices come from `returns._load_price_panel`, which is the only reader that has
already put TSE Rial, BRS Toman and the live-only daily bars into one Toman
panel and applied the gap and integrity gates. Reading the warehouse directly
here would be a fifth price-resolution path, and the unit boundary is exactly
where this project's bugs live.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
from django.utils import timezone

from marketdata.currency import holding_value_to_toman

from ..models import Asset, LedgerEntry
from .ledger import active_entries

MODES = ("counterfactual", "holdings", "benchmark", "lump_sum")

#: The comparison has to reach back to the first purchase, which for a real
#: broker history is years. Bounded so one request cannot ask the panel for an
#: unlimited scan.
MAX_WINDOW_DAYS = 3650
MIN_WINDOW_DAYS = 7

#: Kinds that move a position for money. A rights issue moves the position
#: without any, so it changes the units held but never the amount invested --
#: counting it as a purchase would credit the counterfactual with money that was
#: never spent.
_TRADE_KINDS = (LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL)
_POSITION_KINDS = _TRADE_KINDS + (
    LedgerEntry.Kind.OPENING_POSITION,
    LedgerEntry.Kind.RIGHTS_ISSUE,
)


class ComparisonError(Exception):
    """The comparison cannot be answered, with a reason the UI can render."""

    def __init__(self, reason: str, detail: str, **extra):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.extra = extra


def _accounts(user, account=None):
    return [account] if account is not None else list(user.accounts.all())


def _resolve_asset(key: str, *, field: str) -> Asset:
    asset = Asset.objects.filter(key=key, is_active=True).first()
    if asset is None:
        raise ComparisonError("unknown_asset", f"No asset named {key!r}.", field=field)
    if asset.is_house:
        # Property has no market series -- it is a chain of marks the owner
        # entered, so `resolve_universe` excludes it and there is nothing
        # honest to compare a purchase against.
        raise ComparisonError(
            "real_estate_not_comparable",
            "Property is valued from your own marks, not a market price, "
            "so it cannot stand in as an investment alternative.",
            field=field,
        )
    return asset


def _flows(accounts, asset) -> list[dict]:
    """Dated money-and-units events for one asset, oldest first.

    An opening position is a real position with no purchase behind it: it
    contributes units from day one and nothing to the money invested. Saying
    otherwise would hand the counterfactual a lump of cash the user never had.
    """
    rows = []
    for entry in active_entries(
        accounts, kinds=_POSITION_KINDS, asset_ids=[asset.id]
    ):
        quantity = Decimal(entry.quantity or 0)
        if quantity <= 0:
            continue
        spent = Decimal("0")
        if entry.kind in _TRADE_KINDS and entry.price_tomans:
            spent = holding_value_to_toman(asset, quantity * entry.price_tomans)
        rows.append({
            "at": entry.timestamp,
            "units": -quantity if entry.kind == LedgerEntry.Kind.SELL else quantity,
            "spent": -spent if entry.kind == LedgerEntry.Kind.SELL else spent,
        })
    return rows


def _window(flows, days) -> tuple[int, dt.datetime]:
    """(days of history to LOAD, first day to SHOW).

    These are deliberately different numbers. Every purchase has to be replayed
    at the price on the day it happened, so the panel always reaches back to the
    oldest one no matter how short a window the user asked to look at. Loading
    only the visible window would buy a 2022 purchase at this year's opening
    price and quietly report that as the alternative's cost.
    """
    now = timezone.now()
    first = flows[0]["at"] if flows else now - dt.timedelta(days=365)
    span = (now - first).days + 1
    shown = span if days is None else max(MIN_WINDOW_DAYS, int(days))
    load = min(max(span, shown, MIN_WINDOW_DAYS), MAX_WINDOW_DAYS)
    return load, now - dt.timedelta(days=min(shown, load))


def _panel(keys, days, held) -> pd.DataFrame:
    """Toman close per key per day, gap-gated, forward-filled onto a daily index.

    Reads the PUBLIC panel: `_load_price_panel` is ratio-safe but not
    money-safe, and this page prints money. Its USD-quoted columns are still
    dollars and its live-Price fallback column is Rial for a TSE symbol -- both
    invisible to every other consumer because they all take `pct_change()`
    next, where a constant factor cancels.

    `_build_returns_matrix` is run for its verdicts, which are attached as
    caveats rather than acted on -- see below. What actually refuses a dead
    series is the trailing check: a halted or delisted target must not draw a
    flat line from its last close to today and have the summary report that
    stale number as what you would have made.

    The fill onto a daily index afterwards is a DRAWING bound, not the 5-session
    staleness bound -- the gate above already owns that verdict. Its job is to
    bridge the non-trading days between real closes, so it is counted in
    calendar days on a calendar index and sized to the longest legitimate
    closure (Nowruz shuts the exchange for about two weeks). Using the session
    number here would have refused every March.
    """
    from marketdata.calendars import market_for_asset, sessions_between
    from marketdata.integrity import MAX_FORWARD_FILL_SESSIONS, MAX_OUTAGE_CALENDAR_DAYS

    from .returns import to_jalali_str, toman_price_panel

    keys = list(keys)
    # The gate's verdicts arrive as CAVEATS here, not exclusions. It screens
    # assets for investability over a trailing window -- the right question for
    # the optimizer, the wrong one for "what would this have been worth", and it
    # would throw away four years of real history over an eleven-session halt in
    # year two. The invariant it enforces is about TODAY's number not being a
    # dead price, and that is the trailing check at the end of this function.
    panel, excluded, warnings = toman_price_panel(
        history_days=days, universe=keys, held_keys=frozenset(keys), gate=True
    )
    absent = [key for key in keys if key not in panel.columns]
    _refuse(absent, "missing_price_history", "No usable price history")
    panel = panel[keys].sort_index()
    panel.attrs["warnings"] = [
        row for row in [*excluded, *warnings] if row.get("key") in keys
    ]

    # The FULL loaded span, not the visible window: the curves are built over
    # this and sliced later, so a purchase older than the window still buys at
    # its own day's price.
    index = pd.date_range(
        start=panel.index.min(), end=timezone.now(), freq="D", tz="UTC"
    )
    filled = (
        panel.reindex(panel.index.union(index))
        .ffill(limit=MAX_OUTAGE_CALENDAR_DAYS)
        .reindex(index)
    )
    # Trailing staleness is the real invariant, and it is counted in SESSIONS on
    # the asset's own market calendar -- not in the calendar days the drawing
    # fill above uses. Reusing that constant would have let a target that
    # stopped printing three weeks ago report its last close as today's outcome.
    assets = {
        asset.key: asset
        for asset in Asset.objects.filter(key__in=keys, is_active=True)
    }
    today = to_jalali_str(timezone.now())
    stale = []
    for key in keys:
        real = panel[key].dropna()
        if real.empty:
            stale.append(key)
            continue
        asset = assets.get(key)
        gap = sessions_between(
            to_jalali_str(real.index[-1]), today,
            market=market_for_asset(asset) if asset else "gold_currency",
        )
        if gap > MAX_FORWARD_FILL_SESSIONS or pd.isna(filled[key].iloc[-1]):
            stale.append(key)
    if stale:
        raise ComparisonError(
            "stale_price_history",
            f"{', '.join(sorted(stale))} has no recent price, so there is "
            "nothing to compare against today.",
            keys=sorted(stale),
        )
    result = filled.dropna()
    result.attrs["warnings"] = panel.attrs["warnings"]
    return result


def _refuse(keys, reason, detail) -> None:
    if keys:
        raise ComparisonError(
            reason, f"{detail} for {', '.join(sorted(set(keys)))}.",
            keys=sorted(set(keys)),
        )


def _units_held(flows, index) -> pd.Series:
    """Running units held on each day of the index, from the flow steps."""
    units = pd.Series(0.0, index=index)
    for flow in flows:
        at = pd.Timestamp(flow["at"]).tz_convert("UTC")
        units[index >= at] += float(flow["units"])
    return units


def _assert_covered(flows, index) -> None:
    """Refuse when a purchase predates the price history being replayed into.

    `_panel` drops leading rows where either series is NaN, so the index starts
    at the LATER of the two. A flow older than that would otherwise match the
    first available day and buy at its price -- reporting a 2024 opening price
    as the cost of a 2022 purchase, which is the exact thing the load-vs-show
    split exists to prevent.
    """
    if not flows or index.empty:
        return
    first = pd.Timestamp(flows[0]["at"]).tz_convert("UTC")
    if first < index[0].normalize():
        raise ComparisonError(
            "history_starts_after_purchase",
            "The price history does not reach back to "
            f"{first.date().isoformat()}, when the first purchase was made, so "
            "there is no honest price to have bought at.",
            first_purchase=first.date().isoformat(),
            history_starts=index[0].date().isoformat(),
        )


def _counterfactual_units(flows, prices: pd.Series, index) -> pd.Series:
    """Units of the alternative asset the same money would have bought.

    A sale is mirrored as selling the same FRACTION of the alternative
    position, not the same number of units -- the two assets have unrelated
    prices, and matching units would let a sale of half a stock liquidate a
    whole holding of gold.
    """
    units = pd.Series(0.0, index=index)
    held = 0.0
    position = 0.0
    for flow in flows:
        at = pd.Timestamp(flow["at"]).tz_convert("UTC")
        on_or_after = index[index >= at]
        if on_or_after.empty:
            continue
        price = float(prices.loc[on_or_after[0]])
        change = float(flow["units"])
        if change >= 0:
            bought = float(flow["spent"]) / price if price > 0 else 0.0
            position += bought
        else:
            fraction = min(1.0, -change / held) if held > 0 else 0.0
            position -= position * fraction
        held = max(0.0, held + change)
        units[index >= at] = position
    return units


def _shown(series: pd.Series, start) -> pd.Series:
    """Clip a finished curve to the visible window.

    Applied last, never before: the curve's VALUE on the first visible day
    depends on every purchase that came before it.
    """
    clipped = series[series.index >= pd.Timestamp(start)]
    return clipped if len(clipped) else series.tail(1)


def _points(series: pd.Series) -> list[dict]:
    return [
        {"date": stamp.date().isoformat(), "value": round(float(value), 2)}
        for stamp, value in series.items()
    ]


def _rebase(series: pd.Series) -> pd.Series:
    base = series.iloc[0] if len(series) and series.iloc[0] else 0
    return series / base * 100 if base else series * 0


def _curve(key, label, series, *, unit="toman") -> dict:
    return {"key": key, "label": label, "unit": unit, "points": _points(series)}


def _label(asset) -> str:
    return asset.name_fa or asset.name or asset.key


def _summary(actual: pd.Series, alternative: pd.Series, invested=None) -> dict:
    end_actual = float(actual.iloc[-1]) if len(actual) else 0.0
    end_alt = float(alternative.iloc[-1]) if len(alternative) else 0.0
    summary = {
        "actual_end_tomans": round(end_actual, 2),
        "alternative_end_tomans": round(end_alt, 2),
        "difference_tomans": round(end_actual - end_alt, 2),
        "start_date": actual.index[0].date().isoformat() if len(actual) else None,
        "end_date": actual.index[-1].date().isoformat() if len(actual) else None,
    }
    if invested is not None:
        summary["invested_tomans"] = round(float(invested), 2)
    return summary


def _counterfactual(user, account, subject_key, target_key, days) -> dict:
    subject = _resolve_asset(subject_key, field="subject")
    target = _resolve_asset(target_key, field="target")
    flows = _flows(_accounts(user, account), subject)
    if not flows:
        raise ComparisonError(
            "no_purchase_history",
            f"There are no recorded {_label(subject)} purchases to replay.",
        )
    if sum(flow["spent"] for flow in flows) <= 0:
        # Every position came in as an opening or a price-less row, so there is
        # no amount to move. Reporting zero would look like a real answer.
        raise ComparisonError(
            "no_recorded_cost",
            f"Your {_label(subject)} position has no net money in it -- either "
            "no purchase prices are recorded, or the sales returned more than "
            "the purchases cost -- so there is no amount to invest elsewhere.",
        )
    days, start = _window(flows, days)
    panel = _panel([subject.key, target.key], days, held=[subject.key])
    index = panel.index

    _assert_covered(flows, index)
    actual = _shown(_units_held(flows, index) * panel[subject.key], start)
    alternative = _shown(
        _counterfactual_units(flows, panel[target.key], index) * panel[target.key],
        start,
    )
    invested = sum(float(flow["spent"]) for flow in flows)
    return {
        "mode": "counterfactual",
        "series": [
            _curve(subject.key, f"What you did: {_label(subject)}", actual),
            _curve(target.key, f"Instead: {_label(target)}", alternative),
        ],
        "summary": _summary(actual, alternative, invested=invested),
        "warnings": panel.attrs["warnings"],
    }


def _holdings(user, account, subject_key, target_key, days) -> dict:
    subject = _resolve_asset(subject_key, field="subject")
    target = _resolve_asset(target_key, field="target")
    accounts = _accounts(user, account)
    subject_flows = _flows(accounts, subject)
    target_flows = _flows(accounts, target)
    for asset, flows in ((subject, subject_flows), (target, target_flows)):
        if not flows:
            raise ComparisonError(
                "no_purchase_history",
                f"You have no recorded {_label(asset)} position to show.",
            )
    days, start = _window(subject_flows + target_flows, days)
    panel = _panel([subject.key, target.key], days, held=[subject.key, target.key])
    index = panel.index

    left = _shown(_units_held(subject_flows, index) * panel[subject.key], start)
    right = _shown(_units_held(target_flows, index) * panel[target.key], start)
    return {
        "mode": "holdings",
        "series": [
            _curve(subject.key, _label(subject), left),
            _curve(target.key, _label(target), right),
        ],
        "summary": _summary(left, right),
        "warnings": panel.attrs["warnings"],
    }


def _twr_index(series: list[dict]) -> pd.Series:
    """Chain-linked time-weighted index of a net-worth series, starting at 100.

    The raw `total` moves when prices move AND when the book changes, and only
    the first is performance: recording a position you already owned, or funding
    a purchase, adds money without earning any. Rebasing `total` straight to 100
    read those steps as return -- the family account's openings on 2026-08-09
    showed as a +108% day and turned a real +40% quarter into "+120.7%".

    `total_ex_flows` is the same day valued at the previous day's quantities, so
    `total_ex_flows[t] / total[t-1]` is the pure price move and the product of
    those factors is the time-weighted return. A day whose starting value is zero
    or negative cannot carry a return; it restarts the chain rather than dividing
    by it.
    """
    dates = pd.to_datetime([point["date"] for point in series], utc=True)
    totals = [float(point["total"]) for point in series]
    # A matched pair: the same holdings at the same quantities, priced today and
    # priced yesterday. Their ratio is the day's return over one asset set, so an
    # asset that is priced on only one of the two days sits out of BOTH and
    # cannot book a phantom move. Dividing by the previous day's `total` instead
    # did exactly that -- a holding dropped by the forward-fill guard subtracted
    # its whole weight for a day, and a running product never recovers from it.
    ex_flows = [float(point.get("total_ex_flows", point["total"])) for point in series]
    ex_base = [float(point.get("total_ex_flows_base", 0) or 0) for point in series]
    # Older payloads carry neither field. Falling back to the raw totals
    # reproduces the pre-fix curve rather than raising on a key that is absent.
    legacy = not any(point.get("total_ex_flows_base") for point in series)

    index, level = [], 100.0
    for position in range(len(totals)):
        if position > 0:
            if legacy:
                if totals[position - 1] > 0:
                    level *= ex_flows[position] / totals[position - 1]
            # Both sides net out debt, so an account whose borrowing exceeds the
            # assets that were priced that day has negative equity on one or both
            # -- a ratio that would flip the whole curve through zero and take
            # every later day with it. There is no return on nothing owned; the
            # day is carried flat and the chain resumes when equity is positive.
            elif ex_base[position] > 0 and ex_flows[position] > 0:
                level *= ex_flows[position] / ex_base[position]
        index.append(level)
    return pd.Series(index, index=dates).sort_index()


def _benchmark(user, account, target_key, days) -> dict:
    from .valuation import SYNTHETIC_HISTORY_MAX_DAYS, compute_dynamic_net_worth_series

    target = _resolve_asset(target_key, field="target")
    # No per-asset flows to reach back for here, so unlike the other modes the
    # requested window IS the whole request. The net-worth series is bounded on
    # purpose -- every extra day is another full ledger replay -- so asking for
    # more than it gives returns a shorter window than the caller asked for,
    # and the answer says so rather than letting the axis imply a range the
    # data does not cover. "All" means "whatever the replay reaches", which is
    # the bound itself and therefore not a truncation.
    capped = min(days or SYNTHETIC_HISTORY_MAX_DAYS, SYNTHETIC_HISTORY_MAX_DAYS)
    truncated = capped if days and days > capped else None
    series = compute_dynamic_net_worth_series(user, account, days=capped)
    if not series:
        raise ComparisonError(
            "no_portfolio_history",
            "This portfolio has no value history to compare yet.",
        )
    portfolio = _twr_index(series)
    panel = _panel([target.key], capped, held=[])
    index = portfolio.index.intersection(panel.index)
    if index.empty:
        raise ComparisonError(
            "no_overlapping_history",
            f"Your portfolio history and {_label(target)}'s price history do "
            "not overlap.",
        )
    portfolio = portfolio.reindex(index)
    prices = panel[target.key].reindex(index)
    return {
        "mode": "benchmark",
        "series": [
            _curve("portfolio", "Your portfolio", _rebase(portfolio), unit="index"),
            _curve(target.key, _label(target), _rebase(prices), unit="index"),
        ],
        # Rebased curves are unitless, so the Toman end values would be
        # comparing a portfolio total against one unit of gold. Only the
        # percentages mean anything here.
        "summary": {
            "actual_change_pct": round(float(_rebase(portfolio).iloc[-1]) - 100, 2),
            "alternative_change_pct": round(float(_rebase(prices).iloc[-1]) - 100, 2),
            "start_date": index[0].date().isoformat(),
            "end_date": index[-1].date().isoformat(),
            "truncated_to_days": truncated,
        },
        "warnings": panel.attrs["warnings"],
    }


def _lump_sum(user, account, subject_key, target_key, days) -> dict:
    subject = _resolve_asset(subject_key, field="subject")
    target = _resolve_asset(target_key, field="target")
    flows = _flows(_accounts(user, account), subject)
    # Net of sales, the same figure the drip-fed comparison reports. Summing
    # only the buys answers "how much passed through" rather than "how much you
    # put in", and the two modes would disagree about the same portfolio.
    invested = sum(float(flow["spent"]) for flow in flows)
    if invested <= 0:
        raise ComparisonError(
            "no_recorded_cost",
            f"There is no recorded amount invested in {_label(subject)}.",
        )
    days, start = _window(flows, days)
    panel = _panel([subject.key, target.key], days, held=[subject.key])

    # Bought on the day of the earliest REAL purchase, not on the first day of
    # the loaded panel. The panel deliberately reaches back further than the
    # visible window (and further again when a range button asks for more days
    # than the position has existed), so `iloc[0]` would deploy the money before
    # it existed -- a year of growth on ten-day-old money, and not comparable to
    # the drip mode it sits beside.
    index = panel.index
    _assert_covered(flows, index)
    opened = index[index >= pd.Timestamp(flows[0]["at"]).tz_convert("UTC")]
    if opened.empty:
        raise ComparisonError(
            "no_price_on_purchase_date",
            "There is no price on or after the first purchase date.",
        )
    day = opened[0]
    left = _shown(
        invested / float(panel[subject.key].loc[day]) * panel[subject.key].loc[day:],
        start,
    )
    right = _shown(
        invested / float(panel[target.key].loc[day]) * panel[target.key].loc[day:],
        start,
    )
    return {
        "mode": "lump_sum",
        "series": [
            _curve(subject.key, f"All at once: {_label(subject)}", left),
            _curve(target.key, f"All at once: {_label(target)}", right),
        ],
        "summary": _summary(left, right, invested=invested),
        "warnings": panel.attrs["warnings"],
    }


def compare(user, *, account=None, mode: str, subject=None, target=None, days=None) -> dict:
    """Run one comparison. Raises ComparisonError with a renderable reason."""
    if mode not in MODES:
        raise ComparisonError("unknown_mode", f"No comparison mode {mode!r}.")
    if not target:
        raise ComparisonError(
            "missing_target", "Choose something to compare against.", field="target"
        )
    if mode == "benchmark":
        return _benchmark(user, account, target, days)
    if not subject:
        raise ComparisonError(
            "missing_subject", "Choose a holding to compare.", field="subject"
        )
    if subject == target:
        raise ComparisonError(
            "same_asset", "Pick two different assets.", field="target"
        )
    if mode == "counterfactual":
        return _counterfactual(user, account, subject, target, days)
    if mode == "holdings":
        return _holdings(user, account, subject, target, days)
    return _lump_sum(user, account, subject, target, days)


def comparable_assets(user, account=None) -> dict:
    """What the picker may offer: the user's own holdings, and every target.

    Targets are the tradeable catalog minus property. A holding you own is
    still a valid target -- "what if I had put the stock money into the gold I
    also own" is a fair question.
    """
    from .visibility import hidden_asset_ids

    accounts = _accounts(user, account)
    hidden = hidden_asset_ids(accounts)
    # Netted, like `_flows`. A raw query leaves an asset whose only purchase was
    # reversed in the picker, where choosing it always answers
    # "no_purchase_history".
    held_ids = {
        entry.asset_id
        for entry in active_entries(accounts, kinds=_POSITION_KINDS)
        if entry.asset_id
    }
    # `is_manual` assets are excluded from targets: they carry a `proxy_key`,
    # so their panel column is a DIFFERENT asset's price series (a Swiss bar
    # priced off gold_18k_gram). Drawing that curve under the bar's own name
    # would answer a question about an instrument the user never saw.
    assets = Asset.objects.filter(
        is_active=True, is_house=False, is_manual=False
    ).order_by("name")
    rows = [
        {
            "key": asset.key,
            "label": _label(asset),
            "asset_class": asset.asset_class,
            "held": asset.id in held_ids,
        }
        for asset in assets
        if asset.id not in hidden
    ]
    # Held assets the pickers cannot offer, and why. Dropping them silently left
    # a reader hunting for their own house and Swiss bars in a list that never
    # mentioned them -- four of this account's holdings were absent with no
    # explanation anywhere on the page.
    omitted = [
        {
            "key": asset.key,
            "label": _label(asset),
            "reason": (
                "valued from a mark you set, not a market price series"
                if asset.is_house
                else "tracked against a proxy price, so it has no curve of its own"
            ),
        }
        for asset in Asset.objects.filter(id__in=held_ids, is_active=True).order_by("name")
        if asset.id not in hidden and (asset.is_house or asset.is_manual)
    ]
    return {
        "holdings": [row for row in rows if row["held"]],
        "targets": rows,
        "omitted_holdings": omitted,
    }
