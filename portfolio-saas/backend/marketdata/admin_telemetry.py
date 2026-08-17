"""Read-only operational context for Django admin and the React /ops console.

Request paths never COUNT(*) warehouse tables. Row counts come from pg_class
reltuples; bytes from pg_total_relation_size; history from OperationalMetricSnapshot.

A TimescaleDB hypertable is the exception on both counts: its parent relation is
an empty shell and the rows live in per-chunk children, so those two lookups
report 0 rows and ~32 kB for a table holding tens of millions of rows. See
`_hypertables()` -- partitioned tables are measured with `approximate_row_count`
and `hypertable_size` instead, which are still estimates rather than COUNT(*).
"""
from __future__ import annotations

import math
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import User
from config.health import PRICE_STALE_AFTER
from portfolio.models import Account, Holding, LedgerEntry, Price, Snapshot

from .models import (
    ArchiveFetchState,
    CodalAnnouncement,
    CodalArtifact,
    CodalReport,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketInstrument,
    OperationalMetricSnapshot,
    RejectedRecord,
    ShareholderRecord,
    StockTransactionTick,
    SymbolIntegrity,
    SystemLogEvent,
    WorkflowRun,
)
from .coverage_report import build_coverage_report
from .quota import get_quota_status


DATABASE_MODELS = {
    "accounts": ("Portfolios", Account),
    "holdings": ("Holdings", Holding),
    "ledger_entries": ("Ledger entries", LedgerEntry),
    "prices": ("Live prices", Price),
    "snapshots": ("Portfolio snapshots", Snapshot),
    "market_instruments": ("Market instruments", MarketInstrument),
    "stock_history_rows": ("Stock history", DailyStockHistory),
    "gold_currency_rows": ("Gold / FX history", GoldCurrencyHistory),
    "candles": ("Market candles", MarketCandle),
    "stock_transaction_ticks": ("Transaction ticks", StockTransactionTick),
    "announcements": ("Codal announcements", CodalAnnouncement),
    "shareholders": ("Shareholders", ShareholderRecord),
}

OVERVIEW_CACHE_KEY = "admin_ops_overview"
OVERVIEW_CACHE_TTL = 15


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _hypertables():
    """Names of tables that are TimescaleDB hypertables, or an empty set.

    A hypertable's parent relation holds NO rows and almost no bytes: the data
    lives in per-chunk child tables under _timescaledb_internal. So `reltuples`
    and `pg_total_relation_size` on the parent -- which is what this module used
    to read for every table -- report 0 rows and ~32 kB for a table holding 41.6M
    rows and 2.8 GB. The Ops page showed exactly that after the tick table was
    partitioned: the data was intact, the measurement was looking in the wrong
    place.
    """
    cache_key = "admin_hypertable_names"
    cached = cache.get(cache_key)
    if cached is not None:
        return set(cached)
    names = set()
    try:
        # A savepoint, not a bare cursor: on plain Postgres (no timescaledb
        # extension) this SELECT fails, and an unguarded failure inside an
        # outer atomic block (e.g. a test's transaction) poisons every query
        # after it with InFailedSqlTransaction, not just this one. atomic()
        # rolls back to the savepoint on error, containing the damage here.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT hypertable_name FROM timescaledb_information.hypertables"
                )
                names = {row[0] for row in cursor.fetchall()}
    except Exception:
        # No timescaledb extension here (plain Postgres, CI): every table is a
        # normal relation and the plain lookups below are already correct.
        names = set()
    cache.set(cache_key, sorted(names), 300)
    return names


def get_cached_db_counts():
    cached = cache.get("db_counts_diagnostics")
    if cached is not None:
        return cached

    hypertables = _hypertables()
    counts = {}
    with connection.cursor() as cursor:
        for key, (_label, model) in DATABASE_MODELS.items():
            table_name = model._meta.db_table
            try:
                if table_name in hypertables:
                    # Timescale's own estimator walks the chunks. Summing chunk
                    # reltuples by hand is NOT equivalent: a compressed chunk
                    # reports its compressed row count, which undercounts by
                    # roughly the compression ratio (9.6M vs the real 41.6M here).
                    cursor.execute("SELECT approximate_row_count(%s)", [table_name])
                else:
                    cursor.execute(
                        "SELECT reltuples FROM pg_class WHERE relname = %s", [table_name]
                    )
                row = cursor.fetchone()
                counts[key] = int(row[0]) if row and row[0] is not None and row[0] >= 0 else model.objects.count()
            except Exception:
                counts[key] = model.objects.count()

    cache.set("db_counts_diagnostics", counts, 10)
    return counts


def get_cached_table_bytes():
    cached = cache.get("db_table_bytes_diagnostics")
    if cached is not None:
        return cached

    hypertables = _hypertables()
    sizes = {}
    with connection.cursor() as cursor:
        for key, (_label, model) in DATABASE_MODELS.items():
            table_name = model._meta.db_table
            try:
                if table_name in hypertables:
                    # Includes every chunk plus its indexes; the parent alone is
                    # an empty shell.
                    cursor.execute("SELECT hypertable_size(%s)", [table_name])
                else:
                    cursor.execute("SELECT pg_total_relation_size(%s)", [table_name])
                row = cursor.fetchone()
                sizes[key] = int(row[0]) if row and row[0] is not None else 0
            except Exception:
                sizes[key] = 0
    cache.set("db_table_bytes_diagnostics", sizes, 30)
    return sizes


def get_database_bytes():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _codal_volume_bytes():
    total = CodalArtifact.objects.aggregate(value=Sum("size_bytes"))["value"] or 0
    return int(total)


def _database_history():
    cached = cache.get("admin_database_history")
    if cached is not None:
        return cached

    since = timezone.now() - timedelta(days=90)
    hourly = list(
        OperationalMetricSnapshot.objects.filter(
            captured_at__gte=since,
            captured_at__minute=0,
        )
        .order_by("captured_at")
        .values("captured_at", "database_counts", "table_bytes", "disk")
    )
    latest = (
        OperationalMetricSnapshot.objects.filter(captured_at__gte=since)
        .order_by("-captured_at")
        .values("captured_at", "database_counts", "table_bytes", "disk")
        .first()
    )
    if latest and (not hourly or hourly[-1]["captured_at"] != latest["captured_at"]):
        hourly.append(latest)

    step = max(1, math.ceil(len(hourly) / 180))
    sampled = hourly[::step]
    if hourly and sampled[-1] != hourly[-1]:
        sampled.append(hourly[-1])
    result = [
        {
            "captured_at": row["captured_at"].isoformat(),
            "counts": row["database_counts"] or {},
            "bytes": row["table_bytes"] or {},
            "disk": row["disk"] or {},
        }
        for row in sampled
    ]
    cache.set("admin_database_history", result, 300)
    return result


def _workflow_history():
    cached = cache.get("admin_workflow_history")
    if cached is not None:
        return cached

    since = timezone.now() - timedelta(days=30)
    rows = (
        WorkflowRun.objects.filter(created_at__gte=since)
        .annotate(day=TruncDate("created_at"))
        .values("day", "outcome")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    by_day = {}
    for row in rows:
        day = row["day"].isoformat()
        by_day.setdefault(day, {"day": day})[row["outcome"]] = row["count"]
    result = list(by_day.values())
    cache.set("admin_workflow_history", result, 300)
    return result


def _error_code_breakdown(since=None):
    since = since or timezone.now() - timedelta(hours=24)
    rows = (
        WorkflowRun.objects.filter(created_at__gte=since)
        .exclude(error_code="")
        .values("error_code")
        .annotate(count=Count("id"))
        .order_by("-count")[:12]
    )
    return [{"error_code": row["error_code"], "count": row["count"]} for row in rows]


def _last_success_by_workflow():
    rows = (
        WorkflowRun.objects.filter(outcome=WorkflowRun.Outcome.SUCCESS)
        .values("workflow")
        .annotate(latest=Max("created_at"))
        .order_by("workflow")
    )
    return {row["workflow"]: _iso(row["latest"]) for row in rows}


def _workers():
    cached = cache.get("admin_worker_status")
    if cached is not None:
        return cached
    try:
        from config.celery import app as celery_app

        inspect = celery_app.control.inspect(timeout=0.5)
        ping = inspect.ping()
        if not ping:
            result = {"status": "critical", "items": {}, "message": "No active workers detected."}
            cache.set("admin_worker_status", result, 10)
            return result
        result = {
            "status": "healthy",
            "items": {name: {"status": "online"} for name in ping},
            "message": "",
        }
        cache.set("admin_worker_status", result, 30)
        return result
    except Exception as exc:
        result = {"status": "unknown", "items": {}, "message": str(exc)}
        cache.set("admin_worker_status", result, 10)
        return result


def _queues():
    try:
        from redis import Redis

        broker = Redis.from_url(settings.CELERY_BROKER_URL)
        depths = {queue: broker.llen(queue) for queue in ("live", "archive", "codal")}
        status = "degraded" if max(depths.values(), default=0) > settings.QUEUE_BACKLOG_THRESHOLD else "healthy"
        return {"status": status, "depths": depths, "message": ""}
    except Exception as exc:
        return {"status": "unknown", "depths": {}, "message": str(exc)}


def _archive():
    summary = ArchiveFetchState.objects.aggregate(
        total=Count("id"),
        complete=Count("id", filter=Q(verified_complete=True)),
        failed=Count("id", filter=Q(consecutive_failures__gt=0)),
        missing=Sum("missing_rows"),
        known_gaps=Sum("known_gap_rows"),
    )
    for key in ("total", "complete", "failed", "missing", "known_gaps"):
        summary[key] = summary[key] or 0
    threshold = settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD
    summary["pending"] = max(0, summary["total"] - summary["complete"])
    summary["wedged"] = ArchiveFetchState.objects.filter(
        consecutive_failures__gte=threshold
    ).count()
    summary["progress_pct"] = round(
        summary["complete"] / summary["total"] * 100, 1
    ) if summary["total"] else 0

    grouped = {
        row["endpoint"]: row
        for row in ArchiveFetchState.objects.values("endpoint").annotate(
            total=Count("id"),
            complete=Count("id", filter=Q(verified_complete=True)),
            failed=Count("id", filter=Q(consecutive_failures__gt=0)),
            missing=Sum("missing_rows"),
        )
    }
    summary["categories"] = []
    for endpoint, label in ArchiveFetchState.Endpoint.choices:
        row = grouped.get(endpoint, {})
        total = row.get("total", 0) or 0
        complete = row.get("complete", 0) or 0
        summary["categories"].append({
            "endpoint": endpoint,
            "label": label,
            "total": total,
            "complete": complete,
            "pending": max(0, total - complete),
            "failed": row.get("failed", 0) or 0,
            "missing": row.get("missing", 0) or 0,
            "progress_pct": round(complete / total * 100, 1) if total else 0,
        })
    return summary


def _tick_coverage():
    qs = ArchiveFetchState.objects.filter(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
    )
    summary = qs.aggregate(
        total=Count("id"),
        complete=Count("id", filter=Q(verified_complete=True)),
        oldest=Min("first_date"),
        newest=Max("last_date"),
    )
    total = summary["total"] or 0
    complete = summary["complete"] or 0
    return {
        "window_days": settings.MARKETDATA_TICK_WINDOW_DAYS,
        "total": total,
        "complete": complete,
        "progress_pct": round(complete / total * 100, 1) if total else 0,
        "oldest": summary["oldest"] or "",
        "newest": summary["newest"] or "",
    }


def _codal_status():
    counts = {
        row["status"]: row["c"]
        for row in CodalReport.objects.values("status").annotate(c=Count("id"))
    }
    last_success = (
        CodalReport.objects.filter(status=CodalReport.Status.PARSED)
        .order_by("-extracted_at")
        .values_list("extracted_at", flat=True)
        .first()
    )
    since = timezone.now() - timedelta(hours=24)
    recent = WorkflowRun.objects.filter(workflow="codal_extract", created_at__gte=since)
    recent_total = recent.count()
    blocked = recent.filter(outcome=WorkflowRun.Outcome.BLOCKED_NETWORK).count()
    return {
        "enabled": False,
        "status_counts": counts,
        "last_success": _iso(last_success),
        "blocked_network_24h": blocked,
        "extract_runs_24h": recent_total,
        "blocked_network_rate_24h": round(blocked / recent_total, 4) if recent_total else 0,
        "artifact_bytes": _codal_volume_bytes(),
    }


def _workflow_15m():
    since = timezone.now() - timedelta(minutes=15)
    qs = WorkflowRun.objects.filter(created_at__gte=since)
    outcomes = {
        row["outcome"]: row["c"]
        for row in qs.values("outcome").annotate(c=Count("id"))
    }
    sums = qs.aggregate(accepted=Sum("rows_accepted"), rejected=Sum("rows_rejected"))
    by_table = list(
        qs.exclude(destination_table="")
        .values("destination_table")
        .annotate(accepted=Sum("rows_accepted"), runs=Count("id"))
        .order_by("-accepted")[:12]
    )
    return {
        "outcomes": outcomes,
        "rows_accepted": int(sums["accepted"] or 0),
        "rows_rejected": int(sums["rejected"] or 0),
        "total_runs": sum(outcomes.values()),
        "by_destination": by_table,
    }


def _fill_rates(counts, table_bytes):
    now = timezone.now()
    snap_24h = (
        OperationalMetricSnapshot.objects.filter(captured_at__lte=now - timedelta(hours=24))
        .order_by("-captured_at")
        .first()
    )
    snap_7d = (
        OperationalMetricSnapshot.objects.filter(captured_at__lte=now - timedelta(days=7))
        .order_by("-captured_at")
        .first()
    )
    rates = {}
    for key in DATABASE_MODELS:
        prev24 = (snap_24h.database_counts or {}).get(key) if snap_24h else None
        prev7 = (snap_7d.database_counts or {}).get(key) if snap_7d else None
        bytes24 = (snap_24h.table_bytes or {}).get(key) if snap_24h else None
        bytes7 = (snap_7d.table_bytes or {}).get(key) if snap_7d else None
        current = counts.get(key, 0)
        current_bytes = table_bytes.get(key, 0)
        rates[key] = {
            "count": current,
            "bytes": current_bytes,
            "delta_24h": None if prev24 is None else current - prev24,
            "delta_7d": None if prev7 is None else current - prev7,
            "bytes_delta_24h": None if bytes24 is None else current_bytes - bytes24,
            "bytes_delta_7d": None if bytes7 is None else current_bytes - bytes7,
        }
    return rates


def project_disk(disk=None, history=None):
    """Days until 80% of the VPS disk budget, from snapshot history — never COUNT(*)."""
    budget_gb = int(getattr(settings, "VPS_DISK_BUDGET_GB", 250))
    budget_bytes = budget_gb * 1024 ** 3
    target = int(budget_bytes * 0.80)
    disk = disk or {}
    db_bytes = int(disk.get("database_bytes") or 0)
    codal_bytes = int(disk.get("codal_bytes") or 0)
    used = db_bytes + codal_bytes
    history = history if history is not None else _database_history()
    growth_per_day = None
    if len(history) >= 2:
        first = history[0]
        last = history[-1]
        first_used = int((first.get("disk") or {}).get("database_bytes") or 0) + int(
            (first.get("disk") or {}).get("codal_bytes") or 0
        )
        last_used = int((last.get("disk") or {}).get("database_bytes") or 0) + int(
            (last.get("disk") or {}).get("codal_bytes") or 0
        )
        t0 = parse_datetime(first["captured_at"]) if isinstance(first["captured_at"], str) else first["captured_at"]
        t1 = parse_datetime(last["captured_at"]) if isinstance(last["captured_at"], str) else last["captured_at"]
        if t0 and t1:
            days = max((t1 - t0).total_seconds() / 86400, 1 / 24)
            growth_per_day = (last_used - first_used) / days
    days_to_80pct = None
    if growth_per_day and growth_per_day > 0 and used < target:
        days_to_80pct = round((target - used) / growth_per_day, 1)
    elif used >= target:
        days_to_80pct = 0
    return {
        "budget_gb": budget_gb,
        "database_bytes": db_bytes,
        "codal_bytes": codal_bytes,
        "used_bytes": used,
        "target_80_bytes": target,
        "growth_bytes_per_day": None if growth_per_day is None else round(growth_per_day),
        "days_to_80pct": days_to_80pct,
        "alert": days_to_80pct is not None and days_to_80pct < 30,
    }


def collect_metric_payload():
    """Point-in-time payload stored on OperationalMetricSnapshot (worker path)."""
    counts = get_cached_db_counts()
    table_bytes = get_cached_table_bytes()
    disk = {
        "database_bytes": get_database_bytes(),
        "codal_bytes": _codal_volume_bytes(),
    }
    quota = get_quota_status()
    quota_slim = {
        "limit": quota.get("limit"),
        "used": quota.get("used"),
        "remaining_daily": quota.get("remaining_daily"),
        "archive_used": quota.get("archive_used"),
        "live_used": quota.get("live_used"),
    }
    return {
        "database_counts": counts,
        "table_bytes": table_bytes,
        "archive": _archive(),
        "quota": quota_slim,
        "queues": _queues(),
        "codal_status": _codal_status(),
        "workflow_15m": _workflow_15m(),
        "workers": _workers(),
        "disk": disk,
    }


def get_admin_telemetry_context():
    """Build the staff-only operational dashboard context."""
    now = timezone.now()
    checks = {"database": False, "cache": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone() == (1,)
    except Exception:
        pass
    try:
        cache.set("_admin_ops_ping", "1", timeout=10)
        checks["cache"] = cache.get("_admin_ops_ping") == "1"
    except Exception:
        pass

    latest_price = Price.objects.aggregate(value=Max("fetched_at"))["value"]
    price_age = None if latest_price is None else now - latest_price
    price_status = "stale" if price_age is None or price_age > PRICE_STALE_AFTER else "fresh"
    workers = _workers()
    queues = _queues()
    overall = "healthy"
    if not all(checks.values()) or workers["status"] == "critical":
        overall = "critical"
    elif price_status == "stale" or queues["status"] != "healthy" or workers["status"] != "healthy":
        overall = "degraded"

    counts = get_cached_db_counts()
    table_bytes = get_cached_table_bytes()
    fill_rates = _fill_rates(counts, table_bytes)
    history = _database_history()
    disk = project_disk(
        {"database_bytes": get_database_bytes(), "codal_bytes": _codal_volume_bytes()},
        history,
    )
    latest_values = {
        "accounts": Account.objects.aggregate(value=Max("updated_at"))["value"],
        "holdings": Holding.objects.aggregate(value=Max("updated_at"))["value"],
        "ledger_entries": LedgerEntry.objects.aggregate(value=Max("created_at"))["value"],
        "prices": latest_price,
        "snapshots": Snapshot.objects.aggregate(value=Max("timestamp"))["value"],
        "market_instruments": MarketInstrument.objects.aggregate(value=Max("updated_at"))["value"],
        "stock_history_rows": DailyStockHistory.objects.aggregate(value=Max("date"))["value"],
        "gold_currency_rows": GoldCurrencyHistory.objects.aggregate(value=Max("date"))["value"],
        "candles": MarketCandle.objects.aggregate(value=Max("date_time"))["value"],
        "stock_transaction_ticks": StockTransactionTick.objects.aggregate(value=Max("date"))["value"],
        "announcements": CodalAnnouncement.objects.aggregate(value=Max("date_publish"))["value"],
        "shareholders": ShareholderRecord.objects.aggregate(value=Max("date"))["value"],
    }
    database_rows = [
        {
            "key": key,
            "label": label,
            "count": counts.get(key, 0),
            "bytes": table_bytes.get(key, 0),
            "delta_24h": fill_rates[key]["delta_24h"],
            "delta_7d": fill_rates[key]["delta_7d"],
            "latest": latest_values.get(key),
        }
        for key, (label, _model) in DATABASE_MODELS.items()
    ]

    archive = _archive()
    integrity = SymbolIntegrity.objects.aggregate(
        assessed=Count("id"),
        passing=Count("id", filter=Q(passes_gate=True)),
    )
    integrity["failing"] = integrity["assessed"] - integrity["passing"]
    rejected = RejectedRecord.objects.filter(disposition="quarantined").aggregate(
        records=Count("id"), occurrences=Sum("occurrences")
    )
    rejected["occurrences"] = rejected["occurrences"] or 0
    retention = {
        "snapshot_prune_enabled": bool(settings.SNAPSHOT_PRUNE_ENABLED),
        "snapshot_retention_days": settings.SNAPSHOT_RETENTION_DAYS,
        "price_prune_enabled": bool(getattr(settings, "PRICE_PRUNE_ENABLED", False)),
        "price_retention_days": int(getattr(settings, "PRICE_RETENTION_DAYS", 14)),
    }

    return {
        "generated_at": now,
        "overall_status": overall,
        "checks": checks,
        "price_feed": {
            "status": price_status,
            "latest": latest_price,
            "age_seconds": None if price_age is None else int(price_age.total_seconds()),
            "threshold_seconds": int(PRICE_STALE_AFTER.total_seconds()),
        },
        "workers": workers,
        "queues": queues,
        "archive": archive,
        "tick_coverage": _tick_coverage(),
        "quota": get_quota_status(),
        "codal": _codal_status(),
        "disk": disk,
        "fill_rates": fill_rates,
        "retention": retention,
        "workflow_15m": _workflow_15m(),
        "error_codes_24h": _error_code_breakdown(),
        "last_success": _last_success_by_workflow(),
        "users": {
            "total": User.objects.count(),
            "staff": User.objects.filter(is_staff=True).count(),
        },
        "database_rows": database_rows,
        "database_history": history,
        "workflow_history": _workflow_history(),
        "recent_workflows": list(WorkflowRun.objects.values(
            "id", "created_at", "workflow", "endpoint", "symbol", "outcome", "error_code"
        )[:8]),
        "recent_errors": list(SystemLogEvent.objects.filter(
            level__in=("WARNING", "ERROR", "CRITICAL")
        ).values("id", "timestamp", "level", "service", "message")[:8]),
        "integrity": integrity,
        "rejected": rejected,
    }


def get_ops_overview():
    """JSON-ready overview for /api/admin/overview/ (cached 15s)."""
    cached = cache.get(OVERVIEW_CACHE_KEY)
    if cached is not None:
        return cached

    ctx = get_admin_telemetry_context()
    workers = ctx["workers"]
    items = workers.get("items") or {}
    body = {
        "generated_at": _iso(ctx["generated_at"]),
        "status": ctx["overall_status"],
        "overall_status": ctx["overall_status"],
        "checks": {
            "database": {"ok": ctx["checks"]["database"], "status": "healthy" if ctx["checks"]["database"] else "critical"},
            "cache": {"ok": ctx["checks"]["cache"], "status": "healthy" if ctx["checks"]["cache"] else "critical"},
            "price_feed": {
                "status": ctx["price_feed"]["status"],
                "latest_price_age_seconds": ctx["price_feed"]["age_seconds"],
                "threshold_seconds": ctx["price_feed"]["threshold_seconds"],
            },
        },
        "price_feed": {**ctx["price_feed"], "latest": _iso(ctx["price_feed"]["latest"])},
        "workers": {
            "status": workers.get("status"),
            "summary": {
                "online": len(items),
                "active_tasks": 0,
                "reserved_tasks": 0,
            },
            "detail": workers,
        },
        "queues": ctx["queues"],
        "queue": {
            "status": ctx["queues"].get("status"),
            "depths": ctx["queues"].get("depths") or {},
            "depth": sum((ctx["queues"].get("depths") or {}).values()),
        },
        "archive": {
            **ctx["archive"],
            "complete_states": ctx["archive"]["complete"],
            "total_states": ctx["archive"]["total"],
            "failed_states": ctx["archive"]["failed"],
        },
        "tick_coverage": ctx["tick_coverage"],
        "quota": ctx["quota"],
        "codal": ctx["codal"],
        "disk": ctx["disk"],
        "fill_rates": ctx["fill_rates"],
        "retention": ctx["retention"],
        "workflow_15m": ctx["workflow_15m"],
        "error_codes_24h": ctx["error_codes_24h"],
        "last_success": ctx["last_success"],
        "users": ctx["users"],
        "database_counts": {"approximate": True, "counts": get_cached_db_counts()},
        "database_rows": [
            {**row, "latest": _iso(row["latest"]) if not isinstance(row["latest"], str) else row["latest"]}
            for row in ctx["database_rows"]
        ],
        "database_history": ctx["database_history"],
        "workflow_history": ctx["workflow_history"],
        "integrity": ctx["integrity"],
        "rejected": ctx["rejected"],
        "workflows_24h": _workflows_24h(),
    }
    body["coverage"] = build_coverage_report(database_rows=body["database_rows"])
    cache.set(OVERVIEW_CACHE_KEY, body, OVERVIEW_CACHE_TTL)
    return body


def _workflows_24h():
    since = timezone.now() - timedelta(hours=24)
    qs = WorkflowRun.objects.filter(created_at__gte=since)
    outcomes = {
        row["outcome"]: row["c"]
        for row in qs.values("outcome").annotate(c=Count("id"))
    }
    sums = qs.aggregate(accepted=Sum("rows_accepted"), rejected=Sum("rows_rejected"))
    return {
        "outcomes": outcomes,
        "rows_accepted": int(sums["accepted"] or 0),
        "rows_rejected": int(sums["rejected"] or 0),
        "total_runs": sum(outcomes.values()),
    }


def invalidate_ops_cache():
    cache.delete_many([
        OVERVIEW_CACHE_KEY,
        "admin_database_history",
        "admin_workflow_history",
        "db_counts_diagnostics",
        "db_table_bytes_diagnostics",
        "admin_worker_status",
    ])
