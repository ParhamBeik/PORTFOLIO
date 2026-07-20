"""Liveness + readiness probes for the prod healthchecks and the load balancer.

`/api/health/` (liveness) answers 200 as long as the process is up. The compose
healthcheck and the CDN hit this. `/api/health/ready/` (readiness) adds a DB
SELECT 1 and a cache roundtrip so a load balancer can stop routing traffic when
the app is up but can't actually serve (DB down, cache gone).

Both are public (permission_classes = []): healthchecks carry no auth token.
"""
from django.core.cache import cache
from django.db import connection
from rest_framework.response import Response
from rest_framework.views import APIView


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
