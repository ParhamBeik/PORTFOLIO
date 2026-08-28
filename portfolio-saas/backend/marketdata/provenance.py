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

# The endpoints that feed MarketDailyBar. A rejected row is one the warehouse
# has already judged bad; every reader of these bars must skip the same set or
# a price valuation refuses can still reach the ledger as a trade price.
#: Classes the provider never quotes in Toman, so an unresolved unit on one of
#: them is a refusal rather than a pass-through. See `daily_bar_toman`.
_NEVER_TOMAN_CLASSES = frozenset({
    MarketDailyBar.AssetClass.CRYPTO,
    MarketDailyBar.AssetClass.COMMODITY,
})

DAILY_BAR_ENDPOINTS = (
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY,
    ArchiveFetchState.Endpoint.ETF_NAV_DAILY,
    ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY,
)


# A daily bar is only usable as a portfolio price if it came from the same feed
# the asset is priced from -- `MarketDailyBar` keys on its own asset_class, and
# symbols collide across classes (an index and a coin can both be "BTC").
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

    `valuation._archive_replacements` resolves the same thing in bulk for the
    whole catalog rather than calling this per asset -- that path is on every
    valuation and cannot afford N queries. The two must stay in agreement on
    what counts as usable (class match, positive close, not rejected); change
    one and change the other.
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


def toman_per_dollar(dates=None) -> dict:
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
        queryset = queryset.filter(date__lte=max(dates))
    rates = dict(queryset.order_by("date").values_list("date", "close_price"))
    return rates, sorted(rates)


def rate_on(rates, sorted_dates, date):
    """The newest dollar rate at or before `date`, or None."""
    index = bisect.bisect_right(sorted_dates, date) - 1
    return rates[sorted_dates[index]] if index >= 0 else None


def daily_bar_toman(assets, *, since=None, as_of=None) -> list[tuple]:
    """(symbol, jalali date, Toman close) for the live-only classes.

    THE reader for `MarketDailyBar`. Crypto, commodities, ETF NAV and indexes
    have no provider history endpoint, so these bars are their only close
    series -- and the table stores the provider's number verbatim in whatever
    currency it was quoted, with no unit column of its own.

    Getting a bar to Toman takes three things that were, until this existed,
    re-derived independently by five callers that reached four different
    answers: the class guard (a coin and an index can both be "BTC", so the
    symbol alone is not a key), the rejected-row filter (a day the warehouse
    already judged bad must not become a portfolio price), and the unit, read
    from the originating snapshot and converted at the rate of the row's OWN
    date. A quote that cannot be converted yields no row rather than a foreign
    number dressed up as Toman.
    """
    from .currency import to_toman
    from .models import MarketDailyBar

    assets = list(assets)
    classes_by_symbol = daily_bar_classes_by_symbol(assets)
    if not classes_by_symbol:
        return []
    symbols = list(classes_by_symbol)
    queryset = MarketDailyBar.objects.filter(symbol__in=symbols, close_price__gt=0)
    if since is not None:
        queryset = queryset.filter(date__gte=since)
    if as_of is not None:
        queryset = queryset.filter(date__lte=as_of)
    rows = list(
        queryset.order_by("symbol", "date").values(
            "symbol", "date", "close_price", "asset_class"
        )
    )
    if not rows:
        return []
    rejected = set(
        RejectedRecord.objects.filter(
            symbol__in=symbols, endpoint__in=DAILY_BAR_ENDPOINTS
        ).values_list("symbol", "date")
    )
    units = daily_bar_units(
        symbols,
        asset_classes={cls for classes in classes_by_symbol.values() for cls in classes},
    )
    rates, rate_dates = toman_per_dollar([row["date"] for row in rows])
    out = []
    for row in rows:
        symbol = row["symbol"]
        if row["asset_class"] not in classes_by_symbol.get(symbol, set()):
            continue
        if (symbol, row["date"]) in rejected:
            continue
        unit = units.get(symbol, "")
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


def daily_bar_units(symbols, *, asset_classes=None) -> dict[str, str]:
    """The provider's declared quote unit per symbol, for reading daily bars.

    `MarketDailyBar` has no unit column: `aggregate_market_daily_bars` distils
    it from `MarketSnapshot.last_price` verbatim, and the label the provider
    sent survives only in that snapshot's `provider_payload`. The live-only
    classes these bars serve are exactly the ones NOT quoted in Toman -- crypto
    comes in Tether, commodities in dollars -- so reading a bar as Toman merely
    because its column carries no unit is a 100,000x error. Every reader of
    these bars resolves the unit through here and converts with
    `currency.to_toman`, which returns 0 (the "no price yet" sentinel) rather
    than a foreign number when no rate is available.

    An unknown or absent unit maps to nothing, and `to_toman` then passes the
    value through unchanged -- the same reading as before this existed.
    """
    from .models import MarketSnapshot

    symbols = list(symbols)
    if not symbols:
        return {}
    units = {}
    queryset = MarketSnapshot.objects.filter(symbol__in=symbols)
    if asset_classes:
        # Not just a narrowing: `MarketSnapshot` is indexed on
        # (asset_class, symbol, -observed_at), so without the class this cannot
        # use that index and falls back to sorting a million-row table. It also
        # reintroduces the cross-class symbol collision this module exists to
        # prevent -- a coin and an index can both be "BTC".
        queryset = queryset.filter(asset_class__in=list(asset_classes))
    rows = (
        queryset.order_by("symbol", "-observed_at")
        .distinct("symbol")
        .values_list("symbol", "provider_payload")
    )
    for symbol, payload in rows:
        unit = _declared_unit(payload or {})
        if unit:
            units[symbol] = unit
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
