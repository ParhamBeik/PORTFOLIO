"""Liveness + readiness + feed-staleness probes.

`/api/health/` (liveness) answers 200 as long as the process is up. The compose
healthcheck hits this. `/api/health/ready/` (readiness) adds a DB SELECT 1 and a
cache roundtrip so a load balancer can stop routing traffic when the app is up
but can't actually serve (DB down, cache gone). `/api/health/prices/` is the
dead-man's switch: 503 when the freshest Price row is older than the threshold
AND some live job should be running right now (`marketdata.market_state.
expects_live_prices` -- OVERNIGHT has zero live jobs by design, so a stale row
then is not a fault). The on-VPS cron and the GitHub Actions probe both watch
this endpoint; before this it went stale-by-design every night, which either
paged on nothing or trained whoever watches it to ignore the alert.

All are public (permission_classes = []): healthchecks carry no auth token.
"""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

# Derived, not written again. `PRICE_STALE_THRESHOLD_SECONDS` is the one place
# "a price this old is stale" is declared; docker-compose.prod.yml plumbs it,
# and marketdata/tasks.py (the operator alert) and portfolio/services/returns.py
# both read it. This module -- and marketdata/admin_telemetry.py, which imports
# the name from here -- hardcoded 15 minutes instead, so lowering the env var
# tightened the alert and the returns matrix while leaving the dead-man's switch
# that the on-VPS cron and the GitHub probe actually watch on the old number.
PRICE_STALE_AFTER = timedelta(seconds=settings.PRICE_STALE_THRESHOLD_SECONDS)


class HealthView(APIView):
    """Liveness: the process answers."""

    permission_classes = []

    def get(self, request):
        return Response({"status": "ok"})


class ReadyView(APIView):
    """Readiness: the process can serve (DB + cache reachable)."""

    permission_classes = []

    def get(self, request):
        checks = {"database": False, "cache": False}
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            checks["database"] = True
        except Exception:
            pass
        try:
            cache.set("_health_ping", "1", timeout=10)
            checks["cache"] = cache.get("_health_ping") == "1"
        except Exception:
            pass
        ready = all(checks.values())
        return Response(
            {"status": "ready" if ready else "degraded", "checks": checks},
            status=200 if ready else 503,
        )


class PriceFeedView(APIView):
    """Dead-man's switch: 503 when the price feed has gone stale.

    Lazy model import: config must not import app modules at load time
    (apps aren't ready when settings import this module's siblings).
    """

    permission_classes = []

    def get(self, request):
        from marketdata.market_state import expects_live_prices
        from portfolio.models import Price

        latest = Price.objects.order_by("-fetched_at").values_list(
            "fetched_at", flat=True
        ).first()
        age = None if latest is None else timezone.now() - latest
        # A table that has NEVER had a Price row is always stale, regardless of
        # market hours -- expects_live_prices() only excuses an old-but-real
        # price during a designed pause, not a total absence of data.
        stale = age is None or (age > PRICE_STALE_AFTER and expects_live_prices())
        return Response(
            {
                "status": "stale" if stale else "fresh",
                "latest_price_age_seconds": None if age is None else int(age.total_seconds()),
                "threshold_seconds": int(PRICE_STALE_AFTER.total_seconds()),
            },
            status=503 if stale else 200,
        )
