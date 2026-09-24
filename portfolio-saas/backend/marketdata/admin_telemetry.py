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
import shutil
from datetime import timedelta

from django.conf import settings
from django.contrib import admin
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Avg, Count, Max, Min, Q, Sum, Value
from django.db.models.functions import NullIf, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import User
from config.health import PRICE_STALE_AFTER
from config.mail import mail_is_deliverable
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
# Matches the `capture_operational_metrics` beat interval, which rebuilds this
# straight after invalidating it. A 15-second TTL on a payload that takes
# multiple seconds to assemble meant essentially every visit was a cache miss
# and every operator paid the full cost -- the entire reason the Ops page felt
# broken. The payload carries `generated_at`, and the view still honours
# `?refresh=1`, so staleness is both visible and escapable.
OVERVIEW_CACHE_TTL = 15 * 60


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _latest_iso(value):
    """One calendar for the "latest data" column, whatever the table keys on.

    Half the tables in `database_rows` are keyed on a real timestamp and half on
    a Jalali date STRING ("1405-06-04"), and passing the string through meant the
    console printed a Jalali year under a Gregorian month name -- "04 Jun 1405" --
    next to genuinely Gregorian rows in the same column. An operator could not
    tell which of two rows was fresher. Jalali dates become the Gregorian instant
    they name, so every value in the field is comparable and the client keeps
    formatting one way.
    """
    if isinstance(value, str):
        from . import jalali

        # Returns None for anything that is not a Jalali date, in which case the
        # string is already Gregorian (or not a date at all) and stands as it is.
        gregorian = jalali.to_gregorian(value)
        return gregorian.isoformat() if gregorian is not None else value
    return _iso(value)


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
        # Deliberately NOT cached. A transient failure used to pin an empty set
        # for five minutes, and every count taken in that window read the
        # hypertable *parent* -- which genuinely holds 0 rows -- so the tick
        # table appeared to collapse to zero and recover. That is exactly the
        # phantom gap seen in the warehouse-growth chart on 2026-08-16/17.
        return set()
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
                # `> 0`, not `>= 0`: a zero estimate is either a table that
                # really is empty -- in which case COUNT(*) is free -- or a
                # measurement that looked in the wrong place, which is what
                # made a 41.6M-row table chart as zero. Never record a zero we
                # have not confirmed.
                estimate = row[0] if row else None
                counts[key] = (
                    int(estimate)
                    if estimate is not None and estimate > 0
                    else model.objects.count()
                )
            except Exception:
                counts[key] = model.objects.count()

    cache.set("db_counts_diagnostics", counts, 10)
    return counts


def _latest_tick_day():
    """Jalali day of the newest transaction tick, without scanning the hypertable.

    `MAX(date)` here cost 9.5 of the Ops overview's 15 seconds. `date` is the
    Jalali varchar, not the `ts` partition column, so Timescale can exclude no
    chunks and decompresses its way through tens of millions of rows; asking for
    `MAX(ts)` instead only brings it to 4.3 s, because the merge still visits
    every chunk. Timescale already records each chunk's time range, and the
    newest chunk's start IS the day the newest ticks belong to -- 23 ms, and the
    same answer. Falls back to the honest scan if the extension is absent.
    """
    import jdatetime
    from django.db import transaction

    try:
        # Savepoint, not a bare try: without the extension this query errors and
        # Postgres refuses every later statement in the same transaction, so
        # catching the Python exception alone would poison the whole request.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT range_start FROM timescaledb_information.chunks "
                "WHERE hypertable_name = %s ORDER BY range_start DESC LIMIT 1",
                [StockTransactionTick._meta.db_table],
            )
            row = cursor.fetchone()
        if row and row[0] is not None:
            # Chunks are one UTC day wide and the session (05:00-09:30 UTC)
            # never straddles that boundary, so the chunk's own start day is
            # the day its ticks belong to.
            return jdatetime.date.fromgregorian(date=row[0].date()).strftime("%Y-%m-%d")
    except Exception:
        pass
    return StockTransactionTick.objects.aggregate(value=Max("date"))["value"]


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
    # 5 minutes, not 30 seconds: hypertable_size() alone costs ~1.8 s because it
    # walks every chunk, and on-disk size does not move meaningfully inside a
    # single ops-page visit. The 30 s window mostly guaranteed each visit paid
    # the full price again.
    cache.set("db_table_bytes_diagnostics", sizes, 300)
    return sizes


def get_database_bytes():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def admin_model_inventory():
    """One cached, read-only index of every Django-admin table for Operations."""
    hypertables = _hypertables()
    rows = []
    with connection.cursor() as cursor:
        for model in admin.site._registry:
            table = model._meta.db_table
            cursor.execute(
                "SELECT reltuples, pg_total_relation_size(oid) "
                "FROM pg_class WHERE oid = to_regclass(%s)", [table]
            )
            result = cursor.fetchone()
            if result is None:
                continue
            count, size = result
            if table in hypertables:
                cursor.execute(
                    "SELECT approximate_row_count(%s), hypertable_size(%s)",
                    [table, table],
                )
                count, size = cursor.fetchone()
            rows.append({
                "app": model._meta.app_label,
                "model": model._meta.verbose_name_plural.title(),
                "table": table,
                "rows_estimated": max(0, int(count)) if count is not None else None,
                "bytes": int(size) if size is not None else None,
                "admin_path": f"/admin/{model._meta.app_label}/{model._meta.model_name}/",
            })
    return sorted(rows, key=lambda row: (row["app"], row["model"]))


def _codal_volume_bytes():
    total = CodalArtifact.objects.aggregate(value=Sum("size_bytes"))["value"] or 0
    return int(total)


# Warehouse and price tables only ever gain rows: nothing in the codebase
# deletes from them (`workflow_retention` prunes WorkflowRun, and the metric
# snapshot task prunes its own table -- neither is charted here). User-owned
# tables are excluded because a person really can delete a portfolio, and a
# genuine deletion must stay visible.
APPEND_ONLY_COUNT_KEYS = frozenset({
    "prices", "snapshots", "market_instruments", "stock_history_rows",
    "gold_currency_rows", "candles", "stock_transaction_ticks",
    "announcements", "shareholders",
})


def _clean_count_history(rows, keys):
    """Make the growth series say only true things.

    Two distinct falsehoods came out of this series, and they need different
    answers:

    1. **A missing measurement is not zero.** 54 of the last 1,294 snapshots
       carry no tick count at all (the collector skipped or timed out), and 4
       recorded a literal 0. The client read an absent key as the number 0, so
       4.5% of points drew a spike to the floor and back on a table that had
       simply grown all day. Absent stays `None` here, and the chart renders a
       break in the line instead of inventing a value.

    2. **A decrease on an append-only table is estimator noise, not data loss.**
       These counts come from `pg_class.reltuples` (and `approximate_row_count`
       for the tick hypertable) rather than COUNT(*), which is what keeps the
       Ops page from scanning 58M rows on every render. Those estimates are
       resampled by autovacuum and wobble by a few percent in both directions:
       the tick count "fell" 59.36M -> 56.25M in one hour today while the table
       only gained rows. For tables that cannot shrink, the running maximum is
       strictly closer to the truth than the raw estimate, so it is what we
       plot. Tables that CAN shrink are passed through untouched.

    ponytail: a running max, not a proper estimator-error model. It is exact
    at every new high (the common case on a growing table) and conservative in
    between. If a warehouse table ever gains a retention policy, drop its key
    from APPEND_ONLY_COUNT_KEYS or real deletions will be silently flattened.
    """
    running = {}
    for row in rows:
        counts = row["counts"]
        for key in keys:
            value = counts.get(key)
            if value is None:
                continue
            value = int(value)
            if key in APPEND_ONLY_COUNT_KEYS:
                value = max(value, running.get(key, 0))
                running[key] = value
            counts[key] = value
        for key in keys:
            counts.setdefault(key, None)
    return rows


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
    result = _clean_count_history(
        [
            {
                "captured_at": row["captured_at"].isoformat(),
                "counts": dict(row["database_counts"] or {}),
                "bytes": row["table_bytes"] or {},
                "disk": row["disk"] or {},
            }
            for row in sampled
        ],
        DATABASE_MODELS.keys(),
    )
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


# Reason codes a run sets when it stood down on purpose. Pacing is the archive
# spreading its budget across the day; neither is a failure, and neither belongs
# in the operator's error distribution.
NON_FAILURE_REASON_CODES = ("archive_paced",)


def _error_code_breakdown(since=None):
    """Failure signatures in the window -- not every run that set a reason code.

    `archive_paced` is the pacer spreading a day's budget across Tehran daytime,
    which is the system working, and it fired 6,350 times in 24h: counting it
    made pacing 99% of the "error distribution" and rounded the signatures an
    operator actually needs -- MarketDataFetchError at 60, QuotaExhausted at 1 --
    to 0.9% and 0%.

    Excluded BY CODE, not by outcome. The obvious filter is "drop SKIPPED runs",
    but `quota_exhausted` (the wallet is genuinely empty) and `origin_unreachable`
    (Codal cannot be reached at all) are also recorded SKIPPED, and dropping the
    outcome would leave the panel silent on the day the pipeline actually died.
    """
    since = since or timezone.now() - timedelta(hours=24)
    rows = (
        WorkflowRun.objects.filter(created_at__gte=since)
        .exclude(error_code="")
        .exclude(error_code__in=NON_FAILURE_REASON_CODES)
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


def outbound_mail():
    """Whether a password-reset email can actually leave this process.

    The one user journey with no in-app fallback. `PasswordResetRequestView`
    answers the same 200 whether or not the send worked -- deliberately, so the
    endpoint cannot be used to enumerate accounts -- and logs the failure. That
    means an unconfigured `EMAIL_HOST` is invisible from the outside: the user
    is told to check their inbox for mail that was never sent, and nothing
    surfaces it but a log line nobody greps. This is what surfaces it.

    Configuration alone is not enough to answer it, which is the lesson this
    function was rewritten for. Checked against production on 2026-09-09:
    `EMAIL_HOST` was `localhost` and `EMAIL_PORT` 25 -- Django's own defaults,
    reached because the deployed revision has no `EMAIL_*` block at all -- and
    nothing listens there. A settings-only check calls that "configured" and
    reports healthy while every reset email fails with ConnectionRefusedError.
    So this also opens a socket.

    The probe is one TCP connect with a short timeout, **cached**, so the Ops
    page does not dial a relay on every render. It is a connect, never a
    handshake or a test message: reachability is the question, and sending mail
    to prove you can send mail is not something a dashboard should do.

    Reported as **degraded**, never critical, and deliberately kept out of the
    `checks` dict that decides `critical` -- the API serves every other request
    perfectly well without mail. Losing account recovery is serious and is not
    an outage.
    """
    backend = str(getattr(settings, "EMAIL_BACKEND", ""))
    host = str(getattr(settings, "EMAIL_HOST", ""))
    port = int(getattr(settings, "EMAIL_PORT", 25) or 25)
    # The configuration half of the question lives in `config.mail` because the
    # sign-in card needs the same rule to decide whether to offer self-service
    # reset. Only the socket probe below is ours alone.
    if "smtp" not in backend:
        # console / locmem / filebased: mail is captured somewhere local. That
        # is correct in dev and in the test suite, and is not a fault.
        return {
            "ok": True,
            "status": "not_delivering",
            "backend": backend,
            "host": "",
            "message": "Mail is captured locally by this backend, not delivered.",
        }
    # A loopback host counts as unset, not as configured -- see `config.mail`,
    # which owns that rule. Reaching here means the backend is SMTP, so this is
    # exactly "an SMTP backend with no usable host".
    if not mail_is_deliverable():
        return {
            "ok": False,
            "status": "unconfigured",
            "backend": backend,
            "host": host,
            "message": (
                "EMAIL_HOST is unset (or still Django's localhost default) with "
                "an SMTP backend, so password-reset mail cannot be sent. Users "
                "asking to reset a password are told to check their inbox and "
                "nothing arrives."
            ),
        }

    cache_key = f"admin_outbound_mail:{host}:{port}"
    probe = cache.get(cache_key)
    if probe is None:
        import socket

        try:
            socket.create_connection((host, port), timeout=3).close()
            probe = {"reachable": True, "error": ""}
        except Exception as exc:
            probe = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
        cache.set(cache_key, probe, 300)

    if not probe["reachable"]:
        return {
            "ok": False,
            "status": "unreachable",
            "backend": backend,
            "host": host,
            "message": (
                f"Cannot reach the mail relay at {host}:{port} "
                f"({probe['error']}), so password-reset mail cannot be sent."
            ),
        }
    return {
        "ok": True,
        "status": "healthy",
        "backend": backend,
        "host": host,
        "message": "",
    }


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
        # NULLIF, because `first_date` is a CharField and 195 states carry "".
        # The empty string sorts before every Jalali date, so a bare Min() made
        # this field permanently blank while the real floor was 1394-07-27.
        oldest=Min(NullIf("first_date", Value(""))),
        newest=Max("last_date"),
        # The seed window, not the lived one: `MARKETDATA_TICK_WINDOW_DAYS` is
        # where a state STARTS, and `promote_priority_tick_windows` grows it
        # from there up to MAX_TICK_WINDOW_DAYS. Reporting the setting told the
        # operator "90 days" while 31 symbols were already at the 12,000-day
        # clamp across 29 distinct widths. Report what the states actually hold.
        window_min=Min("target_window_days"),
        window_max=Max("target_window_days"),
        window_avg=Avg("target_window_days"),
    )
    total = summary["total"] or 0
    complete = summary["complete"] or 0
    return {
        "window_days_seed": settings.MARKETDATA_TICK_WINDOW_DAYS,
        "window_days_min": summary["window_min"] or 0,
        "window_days_max": summary["window_max"] or 0,
        "window_days_avg": round(summary["window_avg"] or 0),
        "total": total,
        "complete": complete,
        "progress_pct": round(complete / total * 100, 1) if total else 0,
        "oldest": summary["oldest"] or "",
        "newest": summary["newest"] or "",
    }


def _codal_status():
    if not settings.CODAL_ENABLED:
        # Skip the status/run aggregates: with the subsystem off they only ever
        # describe a frozen backlog. Artifact bytes stay -- the panel reports
        # how much disk the dormant data still occupies, which is the one
        # number an operator wants while it is switched off.
        return {"enabled": False, "artifact_bytes": _codal_volume_bytes()}
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
    # "extract_codal_report" is the workflow name _ledgered() actually records
    # (marketdata/tasks.py) -- this used to read "codal_extract", a name
    # nothing ever wrote, so this panel silently reported zero runs forever.
    recent = WorkflowRun.objects.filter(workflow="extract_codal_report", created_at__gte=since)
    recent_total = recent.count()
    blocked = recent.filter(outcome=WorkflowRun.Outcome.BLOCKED_NETWORK).count()
    return {
        "enabled": True,
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
    # The breakdown drops runs that wrote to no table and keeps only the top 12,
    # so it cannot add up to the totals beside it -- the panel read "126 runs,
    # 9,849 accepted" over a list summing to 66 and 9,801, with nothing saying
    # where the rest went. Carry the remainder instead of leaving it implied.
    total_runs = sum(outcomes.values())
    accepted = int(sums["accepted"] or 0)
    listed_runs = sum(int(row["runs"] or 0) for row in by_table)
    listed_accepted = sum(int(row["accepted"] or 0) for row in by_table)
    return {
        "outcomes": outcomes,
        "rows_accepted": accepted,
        "rows_rejected": int(sums["rejected"] or 0),
        "total_runs": total_runs,
        "by_destination": by_table,
        "unattributed": {
            "runs": max(0, total_runs - listed_runs),
            "accepted": max(0, accepted - listed_accepted),
        },
    }


def _baseline_snapshot(now, age, tolerance):
    """Newest snapshot at least `age` old, but not so old it answers a different question.

    Only the upper bound used to be set, so the query took the newest row at or
    before the cutoff with nothing saying how far before. Let snapshot capture
    stall over a weekend and the "24h change" silently became the change since
    whenever it last ran -- a figure the operator reads as one day, sized like
    several, on the panel whose whole job is to say whether ingest is moving.
    With no baseline close enough to the age asked for, there is no honest
    answer and the delta is reported as unknown.
    """
    return (
        OperationalMetricSnapshot.objects
        .filter(captured_at__lte=now - age, captured_at__gte=now - age - tolerance)
        .order_by("-captured_at")
        .first()
    )


def _fill_rates(counts, table_bytes):
    now = timezone.now()
    snap_24h = _baseline_snapshot(now, timedelta(hours=24), timedelta(hours=24))
    snap_7d = _baseline_snapshot(now, timedelta(days=7), timedelta(days=7))
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


def filesystem_usage(path="/"):
    """Measured size of the device the data sits on, or None if it cannot be read.

    The backend container's `/` is the host's data device through overlay2, so
    statvfs here reports the real filesystem rather than a container-local view.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def _growth_per_day(history, measure):
    """Bytes/day between the ends of the snapshot series, or None if unmeasurable."""
    if len(history) < 2:
        return None
    first, last = history[0], history[-1]
    first_used = measure(first.get("disk") or {})
    last_used = measure(last.get("disk") or {})
    if first_used is None or last_used is None:
        return None
    t0 = parse_datetime(first["captured_at"]) if isinstance(first["captured_at"], str) else first["captured_at"]
    t1 = parse_datetime(last["captured_at"]) if isinstance(last["captured_at"], str) else last["captured_at"]
    if not t0 or not t1:
        return None
    days = max((t1 - t0).total_seconds() / 86400, 1 / 24)
    return (last_used - first_used) / days


def _own_bytes(disk):
    return int(disk.get("database_bytes") or 0) + int(disk.get("codal_bytes") or 0)


def project_disk(disk=None, history=None):
    """Days until the device holding the database is 80% full.

    This used to project against `VPS_DISK_BUDGET_GB`, a hand-typed 250 copied
    from an aspirational comment in `docker-compose.prod.yml`, and to compare
    that budget against `pg_database_size` -- one database's *logical* size.
    Both halves were wrong, and both in the reassuring direction. Measured in
    production on 2026-09-09: the console reported "257 days until 80% of
    250 GB, no alert" while the device was 147.4 GB with 31.6 GB free -- 11.6
    days from the 80% line and ~45 days from full. A 22x overstatement, on the
    one signal that says the box is about to stop accepting writes.

    The numerator was the deeper error. A filesystem fills from everything on
    it, not from us: three other application stacks share this box, 9 GB is
    Docker images and build cache, and 16 GB is this app's own encrypted
    backups -- which grow in lockstep with the database they back up, so the
    thing being measured was funding its own blind spot. `pg_database_size` saw
    19 GB of the 110 GB in use.

    Growth is taken from whichever series is available and *faster*: the device
    itself once `filesystem_used_bytes` has accumulated in snapshot history, or
    our own database+Codal bytes, which is a lower bound because backups, images
    and the neighbouring stacks are not in that series. For a "when do I run
    out" alarm the conservative estimate is the larger one; underestimating
    growth is precisely the failure this function already had.
    """
    disk = disk or {}
    db_bytes = int(disk.get("database_bytes") or 0)
    codal_bytes = int(disk.get("codal_bytes") or 0)
    history = history if history is not None else _database_history()

    rates = [
        rate
        for rate in (
            _growth_per_day(history, lambda d: d.get("filesystem_used_bytes")),
            _growth_per_day(history, _own_bytes),
        )
        if rate is not None and rate > 0
    ]
    growth_per_day = max(rates) if rates else None

    filesystem = filesystem_usage(getattr(settings, "DISK_USAGE_PATH", "/"))
    total = filesystem["total"] if filesystem else None
    used = filesystem["used"] if filesystem else None
    target = int(total * 0.80) if total else None

    days_to_80pct = None
    if target is not None:
        if used >= target:
            days_to_80pct = 0
        elif growth_per_day:
            days_to_80pct = round((target - used) / growth_per_day, 1)
    return {
        "filesystem_total_bytes": total,
        "filesystem_used_bytes": used,
        "filesystem_free_bytes": filesystem["free"] if filesystem else None,
        "target_80_bytes": target,
        # Our share of the device, kept separate: "how full is the disk" and
        # "how much of it is ours" are different questions and the old code
        # answered the second while labelling it the first.
        "database_bytes": db_bytes,
        "codal_bytes": codal_bytes,
        "used_bytes": db_bytes + codal_bytes,
        "growth_bytes_per_day": None if growth_per_day is None else round(growth_per_day),
        "days_to_80pct": days_to_80pct,
        "alert": days_to_80pct is not None and days_to_80pct < 30,
    }


def collect_metric_payload():
    """Point-in-time payload stored on OperationalMetricSnapshot (worker path)."""
    counts = get_cached_db_counts()
    table_bytes = get_cached_table_bytes()
    # `filesystem_used_bytes` is recorded so the growth rate can eventually come
    # from the device rather than from our own tables, which miss everything
    # else that fills the same disk. Until a few days of it exist,
    # `project_disk` falls back to the database+Codal series.
    filesystem = filesystem_usage(getattr(settings, "DISK_USAGE_PATH", "/"))
    disk = {
        "database_bytes": get_database_bytes(),
        "codal_bytes": _codal_volume_bytes(),
        "filesystem_used_bytes": filesystem["used"] if filesystem else None,
        "filesystem_total_bytes": filesystem["total"] if filesystem else None,
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


def _db_connection_metrics() -> dict:
    """Measure PostgreSQL active connections vs max_connections with threshold alerts."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*), (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') "
                "FROM pg_stat_activity WHERE datname = current_database()"
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                active = int(row[0])
                max_conn = int(row[1]) if row[1] else 100
                utilization = round(active / max_conn * 100, 1)
                conn_status = "healthy"
                if active >= max_conn * 0.8:
                    conn_status = "critical"
                elif active >= max_conn * 0.6:
                    conn_status = "warning"
                return {
                    "ok": conn_status != "critical",
                    "status": conn_status,
                    "active": active,
                    "max": max_conn,
                    "utilization_pct": utilization,
                }
    except Exception:
        pass
    return {
        "ok": True,
        "status": "unknown",
        "active": None,
        "max": None,
        "utilization_pct": None,
    }


def get_admin_telemetry_context():
    """Build the admin-only operational dashboard context."""
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

    db_conn = _db_connection_metrics()
    checks["db_connections"] = db_conn

    latest_price = Price.objects.aggregate(value=Max("fetched_at"))["value"]
    price_age = None if latest_price is None else now - latest_price
    price_status = "stale" if price_age is None or price_age > PRICE_STALE_AFTER else "fresh"
    workers = _workers()
    queues = _queues()
    mail = outbound_mail()
    overall = "healthy"
    if not all(v if isinstance(v, bool) else v.get("ok", True) for v in checks.values()) or workers["status"] == "critical":
        overall = "critical"
    elif (
        price_status == "stale"
        or queues["status"] != "healthy"
        or workers["status"] != "healthy"
        or db_conn["status"] == "warning"
        or not mail["ok"]
    ):
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
        "stock_transaction_ticks": _latest_tick_day(),
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
        "outbound_mail": mail,
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
        "users": _user_domain_health(),
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
    """JSON-ready overview for /api/admin/overview/.

    Two tiers: the expensive warehouse/disk/coverage sections come from a cache
    the metrics worker keeps warm, while `live_health_overlay` recomputes the
    is-it-broken-right-now signals on every request. Serving a 15-minute-old
    "price feed healthy" would be worse than serving it slowly.
    """
    cached = cache.get(OVERVIEW_CACHE_KEY)
    if cached is not None:
        return {**cached, **live_health_overlay()}

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
            "db_connections": ctx.get("checks", {}).get("db_connections") or {
                "ok": True, "status": "unknown", "active": None, "max": None, "utilization_pct": None,
            },
            "price_feed": {
                "status": ctx["price_feed"]["status"],
                "latest_price_age_seconds": ctx["price_feed"]["age_seconds"],
                "threshold_seconds": ctx["price_feed"]["threshold_seconds"],
            },
            "outbound_mail": ctx["outbound_mail"],
        },
        "outbound_mail": ctx["outbound_mail"],
        "db_connections": ctx.get("db_connections") or ctx.get("checks", {}).get("db_connections"),
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
        "admin_model_inventory": admin_model_inventory(),
        "database_rows": [
            {**row, "latest": _latest_iso(row["latest"])} for row in ctx["database_rows"]
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


def _user_domain_health():
    """Cheap counts an operator needs about people and books, not the warehouse.

    Cached overview is warehouse/disk. These are small indexed aggregations and
    belong on every request so a silent snapshot writer is visible immediately.
    """
    now = timezone.now()
    since = now - timedelta(hours=24)
    last_login = User.objects.aggregate(value=Max("last_login"))["value"]
    last_snapshot = Snapshot.objects.aggregate(value=Max("timestamp"))["value"]
    return {
        "total": User.objects.count(),
        "admins": User.objects.filter(role="admin").count(),
        "active": User.objects.filter(is_active=True).count(),
        "with_accounts": User.objects.filter(accounts__isnull=False).distinct().count(),
        "accounts": Account.objects.count(),
        "holdings": Holding.objects.count(),
        "last_login": _iso(last_login),
        "last_snapshot_at": _iso(last_snapshot),
        "snapshots_24h": Snapshot.objects.filter(timestamp__gte=since).count(),
    }


def live_health_overlay():
    """The signals an operator needs to be true *now*, recomputed per request.

    Caching the whole overview for 15 minutes makes the page load instantly but
    would also let it report a healthy price feed a quarter of an hour after the
    feed died -- which defeats the point of an ops console. These four are the
    ones that answer "is it broken right now", and they are cheap: the price
    timestamp is an indexed MAX, queue depth is a Redis read, and the worker
    ping is bounded by its own timeout. The expensive warehouse, disk and
    coverage sections stay cached, because their answers do not change minute to
    minute.
    """
    now = timezone.now()
    db_conn = _db_connection_metrics()
    checks = {"database": True, "cache": True, "db_connections": db_conn}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        checks["database"] = False
    try:
        cache.set("admin_health_probe", "1", 5)
        checks["cache"] = cache.get("admin_health_probe") == "1"
    except Exception:
        checks["cache"] = False

    latest_price = Price.objects.aggregate(value=Max("fetched_at"))["value"]
    price_age = None if latest_price is None else now - latest_price
    price_status = "stale" if price_age is None or price_age > PRICE_STALE_AFTER else "fresh"
    workers = _workers()
    queues = _queues()
    mail = outbound_mail()

    overall = "healthy"
    if not all(v if isinstance(v, bool) else v.get("ok", True) for v in checks.values()) or workers["status"] == "critical":
        overall = "critical"
    elif (
        price_status == "stale"
        or queues["status"] != "healthy"
        or workers["status"] != "healthy"
        or db_conn["status"] == "warning"
        or not mail["ok"]
    ):
        overall = "degraded"

    price_feed = {
        "status": price_status,
        "latest": _iso(latest_price),
        "age_seconds": None if price_age is None else int(price_age.total_seconds()),
        "threshold_seconds": int(PRICE_STALE_AFTER.total_seconds()),
    }
    return {
        "generated_at": _iso(now),
        "status": overall,
        "overall_status": overall,
        "checks": {
            "database": {"ok": checks["database"], "status": "healthy" if checks["database"] else "critical"},
            "cache": {"ok": checks["cache"], "status": "healthy" if checks["cache"] else "critical"},
            "db_connections": db_conn,
            "price_feed": {
                "status": price_status,
                "latest_price_age_seconds": price_feed["age_seconds"],
                "threshold_seconds": price_feed["threshold_seconds"],
            },
            "outbound_mail": mail,
        },
        "outbound_mail": mail,
        "db_connections": db_conn,
        "price_feed": price_feed,
        "workers": {
            "status": workers.get("status"),
            "summary": {"online": len(workers.get("items") or {}), "active_tasks": 0, "reserved_tasks": 0},
            "detail": workers,
        },
        "queues": queues,
        "queue": {
            "status": queues.get("status"),
            "depths": queues.get("depths") or {},
            "depth": sum((queues.get("depths") or {}).values()),
        },
        "users": _user_domain_health(),
    }


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
