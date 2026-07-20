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

from celery import chain, shared_task
from django.conf import settings

from . import ingest
from .fetchers import (
    fetch_candlesticks,
    fetch_codal_announcements,
    fetch_daily_history,
    fetch_gold_currency_pro_history_daily,
    fetch_market_index,
    fetch_shareholders,
    fetch_symbol_data,
)

logger = logging.getLogger(__name__)


def _pause():
    time.sleep(settings.MARKETDATA_FETCH_DELAY)


def tracked_tse_symbols() -> list[str]:
    """TSE symbols to sync: assets with a tse_symbol + configured extras."""
    from portfolio.models import Asset  # config read, see module docstring

    symbols = list(
        Asset.objects.filter(is_active=True)
        .exclude(tse_symbol="")
        .values_list("tse_symbol", flat=True)
    )
    for extra in settings.MARKETDATA_EXTRA_SYMBOLS:
        if extra not in symbols:
            symbols.append(extra)
    return symbols


def tracked_brs_symbols() -> list[str]:
    """BrsApi gold/currency/crypto symbols to sync (assets with brs_symbol)."""
    from portfolio.models import Asset

    return list(
        Asset.objects.filter(is_active=True)
        .exclude(brs_symbol="")
        .values_list("brs_symbol", flat=True)
    )


def _invalidate_returns():
    # Lazy: portfolio.services.returns imports pandas; keep worker startup light
    # and avoid an import cycle at module load.
    from portfolio.services.returns import invalidate_returns_cache

    invalidate_returns_cache()


@shared_task(ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def sync_symbol(symbol: str):
    """Sync one TSE symbol: daily history (both types), daily candles, codal, shareholders."""
    key = settings.TSETMC_API_KEY
    if not key:
        logger.warning("sync_symbol(%s): no TSETMC_API_KEY, skipping", symbol)
        return
    wrote = 0

    payload = fetch_daily_history(key, symbol, history_type=0)
    wrote += ingest.ingest_daily_history(symbol, payload, is_adjusted=False)[0]
    _pause()
    payload = fetch_daily_history(key, symbol, history_type=1)
    wrote += ingest.ingest_daily_history(symbol, payload, is_adjusted=True)[0]
    _pause()
    for candle_type in (6, 7):  # daily adjusted + unadjusted
        payload = fetch_candlesticks(key, symbol, candle_type=candle_type)
        wrote += ingest.ingest_candles(symbol, candle_type, payload)[0]
        _pause()
    payload = fetch_codal_announcements(key, symbol=symbol)
    wrote += ingest.ingest_codal(payload)[0]
    _pause()
    payload = fetch_shareholders(key, symbol)
    wrote += ingest.ingest_shareholders(symbol, payload)[0]

    if wrote:
        _invalidate_returns()
    logger.info("sync_symbol(%s): %d new rows", symbol, wrote)


@shared_task(ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def sync_gold_currency():
    """Sync daily gold/currency/crypto history for every asset with a brs_symbol."""
    key = settings.BRS_API_KEY
    if not key:
        logger.warning("sync_gold_currency: no BRS_API_KEY, skipping")
        return
    wrote = 0
    for symbol in tracked_brs_symbols():
        payload = fetch_gold_currency_pro_history_daily(key, symbol)
        wrote += ingest.ingest_gold_currency_history(payload)[0]
        _pause()
    if wrote:
        _invalidate_returns()
    logger.info("sync_gold_currency: %d new rows", wrote)


@shared_task(ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def sync_index():
    """Snapshot the TSE overall/equal-weight index."""
    key = settings.TSETMC_API_KEY
    if not key:
        return
    created, _ = ingest.ingest_market_index(fetch_market_index(key))
    logger.info("sync_index: %d new rows", created)


@shared_task(ignore_result=True)
def daily_sync():
    """Orchestrator: serial chain over all tracked symbols, then gold + index."""
    symbols = tracked_tse_symbols()
    tasks = [sync_symbol.si(s) for s in symbols]
    tasks.append(sync_gold_currency.si())
    tasks.append(sync_index.si())
    chain(*tasks).apply_async()
    logger.info("daily_sync: enqueued chain for %d symbols", len(symbols))


@shared_task(ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def weekly_metadata_sync():
    """Refresh StockSymbolMetadata fundamentals for every tracked symbol."""
    key = settings.TSETMC_API_KEY
    if not key:
        return
    for symbol in tracked_tse_symbols():
        ingest.ingest_symbol_metadata(fetch_symbol_data(key, symbol))
        _pause()
    logger.info("weekly_metadata_sync: done")
