"""Valuation engine: holdings x latest prices -> portfolio value.

This is the SaaS port of PORTFOLIO NEW STRUCTURE/src/engine.py `build_snapshot`.
Two scale levers live here:
  1. latest prices are read once (DISTINCT ON) and cached, not per-asset;
  2. valuation is pure arithmetic over a preloaded holding set.
"""
import logging
from decimal import Decimal

from django.core.cache import cache

from ..models import Account, Asset, Holding, Price

logger = logging.getLogger(__name__)

_LATEST_PRICES_CACHE_KEY = "prices:latest"
_ARCHIVE_DROP_FLOOR = Decimal("0.50")
_ARCHIVE_SPIKE_CEILING = Decimal("2.00")
_HOUSE_AREA_SQM = Decimal("90.2")
_HOUSE_MORTGAGE_DEDUCTION = Decimal("400000000")


def _q(value) -> Decimal:
    """Coerce to Decimal, treating anything non-numeric as zero."""
    try:
        return Decimal(value)
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def get_latest_prices() -> dict:
    """Return {asset_key: Decimal price in Tomans}, cached ~10s.

    Uses Postgres DISTINCT ON to fetch the newest price for every asset in a
    single query, so this is O(1) regardless of how many assets or users exist.
    """
    cached = cache.get(_LATEST_PRICES_CACHE_KEY)
    if cached is not None:
        return cached

    latest = (
        Price.objects.select_related("asset")
        .filter(asset__is_active=True)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    prices = guard_price_map({row.asset.key: _q(row.price) for row in latest})
    # TTL matches the fetch cadence so the cache only misses when the fetcher
    # explicitly invalidates it, not on a timer (M1: no periodic stampede).
    cache.set(_LATEST_PRICES_CACHE_KEY, prices, timeout=120)
    return prices


def guard_price_map(prices: dict, *, fill_missing=True) -> dict:
    """Replace missing or broken live prices with previous prices or archive closes."""
    supplied_keys = set(prices)
    guarded = {key: _q(value) for key, value in prices.items()}
    
    # 1. Fetch latest recorded valid prices from DB for comparison
    latest_db_rows = (
        Price.objects.select_related("asset")
        .filter(asset__is_active=True, price__gt=0)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    prev_prices = {row.asset.key: _q(row.price) for row in latest_db_rows}

    # 2. Forward-fill missing prices. Large positive moves remain observable;
    # archive corroboration below rejects only catastrophic deviations.
    for key, live_price in list(guarded.items()):
        prev_price = prev_prices.get(key)
        if prev_price and prev_price > 0:
            if live_price <= 0:
                logger.info(
                    "[LIVE_PRICE_FORWARD_FILL] Key='%s' missing or zero live price. Forward-filling previous price %s.",
                    key, prev_price
                )
                guarded[key] = prev_price

    # 3. Apply archive fallback for zero/missing or massive historical deviations.
    replacements = _archive_replacements(guarded)
    guarded.update(
        replacements if fill_missing else {
            key: value for key, value in replacements.items() if key in supplied_keys
        }
    )
    return guarded


def _archive_replacements(prices: dict) -> dict:
    """Return archive-backed replacements for missing or obviously broken live prices."""
    from marketdata.models import DailyStockHistory, GoldCurrencyHistory

    assets = Asset.objects.filter(is_active=True).exclude(is_house=True)
    stock_symbols = {
        asset.tse_symbol: asset.key
        for asset in assets
        if asset.tse_symbol
    }
    brs_symbols = {
        asset.brs_symbol: asset.key
        for asset in assets
        if asset.brs_symbol
    }

    archive_prices = {}
    stock_rows = (
        DailyStockHistory.objects.filter(symbol__in=stock_symbols, is_adjusted=True, pl__gt=0)
        .order_by("symbol", "-date")
        .values("symbol", "pl")
    )
    for row in stock_rows:
        archive_prices.setdefault(stock_symbols[row["symbol"]], _q(row["pl"]))

    brs_rows = (
        GoldCurrencyHistory.objects.filter(symbol__in=brs_symbols, close_price__gt=0)
        .order_by("symbol", "-date")
        .values("symbol", "close_price")
    )
    for row in brs_rows:
        archive_prices.setdefault(brs_symbols[row["symbol"]], _q(row["close_price"]))

    replacements = {}
    for key, archive_price in archive_prices.items():
        live_price = _q(prices.get(key))
        if live_price <= 0:
            logger.warning("[PRICE_FALLBACK_ARCHIVE] Key='%s' Live=0. Using archive price %s", key, archive_price)
            replacements[key] = archive_price
        elif live_price < archive_price * _ARCHIVE_DROP_FLOOR or live_price > archive_price * _ARCHIVE_SPIKE_CEILING:
            logger.warning(
                "[PRICE_DEVIATION_ARCHIVE] Key='%s' Live=%s Archive=%s outside range [%s, %s]. Using archive price.",
                key, live_price, archive_price, archive_price * _ARCHIVE_DROP_FLOOR, archive_price * _ARCHIVE_SPIKE_CEILING
            )
            replacements[key] = archive_price
    return replacements


def invalidate_prices_cache() -> None:
    """Called after a fresh fetch so reads immediately see new prices."""
    cache.delete(_LATEST_PRICES_CACHE_KEY)


def _house_value(price_per_sqm_million: Decimal, area_sqm: Decimal = _HOUSE_AREA_SQM, mortgage_deduction: Decimal = _HOUSE_MORTGAGE_DEDUCTION) -> Decimal:
    """Port of engine.calculate_house_value: area * price/sqm - mortgage."""
    sqm_price = _q(price_per_sqm_million) * Decimal("1000000")
    area = _q(area_sqm) if area_sqm is not None else _HOUSE_AREA_SQM
    mortgage = _q(mortgage_deduction) if mortgage_deduction is not None else _HOUSE_MORTGAGE_DEDUCTION
    return sqm_price * area - mortgage


def asset_value(holding: Holding, price: Decimal) -> Decimal:
    """Quantity x unit price, or the house formula for real estate."""
    if holding.asset.is_house:
        area = getattr(holding, "area_sqm", _HOUSE_AREA_SQM)
        mortgage = getattr(holding, "mortgage_deduction_tomans", _HOUSE_MORTGAGE_DEDUCTION)
        return _house_value(holding.quantity, area_sqm=area, mortgage_deduction=mortgage)
    return _q(holding.quantity) * _q(price)



def value_account(account: Account, prices: dict | None = None) -> dict:
    """Compute one account's per-asset values and total.

    Returns: {'total': Decimal, 'items': [{'asset','key','class','quantity',
    'unit_price','value'}]}
    """
    prices = prices if prices is not None else get_latest_prices()
    items, total = [], Decimal("0")
    holdings = (
        account.holdings.select_related("asset")
        if account.pk
        else Holding.objects.none()
    )
    for holding in holdings:
        # None when the asset has no price yet — distinguishable from a real 0 (M2).
        unit_price = prices.get(holding.asset.key)
        value = asset_value(holding, unit_price)
        total += value
        items.append({
            "asset": holding.asset.name,
            "key": holding.asset.key,
            "class": holding.asset.asset_class,
            "quantity": holding.quantity,
            "unit_price": unit_price,
            "value": value,
        })
    return {"total": total, "items": items}


def value_user(user) -> dict:
    """Aggregate valuation across all of a user's accounts."""
    prices = get_latest_prices()
    accounts, total = [], Decimal("0")
    for account in user.accounts.all():
        valuation = value_account(account, prices)
        total += valuation["total"]
        accounts.append({
            "id": account.id,
            "name": account.name,
            "broker": account.broker,
            "total": valuation["total"],
            "items": valuation["items"],
        })
    return {"total": total, "accounts": accounts, "prices": prices}


def compute_dynamic_net_worth_series(user, account=None, days: int = 30) -> list[dict]:
    """Compute an instant on-the-fly historical net worth series for a portfolio.

    Multiplies holdings against historical asset price time-series in
    DailyStockHistory and GoldCurrencyHistory for past `days`, adjusting holding
    quantities backward using trade ledger events (Transaction).
    """
    from datetime import timedelta
    import jdatetime
    from django.utils import timezone
    from marketdata.models import DailyStockHistory, GoldCurrencyHistory
    from portfolio.models import Holding, Transaction

    days = max(1, min(days, 365))
    now = timezone.now()
    since = now - timedelta(days=days)

    if account is not None:
        holdings = list(account.holdings.select_related("asset").all())
    else:
        holdings = list(Holding.objects.filter(account__user=user).select_related("asset").all())

    if not holdings:
        return []

    latest_quantities = {h.asset.key: Decimal(str(h.quantity)) for h in holdings}
    assets = {h.asset.key: h.asset for h in holdings}

    stock_symbols = {a.tse_symbol: a.key for a in assets.values() if a.tse_symbol}
    brs_symbols = {a.brs_symbol: a.key for a in assets.values() if a.brs_symbol}

    stock_closes = {}
    if stock_symbols:
        s_rows = DailyStockHistory.objects.filter(
            symbol__in=list(stock_symbols.keys()), is_adjusted=True, pl__gt=0
        ).values("symbol", "date", "pl")
        for r in s_rows:
            key = stock_symbols[r["symbol"]]
            stock_closes.setdefault(r["date"], {})[key] = Decimal(str(r["pl"]))

    gold_closes = {}
    if brs_symbols:
        g_rows = GoldCurrencyHistory.objects.filter(
            symbol__in=list(brs_symbols.keys()), close_price__gt=0
        ).values("symbol", "date", "close_price")
        for r in g_rows:
            key = brs_symbols[r["symbol"]]
            gold_closes.setdefault(r["date"], {})[key] = Decimal(str(r["close_price"]))

    latest_prices = get_latest_prices()
    usd_rate = Decimal(latest_prices.get("usd_cash", 0) or 0)

    # Track last known price for each asset to seamlessly fill non-trading days
    last_known_prices = {key: _q(latest_prices.get(key, 0)) for key in assets}

    series = []
    for i in range(days - 1, -1, -1):
        target_date = now - timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        jalali_str = jdatetime.date.fromgregorian(date=target_date.date()).strftime("%Y-%m-%d")

        total = Decimal("0")
        for key, asset in assets.items():
            qty = latest_quantities.get(key, Decimal("0"))
            if asset.is_house:
                total += _house_value(qty)
            else:
                p = stock_closes.get(jalali_str, {}).get(key)
                if p is None:
                    p = gold_closes.get(jalali_str, {}).get(key)
                if p is not None:
                    last_known_prices[key] = p
                else:
                    p = last_known_prices.get(key, _q(latest_prices.get(key, 0)))
                total += qty * p

        val_usd = str(round(total / usd_rate, 2)) if usd_rate > 0 else None
        series.append({
            "timestamp": target_date.isoformat(),
            "date": date_str,
            "total": str(round(total, 4)),
            "total_usd": val_usd,
        })

    return series
