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
from .archive import claim_archive_batch, ensure_archive_states, run_archive_state
from .catalog import sync_provider_catalog
from .fetchers import (
    fetch_codal_announcements,
    fetch_shareholders,
    fetch_symbol_data,
)
from .quota import QuotaExhausted

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


@shared_task(ignore_result=True)
def sync_symbol(symbol: str):
    """Sync lower-priority disclosures and shareholder data for one stock."""
    key = settings.TSETMC_API_KEY
    if not key:
        logger.warning("sync_symbol(%s): no TSETMC_API_KEY, skipping", symbol)
        return
    wrote = 0

    payload = fetch_codal_announcements(key, symbol=symbol)
    wrote += ingest.ingest_codal(payload)[0]
    _pause()
    payload = fetch_shareholders(key, symbol)
    wrote += ingest.ingest_shareholders(symbol, payload)[0]

    if wrote:
        _invalidate_returns()
    logger.info("sync_symbol(%s): %d new rows", symbol, wrote)


@shared_task(ignore_result=True)
def daily_sync():
    """Lower-priority daily sync; archive work runs independently every minute."""
    symbols = tracked_tse_symbols()
    tasks = [sync_symbol.si(s) for s in symbols]
    if tasks:
        chain(*tasks).apply_async()
    logger.info("daily_sync: enqueued chain for %d symbols", len(symbols))


@shared_task(ignore_result=True)
def weekly_metadata_sync():
    """Refresh StockSymbolMetadata fundamentals for every tracked symbol."""
    key = settings.TSETMC_API_KEY
    if not key:
        return
    for symbol in tracked_tse_symbols():
        ingest.ingest_symbol_metadata(fetch_symbol_data(key, symbol))
        _pause()
    logger.info("weekly_metadata_sync: done")


@shared_task(ignore_result=True)
def archive_tick(max_seconds: float = 50.0):
    """Claim quota-safe batches and continuously backfill PostgreSQL as long as quota remains."""
    ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
    completed = 0
    start_time = time.monotonic()

    while time.monotonic() - start_time < max_seconds:
        batch = claim_archive_batch()
        if not batch:
            break
        processed_in_batch = False
        for state_id in batch:
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


@shared_task(ignore_result=True)
def catalog_sync():
    result = sync_provider_catalog()
    logger.info("catalog_sync: %d seen, %d eligible", result["seen"], result["eligible"])
