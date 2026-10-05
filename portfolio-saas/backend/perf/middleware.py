"""Per-request latency measurement.

Times every API request end to end, counts its SQL queries and their time,
notes how many other requests this process was serving when it started, and
tags it with the market state so "slow when the market is open" and "slow on
the weekend" can be told apart. Three outputs:

* the hourly Redis rollup (`perf.recorder.record`), flushed to the database;
* a `Server-Timing` header, so browser devtools and the client beacon show the
  app/db split per call;
* one structured log line, at WARNING when the request crossed
  `PERF_SLOW_REQUEST_MS` so slow requests can be grepped with their request id.

The route label is the URL *pattern* (`api/accounts/<int:pk>/performance/`),
never the raw path, so ids cannot multiply the rollup rows.
"""
import json
import logging
import threading
import time

from django.conf import settings
from django.db import connections

from . import recorder

logger = logging.getLogger("perf.request")

_EXCLUDED_PREFIXES = ("/api/health/", "/api/perf/client/")

_inflight = 0
_inflight_lock = threading.Lock()

_state_cache = {"value": "", "at": 0.0}
_STATE_TTL = 30.0


def cached_market_state() -> str:
    """Market state, re-read at most every 30s per process (it costs a Redis GET)."""
    now = time.monotonic()
    if now - _state_cache["at"] < _STATE_TTL and _state_cache["value"]:
        return _state_cache["value"]
    try:
        from marketdata.market_state import market_state

        value = market_state()
    except Exception:
        value = "unknown"
    _state_cache.update(value=value, at=now)
    return value


def route_label(request) -> str:
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None) if match else None
    if route:
        return route[:160]
    return "unresolved"


class _QueryTimer:
    __slots__ = ("count", "seconds")

    def __init__(self):
        self.count = 0
        self.seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.count += 1
            self.seconds += time.perf_counter() - start


class PerfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if (
            not getattr(settings, "PERF_ENABLED", True)
            or not path.startswith("/api/")
            or path.startswith(_EXCLUDED_PREFIXES)
        ):
            return self.get_response(request)

        global _inflight
        with _inflight_lock:
            _inflight += 1
            concurrent = _inflight
        timer = _QueryTimer()
        start = time.perf_counter()
        try:
            # Django converts a view's exception into a 500 response before it
            # reaches this middleware, so get_response returns on errors too.
            with connections["default"].execute_wrapper(timer):
                response = self.get_response(request)
        finally:
            with _inflight_lock:
                _inflight -= 1
        elapsed_ms = (time.perf_counter() - start) * 1000
        response["Server-Timing"] = (
            f'app;dur={elapsed_ms:.1f}, db;dur={timer.seconds * 1000:.1f};desc="{timer.count} queries"'
        )
        self._record(request, elapsed_ms, response.status_code, timer, concurrent)
        return response

    def _record(self, request, elapsed_ms, status, timer, concurrent):
        state = cached_market_state()
        route = route_label(request)
        db_ms = timer.seconds * 1000
        recorder.record(
            source="api", route=route, method=request.method, market_state=state,
            ms=elapsed_ms, status=status, db_queries=timer.count, db_ms=db_ms,
            inflight=concurrent,
        )
        slow = elapsed_ms >= getattr(settings, "PERF_SLOW_REQUEST_MS", 1000)
        if slow or getattr(settings, "PERF_LOG_ALL_REQUESTS", False):
            user = getattr(request, "user", None)
            line = json.dumps({
                "route": route, "method": request.method, "status": status,
                "ms": round(elapsed_ms, 1), "db_queries": timer.count,
                "db_ms": round(db_ms, 1), "inflight": concurrent,
                "market_state": state,
                "user": getattr(user, "pk", None) if getattr(user, "is_authenticated", False) else None,
            })
            logger.log(logging.WARNING if slow else logging.INFO, "perf %s", line)
