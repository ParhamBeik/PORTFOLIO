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

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("portfolio")
TEHRAN = ZoneInfo("Asia/Tehran")
# Read all CELERY_* settings from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Discover tasks.py in each installed app (portfolio.tasks, marketdata.tasks).
app.autodiscover_tasks()
# portfolio/services/maintenance.py and best_overall.py aren't under a
# `tasks.py` module, so each needs its own discovery pass (still lazy --
# resolved after Django apps are ready, same as the call above, not at this
# import time).
app.autodiscover_tasks(["portfolio"], related_name="services.maintenance")
app.autodiscover_tasks(["portfolio"], related_name="services.best_overall")

# Global reliability defaults. Per-task retry policy (autoretry_for) belongs in
# the individual tasks (e.g. portfolio/tasks.py), not here.
app.conf.update(
    timezone=str(TEHRAN),
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
        "marketdata.tasks.recent_history_refresh": {"queue": "live"},
        "marketdata.tasks.archive_maintenance": {"queue": "live"},
        "marketdata.tasks.capture_operational_metrics": {"queue": "live"},
        "marketdata.tasks.*": {"queue": "archive"},
        "portfolio.tasks.*": {"queue": "live"},
    },
)

# Crontab times are declared directly in Tehran local time. ZoneInfo owns any
# future timezone-policy changes; no hand-converted UTC hours are duplicated.
app.conf.beat_schedule = {
    # Beat ticks every minute; the task itself enforces the real cadence, which
    # depends on whether the TSE is open (see marketdata/market_state.py).
    "fetch-prices-every-minute": {
        "task": "portfolio.tasks.fetch_and_publish",
        "schedule": 60.0,
    },
    "marketdata-archive-every-minute": {
        "task": "marketdata.tasks.archive_tick",
        "schedule": 15.0,
    },
    "marketdata-recent-history-after-close": {
        "task": "marketdata.tasks.recent_history_refresh",
        "schedule": crontab(day_of_week="0-4", hour=14, minute=35),
    },
    "marketdata-low-rate-maintenance": {
        "task": "marketdata.tasks.archive_maintenance",
        "schedule": crontab(hour=4, minute=10),
    },

    # Symbol fundamentals refresh weekly on Friday (TSE closed, API quiet).
    "marketdata-weekly-meta": {
        "task": "marketdata.tasks.weekly_metadata_sync",
        "schedule": crontab(day_of_week=4, hour=9, minute=30),
    },
    "marketdata-daily-catalog": {
        "task": "marketdata.tasks.catalog_sync",
        "schedule": crontab(hour=3, minute=40),
    },
    # 24/7 Gold/Currency/Crypto midnight EOD aggregation.
    "aggregate-gold-currency-daily-2359": {
        "task": "marketdata.tasks.aggregate_daily_gold_currency_history",
        "schedule": crontab(hour=23, minute=59),
    },
    # Stock session market-close aggregation.
    "aggregate-stock-daily-market-close": {
        "task": "marketdata.tasks.aggregate_daily_stock_history",
        "schedule": crontab(hour=17, minute=0),
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
    # Snapshot retention. No-op unless SNAPSHOT_PRUNE_ENABLED=1 (see
    # portfolio/services/maintenance.py) -- deleting rows needs explicit sign-off.
    "prune-snapshots-nightly": {
        "task": "portfolio.services.maintenance.prune_snapshots",
        "schedule": crontab(hour=2, minute=0),
    },
    "prune-prices-nightly": {
        "task": "portfolio.services.maintenance.prune_prices",
        "schedule": crontab(hour=2, minute=20),
    },
    # "Best Possible Portfolio Overall" precompute: 4 windows x 2 scenarios,
    # market-wide. Runs after nightly-asset-metrics (01:00) so AssetMetricSnapshot
    # (top performers by class) is fresh when this reads the same warehouse data.
    "best-overall-snapshots-nightly": {
        "task": "portfolio.services.best_overall.run_best_overall_snapshots",
        "schedule": crontab(hour=2, minute=30),
    },
}


@before_task_publish.connect
def attach_request_id(headers=None, **kwargs):
    from config.request_context import get_request_id

    if headers is not None:
        headers["x-request-id"] = get_request_id()


@task_prerun.connect
def restore_request_id(task=None, **kwargs):
    from config.request_context import request_id_var

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


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Celery request: {self.request!r}")
