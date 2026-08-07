"""Celery tasks for the market-data warehouse sync.

Schedule (config/celery.py): `daily_sync` runs after TSE close and dispatches one
independent `sync_symbol` task per tracked symbol as a Celery group.
`archive_tick` claims a batch and fans out the same way via `run_archive_state`.
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
from .archive import (
    claim_archive_batch,
    ensure_archive_states,
    run_archive_state as process_archive_state,
)
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
    sync. Tasks are independent; archive-worker concurrency fans them out.
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


@shared_task(
    ignore_result=True,
    autoretry_for=(TransientMarketDataError,),
    retry_backoff=True,
    max_retries=3,
    time_limit=90,
    soft_time_limit=85,
)
def run_archive_state(state_id):
    """Process one claimed ArchiveFetchState (HTTP + ingest)."""
    try:
        state = process_archive_state(state_id)
    except QuotaExhausted as err:
        logger.info("run_archive_state(%s): quota exhausted: %s", state_id, err)
        return
    if state.verified_complete:
        _invalidate_returns()


@shared_task(ignore_result=True, time_limit=55, soft_time_limit=50)
def archive_tick():
    """Claim a quota-safe batch and fan out one task per archive state."""
    redis_client = get_redis()
    lock_key = "lock:archive_tick"
    lock_token = uuid.uuid4().hex
    # Short lock around claim+dispatch only. Per-state leases use
    # select_for_update(skip_locked=True); quota serializes with its own lock.
    if redis_client and not redis_client.set(lock_key, lock_token, ex=30, nx=True):
        logger.info("archive_tick skipped: another archive tick is still running")
        return
    try:
        ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
        batch = claim_archive_batch(limit=12)
        if not batch:
            return
        group(*(run_archive_state.si(state_id) for state_id in batch)).apply_async()
        logger.info("archive_tick: enqueued %d archive states", len(batch))
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
def catalog_sync(limit: int = None):
    result = sync_provider_catalog(limit=limit)
    logger.info("catalog_sync: %d seen, %d eligible", result["seen"], result["eligible"])


@shared_task(ignore_result=True)
def aggregate_daily_gold_currency_history(date_str: str = None):
    """Aggregate 24-hour (00:00 to 23:59) price ticks for Gold/Currency/Crypto at 23:59 daily."""
    from datetime import timedelta
    import jdatetime
    from django.db.models import Max, Min
    from django.utils import timezone
    from portfolio.models import Asset, Price
    from .models import GoldCurrencyHistory

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

        # Price is Toman-denominated for BRS gold/currency assets
        # (extract_standard_prices routes every source through to_toman());
        # match the unit label the rest of this table uses for these rows.
        GoldCurrencyHistory.objects.update_or_create(
            symbol=symbol,
            date=today_jalali,
            defaults={
                "name": asset.name,
                "unit": "تومان",
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,
            },
        )
        # No MarketCandle row: that table is Rial-denominated TSE data, and these
        # are Toman BRS quotes. Writing them here made candle_close_qs(symbol)
        # match for gold/FX, which sent PerformanceView down the stock branch and
        # exposed every tse_close_to_toman() reader to a 10x error. The row was
        # redundant anyway -- GoldCurrencyHistory above is the series of record.
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
    from .currency import toman_to_tse_close
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
            # `Price` is Toman (extractor.py routes TSE quotes through
            # tse_close_to_toman); MarketCandle is Rial. Convert on the way in,
            # or candle_close_qs serves this row and the read side divides by 10
            # a second time -- a 10x understatement of the current session.
            open_p = toman_to_tse_close(p_ticks.first().price)
            close_p = toman_to_tse_close(p_ticks.last().price)
            stats = p_ticks.aggregate(high=Max("price"), low=Min("price"))
            high_p = toman_to_tse_close(stats["high"]) or close_p
            low_p = toman_to_tse_close(stats["low"]) or close_p
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
def nightly_series_validation(dry_run=False, symbols=None, gold_symbols=None):
    """Derive corporate actions, then persist unexplained cross-day spikes."""
    from decimal import Decimal
    import math
    from django.conf import settings

    from .models import (
        CodalAnnouncement,
        CorporateAction,
        MarketCandle,
        RejectedRecord,
        GoldCurrencyHistory,
    )
    from .validation import detect_factor_ratio_actions, screen_series

    # Helper functions for gold/currency validation
    def get_gold_currency_asset_class(symbol):
        if symbol in {"BTC", "USDT_IRT"}:
            return "crypto"
        if symbol in {"XAUUSD"}:
            return "commodity"
        if symbol.startswith("IR_GOLD_") or symbol.startswith("IR_COIN_"):
            return "gold"
        return "currency"

    # Named configurable settings for validation thresholds (provisional)
    GOLD_CURRENCY_THRESHOLDS = {
        "crypto": getattr(settings, "SERIES_VALIDATION_THRESHOLD_CRYPTO", math.log(2.0)),
        "commodity": getattr(settings, "SERIES_VALIDATION_THRESHOLD_COMMODITY", math.log(1.2)),
        "currency": getattr(settings, "SERIES_VALIDATION_THRESHOLD_CURRENCY", math.log(1.15)),
        "gold": getattr(settings, "SERIES_VALIDATION_THRESHOLD_GOLD", math.log(1.2)),
    }

    ASSET_CLASS_ENDPOINTS = {
        "crypto": "crypto_daily",
        "commodity": "commodity_daily",
        "gold": "gold_daily",
        "currency": "gold_daily",
    }

    # Helper to check Codal confirmation (F2 mapping fix)
    def check_codal_confirmation(symbol, action_date):
        import jdatetime
        from datetime import timedelta
        
        try:
            jy, jm, jd = map(int, action_date.split("-"))
            action_gdt = jdatetime.date(jy, jm, jd).togregorian()
        except Exception:
            return False, "unknown", None

        announcements = CodalAnnouncement.objects.filter(symbol=symbol)
        for ann in announcements:
            try:
                ay, am, ad = map(int, ann.date_publish.split("-"))
                ann_gdt = jdatetime.date(ay, am, ad).togregorian()
            except Exception:
                continue
            
            # Match window: [action_date - 14 days, action_date + 3 days]
            if action_gdt - timedelta(days=14) <= ann_gdt <= action_gdt + timedelta(days=3):
                title = ann.title
                category = ann.category
                
                is_capital_increase = (
                    category == CodalAnnouncement.Category.CAPITAL_INCREASE or
                    "افزایش سرمایه" in title
                )
                is_assembly_decision = (
                    category == CodalAnnouncement.Category.ASSEMBLY_DECISION or
                    "تصمیمات مجمع" in title or "تقسیم سود" in title or "مجمع عمومی" in title
                )
                
                if is_capital_increase:
                    return True, "capital_increase", ann
                if is_assembly_decision:
                    return True, "assembly_decision", ann
                    
        return False, "unknown", None

    # 1. TSE Equities validation (F2 & F4 & F5)
    if symbols is None:
        symbols = list(
            MarketCandle.objects.filter(timeframe=MarketCandle.ADJUSTED)
            .order_by()
            .values_list("symbol", flat=True)
            .distinct()
        )
    else:
        symbols = list(symbols)

    rejected_count = 0
    actions_created_count = 0
    
    # Reports list for dry-run
    corporate_action_candidates = []
    proposed_rejections = []

    for symbol in symbols:
        unadjusted = {}
        for dt, close in MarketCandle.objects.filter(symbol=symbol, timeframe=MarketCandle.UNADJUSTED).values_list("date_time", "close_price"):
            unadjusted[dt.split()[0]] = close

        adjusted = {}
        for dt, close in MarketCandle.objects.filter(symbol=symbol, timeframe=MarketCandle.ADJUSTED).values_list("date_time", "close_price"):
            adjusted[dt.split()[0]] = close

        paired = [
            (date, unadjusted[date], adjusted[date])
            for date in sorted(unadjusted.keys() & adjusted.keys())
        ]

        for action in detect_factor_ratio_actions(paired):
            confirmed, action_kind, ann = check_codal_confirmation(symbol, action["date"])
            
            status = "confirmed" if confirmed else "unconfirmed"
            ann_title = f" (Codal: {ann.title} on {ann.date_publish})" if ann else ""
            corporate_action_candidates.append({
                "symbol": symbol,
                "date": action["date"],
                "factor": action["factor"],
                "status": status,
                "reason": f"Confirmed {action_kind}{ann_title}" if confirmed else "Factor step > 1% without matching Codal announcement"
            })

            # ONLY write to database if confirmed!
            if confirmed and not dry_run:
                kind_map = {
                    "capital_increase": CorporateAction.Kind.CAPITAL_INCREASE,
                    "assembly_decision": CorporateAction.Kind.DIVIDEND,
                }
                _, created = CorporateAction.objects.update_or_create(
                    symbol=symbol,
                    date=action["date"],
                    defaults={
                        "factor": Decimal(str(action["factor"])),
                        "kind": kind_map.get(action_kind, CorporateAction.Kind.UNKNOWN),
                        "source": CorporateAction.Source.CODAL,
                    },
                )
                actions_created_count += int(created)

        action_dates = set(CorporateAction.objects.filter(symbol=symbol).values_list("date", flat=True))
        
        for timeframe in (MarketCandle.UNADJUSTED, MarketCandle.ADJUSTED):
            raw_rows = MarketCandle.objects.filter(
                symbol=symbol,
                timeframe=timeframe,
                close_price__gt=0,
            ).order_by("date_time").values_list("date_time", "close_price")
            
            normalized_rows = []
            for dt, close in raw_rows:
                normalized_rows.append((dt.split()[0], close))

            stock_threshold = getattr(settings, "SERIES_VALIDATION_THRESHOLD_STOCK", math.log(1.5))
            for rejection in screen_series(
                symbol,
                timeframe,
                normalized_rows,
                corporate_action_dates=action_dates,
                max_log_return=stock_threshold,
            ):
                proposed_rejections.append({
                    "symbol": symbol,
                    "date": rejection.record["date"],
                    "endpoint": f"series:{timeframe}",
                    "reason": rejection.reason,
                    "payload": rejection.record,
                })
                if not dry_run:
                    row, created = RejectedRecord.objects.get_or_create(
                        endpoint=f"series:{timeframe}",
                        symbol=symbol,
                        date=rejection.record["date"],
                        reason=rejection.reason,
                        defaults={"payload": rejection.record, "occurrences": 1},
                    )
                    if created:
                        rejected_count += 1
                else:
                    rejected_count += 1

    # 2. Gold / Currency screening (F3)
    if gold_symbols is None:
        gold_symbols = list(
            GoldCurrencyHistory.objects.order_by()
            .values_list("symbol", flat=True)
            .distinct()
        )
    else:
        gold_symbols = list(gold_symbols)

    for symbol in gold_symbols:
        raw_rows = list(
            GoldCurrencyHistory.objects.filter(
                symbol=symbol,
                close_price__gt=0,
            ).order_by("date").values_list("date", "close_price")
        )
        
        normalized_rows = []
        for dt, close in raw_rows:
            normalized_rows.append((dt.split()[0], close))

        asset_class = get_gold_currency_asset_class(symbol)
        endpoint = ASSET_CLASS_ENDPOINTS[asset_class]
        threshold = GOLD_CURRENCY_THRESHOLDS[asset_class]

        for rejection in screen_series(
            symbol,
            endpoint,
            normalized_rows,
            max_log_return=threshold,
        ):
            proposed_rejections.append({
                "symbol": symbol,
                "date": rejection.record["date"],
                "endpoint": endpoint,
                "reason": rejection.reason,
                "payload": rejection.record,
            })
            if not dry_run:
                row, created = RejectedRecord.objects.get_or_create(
                    endpoint=endpoint,
                    symbol=symbol,
                    date=rejection.record["date"],
                    reason=rejection.reason,
                    defaults={"payload": rejection.record, "occurrences": 1},
                )
                if created:
                    rejected_count += 1
            else:
                rejected_count += 1

    logger.info(
        "nightly_series_validation: %d actions created, %d spikes rejected",
        actions_created_count,
        rejected_count,
    )
    
    return {
        "tse_symbols_examined": len(symbols),
        "corporate_action_candidates": corporate_action_candidates,
        "actions_created": actions_created_count,
        "gold_currency_symbols_examined": len(gold_symbols),
        "proposed_rejections": proposed_rejections,
        "spikes_rejected": rejected_count,
    }
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
