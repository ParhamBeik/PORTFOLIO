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


def _panel(keys, days, start) -> pd.DataFrame:
    """Toman close per key per day, forward-filled onto a daily index.

    Forward-filling here is not the 5-session valuation fill and does not need
    its bound: the panel has already dropped anything the gap gate rejected, so
    what is left are non-trading days between real closes -- a curve has to draw
    through Thursday somehow.
    """
    from .returns import _load_price_panel

    panel, excluded, _warnings = _load_price_panel(
        history_days=days, universe=list(keys), held_keys=frozenset(keys)
    )
    missing = [row["key"] for row in excluded if row["key"] in keys]
    if missing:
        raise ComparisonError(
            "missing_price_history",
            f"No usable price history for {', '.join(sorted(missing))}.",
            keys=sorted(missing),
        )
    absent = [key for key in keys if key not in panel.columns]
    if absent:
        raise ComparisonError(
            "missing_price_history",
            f"No usable price history for {', '.join(sorted(absent))}.",
            keys=sorted(absent),
        )
    panel = panel[list(keys)].sort_index()
    # The FULL loaded span, not the visible window: the curves are built over
    # this and sliced to `start` only at the end, so a purchase older than the
    # window still buys at its own day's price.
    index = pd.date_range(
        start=panel.index.min(), end=timezone.now(), freq="D", tz="UTC"
    )
    return panel.reindex(panel.index.union(index)).ffill().reindex(index).dropna()


def _units_held(flows, index) -> pd.Series:
    """Running units held on each day of the index, from the flow steps."""
    units = pd.Series(0.0, index=index)
    for flow in flows:
        at = pd.Timestamp(flow["at"]).tz_convert("UTC")
        units[index >= at] += float(flow["units"])
    return units


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
    if not any(flow["spent"] > 0 for flow in flows):
        # Every position came in as an opening or a price-less row, so there is
        # no amount to move. Reporting zero would look like a real answer.
        raise ComparisonError(
            "no_recorded_cost",
            f"Your {_label(subject)} position has no purchase prices recorded, "
            "so there is no amount of money to invest elsewhere.",
        )
    days, start = _window(flows, days)
    panel = _panel({subject.key, target.key}, days, start)
    index = panel.index

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
    panel = _panel({subject.key, target.key}, days, start)
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
    }


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
    portfolio = pd.Series(
        [float(point["total"]) for point in series],
        index=pd.to_datetime([point["date"] for point in series], utc=True),
    ).sort_index()
    panel = _panel({target.key}, capped, portfolio.index.min())
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
    panel = _panel({subject.key, target.key}, days, start)

    # Everything on the first day the story starts -- the day of the earliest
    # real purchase -- in each asset, and left alone. Buying at the visible
    # window's edge instead would answer a question nobody asked.
    left = _shown(invested / float(panel[subject.key].iloc[0]) * panel[subject.key], start)
    right = _shown(invested / float(panel[target.key].iloc[0]) * panel[target.key], start)
    return {
        "mode": "lump_sum",
        "series": [
            _curve(subject.key, f"All at once: {_label(subject)}", left),
            _curve(target.key, f"All at once: {_label(target)}", right),
        ],
        "summary": _summary(left, right, invested=invested),
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
    held_ids = set(
        LedgerEntry.objects.filter(
            account__in=accounts, kind__in=_POSITION_KINDS
        ).values_list("asset_id", flat=True)
    )
    assets = Asset.objects.filter(is_active=True, is_house=False).order_by("name")
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
    return {
        "holdings": [row for row in rows if row["held"]],
        "targets": rows,
    }
