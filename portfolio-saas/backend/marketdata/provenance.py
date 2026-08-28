"""Shared archive provenance and live-only daily-bar resolution."""

from __future__ import annotations

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


def daily_bar_units(symbols) -> dict[str, str]:
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
    rows = (
        MarketSnapshot.objects.filter(symbol__in=symbols)
        .order_by("symbol", "-observed_at")
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
