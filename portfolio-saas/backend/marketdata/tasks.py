"""Celery tasks for the market-data warehouse sync.

Schedule (config/celery.py): `daily_sync` runs after TSE close and chains one
`sync_symbol` task per tracked symbol SERIALLY (a Celery chain, not a group) —
that is the rate-limit courtesy toward BrsApi: each link is small, individually
retryable, and survives a worker restart, unlike one long task with sleeps.
`weekly_metadata_sync` refreshes symbol fundamentals on Friday (market closed).

The tracked-symbol universe comes from user-land (`Asset.tse_symbol` /
`Asset.brs_symbol`) plus `MARKETDATA_EXTRA_SYMBOLS`. Note the dependency
direction: reading portfolio.models here would invert portfolio->marketdata, so
the Asset lookups are lazy imports kept inside functions and treated as
configuration reads, not domain coupling.
"""
import logging
import time
import uuid

from celery import group, shared_task
from django.conf import settings

from . import ingest
from .archive import claim_archive_batch, ensure_archive_states, release_archive_claims, run_archive_state
from .catalog import sync_provider_catalog
from .fetchers import (
    fetch_codal_announcements,
    fetch_shareholders,
    fetch_symbol_data,
)
from .fetchers.base import TransientMarketDataError
from .quota import QuotaExhausted
from portfolio.live.pubsub import get_redis

logger = logging.getLogger(__name__)


def _pause():
    time.sleep(settings.MARKETDATA_FETCH_DELAY)


def tracked_tse_symbols() -> list[str]:
    """TSE symbols to sync: active portfolio stocks + all catalog-eligible TSE stocks + configured extras."""
    from portfolio.models import Asset
    from .models import MarketInstrument

    symbols = list(
        Asset.objects.filter(is_active=True, asset_class=Asset.AssetClass.STOCK)
        .exclude(tse_symbol="")
        .values_list("tse_symbol", flat=True)
    )
    catalog_symbols = list(
        MarketInstrument.objects.filter(
            source=MarketInstrument.Source.TSETMC,
            eligible=True,
        ).values_list("symbol", flat=True)
    )
    for sym in catalog_symbols:
        if sym not in symbols:
            symbols.append(sym)
    for extra in settings.MARKETDATA_EXTRA_SYMBOLS:
        if extra and extra not in symbols:
            symbols.append(extra)
    return symbols


def tracked_brs_symbols() -> list[str]:
    """BrsApi gold/currency symbols to sync: active portfolio assets + all catalog-eligible BRS symbols."""
    from portfolio.models import Asset
    from .models import MarketInstrument

    symbols = list(
        Asset.objects.filter(
            is_active=True,
            asset_class__in=(Asset.AssetClass.GOLD, Asset.AssetClass.CASH),
        )
        .exclude(brs_symbol="")
        .values_list("brs_symbol", flat=True)
    )
    catalog_symbols = list(
        MarketInstrument.objects.filter(
            source=MarketInstrument.Source.BRS,
            eligible=True,
        ).values_list("symbol", flat=True)
    )
    for sym in catalog_symbols:
        if sym not in symbols:
            symbols.append(sym)
    return symbols


def _invalidate_returns():
    # Lazy: portfolio.services.returns imports pandas; keep worker startup light
    # and avoid an import cycle at module load.
    from portfolio.services.returns import invalidate_returns_cache

    invalidate_returns_cache()


@shared_task(
    ignore_result=True,
    autoretry_for=(TransientMarketDataError,),
    retry_backoff=True,
    max_retries=3,
)
def sync_symbol(symbol: str):
    """Sync lower-priority disclosures and shareholder data for one stock."""
    import re
    if re.search(r"\d$", symbol):
        logger.debug("sync_symbol(%s) skipped: derivative tickers do not file disclosures or shareholder records", symbol)
        return

    key = settings.TSETMC_API_KEY
    if not key:
        logger.warning("sync_symbol(%s): no TSETMC_API_KEY, skipping", symbol)
        return
    try:
        wrote = 0
        payload = fetch_codal_announcements(key, symbol=symbol)
        wrote += ingest.ingest_codal(payload)[0]
        _pause()
        payload = fetch_shareholders(key, symbol)
        wrote += ingest.ingest_shareholders(symbol, payload)[0]
    except QuotaExhausted:
        # ponytail: return cleanly instead of 1,155 traceback-printing failures
        # when the daily budget runs out mid-batch. Retrying same-day is pointless.
        logger.info("sync_symbol(%s): quota exhausted, skipping", symbol)
        return

    if wrote:
        _invalidate_returns()
    logger.info("sync_symbol(%s): %d new rows", symbol, wrote)


@shared_task(ignore_result=True)
def daily_sync():
    """Lower-priority daily sync; archive work runs independently every minute.

    Fired in a group, not a chain: a chain aborts every remaining link when one
    raises, so a single symbol timing out used to cancel the rest of that day's
    sync. The tasks are independent, and the archive worker's concurrency of 1
    already serializes them.
    """
    from .quota import remaining_requests, ARCHIVE
    import re
    if remaining_requests(ARCHIVE) <= 0:
        logger.info("daily_sync: quota exhausted, skipping dispatch")
        return
    # Exclude derivative symbols ending in digits to avoid wasted API requests
    symbols = [s for s in tracked_tse_symbols() if not re.search(r"\d$", s)]
    tasks = [sync_symbol.si(s) for s in symbols]
    if tasks:
        group(*tasks).apply_async()
    logger.info("daily_sync: enqueued %d independent symbol syncs", len(symbols))


@shared_task(ignore_result=True)
def weekly_metadata_sync():
    """Refresh StockSymbolMetadata fundamentals for every tracked symbol."""
    key = settings.TSETMC_API_KEY
    if not key:
        return
    import re
    for symbol in tracked_tse_symbols():
        # Exclude derivative symbols ending in digits
        if re.search(r"\d$", symbol):
            continue
        ingest.ingest_symbol_metadata(fetch_symbol_data(key, symbol))
        _pause()
    logger.info("weekly_metadata_sync: done")


# Retry only what a retry can fix. A permanent 4xx means the request itself is
# wrong, and QuotaExhausted is not transient within the day -- retrying either one
# just burns worker slots and, for quota, real requests.
@shared_task(
    ignore_result=True,
    autoretry_for=(TransientMarketDataError,),
    retry_backoff=True,
    max_retries=3,
)
def archive_tick(max_seconds: float = 50.0):
    """Claim quota-safe batches and continuously backfill PostgreSQL as long as quota remains."""
    redis_client = get_redis()
    lock_key = "lock:archive_tick"
    lock_token = uuid.uuid4().hex
    if redis_client and not redis_client.set(lock_key, lock_token, ex=600, nx=True):
        logger.info("archive_tick skipped: another archive tick is still running")
        return
    try:
        ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
        completed = 0
        start_time = time.monotonic()

        # The batch is deliberately small: a state can include a provider call,
        # so claiming 120 work items for a 50-second lease trapped the worker for
        # eight minutes and starved every queued beat tick.
        while time.monotonic() - start_time < max_seconds:
            batch = claim_archive_batch(limit=12)
            if not batch:
                break
            processed_in_batch = False
            for offset, state_id in enumerate(batch):
                if time.monotonic() - start_time >= max_seconds:
                    release_archive_claims(batch[offset:])
                    break
                try:
                    state = run_archive_state(state_id)
                    completed += int(state.verified_complete)
                    processed_in_batch = True
                except QuotaExhausted as err:
                    logger.info("archive_tick paused: %s", err)
                    if completed:
                        _invalidate_returns()
                    logger.info("archive_tick finished: %d states verified complete", completed)
                    return
            if not processed_in_batch:
                break

        if completed:
            _invalidate_returns()
        logger.info("archive_tick: %d states verified complete", completed)
    finally:
        if redis_client:
            redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_token,
            )


@shared_task(ignore_result=True)
def catalog_sync():
    result = sync_provider_catalog()
    logger.info("catalog_sync: %d seen, %d eligible", result["seen"], result["eligible"])


@shared_task(ignore_result=True)
def aggregate_daily_gold_currency_history(date_str: str = None):
    """Aggregate 24-hour (00:00 to 23:59) price ticks for Gold/Currency/Crypto at 23:59 daily."""
    from datetime import timedelta
    import jdatetime
    from django.db.models import Max, Min
    from django.utils import timezone
    from portfolio.models import Asset, Price
    from .models import GoldCurrencyHistory, MarketCandle

    today_jalali = date_str or jdatetime.date.today().strftime("%Y-%m-%d")
    now = timezone.now()
    since = now - timedelta(hours=24)

    assets = Asset.objects.filter(
        asset_class__in=(Asset.AssetClass.GOLD, Asset.AssetClass.CASH)
    ).exclude(brs_symbol="")

    created_count = 0
    for asset in assets:
        symbol = asset.brs_symbol
        ticks = Price.objects.filter(asset=asset, fetched_at__gte=since).order_by("fetched_at")
        if not ticks.exists():
            continue

        open_p = ticks.first().price
        close_p = ticks.last().price
        stats = ticks.aggregate(high=Max("price"), low=Min("price"))
        high_p = stats["high"] or close_p
        low_p = stats["low"] or close_p

        GoldCurrencyHistory.objects.update_or_create(
            symbol=symbol,
            date=today_jalali,
            defaults={
                "name": asset.name,
                "unit": asset.currency,
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,
            },
        )
        MarketCandle.objects.update_or_create(
            symbol=symbol,
            timeframe="1d_adj",
            date_time=today_jalali,
            defaults={
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,
                "volume": 0,
            },
        )
        created_count += 1

    if created_count:
        _invalidate_returns()
    logger.info("aggregate_daily_gold_currency_history: processed %d symbols for %s", created_count, today_jalali)


@shared_task(ignore_result=True)
def aggregate_daily_stock_history(date_str: str = None):
    """Aggregate trading session price ticks for stocks at market close (17:00 Tehran time)."""
    from datetime import timedelta
    import jdatetime
    from django.db.models import Max, Min, Sum
    from django.utils import timezone
    from portfolio.models import Asset, Price
    from .models import MarketCandle, StockTransactionTick

    today_jalali = date_str or jdatetime.date.today().strftime("%Y-%m-%d")
    now = timezone.now()
    since = now - timedelta(hours=12)

    assets = Asset.objects.filter(
        asset_class=Asset.AssetClass.STOCK
    ).exclude(tse_symbol="")

    created_count = 0
    for asset in assets:
        symbol = asset.tse_symbol
        ticks = StockTransactionTick.objects.filter(symbol=symbol, date=today_jalali).order_by("row")
        if ticks.exists():
            open_p = ticks.first().price
            close_p = ticks.last().price
            stats = ticks.aggregate(high=Max("price"), low=Min("price"), vol=Sum("volume"))
            high_p = stats["high"] or close_p
            low_p = stats["low"] or close_p
            vol = stats["vol"] or 0
        else:
            p_ticks = Price.objects.filter(asset=asset, fetched_at__gte=since).order_by("fetched_at")
            if not p_ticks.exists():
                continue
            open_p = p_ticks.first().price
            close_p = p_ticks.last().price
            stats = p_ticks.aggregate(high=Max("price"), low=Min("price"))
            high_p = stats["high"] or close_p
            low_p = stats["low"] or close_p
            vol = 0

        # No DailyStockHistory write: this aggregate is tick-derived and would
        # occupy the (symbol, date) slot the provider's authoritative History.php
        # row needs, and bulk_create(ignore_conflicts=True) would then never
        # replace it. MarketCandle "1d_adj" is the adjusted read path anyway.
        MarketCandle.objects.update_or_create(
            symbol=symbol,
            timeframe="1d_adj",
            date_time=today_jalali,
            defaults={
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,
                "volume": vol,
            },
        )
        created_count += 1

    if created_count:
        _invalidate_returns()
    logger.info("aggregate_daily_stock_history: processed %d stock symbols for %s", created_count, today_jalali)


@shared_task(ignore_result=True)
def nightly_data_integrity():
    """Run data integrity checks for all symbols nightly."""
    from marketdata.integrity import update_all_symbols_integrity
    logger.info("Starting nightly data integrity checks...")
    results = update_all_symbols_integrity()
    logger.info("Nightly data integrity checks completed for %d symbols.", len(results))
