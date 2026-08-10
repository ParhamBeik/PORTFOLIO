"""Valuation engine: holdings x latest prices -> portfolio value.

SaaS port of the tracker engine build_snapshot pricing rules.
Two scale levers live here:
  1. latest prices are read once (DISTINCT ON) and cached, not per-asset;
  2. valuation is pure arithmetic over a preloaded holding set.
"""
import logging
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from ..models import HOUSE_AREA_SQM, Account, Asset, Holding, Price

logger = logging.getLogger(__name__)

_LATEST_PRICES_CACHE_KEY = "prices:latest"
_ARCHIVE_DROP_FLOOR = Decimal("0.50")
_ARCHIVE_SPIKE_CEILING = Decimal("2.00")


def _q(value) -> Decimal:
    """Coerce to Decimal, treating anything non-numeric as zero."""
    try:
        return Decimal(value)
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def get_latest_prices() -> dict:
    """Return cached provider-scale prices keyed by asset.

    TSE stock values are Rial under the legacy quantity convention; other
    portfolio values are normally Toman.

    Uses Postgres DISTINCT ON to fetch the newest price for every asset in a
    single query, so this is O(1) regardless of how many assets or users exist.
    """
    cached = cache.get(_LATEST_PRICES_CACHE_KEY)
    if cached is not None:
        return cached

    latest = (
        Price.objects.select_related("asset")
        .filter(asset__is_active=True, price__gt=0)
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
    from marketdata.candles import candle_close_qs
    from marketdata.models import GoldCurrencyHistory, RejectedRecord

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

    all_symbols = list(stock_symbols.keys()) + list(brs_symbols.keys())
    rejections = set(
        RejectedRecord.objects.filter(
            symbol__in=all_symbols,
            endpoint__in=[
                "stock_candle_adjusted", "stock_candle_unadjusted",
                "stock_history_adjusted", "stock_history_unadjusted",
                "series:1d_adj", "series:1d_unadj",
                "gold_daily", "crypto_daily", "commodity_daily",
                "market_index_daily", "etf_nav_daily", "option_contract_daily"
            ]
        ).values_list("symbol", "date")
    )

    archive_prices = {}
    # Adjusted closes live in MarketCandle.ADJUSTED. DailyStockHistory(is_adjusted=True)
    # was never adjusted prices at all -- History.php?type=1 is the Real/Legal
    # breakdown -- so every row there had pl=0 and this fallback silently matched
    # nothing.
    stock_rows = (
        candle_close_qs(stock_symbols)
        .order_by("symbol", "-date_time")
        .values("symbol", "date_time", "close_price")
    )
    for row in stock_rows:
        dt_str = row["date_time"].split()[0]
        if (row["symbol"], dt_str) not in rejections:
            archive_prices.setdefault(
                stock_symbols[row["symbol"]], _q(row["close_price"])
            )

    brs_rows = (
        GoldCurrencyHistory.objects.filter(symbol__in=brs_symbols, close_price__gt=0)
        .order_by("symbol", "-date")
        .values("symbol", "date", "close_price")
    )
    for row in brs_rows:
        if (row["symbol"], row["date"]) not in rejections:
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


def _house_value(price_per_sqm_million: Decimal, area_sqm: Decimal = HOUSE_AREA_SQM) -> Decimal:
    """Gross real-estate value: area * price/sqm.

    `price_per_sqm_million` is user-entered as "million Tomans per sqm", matching
    the Toman scale every other asset_value() branch produces (Price is
    Toman-denominated).

    The mortgage is deliberately NOT subtracted here. Since migration 0017 a
    mortgage is a `Liability` row, and every caller already nets liabilities off
    the account total — deducting it again here would double-count the debt.
    """
    sqm_price = _q(price_per_sqm_million) * Decimal("1000000")
    area = _q(area_sqm) if area_sqm is not None else HOUSE_AREA_SQM
    return sqm_price * area


def asset_value(holding: Holding, price: Decimal) -> Decimal:
    """Quantity x unit price, or the house formula for real estate."""
    if holding.asset.is_house:
        return _house_value(holding.quantity, area_sqm=getattr(holding, "area_sqm", HOUSE_AREA_SQM))
    return _q(holding.quantity) * _q(price)



def value_account(account: Account, prices: dict | None = None) -> dict:
    """Compute one account's per-asset values and total.

    Returns: {'total': Decimal, 'items': [{'asset','key','class','quantity',
    'unit_price','value'}]}
    """
    from portfolio.models import Liability
    prices = prices if prices is not None else get_latest_prices()
    items, excluded, total = [], [], Decimal("0")
    liabilities_qs = account.liabilities.all() if account.pk else Liability.objects.none()
    total_liabilities = sum(l.amount_tomans for l in liabilities_qs)
    holdings = (
        account.holdings.select_related("asset")
        if account.pk
        else Holding.objects.none()
    )
    holdings = list(holdings)
    latest_rows = {
        row.asset_id: row
        for row in Price.objects.filter(asset_id__in=[h.asset_id for h in holdings])
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    }
    now = timezone.now()
    priced_assets = 0
    from marketdata.currency import TSE_PRICE_UNIT, tse_unit_verified
    for holding in holdings:
        # None when the asset has no price yet — distinguishable from a real 0 (M2).
        unit_price = prices.get(holding.asset.key)
        row = latest_rows.get(holding.asset_id)
        if holding.asset.is_house:
            value = asset_value(holding, unit_price)
            source = "manual_valuation"
            priced_at = holding.updated_at
            age_seconds = max(0, int((now - holding.updated_at).total_seconds()))
            quality_status = "fallback"
        elif unit_price is None or _q(unit_price) <= 0:
            value = None
            source = None
            priced_at = None
            age_seconds = None
            quality_status = "unavailable"
            excluded.append({
                "asset_key": holding.asset.key,
                "reason": "missing_price",
            })
        else:
            value = asset_value(holding, unit_price)
            if row and _q(row.price) == _q(unit_price):
                source = row.source
                priced_at = row.fetched_at
                age_seconds = max(0, int((now - row.fetched_at).total_seconds()))
                quality_status = "live" if age_seconds <= 300 else "stale"
            else:
                source = "archive"
                priced_at = None
                age_seconds = None
                quality_status = "fallback"
        if value is not None:
            total += value
            priced_assets += 1
        price_unit_status = "ok"
        if holding.asset.tse_symbol and not tse_unit_verified():
            price_unit_status = "unverified"
        items.append({
            "asset": holding.asset.name,
            "key": holding.asset.key,
            "class": holding.asset.asset_class,
            "quantity": holding.quantity,
            "unit_price": unit_price,
            "value": value,
            "source": source,
            "priced_at": priced_at.isoformat() if priced_at else None,
            "age_seconds": age_seconds,
            "quality_status": quality_status,
            "price_unit_status": price_unit_status,
        })
    total_assets = len(holdings)
    if total_assets and priced_assets == 0:
        quality_status = "unavailable"
    elif excluded or any(item["quality_status"] != "live" for item in items):
        quality_status = "partial"
    else:
        quality_status = "complete"
    total -= total_liabilities
    return {
        "total": total,
        "items": items,
        "priced_assets": priced_assets,
        "total_assets": total_assets,
        "quality_status": quality_status,
        "tse_unit_policy": TSE_PRICE_UNIT,
        "excluded": excluded,
        "liabilities": [
            {
                "id": l.id,
                "label": l.label,
                "amount_tomans": float(l.amount_tomans),
                "asset_key": l.asset.key if l.asset else None,
            }
            for l in liabilities_qs
        ],
        "total_liabilities": float(total_liabilities),
    }


def value_user(user) -> dict:
    """Aggregate valuation across all of a user's accounts."""
    prices = get_latest_prices()
    accounts, total = [], Decimal("0")
    priced_assets = total_assets = 0
    excluded = []
    total_liabilities = Decimal("0")
    all_liabilities = []
    for account in user.accounts.all():
        valuation = value_account(account, prices)
        total += valuation["total"]
        priced_assets += valuation["priced_assets"]
        total_assets += valuation["total_assets"]
        excluded.extend(
            {"account_id": account.id, **item} for item in valuation["excluded"]
        )
        accounts.append({
            "id": account.id,
            "name": account.name,
            "broker": account.broker,
            "total": valuation["total"],
            "items": valuation["items"],
            "liabilities": valuation.get("liabilities", []),
            "total_liabilities": valuation.get("total_liabilities", 0.0),
        })
        total_liabilities += Decimal(str(valuation.get("total_liabilities", 0.0)))
        all_liabilities.extend([
            {"account_id": account.id, "account_name": account.name, **l}
            for l in valuation.get("liabilities", [])
        ])
    return {
        "total": total,
        "accounts": accounts,
        "prices": prices,
        "priced_assets": priced_assets,
        "total_assets": total_assets,
        "quality_status": (
            "unavailable" if total_assets and priced_assets == 0
            else "partial" if excluded or priced_assets < total_assets
            else "complete"
        ),
        "excluded": excluded,
        "liabilities": all_liabilities,
        "total_liabilities": float(total_liabilities),
    }


SYNTHETIC_HISTORY_MAX_DAYS = 90


def _accounts_have_buy_sell(accounts) -> bool:
    from portfolio.models import LedgerEntry

    return LedgerEntry.objects.filter(
        account__in=list(accounts),
        kind__in=[LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL],
    ).exists()


def compute_dynamic_net_worth_series(user, account=None, days: int = 30) -> list[dict]:
    """Compute an instant on-the-fly historical net worth series for a portfolio.

    Multiplies holdings against historical asset price time-series in
    MarketCandle and GoldCurrencyHistory for past `days` (capped at 90).

    When the account(s) have no BUY/SELL ledger rows (opening/quantity-only),
    current quantities are held constant across the window. Otherwise quantities
    are walked backward via `holdings_as_of`.
    """
    from datetime import timedelta
    import jdatetime
    from django.utils import timezone
    from marketdata.candles import candle_close_qs
    from marketdata.models import GoldCurrencyHistory
    from portfolio.models import Holding, Liability
    from portfolio.services.timeline import holdings_as_of

    days = max(1, min(int(days), SYNTHETIC_HISTORY_MAX_DAYS))
    now = timezone.now()

    if account is not None:
        holdings = list(account.holdings.select_related("asset").all())
        accounts = [account]
    else:
        holdings = list(Holding.objects.filter(account__user=user).select_related("asset").all())
        accounts = list(user.accounts.all())

    if not holdings:
        return []

    latest_quantities = {h.asset.key: _q(h.quantity) for h in holdings}
    assets = {h.asset.key: h.asset for h in holdings}
    constant_holdings = not _accounts_have_buy_sell(accounts)

    stock_symbols = {a.tse_symbol: a.key for a in assets.values() if a.tse_symbol}
    brs_symbols = {a.brs_symbol: a.key for a in assets.values() if a.brs_symbol}

    stock_closes = {}
    if stock_symbols:
        s_rows = candle_close_qs(list(stock_symbols)).values(
            "symbol", "date_time", "close_price"
        )
        for r in s_rows:
            key = stock_symbols[r["symbol"]]
            # Portfolio TSE quotes follow warehouse Rial under the legacy
            # one-tenth-share convention.
            stock_closes.setdefault(r["date_time"], {})[key] = _q(r["close_price"])

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

    liabilities = Liability.objects.filter(account__in=accounts)
    total_liabilities = sum(l.amount_tomans for l in liabilities)

    series = []
    for i in range(days - 1, -1, -1):
        target_date = now - timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        jalali_str = jdatetime.date.fromgregorian(date=target_date.date()).strftime("%Y-%m-%d")

        total = Decimal("0")

        if constant_holdings:
            day_holdings = dict(latest_quantities)
        else:
            day_holdings = {}
            for acc in accounts:
                for k, v in holdings_as_of(user, acc, target_date).items():
                    day_holdings[k] = day_holdings.get(k, Decimal("0")) + v

        for key, asset in assets.items():
            qty = day_holdings.get(key, Decimal("0"))
            if asset.is_house:
                holding = next((h for h in holdings if h.asset_id == asset.id), None)
                area = holding.area_sqm if holding else HOUSE_AREA_SQM
                total += _house_value(qty, area_sqm=area)
            else:
                p = stock_closes.get(jalali_str, {}).get(key)
                if p is None:
                    p = gold_closes.get(jalali_str, {}).get(key)
                if p is not None:
                    last_known_prices[key] = p
                else:
                    p = last_known_prices.get(key, _q(latest_prices.get(key, 0)))
                total += qty * p

        total -= total_liabilities
        val_usd = str(round(total / usd_rate, 2)) if usd_rate > 0 else None
        series.append({
            "timestamp": target_date.isoformat(),
            "date": date_str,
            "total": str(round(total, 4)),
            "total_usd": val_usd,
            "is_estimated": True,
        })

    return series


MAX_FORWARD_FILL_SESSIONS = 5


def _stale_sessions(queryset, date_field: str, last_date: str, as_of_jalali: str) -> int:
    """Market sessions between a price's own date and `as_of`, exclusive of both.

    These warehouse tables hold one row per symbol per session, so the distinct
    dates across all symbols in the window *are* the market's session calendar.
    A count above MAX_FORWARD_FILL_SESSIONS means the asset stopped printing
    while the market kept trading — carrying its last close further would
    invent a price rather than report one.
    """
    return (
        queryset.filter(**{
            f"{date_field}__gt": last_date,
            f"{date_field}__lte": as_of_jalali,
        })
        .values_list(date_field, flat=True)
        .distinct()
        .count()
    )


def value_as_of(user, account=None, as_of=None, basis="nominal") -> dict:
    """Compute valuation of portfolio assets as of a specific date and basis."""
    from django.utils import timezone
    from portfolio.services.deflator import cpi_for_date, normalize_basis
    from portfolio.services.returns import normalize_as_of, to_jalali_str
    from portfolio.services.timeline import cash_as_of, holdings_as_of
    from marketdata.candles import candle_close_qs
    from marketdata.models import GoldCurrencyHistory
    from portfolio.models import Asset

    as_of_dt = normalize_as_of(as_of)
    if as_of_dt is None:
        as_of_dt = timezone.now()

    jalali_str = to_jalali_str(as_of_dt)

    accounts = [account] if account else user.accounts.all()
    items = []
    total = Decimal("0")

    basis = normalize_basis(basis)

    usd_rate = Decimal("1")
    conversion_source = None
    if basis in ("usd_denominated", "usdt_denominated"):
        rate_found = False
        if basis == "usdt_denominated":
            usdt_hist = GoldCurrencyHistory.objects.filter(symbol="USDT_IRT", date__lte=jalali_str).order_by("-date").first()
            if usdt_hist and usdt_hist.close_price > 0:
                usd_rate = Decimal(str(usdt_hist.close_price))
                conversion_source = "USDT"
                rate_found = True
        
        if not rate_found:
            usd_hist = GoldCurrencyHistory.objects.filter(symbol="USD", date__lte=jalali_str).order_by("-date").first()
            if usd_hist and usd_hist.close_price > 0:
                usd_rate = Decimal(str(usd_hist.close_price))
                conversion_source = "USD"
                rate_found = True
                
        if not rate_found:
            return {
                "total": 0.0,
                "items": [],
                "as_of": as_of_dt.isoformat(),
                "basis": basis,
                "quality_status": "unavailable",
                "excluded": [{"reason": "missing_conversion_rate"}],
            }
    cpi = Decimal(str(cpi_for_date(as_of_dt))) if basis == "real_toman" else None

    excluded = []

    # Resolve close price for each asset
    for acc in accounts:
        acc_holdings = holdings_as_of(user, acc, as_of_dt)
        for key, qty in acc_holdings.items():
            if qty <= 0:
                continue
            asset = Asset.objects.filter(key=key).first()
            if not asset:
                continue

            # Find historical unit price
            price = Decimal("0")
            if asset.is_house:
                holding = Holding.objects.filter(account=acc, asset=asset).first()
                if holding is None:
                    excluded.append({"asset_key": key, "reason": "missing_house_terms"})
                    continue
                val = _house_value(qty, area_sqm=holding.area_sqm)
                price = val / qty if qty else Decimal("0")
            else:
                stale_sessions = 0
                if asset.tse_symbol:
                    from marketdata.models import RejectedRecord
                    rejections = RejectedRecord.objects.filter(
                        symbol=asset.tse_symbol,
                        endpoint__in=[
                            "stock_candle_adjusted", "stock_candle_unadjusted",
                            "stock_history_adjusted", "stock_history_unadjusted",
                            "series:1d_adj", "series:1d_unadj"
                        ]
                    ).values_list("date", flat=True)
                    candles = candle_close_qs(asset.tse_symbol, as_of=jalali_str).exclude(date_time__in=rejections)
                    candle = candles.order_by("-date_time").first()
                    if candle:
                        # Portfolio TSE quotes are Rial (same as warehouse).
                        price = _q(candle.close_price)
                        stale_sessions = _stale_sessions(
                            candles, "date_time", candle.date_time, jalali_str
                        )
                elif asset.brs_symbol:
                    from marketdata.models import RejectedRecord
                    rejections = RejectedRecord.objects.filter(
                        symbol=asset.brs_symbol,
                        endpoint__in=[
                            "gold_daily", "crypto_daily", "commodity_daily",
                            "market_index_daily", "etf_nav_daily", "option_contract_daily"
                        ]
                    ).values_list("date", flat=True)
                    history = GoldCurrencyHistory.objects.all()
                    hist = history.filter(
                        symbol=asset.brs_symbol, date__lte=jalali_str, close_price__gt=0
                    ).exclude(date__in=rejections).order_by("-date").first()
                    if hist:
                        price = Decimal(str(hist.close_price))
                        stale_sessions = _stale_sessions(
                            history, "date", hist.date, jalali_str
                        )

                if price <= 0:
                    excluded.append({"asset_key": key, "reason": "missing_price"})
                    continue
                if stale_sessions > MAX_FORWARD_FILL_SESSIONS:
                    excluded.append({
                        "asset_key": key,
                        "reason": "price_gap_exceeded",
                        "stale_sessions": stale_sessions,
                        "max_forward_fill_sessions": MAX_FORWARD_FILL_SESSIONS,
                    })
                    continue

            # Apply basis
            if not asset.is_house:
                val = qty * price
            if basis in ("usd_denominated", "usdt_denominated") and usd_rate > 0:
                val = val / usd_rate
                price = price / usd_rate
            elif basis == "real_toman":
                val = val / cpi * Decimal("100")
                price = price / cpi * Decimal("100")

            total += val
            items.append({
                "asset": asset.name,
                "key": asset.key,
                "class": asset.asset_class,
                "quantity": float(qty),
                "unit_price": float(price),
                "value": float(val),
            })

        cash = cash_as_of(user, acc, as_of_dt)
        if basis in ("usd_denominated", "usdt_denominated"):
            cash /= usd_rate
        elif basis == "real_toman":
            cash = cash / cpi * Decimal("100")
        total += cash

    from portfolio.models import Liability
    liabilities = Liability.objects.filter(account__in=accounts)
    total_liabilities = sum(l.amount_tomans for l in liabilities)
    
    scaled_liabilities = total_liabilities
    if basis in ("usd_denominated", "usdt_denominated") and usd_rate > 0:
        scaled_liabilities /= usd_rate
    elif basis == "real_toman":
        scaled_liabilities = scaled_liabilities / cpi * Decimal("100")
        
    total -= scaled_liabilities

    return {
        "total": float(total),
        "items": items,
        "as_of": as_of_dt.isoformat(),
        "basis": basis,
        "quality_status": "partial" if excluded else "complete",
        "excluded": excluded,
        "total_liabilities": float(scaled_liabilities),
        "conversion_source": conversion_source,
        "liabilities": [
            {
                "id": l.id,
                "label": l.label,
                "amount_tomans": float(l.amount_tomans),
                "asset_key": l.asset.key if l.asset else None,
            }
            for l in liabilities
        ],
    }
