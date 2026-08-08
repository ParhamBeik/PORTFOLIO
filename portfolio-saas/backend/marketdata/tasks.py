"""Celery tasks for the market-data warehouse sync.

Schedule (config/celery.py): `archive_tick` claims a batch and fans it out via
`run_archive_state`.
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
    claim_archive_maintenance,
    claim_recent_refresh,
    ensure_archive_states,
    run_archive_state as process_archive_state,
    promote_priority_tick_windows,
)
from .catalog import sync_provider_catalog
from .fetchers import (
    fetch_symbol_data,
)


@shared_task(ignore_result=True)
def operational_health_check():
    from redis import Redis

    from config.alerts import notify
    from portfolio.models import Price
    from .models import ApiRequestQuota, ArchiveFetchState, SymbolIntegrity, WorkflowRun

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
            queue: broker.llen(queue) for queue in ("live", "archive", "codal")
        }
        if max(backlog.values(), default=0) > settings.QUEUE_BACKLOG_THRESHOLD:
            alerts.append(("queue-backlog", backlog))
    except Exception:
        logger.exception("Could not inspect Celery queue backlog.")

    failed_integrity = SymbolIntegrity.objects.filter(passes_gate=False).count()
    if failed_integrity:
        alerts.append(("failed-integrity-assessments", {"count": failed_integrity}))

    since = timezone.now() - timedelta(hours=1)
    recent_runs = WorkflowRun.objects.filter(created_at__gte=since)
    run_count = recent_runs.count()
    failed_count = recent_runs.filter(outcome=WorkflowRun.Outcome.FAILED).count()
    failure_rate = failed_count / run_count if run_count else 0
    if run_count and failure_rate > settings.WORKFLOW_FAILURE_RATE_THRESHOLD:
        alerts.append(("elevated-workflow-failure-rate", {
            "failed": failed_count, "total": run_count, "rate": round(failure_rate, 4)
        }))

    stale_before = timezone.now() - timedelta(seconds=settings.ARCHIVE_PROGRESS_STALE_SECONDS)
    if (
        ArchiveFetchState.objects.filter(verified_complete=False).exists()
        and not WorkflowRun.objects.filter(
            workflow="archive_state",
            outcome__in=(WorkflowRun.Outcome.SUCCESS, WorkflowRun.Outcome.PARTIAL),
            created_at__gte=stale_before,
        ).exists()
    ):
        alerts.append(("stale-archive-progress", {"stale_seconds": settings.ARCHIVE_PROGRESS_STALE_SECONDS}))

    quota = ApiRequestQuota.objects.order_by("-day").first()
    if quota:
        bucket_total = quota.archive_used + quota.live_used + quota.other_used
        if bucket_total != quota.used:
            alerts.append(("quota-ledger-drift", {"used": quota.used, "bucket_total": bucket_total}))

    for event, details in alerts:
        notify(event, details, dedupe_seconds=900)
    return {"alerts": [event for event, _details in alerts]}


@shared_task(queue="archive")
def retry_archive_job_task(state_id):
    from .archive import run_archive_state
    run_archive_state(state_id)
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


@shared_task(ignore_result=True)
def run_archive_state(state_id):
    """Process one claimed ArchiveFetchState (HTTP + ingest)."""
    from .models import ArchiveFetchState, WorkflowRun
    from .workflows import WorkflowOutcome

    initial = ArchiveFetchState.objects.get(pk=state_id)
    outcome = WorkflowOutcome(
        "archive_state",
        endpoint=initial.endpoint,
        symbol=initial.symbol,
        source="brsapi.ir",
        destination_table="ArchiveFetchState",
    )
    try:
        state = process_archive_state(state_id)
    except QuotaExhausted as err:
        outcome.finish(
            WorkflowRun.Outcome.RETRY,
            error_code="quota_exhausted",
            metadata={"reason": str(err)},
        )
        return
    except Exception as err:
        outcome.finish(
            WorkflowRun.Outcome.FAILED,
            error_code=type(err).__name__,
            metadata={"reason": str(err)},
        )
        logger.exception(
            "Unhandled archive failure correlation_id=%s", outcome.correlation_id
        )
        raise
    terminal = (
        WorkflowRun.Outcome.RETRY
        if state.last_error
        else WorkflowRun.Outcome.SUCCESS
        if state.verified_complete
        else WorkflowRun.Outcome.PARTIAL
    )
    outcome.finish(
        terminal,
        rows_received=state.expected_rows,
        rows_accepted=state.stored_rows,
        rows_rejected=state.known_gap_rows,
        error_code="archive_fetch_retry" if state.last_error else "",
        metadata={
            "missing_rows": state.missing_rows,
            "target_window_days": state.target_window_days,
        },
    )
    if state.verified_complete:
        _invalidate_returns()


@shared_task(ignore_result=True)
def archive_tick():
    """Claim a quota-safe batch and fan out one task per archive state."""
    from .models import WorkflowRun
    from .workflows import WorkflowOutcome

    outcome = WorkflowOutcome("archive_tick", endpoint="archive_scheduler")
    redis_client = get_redis()
    lock_key = "lock:archive_tick"
    lock_token = uuid.uuid4().hex
    # Short lock around claim+dispatch only. Per-state leases use
    # select_for_update(skip_locked=True); quota serializes with its own lock.
    if redis_client and not redis_client.set(lock_key, lock_token, ex=30, nx=True):
        outcome.finish(WorkflowRun.Outcome.SKIPPED, metadata={"reason": "lock_held"})
        return
    try:
        from .models import ArchiveFetchState
        if not ArchiveFetchState.objects.exists():
            ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
        promote_priority_tick_windows()
        batch = claim_archive_batch(limit=12)
        if not batch:
            outcome.finish(WorkflowRun.Outcome.SKIPPED, metadata={"reason": "no_due_states"})
            return
        group(*(run_archive_state.si(state_id) for state_id in batch)).apply_async()
        outcome.finish(
            WorkflowRun.Outcome.SUCCESS,
            rows_received=len(batch),
            rows_accepted=len(batch),
        )
    except Exception as err:
        outcome.finish(
            WorkflowRun.Outcome.FAILED,
            error_code=type(err).__name__,
            metadata={"reason": str(err)},
        )
        logger.exception("Unhandled archive tick failure correlation_id=%s", outcome.correlation_id)
        raise
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
def recent_history_refresh():
    """Refresh authoritative recent stock series after the TSE close."""
    state_ids = claim_recent_refresh()
    if state_ids:
        group(*(run_archive_state.si(state_id) for state_id in state_ids)).apply_async()
    logger.info("recent_history_refresh: enqueued %d archive states", len(state_ids))


@shared_task(ignore_result=True)
def archive_maintenance():
    """Reverify completed low-volatility endpoints outside normal batches."""
    state_ids = claim_archive_maintenance(limit=2)
    if state_ids:
        group(*(run_archive_state.si(state_id) for state_id in state_ids)).apply_async()
    logger.debug("archive_maintenance enqueued %d states", len(state_ids))


@shared_task(ignore_result=True, rate_limit="1/s")
def process_codal_report(report_id):
    """Download and extract one report; infrastructure blocks are terminal."""
    from .codal_pipeline import extract_report
    from .models import CodalReport, WorkflowRun
    from .workflows import WorkflowOutcome

    report = CodalReport.objects.select_related("announcement").get(pk=report_id)
    outcome = WorkflowOutcome(
        "codal_extract",
        endpoint="codal_document",
        symbol=report.announcement.symbol,
        source="codal.ir",
        destination_table="CodalReport",
    )
    try:
        report, metadata = extract_report(report)
    except Exception as err:
        report.status = CodalReport.Status.FAILED
        report.error_code = type(err).__name__[:64]
        report.save(update_fields=["status", "error_code", "updated_at"])
        outcome.finish(
            WorkflowRun.Outcome.FAILED,
            error_code=type(err).__name__,
            metadata={"report_id": report_id, "reason": str(err)},
        )
        logger.exception("Unhandled Codal failure correlation_id=%s", outcome.correlation_id)
        raise

    terminal = {
        CodalReport.Status.PARSED: WorkflowRun.Outcome.SUCCESS,
        CodalReport.Status.NEEDS_REVIEW: WorkflowRun.Outcome.PARTIAL,
        CodalReport.Status.UNSUPPORTED: WorkflowRun.Outcome.PARTIAL,
        CodalReport.Status.BLOCKED_NETWORK: WorkflowRun.Outcome.BLOCKED_NETWORK,
        CodalReport.Status.BLOCKED_STORAGE: WorkflowRun.Outcome.BLOCKED_STORAGE,
    }.get(report.status, WorkflowRun.Outcome.FAILED)
    outcome.finish(
        terminal,
        rows_received=metadata.get("table_count", 0) + metadata.get("section_count", 0),
        rows_accepted=metadata.get("fact_count", 0),
        error_code=metadata.get("error_code", ""),
        metadata={"report_id": report_id, **metadata},
    )


@shared_task(ignore_result=True)
def enqueue_codal_reports():
    """Stage a bounded newest-first slice of the existing Codal corpus."""
    from datetime import timedelta

    from django.db.models import Q

    from .codal_classification import classify_announcement
    from .models import CodalAnnouncement, CodalReport, WorkflowRun
    from .workflows import WorkflowOutcome

    outcome = WorkflowOutcome("codal_enqueue", endpoint="codal_document")
    if not settings.CODAL_EXTRACTION_ENABLED:
        outcome.finish(WorkflowRun.Outcome.SKIPPED, metadata={"reason": "disabled"})
        return
    stale = timezone.now() - timedelta(hours=1)
    announcements = list(
        CodalAnnouncement.objects.filter(
            Q(report__isnull=True)
            | Q(report__status=CodalReport.Status.PENDING)
            | Q(report__status=CodalReport.Status.FETCHING, report__updated_at__lt=stale)
        )
        .order_by("-date_publish", "-time_publish")
        [: settings.CODAL_ENQUEUE_BATCH_SIZE]
    )
    report_ids = []
    for announcement in announcements:
        defaults = classify_announcement(announcement)
        report, _created = CodalReport.objects.get_or_create(
            announcement=announcement,
            defaults={**defaults, "parser_version": settings.CODAL_PARSER_VERSION},
        )
        report.status = CodalReport.Status.FETCHING
        report.error_code = ""
        report.save(update_fields=["status", "error_code", "updated_at"])
        report_ids.append(report.pk)
    for report_id in report_ids:
        process_codal_report.delay(report_id)
    outcome.finish(
        WorkflowRun.Outcome.SUCCESS,
        rows_received=len(announcements),
        rows_accepted=len(report_ids),
    )


@shared_task(ignore_result=True)
def catalog_sync(limit: int = None):
    result = sync_provider_catalog(limit=limit)
    ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
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
        row, created = GoldCurrencyHistory.objects.get_or_create(
            symbol=symbol,
            date=today_jalali,
            defaults={
                "name": asset.name,
                "unit": "تومان",
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,
                "source": GoldCurrencyHistory.Source.AGGREGATE,
            },
        )
        if not created and row.source == GoldCurrencyHistory.Source.AGGREGATE:
            row.name = asset.name
            row.unit = "تومان"
            row.open_price = open_p
            row.high_price = high_p
            row.low_price = low_p
            row.close_price = close_p
            row.save(update_fields=[
                "name", "unit", "open_price", "high_price", "low_price",
                "close_price",
            ])
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
    """Run data integrity checks for all symbols nightly.

    Also runs the warehouse unit audit in report-only mode. It never writes: the
    point is that a fresh unit regression shows up in the log the night it
    appears, instead of surfacing months later inside somebody's valuation.
    """
    from marketdata.integrity import update_all_symbols_integrity
    logger.info("Starting nightly data integrity checks...")
    results = update_all_symbols_integrity()
    logger.info("Nightly data integrity checks completed for %d symbols.", len(results))

    try:
        from marketdata.management.commands.audit_warehouse import Command as Audit
        from marketdata.models import RejectedRecord

        audit = Audit()
        findings = audit.check_candle_table_purity() + audit.check_unit_steps()
        # Rows already quarantined are known and reported; alerting on them every
        # night would bury the thing this check exists to catch -- a NEW one.
        known = set(
            RejectedRecord.objects.filter(reason="unit_error")
            .values_list("symbol", "date")
        )
        errors = [
            f for f in findings
            if f["verdict"] == "unit_error"
            and (f["symbol"], f["date"][:10]) not in known
        ]
        if errors:
            logger.error(
                "[UNIT_AUDIT] %d mis-scaled row(s) across %d symbol(s). "
                "Run `manage.py audit_warehouse` for the manifest. First: %s",
                len(errors), len({f["symbol"] for f in errors}), errors[0]["evidence"],
            )
        else:
            logger.info("[UNIT_AUDIT] No mis-scaled rows found.")
    except Exception:  # never let a report-only check break the integrity run
        logger.exception("[UNIT_AUDIT] Audit failed; integrity results still stand.")


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
