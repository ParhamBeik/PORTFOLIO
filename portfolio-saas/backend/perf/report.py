"""Read side: flush Redis totals into rollup rows, and summarise them.

The summary splits every route by the conditions that change its cost --
market state (open / closed daytime / overnight), Tehran trading day versus
the Thu/Fri weekend, and Tehran hour of day -- so a before/after comparison
can be made like for like rather than "Tuesday 10:00 versus Friday 23:00".
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import jdatetime
from django.db import transaction

from . import recorder
from .models import RequestPerfRollup

TEHRAN = ZoneInfo("Asia/Tehran")
TRADING_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # jdatetime: Saturday=0 .. Wednesday=4

FLUSH_LOOKBACK_HOURS = 3


def flush(now_ts: float | None = None, lookback_hours: int = FLUSH_LOOKBACK_HOURS) -> int:
    """Write the running totals of recent hourly buckets to the database.

    Idempotent: Redis holds cumulative totals for each hour, so rows are
    overwritten, never incremented. Running it twice, or concurrently with
    itself, cannot double count. Returns the number of rows written.
    """
    current = recorder.hour_bucket(now_ts)
    written = 0
    for back in range(lookback_hours):
        bucket = current - back * 3600
        rows = recorder.read_bucket(bucket)
        if not rows:
            continue
        bucket_dt = recorder.bucket_datetime(bucket)
        objs = [
            RequestPerfRollup(
                bucket=bucket_dt, source=source, route=route, method=method,
                market_state=state,
                count=int(values.get("count", 0)),
                errors=int(values.get("errors", 0)),
                client_errors=int(values.get("client_errors", 0)),
                sum_ms=values.get("sum_ms", 0.0),
                max_ms=values.get("max_ms", 0.0),
                sum_db_queries=int(values.get("sum_db_queries", 0)),
                sum_db_ms=values.get("sum_db_ms", 0.0),
                sum_inflight=int(values.get("sum_inflight", 0)),
                hist=values["hist"],
            )
            for (source, route, method, state), values in rows.items()
        ]
        with transaction.atomic():
            RequestPerfRollup.objects.bulk_create(
                objs,
                update_conflicts=True,
                unique_fields=["bucket", "source", "route", "method", "market_state"],
                update_fields=["count", "errors", "client_errors", "sum_ms", "max_ms",
                               "sum_db_queries", "sum_db_ms", "sum_inflight", "hist"],
            )
        written += len(objs)
    return written


def tehran_context(bucket: datetime) -> tuple[str, int]:
    """(day_type, tehran_hour) for a UTC hour bucket."""
    local = bucket.astimezone(TEHRAN)
    weekday = jdatetime.date.fromgregorian(date=local.date()).weekday()
    return ("trading_day" if weekday in TRADING_WEEKDAYS else "weekend"), local.hour


class _Acc:
    __slots__ = ("count", "errors", "client_errors", "sum_ms", "max_ms",
                 "sum_db_queries", "sum_db_ms", "sum_inflight", "hist")

    def __init__(self):
        self.count = self.errors = self.client_errors = 0
        self.sum_ms = self.max_ms = self.sum_db_ms = 0.0
        self.sum_db_queries = self.sum_inflight = 0
        self.hist = [0] * recorder.HIST_LEN

    def add(self, row):
        self.count += row.count
        self.errors += row.errors
        self.client_errors += row.client_errors
        self.sum_ms += row.sum_ms
        self.max_ms = max(self.max_ms, row.max_ms)
        self.sum_db_queries += row.sum_db_queries
        self.sum_db_ms += row.sum_db_ms
        self.sum_inflight += row.sum_inflight
        for index, value in enumerate((row.hist or [])[: recorder.HIST_LEN]):
            self.hist[index] += value

    def as_dict(self):
        n = self.count or 1
        return {
            "count": self.count,
            "avg_ms": round(self.sum_ms / n, 1),
            "p50_ms": recorder.percentile_from_hist(self.hist, 0.50),
            "p95_ms": recorder.percentile_from_hist(self.hist, 0.95),
            "max_ms": round(self.max_ms, 1),
            "avg_db_queries": round(self.sum_db_queries / n, 1),
            "avg_db_ms": round(self.sum_db_ms / n, 1),
            "avg_inflight": round(self.sum_inflight / n, 2),
            "error_rate": round(self.errors / n, 4),
            "client_error_rate": round(self.client_errors / n, 4),
        }


def summarize(*, since: datetime, until: datetime | None = None, source: str | None = None,
              limit: int = 50) -> dict:
    """Aggregate rollups in [since, until) per route and per load condition."""
    until = until or datetime.now(dt_timezone.utc) + timedelta(hours=1)
    qs = RequestPerfRollup.objects.filter(bucket__gte=since, bucket__lt=until)
    if source:
        qs = qs.filter(source=source)

    routes: dict[tuple, _Acc] = defaultdict(_Acc)
    route_by_state: dict[tuple, dict] = defaultdict(lambda: defaultdict(_Acc))
    route_by_day: dict[tuple, dict] = defaultdict(lambda: defaultdict(_Acc))
    overall_by_state: dict[str, _Acc] = defaultdict(_Acc)
    overall_by_day: dict[str, _Acc] = defaultdict(_Acc)
    overall_by_hour: dict[int, _Acc] = defaultdict(_Acc)
    overall = _Acc()

    for row in qs.iterator(chunk_size=2000):
        key = (row.source, row.route, row.method)
        day_type, hour = tehran_context(row.bucket)
        routes[key].add(row)
        route_by_state[key][row.market_state].add(row)
        route_by_day[key][day_type].add(row)
        overall.add(row)
        overall_by_state[row.market_state].add(row)
        overall_by_day[day_type].add(row)
        overall_by_hour[hour].add(row)

    ranked = sorted(routes.items(), key=lambda item: item[1].sum_ms, reverse=True)[:limit]
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "source": source or "all",
        "overall": overall.as_dict(),
        "by_market_state": {k: v.as_dict() for k, v in sorted(overall_by_state.items())},
        "by_day_type": {k: v.as_dict() for k, v in sorted(overall_by_day.items())},
        "by_tehran_hour": {str(k): v.as_dict() for k, v in sorted(overall_by_hour.items())},
        # Ranked by total time spent (count x avg): what costs the server most.
        "routes": [
            {
                "source": source_, "route": route, "method": method,
                **acc.as_dict(),
                "total_seconds": round(acc.sum_ms / 1000, 1),
                "by_market_state": {k: v.as_dict() for k, v in sorted(route_by_state[(source_, route, method)].items())},
                "by_day_type": {k: v.as_dict() for k, v in sorted(route_by_day[(source_, route, method)].items())},
            }
            for (source_, route, method), acc in ranked
        ],
    }
