"""Client telemetry ingest and the staff-only report."""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.urls import Resolver404, resolve
from django.utils.dateparse import parse_datetime
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import recorder, report
from .middleware import cached_market_state

MAX_EVENTS = 200
MAX_MS = 600_000
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Page labels are `<pathname>` or `<pathname>:<view>`. Both halves are
# whitelisted: anything a client can make up would otherwise become a rollup
# row, and this endpoint is unauthenticated (sendBeacon carries no headers).
PAGE_PATHS = frozenset({"/", "/activity", "/research", "/compare", "/risk", "/ops",
                        "/onboarding", "/login", "/signup"})
PAGE_VIEWS = frozenset({"summary", "breakdown", "health", "transactions", "holdings",
                        "companies", "watchlist", "prices", "personal", "benchmark"})


def _page_label(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    path, _, view = raw.partition(":")
    if path not in PAGE_PATHS:
        return None
    return f"{path}:{view}" if view in PAGE_VIEWS else path


def _api_label(raw) -> str | None:
    if not isinstance(raw, str) or not raw.startswith("/api/") or len(raw) > 300:
        return None
    try:
        match = resolve(raw.split("?", 1)[0])
    except Resolver404:
        return None
    return (match.route or "")[:160] or None


class ClientPerfIngestView(APIView):
    """Accepts batched browser timings: page ready, app boot, per-API-call."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "perf_client"

    def post(self, request):
        events = request.data.get("events") if isinstance(request.data, dict) else None
        if not isinstance(events, list):
            return Response({"detail": "events must be a list"}, status=400)
        state = cached_market_state()
        accepted = 0
        for event in events[:MAX_EVENTS]:
            if not isinstance(event, dict):
                continue
            try:
                ms = float(event.get("ms"))
            except (TypeError, ValueError):
                continue
            if not 0 <= ms <= MAX_MS:
                continue
            kind = event.get("kind")
            method = ""
            status = 200
            if kind == "page":
                source, route = "client_page", _page_label(event.get("route"))
            elif kind == "boot":
                source, route = "client_boot", "boot"
            elif kind == "api":
                source, route = "client_api", _api_label(event.get("route"))
                method = str(event.get("method", "GET")).upper()
                if method not in METHODS:
                    continue
                try:
                    status = int(event.get("status", 200))
                except (TypeError, ValueError):
                    status = 0
            else:
                continue
            if not route:
                continue
            recorder.record(source=source, route=route, method=method,
                            market_state=state, ms=ms, status=status)
            accepted += 1
        return Response({"accepted": accepted}, status=202)


def _aware(raw):
    value = parse_datetime(raw) if raw else None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


class PerfReportView(APIView):
    """GET /api/perf/report/?days=7&source=api&since=...&until=...&limit=50"""

    permission_classes = [IsAdminUser]

    def get(self, request):
        params = request.query_params
        until = _aware(params.get("until"))
        since = _aware(params.get("since"))
        if since is None:
            try:
                days = min(max(float(params.get("days", 7)), 0.04), 90)
            except ValueError:
                days = 7
            since = datetime.now(dt_timezone.utc) - timedelta(days=days)
        try:
            limit = min(max(int(params.get("limit", 50)), 1), 500)
        except ValueError:
            limit = 50
        source = params.get("source") or None
        return Response(report.summarize(since=since, until=until, source=source, limit=limit))
