"""Celery tasks for the market-data warehouse sync.

Schedule (config/celery.py): `daily_sync` runs after TSE close and dispatches one
independent `sync_symbol` task per tracked symbol as a Celery group. The archive
worker queue has concurrency one, so provider calls remain serialized without a
chain that would cancel all remaining symbols after one failure.
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
from datetime import timedelta

from celery import group, shared_task
from django.conf import settings
from django.utils import timezone

from . import ingest
from .archive import claim_archive_batch, ensure_archive_states, release_archive_claims, run_archive_state
from .catalog import sync_provider_catalog
from .fetchers import (
    fetch_codal_announcements,
    fetch_shareholders,
    fetch_symbol_data,
)


@shared_task(ignore_result=True)
def operational_health_check():
    from redis import Redis

    from config.alerts import notify
    from portfolio.models import Price
    from .models import SymbolIntegrity, SystemLogEvent

    alerts = []
    latest = Price.objects.order_by("-fetched_at").values_list(
        "fetched_at", flat=True
    ).first()
    price_age = None if latest is None else (timezone.now() - latest).total_seconds()
    if price_age is None or price_age > settings.PRICE_STALE_THRESHOLD_SECONDS:
        alerts.append(("stale-prices", {"age_seconds": price_age}))

    try:
        broker = Redis.from_url(settings.CELERY_BROKER_URL)
        backlog = {
            queue: broker.llen(queue) for queue in ("live", "archive")
        }
        if max(backlog.values(), default=0) > settings.QUEUE_BACKLOG_THRESHOLD:
            alerts.append(("queue-backlog", backlog))
    except Exception:
        logger.exception("Could not inspect Celery queue backlog.")

    failed_integrity = SymbolIntegrity.objects.filter(passes_gate=False).count()
    if failed_integrity:
        alerts.append(("failed-integrity-assessments", {"count": failed_integrity}))

    recent_errors = SystemLogEvent.objects.filter(
        level__iexact="ERROR",
        timestamp__gte=timezone.now() - timedelta(hours=1),
    ).count()
    if recent_errors > settings.APPLICATION_ERROR_THRESHOLD:
        alerts.append(("elevated-application-errors", {"count": recent_errors}))

    for event, details in alerts:
        notify(event, details, dedupe_seconds=900)
    return {"alerts": [event for event, _details in alerts]}


@shared_task(queue="archive")
def retry_archive_job_task(state_id):
    from .archive import run_archive_state
    run_archive_state(state_id)
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
        # AGGREGATE, never ADJUSTED: the archive writes ADJUSTED with
        # bulk_create(ignore_conflicts=True) and so can never replace a row
        # parked here. See MarketCandle's docstring.
        MarketCandle.objects.update_or_create(
            symbol=symbol,
            timeframe=MarketCandle.AGGREGATE,
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
        # replace it. MarketCandle.ADJUSTED has exactly the same hazard, so this
        # lands in AGGREGATE and readers prefer ADJUSTED over it.
        MarketCandle.objects.update_or_create(
            symbol=symbol,
            timeframe=MarketCandle.AGGREGATE,
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


@shared_task(ignore_result=True)
def nightly_series_validation():
    """Derive corporate actions, then persist unexplained cross-day spikes."""
    from decimal import Decimal

    from django.db.models import F

    from .models import (
        CodalAnnouncement,
        CorporateAction,
        MarketCandle,
        RejectedRecord,
    )
    from .validation import detect_factor_ratio_actions, screen_series

    symbols = MarketCandle.objects.filter(
        timeframe=MarketCandle.ADJUSTED
    ).values_list("symbol", flat=True).distinct()
    rejected = 0
    actions_created = 0
    for symbol in symbols.iterator():
        unadjusted = dict(
            MarketCandle.objects.filter(
                symbol=symbol, timeframe=MarketCandle.UNADJUSTED
            ).values_list("date_time", "close_price")
        )
        adjusted = dict(
            MarketCandle.objects.filter(
                symbol=symbol, timeframe=MarketCandle.ADJUSTED
            ).values_list("date_time", "close_price")
        )
        paired = [
            (date, unadjusted[date], adjusted[date])
            for date in sorted(unadjusted.keys() & adjusted.keys())
        ]
        for action in detect_factor_ratio_actions(paired):
            codal = CodalAnnouncement.objects.filter(
                symbol=symbol,
                date_publish=action["date"],
                category=CodalAnnouncement.Category.CAPITAL_INCREASE,
            ).exists()
            _, created = CorporateAction.objects.update_or_create(
                symbol=symbol,
                date=action["date"],
                defaults={
                    "factor": Decimal(str(action["factor"])),
                    "kind": (
                        CorporateAction.Kind.CAPITAL_INCREASE
                        if codal
                        else CorporateAction.Kind.UNKNOWN
                    ),
                    "source": (
                        CorporateAction.Source.CODAL
                        if codal
                        else CorporateAction.Source.FACTOR_RATIO
                    ),
                },
            )
            actions_created += int(created)

        action_dates = CorporateAction.objects.filter(symbol=symbol).values_list(
            "date", flat=True
        )
        for timeframe in (MarketCandle.UNADJUSTED, MarketCandle.ADJUSTED):
            rows = MarketCandle.objects.filter(
                symbol=symbol,
                timeframe=timeframe,
                close_price__gt=0,
            ).order_by("date_time").values_list("date_time", "close_price")
            for rejection in screen_series(
                symbol,
                timeframe,
                rows,
                corporate_action_dates=action_dates,
            ):
                row, created = RejectedRecord.objects.get_or_create(
                    endpoint=f"series:{timeframe}",
                    symbol=symbol,
                    date=rejection.record["date"],
                    reason=rejection.reason,
                    defaults={"payload": rejection.record},
                )
                if not created:
                    RejectedRecord.objects.filter(pk=row.pk).update(
                        occurrences=F("occurrences") + 1,
                        payload=rejection.record,
                    )
                rejected += 1
    logger.info(
        "nightly_series_validation: %d actions created, %d spikes rejected",
        actions_created,
        rejected,
    )


@shared_task(ignore_result=True)
def nightly_asset_metrics(window_days=365):
    import numpy as np

    from portfolio.services.diagnostics import _load_index_returns
    from portfolio.services.returns import daily_returns_matrix
    from . import jalali
    from .models import AssetMetricSnapshot, MarketInstrument

    instruments = {
        row.symbol: row
        for row in MarketInstrument.objects.filter(eligible=True)
    }
    returns, _ = daily_returns_matrix(
        history_days=window_days,
        universe=list(instruments),
    )
    benchmark = _load_index_returns(returns.index) if not returns.empty else None
    reference = returns["USD"] if "USD" in returns else None
    as_of = jalali.today()
    written = 0
    for symbol in returns.columns:
        series = returns[symbol].dropna()
        if len(series) < 2:
            continue
        annualized_return = float(series.mean() * 252)
        volatility = float(series.std(ddof=1) * np.sqrt(252))
        risk_free = settings.RATE_FOR(int(as_of[:4]))
        downside = (series - risk_free / 252).clip(upper=0)
        downside_deviation = float(np.sqrt(np.mean(downside ** 2)))
        wealth = (1 + series).cumprod()
        max_drawdown = float((wealth / wealth.cummax() - 1).min())
        beta = None
        if benchmark is not None:
            aligned = series.to_frame("asset").join(
                benchmark.rename("benchmark"), how="inner"
            ).dropna()
            if len(aligned) > 1 and aligned["benchmark"].var(ddof=1) > 0:
                beta = float(
                    aligned.cov().loc["asset", "benchmark"]
                    / aligned["benchmark"].var(ddof=1)
                )
        correlation = (
            float(series.corr(reference))
            if reference is not None and symbol != "USD"
            else None
        )
        instrument = instruments.get(symbol)
        AssetMetricSnapshot.objects.update_or_create(
            symbol=symbol,
            as_of=as_of,
            window_days=window_days,
            defaults={
                "asset_class": instrument.category if instrument else "",
                "total_return": float((1 + series).prod() - 1),
                "annualized_volatility": volatility,
                "sharpe": (
                    (annualized_return - risk_free) / volatility
                    if volatility
                    else 0.0
                ),
                "sortino": (
                    (annualized_return - risk_free) / downside_deviation
                    if downside_deviation
                    else 0.0
                ),
                "max_drawdown": max_drawdown,
                "beta": beta,
                "correlation": correlation,
            },
        )
        written += 1
    logger.info("nightly_asset_metrics: wrote %d snapshots", written)
