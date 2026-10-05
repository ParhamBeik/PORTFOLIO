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

from ..models import Asset, LedgerEntry, owner_display_names
from .ledger import active_entries

MODES = ("counterfactual", "holdings", "benchmark", "lump_sum")

#: The comparison has to reach back to the first purchase, which for a real
#: broker history is years. Bounded so one request cannot ask the panel for an
#: unlimited scan.
MAX_WINDOW_DAYS = 3650
MIN_WINDOW_DAYS = 7

#: How far back `_benchmark` may rebuild the portfolio's own value series.
#:
#: Its own bound, deliberately not `SYNTHETIC_HISTORY_MAX_DAYS`. That constant is
#: 90 and it made the 1Y, 6M and 90D buttons on this tab return the identical
#: ninety-day answer -- three range buttons that did nothing. The 90 stands for
#: the other caller (a fabricated series shown when nothing was recorded); the
#: replay itself is real arithmetic and now costs one ledger read per account
#: rather than two per account per day, so a year of it is affordable.
BENCHMARK_MAX_DAYS = 400

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
    # The earliest of the flows, not the first of the list. `_holdings` hands
    # over two assets' flow lists concatenated, so position zero is whichever
    # asset was named first -- reading that as the pair's opening date loaded a
    # panel that stopped short of the other one's own history.
    first = min(
        (flow["at"] for flow in flows), default=now - dt.timedelta(days=365)
    )
    span = (now - first).days + 1
    shown = span if days is None else max(MIN_WINDOW_DAYS, int(days))
    load = min(max(span, shown, MIN_WINDOW_DAYS), MAX_WINDOW_DAYS)
    # Never opens before the money did. A 1Y button on a three-month-old position
    # otherwise drew nine months of a flat zero line and reported a start date on
    # which the user owned nothing -- the range was honoured literally and the
    # answer was nonsense. The window is what the data can support, and the
    # summary reports the span actually drawn so the page can say so.
    return load, max(now - dt.timedelta(days=min(shown, load)), first)


def _day(at) -> pd.Timestamp:
    """The flow's calendar day, in UTC, at midnight.

    The panel is indexed at midnight, so an afternoon purchase compared raw
    sorts AFTER that day's row: the position appeared a day late and the
    counterfactual spent the money at the following close. Same money, wrong
    price, and on a fast-moving series that is the whole difference being
    measured.
    """
    return pd.Timestamp(at).tz_convert("UTC").normalize()


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
        history_days=days, universe=keys, held_keys=frozenset(keys), gate=False
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
    stale, last_real = [], {}
    for key in keys:
        real = panel[key].dropna()
        if real.empty:
            stale.append(key)
            continue
        last_real[key] = real.index[-1]
        asset = assets.get(key)
        gap = sessions_between(
            to_jalali_str(real.index[-1]), today,
            market=market_for_asset(asset) if asset else "gold_currency",
        )
        if gap > MAX_FORWARD_FILL_SESSIONS or pd.isna(filled[key].iloc[-1]):
            stale.append(key)
    _refuse(
        [key for key in stale if key not in last_real],
        "missing_price_history", "No usable price history",
    )
    if stale:
        # A halted or delisted series ENDS the comparison; it no longer refuses
        # it. The window closes on the last day every asset had a real close, so
        # the figure the summary reports is genuinely that day's value and the
        # end date names it. The invariant the fill bound protects is that a
        # stale price is never presented as TODAY's outcome -- a window that
        # stops where the data stops does not do that, while refusing outright
        # threw away years of perfectly good overlap to avoid one dead tail.
        cutoff = min(last_real[key] for key in stale)
        filled = filled[filled.index <= cutoff]
        panel.attrs["warnings"] = [
            *panel.attrs["warnings"],
            *[
                {
                    "key": key,
                    "reason": "series_ended",
                    "detail": (
                        "last real price "
                        f"{last_real[key].date().isoformat()}; the comparison "
                        "stops there rather than carrying it forward to today"
                    ),
                }
                for key in sorted(stale)
            ],
        ]
    result = filled.dropna()
    # Two points is the least that can draw a line or state a change. Below that
    # the overlap is a coincidence, not a comparison.
    if len(result) < 2:
        raise ComparisonError(
            "no_overlapping_history",
            f"{' and '.join(sorted(keys))} do not have enough price history in "
            "common to compare -- their series barely overlap.",
            keys=sorted(keys),
        )
    result.attrs["warnings"] = panel.attrs["warnings"]
    return result


def _refuse(keys, reason, detail) -> None:
    if keys:
        keys = sorted(set(keys))
        # The sentence is shown to the reader as is, so it names assets the way
        # every picker on the page does; the keys stay on the error for code.
        names = {a.key: _label(a) for a in Asset.objects.filter(key__in=keys)}
        # Each name in a first-strong isolate: a Persian name beside an English
        # comma reorders the list in the browser ("for ,سکه امامی یورو.").
        listed = ", ".join(f"\u2068{names.get(k, k)}\u2069" for k in keys)
        raise ComparisonError(reason, f"{detail} for {listed}.", keys=keys)


def _units_held(flows, index) -> pd.Series:
    """Running units held on each day of the index, from the flow steps."""
    units = pd.Series(0.0, index=index)
    for flow in flows:
        units[index >= _day(flow["at"])] += float(flow["units"])
    return units


def _carry_in(flows, index, prices: pd.Series) -> tuple[list[dict], float]:
    """Fold the flows older than the window into one opening stake.

    `_panel` starts on the first day BOTH assets are priced, which for a target
    listed (or backfilled) after the purchase is long after the money was spent.
    Refusing there answered nothing at all -- and "nothing" is the wrong answer
    to a question that has a good one sitting right next to it.

    So the position WALKS IN. On the first shared day it is worth what it was
    actually worth: its own units at its own close. The alternative starts from
    that same stake, and every later purchase is replayed on its real date as
    before. The question becomes "from the day this comparison first became
    possible, which of these did better with the same money" -- answerable, and
    honest, because no price is invented for a day nobody has one for. What is
    NOT done is pricing the old purchase at the window's opening price and
    calling that its cost; that would be the fiction the refusal existed to
    prevent, and the carried stake is a market value on a real day instead.

    Returns the rewritten flows and the carried amount (0 when the history
    already reached back far enough, which leaves the flows untouched).
    """
    if index.empty or not flows:
        return list(flows), 0.0
    opens = index[0]
    before = [flow for flow in flows if _day(flow["at"]) < opens]
    if not before:
        return list(flows), 0.0
    rest = [flow for flow in flows if _day(flow["at"]) >= opens]
    units = sum(float(flow["units"]) for flow in before)
    if units <= 0:
        # Bought and sold again entirely before the window opened. Nothing
        # crossed the line, so nothing is carried; only the later flows remain.
        return rest, 0.0
    carried = units * float(prices.loc[opens])
    return [{"at": opens, "units": units, "spent": carried}, *rest], carried


def _carried_note(asset, index, carried) -> list[dict]:
    """The one sentence a reader needs when the window opens late."""
    if not carried:
        return []
    return [{
        "key": asset.key,
        "reason": "history_starts_late",
        "detail": (
            "the shared price history only reaches back to "
            f"{index[0].date().isoformat()}, so the comparison starts there "
            f"with the position valued at what it was worth that day "
            f"({round(carried):,.0f} Toman) rather than at its original cost"
        ),
    }]


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
        on_or_after = index[index >= _day(flow["at"])]
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
        units[index >= on_or_after[0]] = position
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


def _summary(
    actual: pd.Series, alternative: pd.Series, invested=None, *,
    requested_days=None, carried=0.0,
) -> dict:
    end_actual = float(actual.iloc[-1]) if len(actual) else 0.0
    end_alt = float(alternative.iloc[-1]) if len(alternative) else 0.0
    summary = {
        "actual_end_tomans": round(end_actual, 2),
        "alternative_end_tomans": round(end_alt, 2),
        "difference_tomans": round(end_actual - end_alt, 2),
        "start_date": actual.index[0].date().isoformat() if len(actual) else None,
        "end_date": actual.index[-1].date().isoformat() if len(actual) else None,
        # What the range button asked for against what the data could give. The
        # page prints a "1Y" label above a curve whose start date is the only
        # thing that ever said otherwise; a reader comparing two tabs had no way
        # to tell a short answer from a short position.
        "requested_days": requested_days,
        "window_days": (
            (actual.index[-1] - actual.index[0]).days + 1 if len(actual) else 0
        ),
    }
    if invested is not None:
        summary["invested_tomans"] = round(float(invested), 2)
    if carried:
        summary["carried_in_tomans"] = round(float(carried), 2)
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
    requested = days
    days, start = _window(flows, days)
    panel = _panel([subject.key, target.key], days, held=[subject.key])
    index = panel.index

    flows, carried = _carry_in(flows, index, panel[subject.key])
    invested = sum(float(flow["spent"]) for flow in flows)
    if invested <= 0:
        raise ComparisonError(
            "no_recorded_cost",
            f"Nothing of your {_label(subject)} position carries into the "
            f"window that {_label(target)} has prices for, so there is no "
            "amount to invest elsewhere.",
        )
    actual = _shown(_units_held(flows, index) * panel[subject.key], start)
    alternative = _shown(
        _counterfactual_units(flows, panel[target.key], index) * panel[target.key],
        start,
    )
    return {
        "mode": "counterfactual",
        "series": [
            _curve(subject.key, f"What you did: {_label(subject)}", actual),
            _curve(target.key, f"Instead: {_label(target)}", alternative),
        ],
        "summary": _summary(
            actual, alternative, invested=invested,
            requested_days=requested, carried=carried,
        ),
        "warnings": [
            *panel.attrs["warnings"], *_carried_note(subject, index, carried)
        ],
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
    requested = days
    days, start = _window(subject_flows + target_flows, days)
    panel = _panel([subject.key, target.key], days, held=[subject.key, target.key])
    index = panel.index

    # No carry-in here: both curves are units x price, and `_units_held` already
    # applies a purchase older than the window from the first day onwards. The
    # value on that day is what the position was worth, which is what this tab
    # asks. Only the money-replay modes need a stake to start from.
    left = _shown(_units_held(subject_flows, index) * panel[subject.key], start)
    right = _shown(_units_held(target_flows, index) * panel[target.key], start)
    return {
        "mode": "holdings",
        "series": [
            _curve(subject.key, _label(subject), left),
            _curve(target.key, _label(target), right),
        ],
        "summary": _summary(left, right, requested_days=requested),
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
    # Absence is asked about by key, not by truthiness: a book whose assets are
    # never priced on two consecutive days reports a base of 0 every day, which
    # is a real answer, and reading it as "no such field" quietly restored the
    # very chaining this replaced.
    legacy = not any("total_ex_flows_base" in point for point in series)

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
    from .valuation import compute_dynamic_net_worth_series

    target = _resolve_asset(target_key, field="target")
    # No per-asset flows to reach back for here, so unlike the other modes the
    # requested window IS the whole request. The net-worth series is still
    # bounded -- every extra day is another day of the replay -- so asking for
    # more than it gives returns a shorter window than the caller asked for,
    # and the answer says so rather than letting the axis imply a range the
    # data does not cover. "All" means "whatever the replay reaches", which is
    # the bound itself and therefore not a truncation.
    #
    # The bound is BENCHMARK_MAX_DAYS, not the 90-day synthetic-series cap this
    # used to borrow. That cap made 1Y, 6M and 90D three names for the same
    # ninety days, so two of the four range buttons on this tab were decoration.
    capped = min(days or BENCHMARK_MAX_DAYS, BENCHMARK_MAX_DAYS)
    truncated = capped if days and days > capped else None
    series = compute_dynamic_net_worth_series(
        user, account, days=capped, max_days=BENCHMARK_MAX_DAYS
    )
    if not series:
        raise ComparisonError(
            "no_portfolio_history",
            "This portfolio has no value history to compare yet.",
        )
    portfolio = _twr_index(series)
    # Days before the book held anything are a flat 100 that never moved. A
    # one-year window on a two-month-old portfolio drew ten months of that line
    # and dated the verdict to a day on which there was nothing to compare --
    # the same rule `_window` applies to the money-replay modes.
    funded = [
        pd.Timestamp(point["date"], tz="UTC")
        for point in series
        if float(point["total"]) != 0
    ]
    if funded:
        portfolio = portfolio[portfolio.index >= min(funded)]
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
            "requested_days": days,
            "window_days": (index[-1] - index[0]).days + 1,
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
    requested = days
    days, start = _window(flows, days)
    panel = _panel([subject.key, target.key], days, held=[subject.key])

    # Bought on the day of the earliest REAL purchase, not on the first day of
    # the loaded panel. The panel deliberately reaches back further than the
    # visible window (and further again when a range button asks for more days
    # than the position has existed), so `iloc[0]` would deploy the money before
    # it existed -- a year of growth on ten-day-old money, and not comparable to
    # the drip mode it sits beside.
    #
    # When the shared history starts LATER than that purchase the stake is what
    # the position was worth on the first shared day plus anything added after
    # it -- the same walk-in the drip mode does, so the two tabs still describe
    # the same money over the same window.
    index = panel.index
    flows, carried = _carry_in(flows, index, panel[subject.key])
    stake = sum(float(flow["spent"]) for flow in flows)
    if not flows or stake <= 0:
        raise ComparisonError(
            "no_recorded_cost",
            f"Nothing of your {_label(subject)} position carries into the "
            f"window that {_label(target)} has prices for, so there is no "
            "amount to put in on day one.",
        )
    opened = index[index >= _day(flows[0]["at"])]
    if opened.empty:
        raise ComparisonError(
            "no_price_on_purchase_date",
            "There is no price on or after the first purchase date.",
        )
    day = opened[0]
    p_left = float(panel[subject.key].loc[day])
    p_right = float(panel[target.key].loc[day])
    if p_left <= 0 or p_right <= 0:
        _refuse(
            [k for k, p in ((subject.key, p_left), (target.key, p_right)) if p <= 0],
            "unpriced_on_purchase_date",
            "Price on start date must be positive.",
        )
    left = _shown(
        stake / p_left * panel[subject.key].loc[day:],
        start,
    )
    right = _shown(
        stake / p_right * panel[target.key].loc[day:],
        start,
    )
    return {
        "mode": "lump_sum",
        "series": [
            _curve(subject.key, f"All at once: {_label(subject)}", left),
            _curve(target.key, f"All at once: {_label(target)}", right),
        ],
        "summary": _summary(
            left, right, invested=stake,
            requested_days=requested, carried=carried,
        ),
        "warnings": [
            *panel.attrs["warnings"], *_carried_note(subject, index, carried)
        ],
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
    # The omitted list is the one place a property is certain to appear, so it is
    # also where "Real Estate" was least useful: it told the reader an asset they
    # have never called that was left out, and could not distinguish two houses.
    owned = owner_display_names(accounts)
    omitted = [
        {
            "key": asset.key,
            "label": owned.get(asset.id) or _label(asset),
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
