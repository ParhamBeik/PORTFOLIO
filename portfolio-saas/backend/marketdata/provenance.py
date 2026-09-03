"""Shared archive provenance and live-only daily-bar resolution."""

from __future__ import annotations

import bisect

from .calendars import candle_close_qs
from .models import (
    ArchiveFetchState,
    GoldCurrencyHistory,
    MarketDailyBar,
    MarketInstrument,
    RejectedRecord,
)

#: Classes the provider never quotes in Toman, so an unresolved unit on one of
#: them is a refusal rather than a pass-through. See `daily_bar_price`.
_NEVER_TOMAN_CLASSES = frozenset({
    MarketDailyBar.AssetClass.CRYPTO,
    MarketDailyBar.AssetClass.COMMODITY,
})

# The endpoints that feed MarketDailyBar. A rejected row is one the warehouse
# has already judged bad; every reader of these bars must skip the same set or
# a price a valuation refuses can still reach the ledger as a trade price.
DAILY_BAR_ENDPOINTS = (
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY,
    ArchiveFetchState.Endpoint.ETF_NAV_DAILY,
    ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY,
)

#: Rejection verdicts covering the TSE daily-close tables (`MarketCandle`,
#: `DailyStockHistory`). Literals rather than `ArchiveFetchState.Endpoint`
#: members because `series:1d_adj` / `series:1d_unadj` are written by the
#: nightly series validator, which has no archive endpoint of its own.
STOCK_SERIES_ENDPOINTS = (
    "stock_candle_adjusted",
    "stock_candle_unadjusted",
    "stock_history_adjusted",
    "stock_history_unadjusted",
    "series:1d_adj",
    "series:1d_unadj",
)

#: Rejection verdicts covering `GoldCurrencyHistory` and the live-only classes
#: that land in `MarketDailyBar`.
BRS_SERIES_ENDPOINTS = (
    ArchiveFetchState.Endpoint.GOLD_DAILY,
    *DAILY_BAR_ENDPOINTS,
)

#: Every verdict that can disqualify a daily close, whichever table it came
#: from. Six call sites across `portfolio/` used to inline this list; they must
#: agree, because a row one price path refuses and another accepts is how the
#: same holding gets two different values on the same day.
PRICE_SERIES_ENDPOINTS = (*STOCK_SERIES_ENDPOINTS, *BRS_SERIES_ENDPOINTS)


def rejected_pairs(symbols, endpoints=PRICE_SERIES_ENDPOINTS, *, since=None):
    """`{(symbol, jalali_day)}` the warehouse has already judged bad.

    `since` is an optional Jalali lower bound: worth passing when the caller
    only reads a bounded window, pointless when it needs the newest usable row
    however far back that sits.
    """
    symbols = [s for s in symbols if s]
    if not symbols:
        return set()
    queryset = RejectedRecord.objects.filter(
        symbol__in=symbols, endpoint__in=list(endpoints)
    )
    if since is not None:
        queryset = queryset.filter(date__gte=since)
    return set(queryset.values_list("symbol", "date"))


# A daily bar is only usable as a portfolio price if it came from the same feed
# the asset is priced from -- `MarketDailyBar` keys on its own asset_class, and
# symbols collide across classes (a commodity and a coin can both be "BTC").
# Note there is deliberately no INDEX entry: MarketInstrument.Category has no
# index member, so an index bar can never be admitted as a portfolio price.
# The provider catalog is the authority; Asset.asset_class only distinguishes
# crypto, which is the one live-only class the portfolio seeds directly.
_CATEGORY_CLASSES = {
    MarketInstrument.Category.ETF: {MarketDailyBar.AssetClass.ETF_NAV},
    MarketInstrument.Category.CRYPTO: {MarketDailyBar.AssetClass.CRYPTO},
    MarketInstrument.Category.COMMODITY: {MarketDailyBar.AssetClass.COMMODITY},
}


def daily_bar_classes(asset, *, instrument_category: str | None = None) -> set[str]:
    """Return only MarketDailyBar classes compatible with a portfolio asset."""
    from portfolio.models import Asset

    if getattr(asset, "asset_class", "") == Asset.AssetClass.CRYPTO:
        return {MarketDailyBar.AssetClass.CRYPTO}
    return _CATEGORY_CLASSES.get(instrument_category, set())


def instrument_lookup(asset) -> tuple[str, str] | None:
    """The (source, symbol) an asset joins to the provider catalog on.

    MarketInstrument is unique per (source, symbol), not per symbol -- the same
    string can be listed by both providers. Which field the asset carries is
    what says whose catalog to read; matching on the symbol alone lets the
    other provider's category decide, and the class guard then rejects the
    asset's own bars.
    """
    if getattr(asset, "tse_symbol", ""):
        return MarketInstrument.Source.TSETMC, asset.tse_symbol
    if getattr(asset, "brs_symbol", ""):
        return MarketInstrument.Source.BRS, asset.brs_symbol
    return None


def latest_market_daily_bar(asset, *, as_of: str | None = None):
    """Newest usable daily bar for ONE asset.

    Returns the ROW, unconverted, for the Ops console -- which wants to show
    what the warehouse actually stored. Everything that needs a usable price
    goes through `daily_bar_price` instead.
    """
    lookup = instrument_lookup(asset)
    if lookup is None:
        return None
    source, symbol = lookup
    classes = daily_bar_classes(
        asset,
        instrument_category=MarketInstrument.objects.filter(source=source, symbol=symbol)
        .values_list("category", flat=True)
        .first(),
    )
    if not classes:
        return None
    queryset = MarketDailyBar.objects.filter(
        asset_class__in=classes,
        symbol=symbol,
        close_price__gt=0,
    ).exclude(
        date__in=RejectedRecord.objects.filter(
            symbol=symbol, endpoint__in=DAILY_BAR_ENDPOINTS
        ).values("date")
    )
    if as_of is not None:
        queryset = queryset.filter(date__lte=as_of)
    return queryset.order_by("-date", "-id").first()


def toman_per_dollar(dates=None) -> tuple[dict, list]:
    """Toman-per-dollar by Jalali date, plus a sorted key list for bisecting.

    Returns `(rates, sorted_dates)`. Callers convert a row dated D at the newest
    rate on or before D -- never at today's. A foreign series flattened by one
    fixed rate is a different asset's returns: it erases every move the rial
    itself made, which for a 90-day window of a depreciating rial restates the
    whole history by the drift.
    """
    from .models import GoldCurrencyHistory

    queryset = GoldCurrencyHistory.objects.filter(symbol="USD", close_price__gt=0)
    if dates:
        # Bounded BOTH ways. Only the rates spanning the rows being converted
        # are needed, plus the one immediately before the earliest (which is
        # what a row on a non-quoting day resolves to). Without the lower bound
        # a 90-day panel materialised years of USD history to answer 90 lookups.
        floor = (
            queryset.filter(date__lte=min(dates)).order_by("-date")
            .values_list("date", flat=True).first()
        )
        queryset = queryset.filter(date__lte=max(dates))
        if floor is not None:
            queryset = queryset.filter(date__gte=floor)
    rates = dict(queryset.order_by("date").values_list("date", "close_price"))
    return rates, sorted(rates)


def rate_on(rates, sorted_dates, date):
    """The newest dollar rate at or before `date`, or None."""
    index = bisect.bisect_right(sorted_dates, date) - 1
    return rates[sorted_dates[index]] if index >= 0 else None


def daily_bar_price(assets, *, since=None, as_of=None, latest_only=False) -> list[tuple]:
    """(symbol, jalali date, close) for the live-only classes.

    THE reader for `MarketDailyBar`. Crypto, commodities and ETF NAV have no
    provider history endpoint, so these bars are their only close series -- and the table stores the provider's number verbatim in whatever
    currency it was quoted, with no unit column of its own.

    Getting a bar to a usable price takes three things that were, until this
    existed, re-derived independently by five callers that reached four
    different answers: the class guard (a coin and a commodity can both be
    "BTC", so the symbol alone is not a key), the rejected-row filter (a day the
    warehouse already judged bad must not become a portfolio price), and the
    unit, read from the originating snapshot and converted at the rate of the
    row's OWN date. A quote that cannot be converted yields no row rather than
    a foreign number dressed up as local currency.

    UNIT CONTRACT -- the returned price is in the asset's own portfolio quote
    unit, exactly like every other price in this codebase:
      * BRS-sourced (crypto, commodity) -> Toman, converted from the provider's
        dollars. An unresolvable unit on these is refused outright, because
        they are NEVER quoted in Toman.
      * TSE-sourced (ETF NAV) -> Rial, left alone, matching `candle_close_qs`.
        Callers divide the qty x price PRODUCT once via
        `currency.holding_value_to_toman`, per the unit-boundary rule.
    Analytics readers that need a pure-Toman panel convert with
    `currency.tse_close_to_toman`, the documented sibling -- that is the same
    intentional split MarketCandle already has, not a second policy.

    `latest_only` returns just the newest usable row per symbol and narrows the
    query to match. The per-asset callers want exactly one row; computing the
    whole converted series to `max()` it turned two bounded lookups into a
    full-history scan, once per holding per cash-flow boundary.
    """
    from .currency import to_toman
    from .models import MarketDailyBar

    assets = list(assets)
    classes_by_symbol = daily_bar_classes_by_symbol(assets)
    if not classes_by_symbol:
        return []
    from django.db.models import Q

    symbols = list(classes_by_symbol)
    # Class match and rejected rows are both applied IN THE QUERY, not after it.
    # Filtering in Python is fine when every row is returned, but under
    # `latest_only` the newest row is chosen by the database: a rejected newest
    # row would be picked and then dropped, yielding nothing at all instead of
    # the previous good close.
    class_predicate = Q()
    for symbol, classes in classes_by_symbol.items():
        class_predicate |= Q(symbol=symbol, asset_class__in=list(classes))
    queryset = MarketDailyBar.objects.filter(
        class_predicate, close_price__gt=0
    )
    if since is not None:
        queryset = queryset.filter(date__gte=since)
    if as_of is not None:
        queryset = queryset.filter(date__lte=as_of)
    rejected = set(
        RejectedRecord.objects.filter(
            symbol__in=symbols, endpoint__in=DAILY_BAR_ENDPOINTS
        ).values_list("symbol", "date")
    )
    for symbol, date in rejected:
        queryset = queryset.exclude(symbol=symbol, date=date)
    if latest_only:
        queryset = queryset.order_by("symbol", "asset_class", "-date").distinct(
            "symbol", "asset_class"
        )
    else:
        queryset = queryset.order_by("symbol", "date")
    rows = list(queryset.values("symbol", "date", "close_price", "asset_class"))
    if not rows:
        return []
    # Keyed on (class, symbol), not symbol: a union of classes lets
    # `distinct("symbol")` resolve a symbol listed under two feeds from
    # whichever polled last, which is the collision this module exists to stop.
    units = daily_bar_units(
        [(cls, symbol) for symbol, classes in classes_by_symbol.items()
         for cls in classes]
    )
    rates, rate_dates = toman_per_dollar([row["date"] for row in rows])
    out = []
    for row in rows:
        symbol = row["symbol"]
        unit = units.get((row["asset_class"], symbol), "")
        if not unit and row["asset_class"] in _NEVER_TOMAN_CLASSES:
            # Fail closed. `to_toman` passes an unlabelled number through
            # unchanged, which is right for a class that IS quoted in Toman and
            # catastrophic for these two, which never are: a provider that stops
            # sending the label would put a dollar figure into net worth as
            # Toman. Every other unit decision in this codebase refuses rather
            # than assumes, and so does this one.
            continue
        price = to_toman(
            symbol, row["close_price"], unit,
            usd_rate=rate_on(rates, rate_dates, row["date"]),
        )
        if price > 0:
            out.append((symbol, row["date"], price))
    return out


def daily_bar_classes_by_symbol(assets) -> dict:
    """{symbol: {usable MarketDailyBar class}} for many assets, in one query.

    Keyed by symbol because every consumer has only the bar row to match, and
    `MarketDailyBar` records no source -- the class set is what keeps another
    feed's same-named row out.
    """
    lookups = {}
    for asset in assets:
        lookup = instrument_lookup(asset)
        if lookup is not None:
            lookups[asset] = lookup
    if not lookups:
        return {}
    categories = {
        (source, symbol): category
        for source, symbol, category in MarketInstrument.objects.filter(
            symbol__in=[symbol for _source, symbol in lookups.values()]
        ).values_list("source", "symbol", "category")
    }
    result = {}
    for asset, lookup in lookups.items():
        classes = daily_bar_classes(asset, instrument_category=categories.get(lookup))
        if classes:
            result[lookup[1]] = classes
    return result


def daily_bar_units(pairs) -> dict[tuple, str]:
    """{(asset_class, symbol): declared quote unit} for reading daily bars.

    `MarketDailyBar` has no unit column: `aggregate_market_daily_bars` distils
    it from `MarketSnapshot.last_price` verbatim, and the label the provider
    sent survives only in that snapshot's `provider_payload`. The BRS classes
    these bars serve are exactly the ones NOT quoted in Toman -- crypto comes in
    Tether, commodities in dollars -- so reading one as Toman merely because its
    column carries no unit is a 100,000x error.

    Keyed on the PAIR, and queried on it. `MarketSnapshot` is indexed
    (asset_class, symbol, -observed_at), so filtering on the symbol alone cannot
    use that index and sorts a million-row table instead; and a symbol listed
    under two feeds would resolve from whichever polled last, which is the
    cross-class collision this module exists to prevent.
    """
    from django.db.models import Q

    from .models import MarketSnapshot

    pairs = list(pairs)
    if not pairs:
        return {}
    predicate = Q()
    for asset_class, symbol in pairs:
        predicate |= Q(asset_class=asset_class, symbol=symbol)
    rows = (
        MarketSnapshot.objects.filter(predicate)
        .order_by("asset_class", "symbol", "-observed_at")
        .distinct("asset_class", "symbol")
        .values_list("asset_class", "symbol", "provider_payload")
    )
    units = {}
    for asset_class, symbol, payload in rows:
        unit = _declared_unit(payload or {})
        if unit:
            units[(asset_class, symbol)] = unit
    return units


def _declared_unit(payload) -> str:
    """The quote unit a BrsApi snapshot declares, by label or by construction.

    Commodity rows carry `unit: "دلار"` outright. Cryptocurrency.php carries no
    unit string at all -- it states the same quote TWICE, `price` in dollars
    beside `price_toman` converted, and `ingest_market_snapshots` stores the
    first. Two fields for one number is the provider naming the unit as plainly
    as a label would, and reading the pair is not the same thing as guessing
    from magnitude: without it Bitcoin priced at 79,606 TOMAN instead of 15.9
    billion, a factor of two hundred thousand.
    """
    unit = payload.get("unit")
    if unit:
        return str(unit)
    if payload.get("price_toman") and payload.get("price"):
        return "usd"
    return ""


def latest_archive_close(asset):
    """Return the latest class-correct archive close for ops and valuation."""
    if asset is None:
        return None
    if asset.tse_symbol:
        row = (
            candle_close_qs(asset.tse_symbol)
            .order_by("-date_time")
            .values("id", "date_time", "timeframe", "close_price")
            .first()
        )
        if row:
            return {
                "id": row["id"],
                "date": str(row["date_time"]).split()[0],
                "timeframe": row["timeframe"],
                "table": "MarketCandle",
                "close": row["close_price"],
            }
    if asset.brs_symbol:
        row = (
            GoldCurrencyHistory.objects.filter(
                symbol=asset.brs_symbol, close_price__gt=0
            )
            .order_by("-date")
            .values("id", "date", "close_price")
            .first()
        )
        if row:
            return {
                "id": row["id"],
                "date": row["date"],
                "timeframe": None,
                "table": "GoldCurrencyHistory",
                "close": row["close_price"],
            }
    row = latest_market_daily_bar(asset)
    if row:
        return {
            "id": row.id,
            "date": row.date,
            "timeframe": "1d",
            "table": "MarketDailyBar",
            "asset_class": row.asset_class,
            "close": row.close_price,
        }
    return None
