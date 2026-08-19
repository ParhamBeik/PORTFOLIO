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
import os
import re
import time
import uuid
from collections import Counter
from datetime import timedelta

from celery import group, shared_task
from django.conf import settings
from django.utils import timezone

from . import ingest, jalali
from .archive import (
    claim_archive_batch,
    claim_archive_maintenance,
    claim_recent_refresh,
    ensure_archive_states,
    run_archive_state as process_archive_state,
    grow_tick_windows,
)
from .catalog import sync_provider_catalog
from .fetchers import (
    fetch_symbol_data,
)


@shared_task
def capture_derivative_snapshots():
    """Collect live contracts now; analytics waits for real accumulated history.

    Each kind is its own provider endpoint and its own failure domain: one
    endpoint timing out must not mask or abort the others' results. Before
    this split, `ime_futures`/`ime_options` (evaluated after `tse_option`)
    repeatedly timed out and re-raised, so `tse_option`'s already-ingested
    rows were the only ones ever reflected in the ledger and the ime kinds
    never got an independent success/failure signal of their own.
    """
    from .fetchers import fetch_derivatives

    results = {}
    for kind, endpoint_key in (
        ("tse_option", "option_contracts"),
        ("ime_future", "ime_futures"),
        ("ime_option", "ime_options"),
    ):
        outcome = _ledgered(
            f"capture_derivative_snapshots:{kind}",
            endpoint=endpoint_key,
            destination_table="DerivativeSnapshot",
        )
        try:
            payload = fetch_derivatives(settings.TSETMC_API_KEY, endpoint_key)
            created, rejected = ingest.ingest_derivative_snapshots(kind, payload) or (0, 0)
            results[kind] = (created, rejected)
            _finish_ok(outcome, rows_created=created, rows_rejected=rejected)
        except Exception as err:
            results[kind] = err
            _finish_fail(outcome, err)
    return results


@shared_task
def capture_market_snapshots():
    """Poll the live endpoints that are registered but were never called until
    recently: crypto, commodity (batch, one call each) and ETF NAV (per-symbol,
    see _capture_etf_nav_batch below -- Tsetmc/Nav.php has no batch form).

    Same failure-domain-per-endpoint shape as `capture_derivative_snapshots`.
    `Market/*` paths (crypto, commodity) use `BRS_API_KEY`; `Tsetmc/*` paths
    (etf_nav) use `TSETMC_API_KEY` -- same split the existing gold/currency
    vs. index/option fetchers already follow.
    """
    from .fetchers import fetch_derivatives

    results = {}
    for asset_class, endpoint_key, api_key in (
        ("crypto", "crypto", settings.BRS_API_KEY),
        ("commodity", "commodity", settings.BRS_API_KEY),
    ):
        outcome = _ledgered(
            f"capture_market_snapshots:{asset_class}",
            endpoint=endpoint_key,
            destination_table="MarketSnapshot",
        )
        try:
            payload = fetch_derivatives(api_key, endpoint_key)
            created, skipped = ingest.ingest_market_snapshots(asset_class, payload) or (0, 0)
            results[asset_class] = (created, skipped)
            _finish_ok(outcome, rows_created=created, rows_rejected=skipped)
        except Exception as err:
            results[asset_class] = err
            _finish_fail(outcome, err)

    results["etf_nav"] = _capture_etf_nav_batch()
    return results


_ETF_NAV_CURSOR_CACHE_KEY = "marketdata:etf_nav:cursor"


def _capture_etf_nav_batch():
    """Fetch NAV for a bounded, rotating slice of known ETFs.

    Nav.php is one request per ETF (see fetch_etf_nav), and there are ~417 of
    them (marketdata.catalog's IRT-ISIN discovery) -- fetching all of them
    every 5-minute tick (this task's schedule, config/celery.py) would burn
    roughly 12x that per hour against a 10,000/day account-wide quota shared
    with every other endpoint this app calls. ETF NAV only ever feeds a daily
    OHLC bar (aggregate_market_daily_bars), not the 2-minute held-asset price
    loop, so it does not need every symbol fresh every tick -- a bounded slice
    per tick, rotating through the full set via a cache-held cursor, is
    sufficient and keeps steady-state cost to
    settings.ETF_NAV_BATCH_SIZE requests per 5 minutes.
    """
    from django.core.cache import cache

    from .fetchers import fetch_etf_nav
    from .fetchers.base import PermanentMarketDataError, TransientMarketDataError
    from .models import MarketInstrument

    outcome = _ledgered(
        "capture_market_snapshots:etf_nav",
        endpoint="etf_nav",
        destination_table="MarketSnapshot",
    )
    symbols = list(
        MarketInstrument.objects.filter(
            category=MarketInstrument.Category.ETF, eligible=True,
        ).order_by("symbol").values_list("symbol", flat=True)
    )
    if not symbols:
        # Expected before the first catalog sync of the day (sync_provider_catalog
        # is what populates category=ETF rows); not an error.
        _finish_ok(outcome, rows_created=0, metadata={"reason": "no_etf_instruments_yet"})
        logger.info("capture_market_snapshots(etf_nav): no ETF instruments in catalog yet")
        return (0, 0)

    batch_size = max(1, getattr(settings, "ETF_NAV_BATCH_SIZE", 5))
    cursor = cache.get(_ETF_NAV_CURSOR_CACHE_KEY, 0) % len(symbols)
    batch = [symbols[(cursor + i) % len(symbols)] for i in range(min(batch_size, len(symbols)))]
    cache.set(_ETF_NAV_CURSOR_CACHE_KEY, (cursor + len(batch)) % len(symbols), None)

    created = failed = 0
    permanent_failures = []
    for symbol in batch:
        try:
            payload = fetch_etf_nav(settings.TSETMC_API_KEY, symbol)
            if ingest.ingest_etf_nav_snapshot(symbol, payload):
                created += 1
            else:
                failed += 1
        except PermanentMarketDataError as exc:
            # A specific symbol rejected (e.g. delisted, per endpoints.py's
            # "non-ETF symbol returns 502" note) must not abort the rest of
            # the batch -- log it and keep going, same as the other per-item
            # loops in this module (ingest_market_snapshots et al).
            failed += 1
            permanent_failures.append(symbol)
            logger.warning("capture_market_snapshots(etf_nav): %s permanently failed: %s", symbol, exc)
        except TransientMarketDataError as exc:
            failed += 1
            logger.warning("capture_market_snapshots(etf_nav): %s transient failure: %s", symbol, exc)

    logger.info(
        "capture_market_snapshots(etf_nav): created %d, failed %d of %d fetched "
        "(cursor now %d/%d, permanent_failures=%s)",
        created, failed, len(batch), cache.get(_ETF_NAV_CURSOR_CACHE_KEY, 0), len(symbols),
        permanent_failures or None,
    )
    _finish_ok(outcome, rows_created=created, rows_rejected=failed)
    return (created, failed)


@shared_task(ignore_result=True)
def aggregate_market_daily_bars_task(jalali_date=None):
    """Distill today's live snapshots into `MarketDailyBar` OHLC rows -- one
    call per asset class covered by the unified live->historical mechanism.
    `MarketDailyBar`'s own row-existence is the completeness signal for these
    classes, so this must run once per day, after the day's snapshots exist.
    """
    target_date = jalali_date or jalali.today()
    results = {}
    for asset_class in (
        "crypto", "commodity", "etf_nav", "index",
        "tse_option", "ime_future", "ime_option",
    ):
        outcome = _ledgered(
            f"aggregate_market_daily_bars:{asset_class}",
            endpoint=asset_class,
            destination_table="MarketDailyBar",
        )
        try:
            created, skipped = ingest.aggregate_market_daily_bars(asset_class, target_date)
            results[asset_class] = (created, skipped)
            _finish_ok(outcome, rows_created=created, rows_rejected=skipped)
        except Exception as err:
            results[asset_class] = err
            _finish_fail(outcome, err)
    return results


#: CodalReport.status -> the WorkflowRun outcome that best describes it, so
#: the ops dashboard's blocked_network/blocked_storage rates (admin_telemetry
#: ._codal_status) reflect what actually happened instead of a blanket
#: SUCCESS/FAILED. See codal_pipeline.py's module docstring for the retry
#: policy each status implies.
_CODAL_STATUS_OUTCOME = {}


def _codal_status_outcome():
    if not _CODAL_STATUS_OUTCOME:
        from .models import CodalReport, WorkflowRun

        _CODAL_STATUS_OUTCOME.update({
            CodalReport.Status.PARSED: WorkflowRun.Outcome.SUCCESS,
            CodalReport.Status.NEEDS_REVIEW: WorkflowRun.Outcome.PARTIAL,
            CodalReport.Status.BLOCKED_NETWORK: WorkflowRun.Outcome.BLOCKED_NETWORK,
            CodalReport.Status.BLOCKED_STORAGE: WorkflowRun.Outcome.BLOCKED_STORAGE,
            CodalReport.Status.FAILED: WorkflowRun.Outcome.RETRY,
            CodalReport.Status.UNSUPPORTED: WorkflowRun.Outcome.SKIPPED,
        })
    return _CODAL_STATUS_OUTCOME


@shared_task
def extract_codal_report(announcement_id):
    from .codal_pipeline import extract_report
    from .models import WorkflowRun

    outcome = _ledgered(
        "extract_codal_report",
        endpoint="codal_announcements",
        destination_table="s3:codal-artifacts",
    )
    try:
        report, result = extract_report(announcement_id)
    except QuotaExhausted as err:
        # Not a failure: the day's provider budget is spent and the next run
        # picks this up. Recording it as FAILED would make a healthy backlog
        # look like a broken pipeline.
        outcome.finish(
            WorkflowRun.Outcome.SKIPPED, metadata={"reason": str(err)[:200]}
        )
        return {"status": "skipped"}
    except Exception as err:
        _finish_fail(outcome, err)
        raise
    outcome.finish(
        _codal_status_outcome().get(report.status, WorkflowRun.Outcome.PARTIAL),
        rows_created=result.get("fact_count", 0),
        metadata=result,
    )
    return result


@shared_task
def queue_codal_extractions():
    """Fan out a bounded batch so document work never occupies archive workers.

    Picks up never-attempted announcements AND stranded ones. The original
    filter was `report__isnull=True`, which meant a report that died mid-flight
    could never be retried: `extract` sets status=FETCHING before the download,
    so a worker restart, a hung socket or an unreachable provider left the row
    in FETCHING permanently and the next run skipped it for having a report at
    all. Production had 630 rows stuck in FETCHING for eight days, plus 912 in
    blocked_network, none of them reachable again.

    UNSUPPORTED and PARSED are terminal on purpose -- a missing artifact URL or a
    successful extraction should not be retried every night.
    """
    from django.db.models import Q

    from .models import CodalAnnouncement, CodalReport

    has_artifact = (
        Q(link_excel__gt="")
        | Q(link_pdf__gt="")
        | Q(link__gt="")
        | Q(link_attachment__gt="")
    )
    limit = settings.CODAL_EXTRACT_BATCH_SIZE

    # A FETCHING row younger than this may still be in flight on a live worker.
    stale_before = timezone.now() - timedelta(
        seconds=settings.CODAL_FETCHING_STALE_SECONDS
    )
    retryable = (
        Q(report__status=CodalReport.Status.FETCHING, report__updated_at__lt=stale_before)
        | Q(report__status__in=(
            CodalReport.Status.BLOCKED_NETWORK,
            CodalReport.Status.BLOCKED_STORAGE,
            CodalReport.Status.FAILED,
        ))
    )
    ids = list(
        CodalAnnouncement.objects.filter(has_artifact)
        .filter(Q(report__isnull=True) | retryable)
        .order_by("-date_publish", "-time_publish")
        .values_list("id", flat=True)[:limit]
    )
    for announcement_id in ids:
        extract_codal_report.delay(announcement_id)
    return len(ids)


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

    # A gate failure is the warehouse's normal resting state while the archive is
    # still filling -- 1,072 of 1,346 symbols fail it today. Alerting on "any
    # failure" therefore fired every 15 minutes forever and buried the five alerts
    # that do mean something. Alert on the share failing, not on the fact that any
    # does, so the signal is "this got worse" rather than "backfill is unfinished".
    assessed = SymbolIntegrity.objects.count()
    failed_integrity = SymbolIntegrity.objects.filter(passes_gate=False).count()
    if assessed and failed_integrity / assessed > settings.INTEGRITY_FAILURE_RATE_THRESHOLD:
        alerts.append(("failed-integrity-assessments", {
            "failed": failed_integrity,
            "assessed": assessed,
            "rate": round(failed_integrity / assessed, 4),
        }))

    since = timezone.now() - timedelta(hours=1)
    recent_runs = WorkflowRun.objects.filter(created_at__gte=since)
    run_count = recent_runs.count()
    failed_count = recent_runs.filter(outcome=WorkflowRun.Outcome.FAILED).count()
    failure_rate = failed_count / run_count if run_count else 0
    if run_count and failure_rate > settings.WORKFLOW_FAILURE_RATE_THRESHOLD:
        alerts.append(("elevated-workflow-failure-rate", {
            "failed": failed_count, "total": run_count, "rate": round(failure_rate, 4)
        }))

    # Individually wedged states were invisible: `stale-archive-progress` below
    # only fires when the *whole* archive goes quiet, so 59 symbols that could
    # never converge sat failing for days inside a busy, healthy-looking archive.
    # `consecutive_failures` is exactly the right signal because it resets only on
    # genuine progress (len(missing) shrinking), never on a mere successful call.
    # Past ~5 the backoff has capped at 24h, so a state here retries once a day
    # and gets nowhere. Report the causes, not just the count.
    wedged = ArchiveFetchState.objects.filter(
        consecutive_failures__gte=settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD
    )
    wedged_count = wedged.count()
    if wedged_count:
        causes = Counter(
            _retry_code(error)
            for error in wedged.values_list("last_error", flat=True)
        )
        alerts.append(("wedged-archive-states", {
            "count": wedged_count,
            "threshold": settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD,
            "causes": dict(causes.most_common(5)),
            "sample": list(
                wedged.values_list("endpoint", "symbol")[:5]
            ),
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

    from marketdata.admin_telemetry import project_disk

    disk = project_disk()
    if disk.get("alert"):
        alerts.append(("disk-projection", disk))

    for event, details in alerts:
        notify(event, details, dedupe_seconds=900)
    return {"alerts": [event for event, _details in alerts]}


@shared_task(queue="archive")
def retry_archive_job_task(state_id):
    from .archive import run_archive_state
    run_archive_state(state_id)
from .quota import QuotaExhausted
from portfolio.live.redis_client import get_redis

logger = logging.getLogger(__name__)


def _ledgered(workflow, *, endpoint="", destination_table=""):
    from .models import WorkflowRun
    from .workflows import WorkflowOutcome

    return WorkflowOutcome(workflow, endpoint=endpoint, destination_table=destination_table)


def _finish_ok(outcome, **values):
    from .models import WorkflowRun

    outcome.finish(WorkflowRun.Outcome.SUCCESS, **values)


def _finish_fail(outcome, err):
    from .models import WorkflowRun

    outcome.finish(
        WorkflowRun.Outcome.FAILED,
        error_code=type(err).__name__,
        metadata={"reason": str(err)[:300]},
    )


def _queue_slots(queue, limit):
    """Return free pending slots; fail closed when the broker is unavailable."""
    from redis import Redis

    try:
        depth = Redis.from_url(settings.CELERY_BROKER_URL).llen(queue)
    except Exception:
        logger.exception("Could not inspect Celery queue %s.", queue)
        return 0, None
    return max(0, limit - depth), depth


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
    outcome = _ledgered("weekly_metadata_sync", destination_table="StockSymbolMetadata")
    try:
        key = settings.TSETMC_API_KEY
        if not key:
            _finish_ok(outcome, metadata={"reason": "no_api_key"})
            return
        import re
        count = 0
        for symbol in tracked_tse_symbols():
            if re.search(r"\d$", symbol):
                continue
            ingest.ingest_symbol_metadata(fetch_symbol_data(key, symbol))
            _pause()
            count += 1
        _finish_ok(outcome, rows_accepted=count)
    except Exception as err:
        _finish_fail(outcome, err)
        raise


_RETRY_CLASS_RE = re.compile(r"\((\w+), status=")


def _retry_code(last_error):
    """A groupable reason for why a state asked to be retried.

    A constant `archive_fetch_retry` covered 773 of 1,752 ledger rows: the ledger
    recorded *that* something retried but never *what*, so answering "why is the
    archive retrying" meant reading `last_error` off the states table by hand.
    The full message still goes to metadata; this is only the grouping key.
    """
    match = _RETRY_CLASS_RE.search(last_error)
    if match:
        return match.group(1)  # ReadTimeout, SSLError, ConnectionError
    if "tick_volume_mismatch" in last_error:
        return "tick_volume_mismatch"
    return last_error.split(":")[0][:80] or "archive_fetch_retry"


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
        logger.error(
            "archive_state failed correlation_id=%s error=%s",
            outcome.correlation_id, type(err).__name__,
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
        error_code=_retry_code(state.last_error) if state.last_error else "",
        metadata={
            "missing_rows": state.missing_rows,
            "target_window_days": state.target_window_days,
            **({"reason": state.last_error[:300]} if state.last_error else {}),
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
        from .quota import ARCHIVE, remaining_requests
        if remaining_requests(ARCHIVE) <= 0:
            outcome.finish(
                WorkflowRun.Outcome.SKIPPED,
                error_code="quota_exhausted",
                metadata={"reason": "archive_budget_empty"},
            )
            return
        slots, depth = _queue_slots(
            "archive", settings.MARKETDATA_ARCHIVE_QUEUE_LIMIT
        )
        if not slots:
            outcome.finish(
                WorkflowRun.Outcome.SKIPPED,
                metadata={
                    "reason": "queue_unavailable" if depth is None else "queue_full",
                    "queue_depth": depth,
                    "queue_limit": settings.MARKETDATA_ARCHIVE_QUEUE_LIMIT,
                },
            )
            return
        if not ArchiveFetchState.objects.exists():
            ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
        grow_tick_windows()
        claim_limit = min(settings.MARKETDATA_ARCHIVE_BATCH_SIZE, slots)
        batch = claim_archive_batch(limit=claim_limit)
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
        logger.error(
            "archive_tick failed correlation_id=%s error=%s",
            outcome.correlation_id, type(err).__name__,
        )
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
    outcome = _ledgered("recent_history_refresh", destination_table="ArchiveFetchState")
    try:
        slots, depth = _queue_slots("archive", settings.MARKETDATA_ARCHIVE_QUEUE_LIMIT)
        if not slots:
            from .models import WorkflowRun
            outcome.finish(WorkflowRun.Outcome.SKIPPED, metadata={"reason": "queue_full", "queue_depth": depth})
            return
        state_ids = claim_recent_refresh(limit=slots)
        if state_ids:
            group(*(run_archive_state.si(state_id) for state_id in state_ids)).apply_async()
        _finish_ok(outcome, rows_accepted=len(state_ids))
    except Exception as err:
        _finish_fail(outcome, err)
        raise


@shared_task(ignore_result=True)
def weekly_warehouse_audit():
    """Run the full read-only audit and leave a timestamped manifest behind."""
    outcome = _ledgered("weekly_warehouse_audit")
    try:
        from django.core.management import call_command

        path = os.path.join(
            settings.WAREHOUSE_AUDIT_DIR,
            f"warehouse_audit_{timezone.now():%Y%m%d}.csv",
        )
        os.makedirs(settings.WAREHOUSE_AUDIT_DIR, exist_ok=True)
        call_command("audit_warehouse", manifest_path=path)
        _finish_ok(outcome, metadata={"path": path})
        return path
    except Exception as err:
        _finish_fail(outcome, err)
        raise


@shared_task(ignore_result=True)
def capture_operational_metrics():
    """Capture one idempotent 15-minute ops point and retain 90 days."""
    from marketdata.admin_telemetry import collect_metric_payload, invalidate_ops_cache
    from marketdata.models import OperationalMetricSnapshot, WorkflowRun
    from marketdata.workflows import WorkflowOutcome

    outcome = WorkflowOutcome(
        "capture_operational_metrics",
        endpoint="ops_metrics",
        destination_table="OperationalMetricSnapshot",
    )
    now = timezone.now()
    slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    payload = collect_metric_payload()
    snapshot, created = OperationalMetricSnapshot.objects.update_or_create(
        captured_at=slot,
        defaults=payload,
    )
    invalidate_ops_cache()
    OperationalMetricSnapshot.objects.filter(
        captured_at__lt=now - timedelta(days=90)
    ).delete()
    outcome.finish(
        WorkflowRun.Outcome.SUCCESS,
        rows_accepted=1,
        rows_created=1 if created else 0,
        rows_updated=0 if created else 1,
        metadata={"slot": slot.isoformat()},
    )
    return snapshot.pk


@shared_task(ignore_result=True)
def prune_workflow_runs():
    """Enforce the ledger's own retention window.

    The ledger replaced 241,000 unbounded legacy log rows, and was itself
    unbounded: `workflow_retention` existed but was never scheduled. Legacy
    SystemLogEvent rows stay untouched -- they are a frozen historical archive
    and nothing writes to them any more.
    """
    from .models import WorkflowRun

    cutoff = timezone.now() - timedelta(days=settings.WORKFLOW_RETENTION_DAYS)
    deleted, _ = WorkflowRun.objects.filter(created_at__lt=cutoff).delete()
    logger.info("prune_workflow_runs: deleted %d rows older than %s", deleted, cutoff.date())
    return deleted


@shared_task(ignore_result=True)
def archive_maintenance():
    """Reverify completed low-volatility endpoints outside normal batches."""
    outcome = _ledgered("archive_maintenance", destination_table="ArchiveFetchState")
    try:
        slots, depth = _queue_slots("archive", settings.MARKETDATA_ARCHIVE_QUEUE_LIMIT)
        if not slots:
            from .models import WorkflowRun
            outcome.finish(WorkflowRun.Outcome.SKIPPED, metadata={"reason": "queue_full", "queue_depth": depth})
            return
        state_ids = claim_archive_maintenance(limit=min(2, slots))

        # Peer-relative suspension sweep: flag symbols failing far more than
        # their peers on the same endpoint, so quota stops being spent on them.
        # Cheap (one query per endpoint) and it refuses to act during a
        # provider-wide outage, when every symbol fails at once.
        from . import suspension

        suspended = suspension.suspend_outliers()

        # Weekly probe of already-suspended states, at the back of the queue so
        # it can never crowd out real archive work. A probe is an ordinary fetch
        # through run_archive_state; try_recover clears the flag if it comes
        # back clean.
        probe_ids = suspension.claim_probe_batch(
            limit=max(0, min(2, slots - len(state_ids)))
        )

        for state_id in list(state_ids) + list(probe_ids):
            run_archive_state.si(state_id).apply_async()
        _finish_ok(
            outcome,
            rows_accepted=len(state_ids) + len(probe_ids),
            metadata={"suspended": len(suspended), "probes": len(probe_ids)},
        )
    except Exception as err:
        _finish_fail(outcome, err)
        raise


@shared_task(ignore_result=True)
def catalog_sync(limit: int = None):
    outcome = _ledgered("catalog_sync", destination_table="MarketInstrument")
    try:
        result = sync_provider_catalog(limit=limit)
        ensure_archive_states(tracked_tse_symbols(), tracked_brs_symbols())
        _finish_ok(outcome, rows_received=result["seen"], rows_accepted=result["eligible"])
        return result
    except Exception as err:
        _finish_fail(outcome, err)
        raise


@shared_task(ignore_result=True)
def nightly_data_integrity():
    """Run data integrity checks for all symbols nightly.

    Also runs the warehouse unit audit in report-only mode. It never writes: the
    point is that a fresh unit regression shows up in the log the night it
    appears, instead of surfacing months later inside somebody's valuation.
    """
    from marketdata.archive import reopen_states_with_gaps
    from marketdata.integrity import market_outage_windows, update_all_symbols_integrity
    results = update_all_symbols_integrity()

    outages = market_outage_windows()
    if outages:
        # Market-wide, so every symbol is short the same sessions and the
        # per-symbol gate cannot see it. Reopen the lot.
        reopened = reopen_states_with_gaps(None)
        logger.error(
            "market outage: %d gap(s) in the session calendar; longest %s to %s; "
            "reopened %d archive state(s) to refetch",
            len(outages), outages[0][0], outages[0][1], reopened,
        )
    else:
        gapped = [
            row.symbol for row in results
            if "low_coverage" in row.reason or "price_gap_exceeded" in row.reason
        ]
        reopened = reopen_states_with_gaps(gapped)
        if reopened:
            logger.info(
                "Reopened %d archive state(s) across %d symbol(s) with missing sessions.",
                reopened, len(gapped),
            )

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
                "unit_audit: %d mis-scaled row(s) across %d symbol(s); "
                "run `manage.py audit_warehouse` for the manifest; first: %s",
                len(errors), len({f["symbol"] for f in errors}), errors[0]["evidence"],
            )
    except Exception:  # never let a report-only check break the integrity run
        logger.exception("unit_audit failed; integrity results still stand")
    _finish_ok(
        _ledgered("nightly_data_integrity", destination_table="SymbolIntegrity"),
        rows_accepted=len(results),
    )


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

    result = {
        "tse_symbols_examined": len(symbols),
        "corporate_action_candidates": corporate_action_candidates,
        "actions_created": actions_created_count,
        "gold_currency_symbols_examined": len(gold_symbols),
        "proposed_rejections": proposed_rejections,
        "spikes_rejected": rejected_count,
    }
    _finish_ok(
        _ledgered("nightly_series_validation", destination_table="RejectedRecord"),
        rows_accepted=actions_created_count,
        rows_rejected=rejected_count,
        metadata=result,
    )
    return result
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
    returns, excluded = daily_returns_matrix(
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
        # Geometric daily rf (matches the compounding return side); simple
        # rf/252 division understates the daily rate by ~13% at rf=0.30.
        rf_daily = (1.0 + risk_free) ** (1.0 / 252) - 1.0
        downside = (series - rf_daily).clip(upper=0)
        downside_deviation = float(np.sqrt(np.mean(downside ** 2))) * np.sqrt(252)
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
    # `excluded` was previously discarded here -- price_gap_exceeded/
    # insufficient_history/insufficient_coverage assets silently dropped out of
    # the panel with no trace outside a manual DB query. Surface counts by
    # reason so a sudden jump (e.g. a provider outage excluding half the
    # universe) is visible in logs, not just in a lower `written` count.
    reason_counts = Counter(row.get("reason", "unknown") for row in excluded)
    _finish_ok(
        _ledgered("nightly_asset_metrics", destination_table="AssetMetricSnapshot"),
        rows_accepted=written,
        rows_rejected=len(excluded),
        metadata={"excluded_reasons": dict(reason_counts.most_common(10))} if excluded else {},
    )


@shared_task(ignore_result=True)
def nightly_asset_signals(window_days=365):
    """Technical stance per eligible symbol, written to AssetSignalSnapshot.

    Sits beside nightly_asset_metrics in the batch layer and reads the same panel,
    so the indicators describe exactly the series every other number on the site
    describes. Runs after it in the beat schedule for that reason.

    Symbols failing the integrity gate are still COMPUTED and stored, with
    `passes_integrity=False` recorded on the row. Skipping them would leave the
    reader unable to distinguish "no signal" from "signal we chose not to show",
    and the flag is what lets the UI refuse to present a stance drawn from a
    series with holes in it as actionable.
    """
    from portfolio.services import signals as signal_math
    from portfolio.services.returns import daily_returns_matrix
    from . import jalali
    from .models import AssetSignalSnapshot, MarketInstrument, SymbolIntegrity

    outcome = _ledgered(
        "nightly_asset_signals", destination_table="AssetSignalSnapshot"
    )
    try:
        instruments = {
            row.symbol: row for row in MarketInstrument.objects.filter(eligible=True)
        }
        if not instruments:
            _finish_ok(outcome, metadata={"reason": "no_eligible_instruments"})
            return {"written": 0}

        returns, excluded = daily_returns_matrix(
            history_days=window_days, universe=list(instruments)
        )
        if returns.empty:
            _finish_ok(outcome, metadata={"reason": "empty_returns_panel"})
            return {"written": 0}

        gates = dict(
            SymbolIntegrity.objects.values_list("symbol", "passes_gate")
        )
        as_of = jalali.today()
        written = skipped = 0
        stances = {}
        for symbol in returns.columns:
            prices = signal_math.price_index_from_returns(returns[symbol])
            reading = signal_math.describe(prices)
            if reading is None:
                # Too little history for the indicators to mean anything; a
                # stance from 20 sessions of RSI would be noise wearing a verdict.
                skipped += 1
                continue
            instrument = instruments.get(symbol)
            AssetSignalSnapshot.objects.update_or_create(
                symbol=symbol,
                as_of=as_of,
                window_days=window_days,
                defaults={
                    "asset_class": instrument.category if instrument else "",
                    "rsi": reading["rsi"],
                    "macd_histogram": reading["macd_histogram"],
                    "above_trend": reading["above_trend"],
                    "trend_window": reading["trend_window"],
                    "overbought": reading["overbought"],
                    "oversold": reading["oversold"],
                    "stance": reading["stance"],
                    "observations": reading["observations"],
                    "passes_integrity": bool(gates.get(symbol, False)),
                },
            )
            written += 1
            stances[reading["stance"]] = stances.get(reading["stance"], 0) + 1
    except Exception as err:
        _finish_fail(outcome, err)
        raise

    excluded_reasons = Counter(row.get("reason", "unknown") for row in excluded)
    _finish_ok(
        outcome,
        rows_accepted=written,
        rows_rejected=skipped + len(excluded),
        metadata={
            "as_of": as_of,
            "stances": stances,
            "excluded_reasons": dict(excluded_reasons.most_common(10)),
        },
    )
    return {"written": written, "skipped": skipped, "stances": stances}
