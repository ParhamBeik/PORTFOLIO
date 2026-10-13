"""What the same money would have done somewhere else.

One question, asked of several alternatives at once: every purchase you
actually made of one holding -- real dates, real amounts -- replayed into each
target. Each target answers with what that money would be worth today, how
far ahead or behind the holding is, and the holding price at which the two
would be level.

The page used to offer four modes (this one, two holdings side by side, the
portfolio rebased against one asset, and a lump sum on day one) one pair at a
time. They were removed: the question people bring here is this one, against
the dollar, gold and coins together.

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

from marketdata.currency import (
    TSE_RIAL_PER_TOMAN, holding_value_to_toman, is_tse_priced,
)

from ..models import Asset, LedgerEntry, owner_display_names
from .ledger import active_entries

#: What the same money is measured against when the page asks for nothing
#: else: the three places an Iranian saver actually moves money to.
DEFAULT_TARGETS = ("usd_cash", "gold_18k_gram", "emami_coin")
#: Each target is its own panel load, so one request cannot fan out over the
#: whole catalog.
MAX_TARGETS = 6

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


def _curve(key, label, series) -> dict:
    return {"key": key, "label": label, "unit": "toman", "points": _points(series)}


def _label(asset) -> str:
    return asset.name_fa or asset.name or asset.key


def _price_in_quote(asset, tomans_per_unit: float) -> dict:
    """A per-unit Toman figure in the unit the rest of the app shows this price in.

    TSE prices are shown in Rial everywhere else (see `holding_value_to_toman`),
    so a break-even stock price printed in Toman would read as a tenth of the
    number on the quote screen. The unit travels with the value; nothing
    downstream has to guess it from the asset class.
    """
    value = Decimal(str(tomans_per_unit))
    if is_tse_priced(asset):
        return {"value": round(float(value * TSE_RIAL_PER_TOMAN)), "unit": "IRR"}
    return {"value": round(float(value), 2), "unit": "IRT"}


def _live_tomans(asset, prices) -> float | None:
    """Today's price per unit in Toman, from the live price map, or None."""
    raw = prices.get(asset.key)
    if not raw or raw <= 0:
        return None
    return float(holding_value_to_toman(asset, Decimal(raw)))


def _close_today(series: pd.Series, day, value: float) -> pd.Series:
    """The curve with today's point set to a live value (added or replaced)."""
    series = series.copy()
    series.loc[day] = value
    return series.sort_index()


def _replay(subject, target, flows, days, prices=None) -> dict:
    """One row of the table: every real purchase of `subject`, replayed into `target`.

    The panel ends at the last daily close. The row ends at today's LIVE price
    instead, for both sides, so "worth today" means today: on 2026-10-13 KAMA
    closed at 6,290 Rial and traded live at 6,470, which is the difference
    between trailing the dollar by 1.5% and leading it by 1.2%. A row whose
    series stopped early keeps its own last real day -- a live tick cannot
    stand in for a series that has ended.
    """
    load, start = _window(flows, days)
    panel = _panel([subject.key, target.key], load, held=[subject.key])
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
    held = _units_held(flows, index)
    bought = _counterfactual_units(flows, panel[target.key], index)
    actual = _shown(held * panel[subject.key], start)
    alternative = _shown(bought * panel[target.key], start)
    units_now = float(held.iloc[-1])
    ended = any(w.get("reason") == "series_ended" for w in panel.attrs["warnings"])
    live_subject = _live_tomans(subject, prices or {})
    live_target = _live_tomans(target, prices or {})
    priced_live = bool(not ended and live_subject and live_target)
    if priced_live:
        today = _day(timezone.now())
        actual = _close_today(actual, today, units_now * live_subject)
        alternative = _close_today(
            alternative, today, float(bought.iloc[-1]) * live_target
        )
    end_actual = float(actual.iloc[-1])
    end_alt = float(alternative.iloc[-1])
    row = {
        "key": target.key,
        "label": _label(target),
        # How much of the alternative the same money would hold today: 105.6
        # coins says more to a reader than a Toman total does.
        "units": round(float(bought.iloc[-1]), 6),
        "value_tomans": round(end_alt, 2),
        "actual_tomans": round(end_actual, 2),
        "invested_tomans": round(invested, 2),
        "difference_tomans": round(end_actual - end_alt, 2),
        "difference_pct": (
            round((end_actual / end_alt - 1) * 100, 2) if end_alt > 0 else None
        ),
        # The subject price at which today's position would be worth exactly
        # what the alternative is: "KAMA has to reach 11,716 Rial to match gold".
        "breakeven_price": (
            _price_in_quote(subject, end_alt / units_now) if units_now > 0 else None
        ),
        "start_date": actual.index[0].date().isoformat(),
        "end_date": actual.index[-1].date().isoformat(),
        "window_days": (actual.index[-1] - actual.index[0]).days + 1,
        "priced_live": priced_live,
        "series": [
            _curve(subject.key, _label(subject), actual),
            _curve(target.key, _label(target), alternative),
        ],
        "warnings": [
            *panel.attrs["warnings"], *_carried_note(subject, index, carried)
        ],
    }
    if carried:
        row["carried_in_tomans"] = round(carried, 2)
    return row


def compare(user, *, account=None, subject=None, targets=None, days=None) -> dict:
    """Replay one holding's real purchases into each target.

    Raises ComparisonError with a renderable reason when the holding itself
    cannot be replayed. A target that cannot be compared becomes a row carrying
    its own reason instead: one coin with a short history must not blank the
    dollar and gold rows beside it.
    """
    if not subject:
        raise ComparisonError(
            "missing_subject", "Choose a holding to compare.", field="subject"
        )
    holding = _resolve_asset(subject, field="subject")
    flows = _flows(_accounts(user, account), holding)
    if not flows:
        raise ComparisonError(
            "no_purchase_history",
            f"There are no recorded {_label(holding)} purchases to replay.",
        )
    invested = sum(float(flow["spent"]) for flow in flows)
    if invested <= 0:
        # Every position came in as an opening or a price-less row, so there is
        # no amount to move. Reporting zero would look like a real answer.
        raise ComparisonError(
            "no_recorded_cost",
            f"Your {_label(holding)} position has no net money in it -- either "
            "no purchase prices are recorded, or the sales returned more than "
            "the purchases cost -- so there is no amount to invest elsewhere.",
        )
    keys = [
        key for key in dict.fromkeys(targets or DEFAULT_TARGETS) if key != holding.key
    ][:MAX_TARGETS]
    if not keys:
        raise ComparisonError(
            "missing_target", "Choose something to compare against.", field="target"
        )
    names = {a.key: _label(a) for a in Asset.objects.filter(key__in=keys)}
    from .valuation import get_latest_prices

    prices = get_latest_prices()
    rows = []
    for key in keys:
        try:
            target = _resolve_asset(key, field="target")
            rows.append(_replay(holding, target, flows, days, prices))
        except ComparisonError as exc:
            # A target that cannot answer is a row with its reason, never a
            # failed request: the page asks one target at a time and keeps the
            # rows it already has, so a refusal must not read as "the holding
            # cannot be compared".
            rows.append({
                "key": key, "label": names.get(key, key),
                "reason": exc.reason, "detail": exc.detail,
            })
    units = float(sum(flow["units"] for flow in flows))
    live = _live_tomans(holding, prices)
    answered = [row for row in rows if "reason" not in row]
    latest = max(answered, key=lambda row: row["end_date"], default=None)
    # Today's live price when there is one; otherwise the last close any row
    # reached. Both are the holding's own figures, independent of which targets
    # were asked for.
    per_unit = live if live else (
        latest["actual_tomans"] / units if latest and units > 0 else None
    )
    return {
        "subject": {
            "key": holding.key,
            "label": _label(holding),
            "units": units,
            # Net of sales, before any walk-in: the money really put in.
            "invested_tomans": round(invested, 2),
            "value_tomans": round(units * per_unit, 2) if per_unit else None,
            "price": _price_in_quote(holding, per_unit) if per_unit else None,
            "priced_live": bool(live),
            "end_date": (
                timezone.now().date().isoformat() if live
                else latest["end_date"] if latest else None
            ),
        },
        "rows": rows,
        "requested_days": days,
    }


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
    entries = [
        entry for entry in active_entries(accounts, kinds=_POSITION_KINDS)
        if entry.asset_id
    ]
    held_ids = {entry.asset_id for entry in entries}
    # Which holdings have money in them to replay. The page opens on one of
    # these: opening on a position recorded without a price answered
    # "no_recorded_cost" before the reader had chosen anything.
    priced_ids = {
        entry.asset_id for entry in entries
        if entry.kind == LedgerEntry.Kind.BUY and entry.price_tomans
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
            "priced": asset.id in priced_ids,
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
    offered = {row["key"] for row in rows}
    return {
        "holdings": [row for row in rows if row["held"]],
        "targets": rows,
        "default_targets": [key for key in DEFAULT_TARGETS if key in offered],
        "omitted_holdings": omitted,
    }
