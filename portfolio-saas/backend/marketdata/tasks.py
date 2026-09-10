"""Celery tasks for the market-data warehouse sync.

Schedule (config/celery.py): `archive_tick` claims a batch and fans it out via
`run_archive_state`.
`sync_symbol_metadata` refreshes symbol fundamentals daily, stalest first, and
stops cleanly when its bucket is spent rather than failing.

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
    _ENDPOINT_PLAN,
    claim_archive_batch,
    claim_archive_maintenance,
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
    endpoint timing out must not mask or abort the others' results.

    Only `tse_option` remains. The two IME (Iran Mercantile Exchange) kinds were
    removed on 2026-09-06 -- see `endpoints.py` for why. The loop keeps its shape
    because the per-kind failure isolation is the point, not the number of kinds.

    Cadence comes from `LiveFetchState`, not from the beat interval: these fetches
    bill the live bucket, and the quota reserve can only be exact if the schedule
    it prices is the schedule that runs.
    """
    from . import live_states
    from .fetchers import fetch_derivatives

    # Insert-only and idempotent. Self-seeding here rather than in some earlier
    # job means a fresh deployment starts snapshotting on its first tick instead
    # of waiting for whatever else happened to own the seeding.
    live_states.ensure_live_states()

    results = {}
    for kind, endpoint_key in (
        ("tse_option", "option_contracts"),
    ):
        claimed = live_states.claim_due(endpoint_key, limit=1)
        if not claimed:
            continue
        state = claimed[0]
        outcome = _ledgered(
            f"capture_derivative_snapshots:{kind}",
            endpoint=endpoint_key,
            destination_table="DerivativeSnapshot",
        )
        try:
            payload = fetch_derivatives(settings.TSETMC_API_KEY, endpoint_key)
            created, rejected = ingest.ingest_derivative_snapshots(kind, payload) or (0, 0)
            results[kind] = (created, rejected)
            live_states.record_result(state, ok=True)
            _finish_ok(outcome, rows_created=created, rows_rejected=rejected)
        except Exception as err:
            results[kind] = err
            live_states.record_result(state, ok=False, error=err)
            _finish_fail(outcome, err)
    return results


@shared_task
def capture_market_snapshots():
    """Poll crypto and commodity (one Market/* call each).

    Nav.php is retired -- it never produced a usable series and billed TSETMC.
    Cadence comes from `LiveFetchState`.
    """
    from . import live_states
    live_states.ensure_live_states()  # see capture_derivative_snapshots

    results = {}
    for asset_class, endpoint_key, api_key in (
        ("crypto", "crypto", settings.BRS_API_KEY),
        ("commodity", "commodity", settings.BRS_API_KEY),
    ):
        claimed = live_states.claim_due(endpoint_key, limit=1)
        if not claimed:
            continue
        state = claimed[0]
        outcome = _ledgered(
            f"capture_market_snapshots:{asset_class}",
            endpoint=endpoint_key,
            destination_table="MarketSnapshot",
        )
        try:
            payload = _market_snapshot_payload(asset_class, endpoint_key, api_key)
            created, skipped = ingest.ingest_market_snapshots(asset_class, payload) or (0, 0)
            results[asset_class] = (created, skipped)
            live_states.record_result(state, ok=True)
            _finish_ok(outcome, rows_created=created, rows_rejected=skipped)
        except Exception as err:
            results[asset_class] = err
            live_states.record_result(state, ok=False, error=err)
            _finish_fail(outcome, err)

    return results


def _market_snapshot_payload(asset_class, endpoint_key, api_key):
    """Use an unmetered origin first; call BrsApi only as a fallback."""
    if asset_class == "crypto" and getattr(settings, "WALLEX_ENABLED", False):
        from .sources import wallex
        from .sources.http import SourceError

        try:
            rows = [
                {
                    "symbol": row["base"],
                    "price": row["price"],
                    "unit": row["unit"],
                }
                for row in wallex.live_rows()
                if row.get("quote") == "TMN"
                and row.get("base")
                and row.get("price") is not None
            ]
            if rows:
                return rows
        except SourceError as exc:
            logger.warning("Wallex snapshot fetch failed; using BrsApi fallback: %s", exc)

    from .fetchers import fetch_derivatives

    return fetch_derivatives(api_key, endpoint_key)


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
        "crypto", "commodity", "index", "tse_option",
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
    if not settings.CODAL_ENABLED:
        # Belt and braces: nothing should be enqueueing these, but a message
        # left in the broker from before the flag flipped must not run.
        outcome.finish(WorkflowRun.Outcome.SKIPPED, error_code="codal_disabled")
        return {}
    try:
        report, result = extract_report(announcement_id)
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
    """Sweeper for Codal reports the event-driven path did not finish.

    New announcements are enqueued the moment `ingest.ingest_codal_announcements`
    writes them, so this is no longer the main way work starts -- it is the safety
    net for rows that were stranded mid-flight or failed retryably. It runs every
    few minutes rather than every six hours because the backlog is ~74,000
    documents and none of this spends provider quota.

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

    from .codal_storage import origin_unreachable
    from .models import CodalAnnouncement, CodalReport, WorkflowRun

    outcome = _ledgered("queue_codal_extractions", endpoint="codal_artifacts")
    if not settings.CODAL_ENABLED:
        outcome.finish(WorkflowRun.Outcome.SKIPPED, error_code="codal_disabled")
        return 0
    if origin_unreachable():
        # Every one of these would fail at connect. Enqueueing them anyway is how
        # the ledger filled with ~986 identical timeouts a day.
        outcome.finish(
            WorkflowRun.Outcome.SKIPPED, error_code="origin_unreachable"
        )
        return 0

    # Never enqueue more than the codal queue can absorb: the backlog is ~74,000
    # rows, and a flat batch size would just move it from the database into Redis.
    limit, depth = _queue_slots("codal", settings.CODAL_EXTRACT_BATCH_SIZE)
    if not limit:
        outcome.finish(
            WorkflowRun.Outcome.SKIPPED,
            metadata={"reason": "queue_full", "queue_depth": depth},
        )
        return 0

    has_artifact = (
        Q(link_excel__gt="")
        | Q(link_pdf__gt="")
        | Q(link__gt="")
        | Q(link_attachment__gt="")
    )

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
    queued = _dispatch_codal_ids(ids, slots=limit)
    _finish_ok(outcome, rows_accepted=queued)
    return queued


@shared_task(ignore_result=True)
def operational_health_check():
    from redis import Redis

    from config.observability import notify
    from portfolio.models import Price
    from . import archive
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
    #
    # Suspended and blacklisted states are excluded for the same reason
    # `classify_archive_state` tests those flags first: a state the archive has
    # already stopped claiming is neither coverage nor backlog, and re-reporting
    # it is asking an operator to act on a decision the system has made. Without
    # this the alert was 100% noise -- all 322 states it named on 2026-09-07
    # were suspended, so it fired every 15 minutes for weeks with an unchanging
    # payload and buried the four detections above it that do mean something.
    wedged = ArchiveFetchState.objects.filter(
        consecutive_failures__gte=settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD,
        blacklisted=False,
        suspended_at__isnull=True,
    ).exclude(endpoint__in=archive.disabled_endpoints())
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

    # "Quiet" only means something when the archive COULD be spending. Under
    # burst allocation the wallet is empty for most of the day by design -- the
    # backlog is larger than the subscription, so backfill runs flat out from
    # Tehran midnight and then idles until the next reset. That silence is the
    # intended shape, not a stall: this fired 86 times in the 25h to 2026-09-07,
    # every quarter hour outside the 2h38m burst, which is the same "cries wolf
    # daily" failure the integrity-rate alert above was already rewritten to
    # avoid. Ask whether there is quota to make progress with first; if there is
    # and nothing has moved in 30 minutes, that is a real stall.
    from .quota import archive_capacity
    from django.db.models import Q

    stale_before = timezone.now() - timedelta(seconds=settings.ARCHIVE_PROGRESS_STALE_SECONDS)
    capacity_by_plan = archive_capacity()
    available_endpoints = [
        endpoint for endpoint, plan in _ENDPOINT_PLAN.items()
        if plan is None or capacity_by_plan.get(plan, 0) > 0
    ]
    if (
        available_endpoints
        and ArchiveFetchState.objects.filter(
            verified_complete=False,
            endpoint__in=available_endpoints,
            suspended_at__isnull=True,
            blacklisted=False,
        ).filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())).exists()
        and not WorkflowRun.objects.filter(
            workflow="archive_state",
            outcome__in=(WorkflowRun.Outcome.SUCCESS, WorkflowRun.Outcome.PARTIAL),
            created_at__gte=stale_before,
        ).exists()
    ):
        alerts.append(("stale-archive-progress", {
            "stale_seconds": settings.ARCHIVE_PROGRESS_STALE_SECONDS,
            "archive_capacity": sum(capacity_by_plan.values()),
        }))

    # Per plan: each subscription is its own ledger, and summing them would let
    # an overcount on one wallet cancel an undercount on the other.
    from .quota import quota_day as _quota_day

    for quota in ApiRequestQuota.objects.filter(day=_quota_day()):
        bucket_total = quota.archive_used + quota.live_used + quota.other_used
        if bucket_total != quota.used:
            alerts.append((
                "quota-ledger-drift",
                {"plan": quota.plan, "used": quota.used, "bucket_total": bucket_total},
            ))

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


def _ledgered(workflow, *, endpoint="", destination_table="", source=None, symbol=""):
    """Open a ledgered workflow, naming its origin automatically where one exists.

    `source` defaults to the endpoint registry's provider path rather than to
    the empty string. Every marketdata workflow used to record an empty source
    because `_ledgered` had no way to accept one, so the column existed, the log
    line omitted it, and "where did this row come from" was unanswerable from
    either. Pass `source=""` explicitly for a workflow that genuinely has no
    origin -- a scheduler, a prune, an aggregation over rows already stored.
    """
    from .workflows import WorkflowOutcome
    from . import endpoints as endpoint_registry

    if source is None:
        source = endpoint_registry.source_for(endpoint)
    return WorkflowOutcome(
        workflow,
        endpoint=endpoint,
        symbol=symbol,
        source=source,
        destination_table=destination_table,
    )


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


def _dispatch_codal_ids(ids, *, slots=None):
    """Best-effort, queue-bounded dispatch shared by ingest and the sweeper."""
    if slots is None:
        slots, _ = _queue_slots("codal", settings.CODAL_EXTRACT_BATCH_SIZE)
    queued = 0
    for announcement_id in list(ids)[:slots]:
        try:
            extract_codal_report.delay(announcement_id)
        except Exception:
            logger.exception(
                "Could not enqueue Codal extraction announcement_id=%s.",
                announcement_id,
            )
            break
        queued += 1
    return queued


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
    """BrsApi gold/currency symbols to sync: active portfolio assets + all catalog-eligible BRS gold/currency symbols."""
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
            # GOLD is the whole gold_daily universe: bullion, coins AND hard
            # currency, which `sync_provider_catalog` stamps GOLD on purpose and
            # separates by `provider_group` instead (see catalog.py). There is no
            # CURRENCY category -- filtering for one matches nothing, so pairing
            # it with a literal "gold" only looks like it covers USD/EUR.
            category=MarketInstrument.Category.GOLD,
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
def sync_symbol_metadata():
    """Refresh StockSymbolMetadata fundamentals, stalest symbols first.

    `Tsetmc/Symbol.php` costs one request per symbol and bills the OTHER bucket,
    which is capped at `MARKETDATA_OTHER_REQUEST_BUDGET` (200/day). There are
    ~1,900 tracked symbols. The previous version walked all of them in
    `tracked_tse_symbols()` order and let `QuotaExhausted` propagate, so every
    run refused at ~200, raised, and started from the SAME symbols next time --
    the tail was structurally unreachable. Production evidence: 86 of 1,969
    symbols had a non-zero `market_cap`, and the last run failed
    `QuotaExhausted`.

    Two changes make it converge. Ordering by staleness means each run resumes
    where the last stopped rather than repeating its prefix, and running out of
    budget is a clean stop, not a failure -- there is nothing wrong with a job
    that has spent its daily allowance.

    This matters well beyond fundamentals: `market_cap` used to pick the deep
    tick tier, and with 96% of it missing that tier was decided by which symbols
    this task happened to reach. Liquidity now comes from candle turnover
    (`reversal.liquid_symbols`) precisely so it does not depend on this job, but
    the fundamentals themselves still do.
    """
    from .models import StockSymbolMetadata
    from .quota import QuotaExhausted

    outcome = _ledgered("sync_symbol_metadata", destination_table="StockSymbolMetadata")
    try:
        key = settings.TSETMC_API_KEY
        if not key:
            _finish_ok(outcome, metadata={"reason": "no_api_key"})
            return

        # Symbols ending in a digit are secondary boards ("سامان2"); the provider
        # has no fundamentals row for them.
        candidates = [s for s in tracked_tse_symbols() if not re.search(r"\d$", s)]
        seen = dict(
            StockSymbolMetadata.objects.filter(l18__in=candidates).values_list(
                "l18", "updated_at"
            )
        )
        # Never-fetched first (None sorts before any timestamp), then oldest.
        candidates.sort(key=lambda s: (seen.get(s) is not None, seen.get(s)))

        count = 0
        stopped = ""
        for symbol in candidates:
            try:
                ingest.ingest_symbol_metadata(fetch_symbol_data(key, symbol))
            except QuotaExhausted as exc:
                stopped = exc.reason
                break
            _pause()
            count += 1
        _finish_ok(
            outcome,
            rows_accepted=count,
            metadata={
                "stopped_on": stopped or "completed",
                "remaining": len(candidates) - count,
                "never_fetched": sum(1 for s in candidates if s not in seen),
            },
        )
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
    from .archive import destination_for, source_for

    initial = ArchiveFetchState.objects.get(pk=state_id)
    outcome = _ledgered(
        "archive_state",
        endpoint=initial.endpoint,
        symbol=initial.symbol,
        source=source_for(initial.endpoint),
        destination_table=destination_for(initial.endpoint),
    )
    try:
        state = process_archive_state(state_id)
    except QuotaExhausted as err:
        # When it comes back, and whether the wait is minutes or until the next
        # quota day. A refusal that does not say what happens next reads as a
        # failure, and `archive_paced` vs `live_reserved` is exactly the
        # difference between "again in three minutes" and "idle until 00:01".
        deferred = ArchiveFetchState.objects.filter(pk=state_id).values_list(
            "next_attempt_at", flat=True
        ).first()
        outcome.finish(
            WorkflowRun.Outcome.RETRY,
            error_code=getattr(err, "reason", None) or "quota_exhausted",
            metadata={
                "reason": str(err),
                "next_attempt_at": deferred.isoformat() if deferred else "-",
                "wait": "minutes" if getattr(err, "is_pacing", False) else "next_quota_day",
            },
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
        # NOTE: received/accepted are the state's CUMULATIVE totals, not this
        # run's. Only `rows_created` is a per-run delta -- use it, not a sum of
        # the other two, when asking what a day of requests actually bought.
        rows_received=state.expected_rows,
        rows_accepted=state.stored_rows,
        rows_created=getattr(state, "run_rows_created", 0) or 0,
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
        from .quota import PLANS, archive_capacity, archive_idle_reason
        capacity = archive_capacity()
        if not any(value > 0 for value in capacity.values()):
            reasons = {plan: archive_idle_reason(plan) for plan in PLANS}
            paced = bool(reasons) and all(
                reason == "archive_paced" for reason in reasons.values()
            )
            outcome.finish(
                WorkflowRun.Outcome.SKIPPED,
                error_code="archive_paced" if paced else "quota_exhausted",
                metadata={
                    "reason": "archive_paced" if paced else "archive_budget_empty",
                    **capacity,
                    "idle": reasons,
                },
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
        # Bookkeeping, not dispatch. It shares the `try` below with the claim,
        # so when it started raising on 2026-08-27 every scheduling attempt --
        # one per 15s, 3,300+ of them -- died before claiming a single state and
        # the archive went dark for thirteen hours with 89% of the day's TSETMC
        # wallet unspent. Widening a window is never worth not fetching.
        try:
            grow_tick_windows()
        except Exception as err:  # noqa: BLE001 -- must not reach the claim
            logger.error(
                "grow_tick_windows failed correlation_id=%s error=%s: %s",
                outcome.correlation_id, type(err).__name__, err,
            )
        # Bound the batch by the quota actually left, not just by the batch size
        # and the queue. Dispatching more states than there are requests to
        # spend does not fetch more -- every surplus task wakes up, loses the
        # race for the last request, and re-parks itself. On 2026-09-06 that was
        # 101,098 no-op `archive_paced` runs against 4,139 real fetches in a
        # single day: 96% of all archive executions, each one a database round
        # trip and a log line, hiding the 4% that mattered.
        budget = sum(value for value in capacity.values() if value > 0)
        claim_limit = min(settings.MARKETDATA_ARCHIVE_BATCH_SIZE, slots, budget)
        if claim_limit <= 0:
            outcome.finish(
                WorkflowRun.Outcome.SKIPPED,
                error_code="archive_paced",
                metadata={"reason": "no_quota_headroom", **capacity},
            )
            return
        batch = claim_archive_batch(limit=claim_limit)
        if not batch:
            outcome.finish(WorkflowRun.Outcome.SKIPPED, metadata={"reason": "no_due_states"})
            return
        group(*(run_archive_state.si(state_id) for state_id in batch)).apply_async()
        outcome.finish(
            WorkflowRun.Outcome.SUCCESS,
            rows_received=len(batch),
            rows_accepted=len(batch),
            metadata={"quota_headroom": budget, "dispatched": len(batch)},
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
    from marketdata.admin_telemetry import (
        collect_metric_payload,
        get_ops_overview,
        invalidate_ops_cache,
    )
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
    # Rebuild it here rather than leaving the next operator to pay for it.
    # Invalidating alone meant whoever opened the Ops page after this task ran
    # reassembled the whole payload themselves, and the slowest piece of it --
    # hypertable_size() over 528 chunks -- was measured at 10 s.
    try:
        get_ops_overview()
    except Exception:
        logger.warning("ops overview cache re-warm failed", exc_info=True)
    OperationalMetricSnapshot.objects.filter(
        captured_at__lt=now - timedelta(days=90)
    ).delete()
    outcome.finish(
        WorkflowRun.Outcome.SUCCESS,
        rows_accepted=1,
        rows_created=1 if created else 0,
        rows_updated=0 if created else 1,
        metadata={"slot": slot.isoformat(), **_quota_attribution_drift()},
    )
    return snapshot.pk


def _quota_attribution_drift():
    """Compare what the ledger claims we spent against what the counter charged.

    Every metered request goes through `quota.reserve_request` and is tallied by
    the workflow that owns it, so these two numbers should agree. When they do
    not, some lane is spending quota nobody can account for -- which is how ~1,191
    requests a day went missing before the live lane's thread-pool attribution was
    fixed. Surfaced here so it shows up on the Ops console rather than needing a
    shell and a hand-written aggregate.
    """
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    from django.db.models import Sum

    from .models import ApiRequestQuota, WorkflowRun
    from .quota import quota_day

    try:
        day = quota_day()
        zone = ZoneInfo(settings.MARKETDATA_QUOTA_TIMEZONE)
        start = datetime.combine(day, dtime.min, tzinfo=zone)
        # Summed across plans: `attributed` counts every workflow run regardless
        # of which subscription it billed, so the charged side has to match.
        charged = ApiRequestQuota.objects.filter(day=day).aggregate(
            total=Sum("used")
        )["total"] or 0
        attributed = WorkflowRun.objects.filter(created_at__gte=start).aggregate(
            total=Sum("quota_attempts")
        )["total"] or 0
        return {
            "quota_charged": charged,
            "quota_attributed": attributed,
            "quota_unattributed": charged - attributed,
        }
    except Exception:
        logger.warning("quota attribution check failed", exc_info=True)
        return {}


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
def reconcile_quota_meters():
    """Ask the provider what it has actually billed today, one request per plan.

    This is the only way to know. BrsApi does not report usage on a successful
    response, so between error responses the local counter is an estimate that
    can only drift downward -- it was 2,226 requests light on 2026-09-08, and
    the response to that was a 1.30 correction factor on the archive's ceiling
    that threw away 1,950 requests a day. A probe costs one request and replaces
    the guess with the number the vendor panel shows.

    **Probes only while a plan is spending.** The counter cannot drift when
    nothing is being fetched, so an idle plan is skipped and a quiet night costs
    nothing. In practice this is ~1-3% of the wallet on an active day, which is
    what the accuracy is worth: without it the safety margin has to absorb the
    unknown, and a margin sized for an unknown is indistinguishable from waste.
    """
    from .fetchers import probe_meter
    from .quota import PLANS, quota_day, unattributed_used
    from .models import ApiRequestQuota, WorkflowRun

    outcome = _ledgered("reconcile_quota_meters", destination_table="ApiRequestQuota")
    try:
        from django.core.cache import cache

        day = quota_day()
        probed, skipped = {}, []
        for plan in PLANS:
            row = ApiRequestQuota.objects.filter(day=day, plan=plan).first()
            used = row.used if row else 0
            mark_key = f"quota:meter:last_used:{plan}:{day}"
            if used and cache.get(mark_key) == used:
                skipped.append(plan)
                continue
            account = probe_meter(plan)
            if account is None:
                skipped.append(plan)
                continue
            row = ApiRequestQuota.objects.filter(day=day, plan=plan).first()
            cache.set(mark_key, row.used if row else used, timeout=_SECONDS_PER_DAY)
            probed[plan] = {
                "provider_usage": account.get("usage_today"),
                "provider_limit": account.get("usage_today_limit"),
                "local_used": row.used if row else used,
                "unattributed": unattributed_used(row),
            }
            drift = probed[plan]["unattributed"]
            if drift > _QUOTA_DRIFT_ALERT:
                logger.warning(
                    "quota_drift plan=%s unattributed=%d of %d billed; something is "
                    "spending this key outside reserve_request",
                    plan, drift, probed[plan]["local_used"],
                )
        outcome.finish(
            WorkflowRun.Outcome.SUCCESS,
            rows_accepted=len(probed),
            metadata={"probed": probed, "skipped": skipped},
        )
        return probed
    except Exception as err:
        _finish_fail(outcome, err)
        raise


#: Unattributed requests on one plan that mean the ledger has stopped describing
#: reality. Sized above the handful a deploy or a manual probe leaves behind and
#: well under the 2,226 seen on 2026-09-08.
_QUOTA_DRIFT_ALERT = 200
_SECONDS_PER_DAY = 86400


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
        #
        # Only claim when a wallet actually has room. `claim_probe_batch` stamps
        # `last_probe_at` at lease time, so a probe that reaches the provider
        # gate and is refused on quota still consumes the state's slot for the
        # next PROBE_INTERVAL. This task runs at 04:10 Tehran, which on any day
        # the archive burst empties the wallet is 90 minutes after the last
        # request -- both probes on 2026-09-07 came back `live_reserved` having
        # made no call at all, and their two states are now parked until
        # 2026-09-14. Skipping is free; the states stay due.
        from .quota import archive_capacity

        capacity = sum(archive_capacity().values())
        probe_ids = []
        if capacity:
            probe_ids = suspension.claim_probe_batch(
                limit=max(0, min(2, slots - len(state_ids)))
            )

        for state_id in list(state_ids) + list(probe_ids):
            run_archive_state.si(state_id).apply_async()
        _finish_ok(
            outcome,
            rows_accepted=len(state_ids) + len(probe_ids),
            metadata={
                "suspended": len(suspended),
                "probes": len(probe_ids),
                "probe_capacity": capacity,
            },
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
    from portfolio.services.returns import daily_returns_matrix, periods_per_year
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
        # Measured from this symbol's OWN observation dates, not the literal 252
        # that used to be written here four times. This panel is mixed: a TSE
        # stock prints ~252 times a year on the Sat-Wed calendar while gold, FX
        # and crypto print ~365, and `periods_per_year` exists precisely because
        # annualizing a 7-day series at 252 overstates volatility by
        # sqrt(365/252) ~= 1.20x and understates Sharpe by the same factor.
        # Every other consumer of this panel already reads the frequency
        # (diagnostics, optimization, expected_returns, the analytics views);
        # this task was the one place writing a constant, and it persists the
        # result into AssetMetricSnapshot, which the UI reads as fact.
        #
        # Per symbol rather than per panel: `series` is already `.dropna()`, so
        # its index is the days this instrument actually traded, whereas the
        # panel index is the union across all of them.
        frequency = periods_per_year(series.index)
        annualized_return = float(series.mean() * frequency)
        volatility = float(series.std(ddof=1) * np.sqrt(frequency))
        risk_free = settings.RATE_FOR(int(as_of[:4]))
        # Geometric daily rf (matches the compounding return side); simple
        # rf/frequency division understates the daily rate by ~13% at rf=0.30.
        rf_daily = (1.0 + risk_free) ** (1.0 / frequency) - 1.0
        downside = (series - rf_daily).clip(upper=0)
        downside_deviation = float(np.sqrt(np.mean(downside ** 2))) * np.sqrt(frequency)
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
