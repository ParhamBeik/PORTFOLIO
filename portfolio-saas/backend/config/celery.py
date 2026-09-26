"""Celery app + static beat schedule.

Real-time push is one Celery beat tick (every 2 min) -> fetch_and_publish, which
fetches global prices, persists them, and broadcasts the price map over Redis
pub/sub to every SSE client. The schedule is static (version-controlled) rather
than DB-backed; a single beat instance is plenty for v1 and the scheduler stays
swappable if that changes.
"""
import os
from zoneinfo import ZoneInfo

from celery import Celery
from celery.signals import before_task_publish, task_prerun
from celery.schedules import crontab
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("portfolio")
TEHRAN = ZoneInfo("Asia/Tehran")
# Read all CELERY_* settings from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Discover tasks.py in each installed app (portfolio.tasks, marketdata.tasks).
# Every task lives in one of those two modules; there is no second discovery pass.
app.autodiscover_tasks()
import marketdata.burst_probes  # noqa: F401 -- not in tasks.py; beat must register it

# Global reliability defaults. Per-task retry policy (autoretry_for) belongs in
# the individual tasks (e.g. portfolio/tasks.py), not here.
app.conf.update(
    # `timezone` is deliberately NOT set here. It is declared once, as
    # CELERY_TIMEZONE in config/settings.py, because config_from_object resolves
    # after this update() and silently wins -- setting it in both places is what
    # let the schedule run on UTC while this file claimed Tehran.
    task_acks_late=True,  # ack after the task runs, not on receipt: a killed worker redelivers the task
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # pair with acks_late so one worker doesn't hoard several long tasks
    result_expires=3600,  # results aren't polled here (fire-and-forget beat schedule); don't let them pile up in Redis
    # Nightly integrity/validation/metrics can run longer than five minutes on a
    # cold restart. Redis redelivers unacked messages after visibility_timeout,
    # which duplicated those jobs onto the capped archive queue and starved
    # run_archive_state (workers unhealthy, pending stuck above the claim limit).
    broker_transport_options={"visibility_timeout": 3600},
    task_default_queue="live",
    # Lightweight producers stay on live so their own backlog cannot starve
    # dispatch control. The work they create still runs on its dedicated queue.
    task_routes={
        "marketdata.tasks.archive_tick": {"queue": "live"},
        "marketdata.tasks.archive_maintenance": {"queue": "live"},
        # Not the archive queue: this is what tells the archive how much it may
        # spend, and it is useless if it queues behind the backlog it governs.
        "marketdata.tasks.reconcile_quota_meters": {"queue": "live"},
        # A paused archive worker must not silence the check that reports its
        # backlog and other operational failures.
        "marketdata.tasks.operational_health_check": {"queue": "live"},
        "marketdata.tasks.capture_operational_metrics": {"queue": "live"},
        "marketdata.tasks.capture_derivative_snapshots": {"queue": "live"},
        "marketdata.tasks.capture_market_snapshots": {"queue": "live"},
        # Routed unconditionally, and that is deliberate: dropping these entries
        # while Codal is off does NOT stop them, it hands them to the
        # `marketdata.tasks.*` glob below and runs document work on the archive
        # worker. Parking a straggler on a queue nobody consumes is the harmless
        # outcome; executing it on the wrong worker is not. Nothing enqueues
        # these while disabled anyway -- both dispatch paths and the task bodies
        # check the flag.
        "marketdata.tasks.extract_codal_report": {"queue": "codal"},
        "marketdata.tasks.queue_codal_extractions": {"queue": "live"},
        "marketdata.tasks.*": {"queue": "archive"},
        "portfolio.tasks.*": {"queue": "live"},
    },
)

# Crontab times are declared directly in Tehran local time. ZoneInfo owns any
# future timezone-policy changes; no hand-converted UTC hours are duplicated.
app.conf.beat_schedule = {
    "write-daily-net-worth": {
        "task": "portfolio.tasks.write_daily_net_worth_snapshot",
        "schedule": crontab(hour=0, minute=1),
    },
    # Beat ticks every 20s; the task itself enforces the real cadence, which
    # depends on whether the TSE is open (see marketdata/market_state.py).
    "fetch-prices-every-minute": {
        "task": "portfolio.tasks.fetch_and_publish",
        "schedule": 20.0,
    },
    "marketdata-archive-every-minute": {
        "task": "marketdata.tasks.archive_tick",
        "schedule": 15.0,
    },
    # No post-close refresh entry: the daily catch-up is no longer a privileged
    # job. `claim_archive_batch` surfaces completed states by longest-since-success
    # once the backlog has taken its share, and `next_post_close` scheduling
    # already makes them due at the right moment. Held symbols' same-day close
    # comes from the live lane, which is reserved first.
    "marketdata-low-rate-maintenance": {
        "task": "marketdata.tasks.archive_maintenance",
        "schedule": crontab(hour=4, minute=10),
    },
    # Two weekly probes take TSETMC slots at 00:05, while the midnight burst
    # still has remaining requests. 04:10 is leftover-only and usually empty.
    "marketdata-burst-probes": {
        "task": "marketdata.burst_probes.claim_burst_probes",
        "schedule": crontab(hour=0, minute=5),
    },

    # Symbol fundamentals, DAILY rather than weekly. One request per symbol
    # against the 200/day OTHER bucket and ~1,900 symbols means a weekly run can
    # cover at most 200 of them, so 90% of the universe was permanently stale --
    # 86 of 1,969 symbols had a market cap. The task is resumable (stalest
    # first, clean stop on quota), so a daily run walks the whole universe in
    # ~10 days and then keeps it rolling. The OTHER bucket is otherwise idle:
    # 51 of 200 used on the day this was measured.
    "marketdata-daily-meta": {
        "task": "marketdata.tasks.sync_symbol_metadata",
        "schedule": crontab(hour=9, minute=30),
    },
    "marketdata-daily-catalog": {
        "task": "marketdata.tasks.catalog_sync",
        "schedule": crontab(hour=3, minute=40),
    },
    "capture-derivative-snapshots": {
        "task": "marketdata.tasks.capture_derivative_snapshots",
        "schedule": 300.0,
    },
    # Crypto/commodity/ETF NAV: previously-registered, never-called endpoints.
    # Every few minutes is enough -- these feed a daily OHLC bar, not the
    # 2-minute held-asset price loop.
    "capture-market-snapshots": {
        "task": "marketdata.tasks.capture_market_snapshots",
        "schedule": 300.0,
    },
    # After the day's snapshots exist, distill them into MarketDailyBar rows.
    "aggregate-market-daily-bars": {
        "task": "marketdata.tasks.aggregate_market_daily_bars_task",
        "schedule": crontab(hour=1, minute=40),
    },
    # Roll the day's live Price ticks into one DailyPriceAverage row per asset.
    "aggregate-daily-price-averages": {
        "task": "portfolio.tasks.aggregate_daily_price_averages",
        "schedule": crontab(hour=23, minute=59),
    },
    # Nightly data integrity gate checks at Tehran midnight.
    "nightly-data-integrity": {
        "task": "marketdata.tasks.nightly_data_integrity",
        "schedule": crontab(hour=0, minute=0),
    },
    "nightly-series-validation": {
        "task": "marketdata.tasks.nightly_series_validation",
        "schedule": crontab(hour=0, minute=30),
    },
    "nightly-asset-metrics": {
        "task": "marketdata.tasks.nightly_asset_metrics",
        "schedule": crontab(hour=1, minute=0),
    },
    # Reads the same returns panel as nightly-asset-metrics, so it runs after it:
    # the panel is cached, and the two should describe the same series rather
    # than straddle a mid-run warehouse write.
    "nightly-asset-signals": {
        "task": "marketdata.tasks.nightly_asset_signals",
        "schedule": crontab(hour=1, minute=20),
    },
    "operational-health-every-15-minutes": {
        "task": "marketdata.tasks.operational_health_check",
        "schedule": crontab(minute="*/15"),
    },
    "capture-operational-metrics-every-15-minutes": {
        "task": "marketdata.tasks.capture_operational_metrics",
        "schedule": crontab(minute="*/15"),
    },
    # The workflow ledger enforces its own 30-day window. Without this it grows
    # forever, which is the exact failure it was built to replace.
    "prune-workflow-runs-nightly": {
        "task": "marketdata.tasks.prune_workflow_runs",
        "schedule": crontab(hour=3, minute=10),
    },
    # Only 2 of the audit's 9 checks ran on a schedule; the full sweep ran only
    # when a human remembered. Read-only -- it writes a manifest, never data.
    "weekly-warehouse-audit": {
        "task": "marketdata.tasks.weekly_warehouse_audit",
        "schedule": crontab(day_of_week=5, hour=5, minute=0),
    },
    "prune-prices-nightly": {
        "task": "portfolio.tasks.prune_prices",
        "schedule": crontab(hour=2, minute=20),
    },
    # Refresh tokens that have already expired. Unlike the two above this needs
    # no enable flag: the rows it deletes are credentials the auth layer already
    # refuses, so there is nothing to sign off on. Without it both
    # token_blacklist tables grow for the life of the deployment, because
    # rotation writes a row on every single refresh.
    "prune-expired-refresh-tokens-nightly": {
        "task": "portfolio.tasks.prune_expired_refresh_tokens",
        "schedule": crontab(hour=2, minute=10),
    },
    # "Best Possible Portfolio Overall" precompute: 4 windows x 2 scenarios,
    # market-wide. Runs after nightly-asset-metrics (01:00) so AssetMetricSnapshot
    # (top performers by class) is fresh when this reads the same warehouse data.
    "best-overall-snapshots-nightly": {
        "task": "portfolio.tasks.run_best_overall_snapshots",
        "schedule": crontab(hour=2, minute=30),
    },
    # "Optimal version of my portfolio" precompute sweep: refreshes stale
    # snapshots for active accounts.
    "nightly-my-optimal-sweep": {
        "task": "portfolio.tasks.sweep_my_optimal_snapshots",
        "schedule": crontab(hour=2, minute=45),
    },
}

# Do not poll an unverified account endpoint or create no-op workflow rows.
if os.getenv("MARKETDATA_PANEL_METER_ENABLED", "0") == "1":
    app.conf.beat_schedule["marketdata-read-provider-panel"] = {
        "task": "marketdata.tasks.reconcile_quota_meters",
        "schedule": 300.0,
    }

if settings.CODAL_ENABLED:
    # Sweeper only -- new announcements are queued at ingest time. Frequent
    # because document work costs no provider quota and the backlog is ~74,000;
    # it is bounded by the codal queue depth, not by the clock.
    app.conf.beat_schedule["queue-codal-extractions"] = {
        "task": "marketdata.tasks.queue_codal_extractions",
        "schedule": 300.0,
    }


@before_task_publish.connect
def attach_request_id(headers=None, **kwargs):
    from config.observability import get_request_id

    if headers is not None:
        headers["x-request-id"] = get_request_id()


@task_prerun.connect
def restore_request_id(task=None, **kwargs):
    from config.observability import request_id_var

    request_id = (
        getattr(getattr(task, "request", None), "x-request-id", None)
        or (getattr(getattr(task, "request", None), "headers", None) or {}).get(
            "x-request-id", "-"
        )
    )
    request_id_var.set(request_id)
    from django.conf import settings

    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", request_id)
