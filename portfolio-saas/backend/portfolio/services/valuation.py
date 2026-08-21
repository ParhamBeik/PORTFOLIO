"""Valuation engine: holdings x latest prices -> portfolio value.

SaaS port of the tracker engine build_snapshot pricing rules.
Two scale levers live here:
  1. latest prices are read once (DISTINCT ON) and cached, not per-asset;
  2. valuation is pure arithmetic over a preloaded holding set.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from ..models import HOUSE_AREA_SQM, Account, Asset, Holding, Price

logger = logging.getLogger(__name__)

_LATEST_PRICES_CACHE_KEY = "prices:latest"
_LATEST_PRICES_STATE_KEY = "prices:latest:market-state"
_ARCHIVE_DROP_FLOOR = Decimal("0.50")
_ARCHIVE_SPIKE_CEILING = Decimal("2.00")

_FRESH_SECONDS = 300
# The live loop only fetches TSE prices while the market session is OPEN, and
# gold/currency/crypto prices during OPEN or CLOSED_DAYTIME, pausing only
# OVERNIGHT (marketdata.market_state.live_job_keys). Outside those windows the
# last price IS the current price -- flagging it "stale" by a flat clock
# threshold marked every held stock stale every evening and every Thursday/
# Friday (the Iranian weekend), for data that was never wrong.
#
# ponytail: these are calendar-day grace bounds, not a real trading-session
# calendar (see MAX_FORWARD_FILL_SESSIONS below for that). Good enough to
# cover the Thu/Fri weekend without a session lookup on every valuation call;
# upgrade to `calendars.sessions_between` if a genuine multi-day outage needs
# finer detection than "older than the grace window".
_CLOSED_TSE_GRACE_SECONDS = 4 * 24 * 3600
_CLOSED_BRS_GRACE_SECONDS = 24 * 3600

# Shared by every price-resolution path in this module (guard_price_map,
# compute_dynamic_net_worth_series, value_as_of): forward-fill past this many
# sessions invents a price rather than reports one. Sessions are always counted
# with `marketdata.calendars.sessions_between` -- never in calendar days, and
# never off a single symbol's own rows. See that helper for why both fail.
MAX_FORWARD_FILL_SESSIONS = 5


def current_market_state() -> str:
    from marketdata.market_state import market_state

    return market_state()


def tse_market_is_closed(state: str | None = None) -> bool:
    from marketdata.market_state import OPEN

    return (state or current_market_state()) != OPEN


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

    The archive cross-check below is deliberately on the READ path as well as the
    write path (`guard_price_map`). Guarding only writes assumes every row in the
    table arrived through the fetch loop, and a single bad row that got in by any
    other route -- a direct edit, a row predating the guard, a future writer that
    forgets to call it -- is otherwise trusted forever by every valuation. The
    blast radius is total rather than partial: one `price=1` row values the whole
    holding at one Toman. The extra queries are amortised by the 120s cache.
    """
    current_state = current_market_state()
    cached = cache.get(_LATEST_PRICES_CACHE_KEY)
    if cached is not None and cache.get(_LATEST_PRICES_STATE_KEY) == current_state:
        return cached

    latest = (
        Price.objects.select_related("asset")
        .filter(asset__is_active=True, price__gt=0)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    latest = list(latest)
    prices = {row.asset.key: _q(row.price) for row in latest}
    fetched_at = {row.asset.key: row.fetched_at for row in latest}
    # Replace only what is already priced. Filling assets that have no Price row
    # at all is the write path's job; doing it here would turn "unpriced" into a
    # silent archive value and hide the gap the valuation layer reports.
    prices.update({
        key: value
        for key, value in _archive_replacements(
            prices,
            live_fetched_at=fetched_at,
            prefer_closed_tse=tse_market_is_closed(current_state),
        ).items()
        if key in prices
    })
    cache.set(_LATEST_PRICES_CACHE_KEY, prices, timeout=120)
    cache.set(_LATEST_PRICES_STATE_KEY, current_state, timeout=120)
    return prices


def guard_price_map(prices: dict, *, fill_missing=True, archive_replacements=None) -> dict:
    """Replace missing or broken live prices with previous prices or archive closes."""
    supplied_keys = set(prices)
    guarded = {key: _q(value) for key, value in prices.items()}

    # 1. The warehouse close is more authoritative than an older live row.
    replacements = archive_replacements if archive_replacements is not None else _archive_replacements(guarded)
    guarded.update(
        replacements if fill_missing else {
            key: value for key, value in replacements.items() if key in supplied_keys
        }
    )

    # 2. Forward-fill only when neither live nor archive data is available, and
    # only within MAX_FORWARD_FILL_SESSIONS days. The forward-filled value is
    # never persisted as a new Price row (see _persistable_prices), so without
    # this bound a delisted/broken asset's `fetched_at` never advances and this
    # keeps re-forward-filling the same ancient price forever -- the same
    # invariant value_as_of enforces via `calendars.sessions_between`.
    now = timezone.now()
    max_age = timedelta(days=MAX_FORWARD_FILL_SESSIONS)
    latest_db_rows = (
        Price.objects.select_related("asset")
        .filter(asset__is_active=True, price__gt=0)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    prev_prices = {row.asset.key: (_q(row.price), row.fetched_at) for row in latest_db_rows}

    # Large positive moves remain observable; archive corroboration above
    # rejects only catastrophic deviations.
    for key, live_price in list(guarded.items()):
        prev = prev_prices.get(key)
        if not prev or live_price > 0:
            continue
        prev_price, prev_fetched_at = prev
        if prev_price <= 0:
            continue
        if now - prev_fetched_at > max_age:
            logger.warning(
                "Key='%s' missing or zero live price and last price is older than "
                "%s days (fetched_at=%s). Not forward-filling.",
                key, MAX_FORWARD_FILL_SESSIONS, prev_fetched_at,
            )
            continue
        logger.info(
            "Key='%s' missing or zero live price. Forward-filling previous price %s.",
            key, prev_price
        )
        guarded[key] = prev_price

    return guarded


def _archive_replacements(
    prices: dict,
    *,
    live_fetched_at: dict | None = None,
    prefer_closed_tse: bool = False,
    verified_close_keys: set[str] | None = None,
) -> dict:
    """Return archive-backed replacements for missing, broken, or outrun live prices.

    `live_fetched_at`, when given, also catches a live price that passes the
    magnitude sanity band below but is simply behind: the live loop can miss
    an entire trading session outright (an outage), while the warehouse's own
    archive backfill -- a separate pipeline, unaffected by a live-loop outage
    -- keeps converging on real closes. A live price from session N-1 sitting
    next to an archive close already at session N is not a spike, it is stale
    data that happens to still be in a plausible range.
    """
    from marketdata.calendars import candle_close_qs
    from marketdata.models import GoldCurrencyHistory, RejectedRecord
    from portfolio.services.returns import to_jalali_str

    assets = Asset.objects.filter(is_active=True).exclude(is_house=True)
    assets_by_key = {asset.key: asset for asset in assets}
    current_jalali_date = to_jalali_str(timezone.now())
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
    archive_dates = {}
    # Adjusted closes live in MarketCandle.ADJUSTED. History.php?type=1 (once
    # tagged is_adjusted=True on DailyStockHistory, since removed -- see
    # marketdata.ingest.ingest_real_legal) was never adjusted prices at all,
    # it is the Real/Legal breakdown, so that fallback used to silently match
    # nothing.
    stock_rows = (
        candle_close_qs(stock_symbols)
        .order_by("symbol", "-date_time")
        .values("symbol", "date_time", "close_price")
    )
    for row in stock_rows:
        dt_str = row["date_time"].split()[0]
        if (row["symbol"], dt_str) not in rejections:
            key = stock_symbols[row["symbol"]]
            archive_prices.setdefault(key, _q(row["close_price"]))
            archive_dates.setdefault(key, dt_str)

    brs_rows = (
        GoldCurrencyHistory.objects.filter(symbol__in=brs_symbols, close_price__gt=0)
        .order_by("symbol", "-date")
        .values("symbol", "date", "close_price")
    )
    for row in brs_rows:
        if (row["symbol"], row["date"]) not in rejections:
            key = brs_symbols[row["symbol"]]
            archive_prices.setdefault(key, _q(row["close_price"]))
            archive_dates.setdefault(key, row["date"])

    replacements = {}
    for key, archive_price in archive_prices.items():
        live_price = _q(prices.get(key))
        asset = assets_by_key.get(key)
        if (
            prefer_closed_tse
            and asset
            and asset.tse_symbol
            and archive_dates[key] == current_jalali_date
        ):
            logger.info(
                "Key='%s' TSE is closed; using latest archive close %s.",
                key,
                archive_price,
            )
            replacements[key] = archive_price
            if verified_close_keys is not None:
                verified_close_keys.add(key)
        elif live_price <= 0:
            logger.warning("Key='%s' Live=0. Using archive price %s", key, archive_price)
            replacements[key] = archive_price
        elif live_price < archive_price * _ARCHIVE_DROP_FLOOR or live_price > archive_price * _ARCHIVE_SPIKE_CEILING:
            logger.warning(
                "Key='%s' Live=%s Archive=%s outside range [%s, %s]. Using archive price.",
                key, live_price, archive_price, archive_price * _ARCHIVE_DROP_FLOOR, archive_price * _ARCHIVE_SPIKE_CEILING
            )
            replacements[key] = archive_price
        elif live_fetched_at and key in live_fetched_at:
            live_session = to_jalali_str(live_fetched_at[key])
            if archive_dates[key] > live_session:
                logger.warning(
                    "Key='%s' live price is from session %s but the archive already "
                    "has session %s (%s) -- the live loop missed a session, using "
                    "the newer archive close.",
                    key, live_session, archive_dates[key], archive_price,
                )
                replacements[key] = archive_price
    return replacements


def _live_quality_status(asset, age_seconds: int, state: str) -> str:
    """"live" vs "stale" for a fresh Price row, aware of whether the asset's
    market/desk is even open right now.

    A price older than `_FRESH_SECONDS` is only "stale" if a fresher one
    should have arrived by now — i.e. the relevant market is open. If it's
    closed, the last price is still the correct current price; only flag it
    once it's older than the grace window (missed a whole session, a real
    problem) rather than every evening/weekend by design.
    """
    from marketdata.market_state import OPEN, OVERNIGHT

    if age_seconds <= _FRESH_SECONDS:
        return "live"
    if asset.tse_symbol and state != OPEN:
        return "live" if age_seconds <= _CLOSED_TSE_GRACE_SECONDS else "stale"
    if asset.brs_symbol and state == OVERNIGHT:
        return "live" if age_seconds <= _CLOSED_BRS_GRACE_SECONDS else "stale"
    return "stale"


def invalidate_prices_cache() -> None:
    """Called after a fresh fetch so reads immediately see new prices."""
    cache.delete(_LATEST_PRICES_CACHE_KEY)
    cache.delete(_LATEST_PRICES_STATE_KEY)


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



def _latest_archive_close(asset) -> dict | None:
    """Latest warehouse close used when live Price is missing or replaced."""
    from marketdata.calendars import candle_close_qs
    from marketdata.models import GoldCurrencyHistory

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
            GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol, close_price__gt=0)
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
    return None



def _quality_rollup(items, excluded, total_assets, priced_assets):
    if total_assets and priced_assets == 0:
        return "unavailable"
    statuses = {item["quality_status"] for item in items}
    if excluded or statuses & {"unavailable", "stale", "fallback"}:
        return "partial"
    if statuses <= {"live"}:
        return "complete"
    return "manual"


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
    from marketdata.market_state import market_state as _current_market_state

    market_state_now = _current_market_state()
    for holding in holdings:
        # None when the asset has no price yet — distinguishable from a real 0 (M2).
        unit_price = prices.get(holding.asset.key)
        row = latest_rows.get(holding.asset_id)
        archive_record = None
        if holding.asset.is_house or (
            holding.asset.is_manual and unit_price is not None and _q(unit_price) > 0
        ):
            value = asset_value(holding, unit_price)
            source = "manual_valuation"
            priced_at = holding.updated_at
            age_seconds = max(0, int((now - holding.updated_at).total_seconds()))
            # Manual marks never poll; stop calling them live after 90 days.
            quality_status = "manual" if age_seconds <= 90 * 86400 else "stale"
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
                quality_status = _live_quality_status(holding.asset, age_seconds, market_state_now)
            else:
                source = "archive"
                priced_at = None
                age_seconds = None
                quality_status = "fallback"
                archive_record = _latest_archive_close(holding.asset)
        if value is not None:
            total += value
            priced_assets += 1
        price_unit_status = "ok"
        if holding.asset.tse_symbol and not tse_unit_verified():
            price_unit_status = "unverified"
        item = {
            "asset": holding.asset.name,
            "name_fa": holding.asset.name_fa or "",
            "key": holding.asset.key,
            "class": holding.asset.asset_class,
            "is_manual": holding.asset.is_manual,
            "is_house": holding.asset.is_house,
            "quantity": holding.quantity,
            "unit_price": unit_price,
            "value": value,
            "source": source,
            "priced_at": priced_at.isoformat() if priced_at else None,
            "age_seconds": age_seconds,
            "quality_status": quality_status,
            "price_unit_status": price_unit_status,
        }
        if archive_record:
            item["archive_record"] = archive_record
        items.append(item)
    total_assets = len(holdings)
    quality_status = _quality_rollup(items, excluded, total_assets, priced_assets)
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
    items = [
        {"account_id": acc["id"], "account_name": acc["name"], **item}
        for acc in accounts
        for item in acc["items"]
    ]
    quality_status = _quality_rollup(items, excluded, total_assets, priced_assets)
    return {
        "total": total,
        "accounts": accounts,
        "items": items,
        "prices": prices,
        "priced_assets": priced_assets,
        "total_assets": total_assets,
        "quality_status": quality_status,
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
    from marketdata.calendars import (
        candle_close_qs,
        market_for_asset,
        sessions_between,
    )
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

    window_start_jalali = jdatetime.date.fromgregorian(
        date=(now - timedelta(days=days - 1)).date()
    ).strftime("%Y-%m-%d")

    # Track last known price for each asset to seamlessly fill non-trading days.
    #
    # Primed from each asset's last real close at or before the window opens, so
    # a window that starts on a Thursday values a stock at the Wednesday close
    # rather than at today's price. Assets with no warehouse history at all
    # (crypto, the Swiss bars -- the provider has no series for them) still fall
    # back to the live price: it is the only number that exists, and the whole
    # series is flagged `is_estimated`.
    last_known_prices = {}
    # Jalali date of the last REAL close used per asset. Once an asset stops
    # printing, last_known_prices must not be trusted past
    # MAX_FORWARD_FILL_SESSIONS *sessions* -- counted on the market's own
    # calendar, never in calendar days, or the Thu/Fri weekend and every public
    # holiday age a price faster than the market does. Same invariant, and now
    # the same helper, that value_as_of enforces. A live-price fallback gets no
    # entry here: there is no real close to measure staleness from.
    last_priced_date = {}
    for closes in (stock_closes, gold_closes):
        for date, by_key in closes.items():
            if date > window_start_jalali:
                continue
            for key, price in by_key.items():
                if date >= last_priced_date.get(key, ""):
                    last_priced_date[key] = date
                    last_known_prices[key] = price
    for key in assets:
        if key not in last_known_prices:
            last_known_prices[key] = _q(latest_prices.get(key, 0))

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
                    last_priced_date[key] = jalali_str
                else:
                    last_real_date = last_priced_date.get(key)
                    if last_real_date is not None:
                        stale = sessions_between(
                            last_real_date, jalali_str, market=market_for_asset(asset)
                        )
                        if stale > MAX_FORWARD_FILL_SESSIONS:
                            continue  # gap exceeded: don't invent a price
                    # No real close anywhere means an asset the provider has no
                    # history for; the live price is the only figure available.
                    p = last_known_prices[key]
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


def value_as_of(user, account=None, as_of=None, basis="nominal") -> dict:
    """Compute valuation of portfolio assets as of a specific date and basis."""
    from django.utils import timezone
    from portfolio.services.deflator import cpi_for_date, normalize_basis
    from portfolio.services.returns import normalize_as_of, to_jalali_str
    from portfolio.services.timeline import cash_as_of, holdings_as_of
    from marketdata.calendars import candle_close_qs, sessions_between
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
                # The area belongs to the mark in force on this date, not to the
                # holding's current value: `qty` above is already the historical
                # price-per-sqm, so pairing it with today's area would mix two
                # different points in time.
                from portfolio.services.timeline import house_area_as_of

                area = house_area_as_of(acc, as_of_dt).get(key)
                if area is None:
                    if holding is None:
                        excluded.append(
                            {"asset_key": key, "reason": "missing_house_terms"}
                        )
                        continue
                    area = holding.area_sqm
                val = _house_value(qty, area_sqm=area)
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
                        stale_sessions = sessions_between(
                            candle.date_time[:10], jalali_str, market="tse"
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
                        stale_sessions = sessions_between(
                            hist.date, jalali_str, market="gold_currency"
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
