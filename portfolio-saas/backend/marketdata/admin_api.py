"""Staff-only operations APIs for the React /ops center."""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from marketdata.admin_telemetry import get_ops_overview, invalidate_ops_cache
from marketdata.coverage_report import list_ops_assets
from marketdata.evidence import assemble_asset_evidence
from marketdata.models import ArchiveFetchState, SystemLogEvent, WorkflowRun
from .integrity import update_symbol_integrity
from .workflows import WorkflowOutcome
from django.conf import settings


class OpsPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def _parse_dt(value: str | None):
    if not value:
        return None
    return parse_datetime(value)


class AdminOverviewView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        if request.query_params.get("refresh") in ("1", "true", "yes"):
            invalidate_ops_cache()
        return Response(get_ops_overview())


class AdminWorkflowListView(APIView):
    permission_classes = [IsAdminUser]

    ORDERING = {
        "created_at": "created_at",
        "-created_at": "-created_at",
        "outcome": "outcome",
        "-outcome": "-outcome",
        "duration_ms": "duration_ms",
        "-duration_ms": "-duration_ms",
    }

    def get(self, request):
        qs = WorkflowRun.objects.all()
        outcome = request.query_params.get("outcome")
        if outcome:
            qs = qs.filter(outcome=outcome)
        if request.query_params.get("failed_only") == "true":
            qs = qs.filter(
                outcome__in=(
                    WorkflowRun.Outcome.FAILED,
                    WorkflowRun.Outcome.BLOCKED_NETWORK,
                    WorkflowRun.Outcome.BLOCKED_STORAGE,
                    WorkflowRun.Outcome.RETRY,
                )
            )
        endpoint = request.query_params.get("endpoint")
        if endpoint:
            qs = qs.filter(endpoint=endpoint)
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(workflow__icontains=search)
                | Q(symbol__icontains=search)
                | Q(error_code__icontains=search)
                | Q(task_id__icontains=search)
            )
        start = _parse_dt(request.query_params.get("start"))
        end = _parse_dt(request.query_params.get("end"))
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)
        ordering = self.ORDERING.get(request.query_params.get("ordering", "-created_at"), "-created_at")
        qs = qs.order_by(ordering)
        paginator = OpsPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "workflow": r.workflow,
                "outcome": r.outcome,
                "endpoint": r.endpoint,
                "symbol": r.symbol,
                "rows_accepted": r.rows_accepted,
                "rows_rejected": r.rows_rejected,
                "duration_ms": r.duration_ms,
                "error_code": r.error_code,
                "task_id": r.task_id,
                "destination_table": r.destination_table,
            }
            for r in page
        ]
        return paginator.get_paginated_response(results)


class AdminArchiveStateListView(APIView):
    permission_classes = [IsAdminUser]

    ORDERING = {
        "last_attempt_at": "last_attempt_at",
        "-last_attempt_at": "-last_attempt_at",
        "consecutive_failures": "consecutive_failures",
        "-consecutive_failures": "-consecutive_failures",
        "missing_rows": "missing_rows",
        "-missing_rows": "-missing_rows",
        "symbol": "symbol",
        "-symbol": "-symbol",
    }

    def get(self, request):
        qs = ArchiveFetchState.objects.all()
        endpoint = request.query_params.get("endpoint")
        if endpoint:
            qs = qs.filter(endpoint=endpoint)
        verified = request.query_params.get("verified_complete")
        if verified in ("true", "false"):
            qs = qs.filter(verified_complete=(verified == "true"))
        failed_only = request.query_params.get("failed_only")
        if failed_only == "true":
            qs = qs.filter(consecutive_failures__gt=0)
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(symbol__icontains=search)
        ordering = self.ORDERING.get(
            request.query_params.get("ordering", "-consecutive_failures"),
            "-consecutive_failures",
        )
        qs = qs.order_by(ordering)
        paginator = OpsPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [
            {
                "id": r.id,
                "symbol": r.symbol,
                "endpoint": r.endpoint,
                "stored_rows": r.stored_rows,
                "expected_rows": r.expected_rows,
                "missing_rows": r.missing_rows,
                "consecutive_failures": r.consecutive_failures,
                "verified_complete": r.verified_complete,
                # The console classifies on these two before anything else
                # (Ops.jsx:archiveJobVariant). Without them on the wire every
                # unfetchable symbol renders as whatever its stale row counts
                # imply, which is the reading this pair exists to correct.
                "blacklisted": r.blacklisted,
                "suspended_at": r.suspended_at.isoformat() if r.suspended_at else None,
                "suspension_reason": r.suspension_reason,
                "last_error": r.last_error,
                "last_attempt_at": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
            }
            for r in page
        ]
        return paginator.get_paginated_response(results)


class RetryBlocked(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def enqueue_archive_retries(ids, actor_email, *, require_failed=True):
    """Reset and enqueue archive states. Shared by the ops API and Django admin."""
    if not isinstance(ids, list) or not ids:
        raise RetryBlocked(400, "ids must be a non-empty list.")
    if len(ids) > 50:
        raise RetryBlocked(400, "At most 50 archive-state IDs allowed.")
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError) as exc:
        raise RetryBlocked(400, "ids must be integers.") from exc

    # "Exhausted" means the provider said so, not that our arithmetic hit zero.
    # There is no hardcoded daily limit any more, so an undisclosed ceiling reads
    # as 0 remaining -- treating that as exhausted would block every retry on a
    # fresh quota day. The per-plan breaker is the authoritative signal.
    from marketdata.quota import PLANS, is_plan_blocked

    if all(is_plan_blocked(plan) for plan in PLANS):
        raise RetryBlocked(503, "Provider quota exhausted; retry blocked.")

    from redis import Redis

    try:
        Redis.from_url(settings.CELERY_BROKER_URL).ping()
    except Exception as exc:
        raise RetryBlocked(503, "Broker unavailable; retry blocked.") from exc

    from marketdata.tasks import retry_archive_job_task

    qs = ArchiveFetchState.objects.filter(id__in=ids)
    if require_failed:
        qs = qs.filter(consecutive_failures__gt=0)
    states = list(qs)
    found_ids = {s.id for s in states}
    missing = [i for i in ids if i not in found_ids]
    now = timezone.now()
    queued = []
    for state in states:
        state.consecutive_failures = 0
        state.last_error = f"Ops retry by {actor_email}"
        state.next_attempt_at = now
        state.save(update_fields=["consecutive_failures", "last_error", "next_attempt_at"])
        retry_archive_job_task.delay(state.id)
        queued.append(state.id)

    SystemLogEvent.objects.create(
        level="INFO",
        category="ops_retry",
        logger_name="marketdata.admin_api",
        message=f"Admin {actor_email} retried archive states {queued}",
        service="backend",
    )
    WorkflowOutcome(
        "ops_retry",
        endpoint="archive_retry",
        source="ops",
        destination_table="ArchiveFetchState",
    ).finish(
        WorkflowRun.Outcome.SUCCESS,
        rows_received=len(ids),
        rows_accepted=len(queued),
        metadata={"queued": queued, "actor": actor_email},
    )
    invalidate_ops_cache()
    return {"queued": queued, "skipped_missing_or_healthy": missing}


class AdminArchiveRetryView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        ids = request.data.get("ids") or []
        confirm = bool(request.data.get("confirm"))
        if not confirm:
            return Response({"detail": "confirm must be true."}, status=400)
        try:
            result = enqueue_archive_retries(ids, request.user.email)
        except RetryBlocked as err:
            return Response({"detail": err.detail}, status=err.status)
        return Response(result)


class AdminAssetListView(APIView):
    permission_classes = [IsAdminUser]

    ORDERING = {
        "name",
        "-name",
        "key",
        "-key",
        "asset_class",
        "-asset_class",
        "live_status",
        "-live_status",
        "age_seconds",
        "-age_seconds",
        "integrity_status",
        "-integrity_status",
    }

    def get(self, request):
        ordering = request.query_params.get("ordering", "name")
        if ordering not in self.ORDERING:
            ordering = "name"
        payload = list_ops_assets(
            asset_class=request.query_params.get("asset_class"),
            search=request.query_params.get("search"),
            live_status=request.query_params.get("live_status"),
            held_only=request.query_params.get("held_only") == "true",
            integrity=request.query_params.get("integrity"),
            ordering=ordering,
        )
        try:
            page_size = min(int(request.query_params.get("page_size", 50)), 200)
        except (TypeError, ValueError):
            page_size = 50
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        rows = payload["results"]
        count = len(rows)
        start = (page - 1) * page_size
        end = start + page_size
        base = request.build_absolute_uri(request.path)
        qs = request.GET.copy()

        def page_link(page_num):
            qs["page"] = str(page_num)
            return f"{base}?{qs.urlencode()}"

        return Response({
            "count": count,
            "generated_at": payload["generated_at"],
            "asset_classes": payload["asset_classes"],
            "next": page_link(page + 1) if end < count else None,
            "previous": page_link(page - 1) if page > 1 else None,
            "results": rows[start:end],
        })


class AdminAssetEvidenceView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, key):
        payload = assemble_asset_evidence(key)
        if payload is None:
            return Response({"detail": "Asset not found."}, status=404)
        return Response(payload)


def _asset_symbols(key):
    payload = assemble_asset_evidence(key)
    if payload is None:
        return None, None
    return payload, payload["identity"]["warehouse_symbols"] or []


class AdminAssetRetryView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, key):
        if not request.data.get("confirm"):
            return Response({"detail": "confirm must be true."}, status=400)
        payload, symbols = _asset_symbols(key)
        if payload is None:
            return Response({"detail": "Asset not found."}, status=404)
        ids = list(
            ArchiveFetchState.objects.filter(
                symbol__in=symbols, consecutive_failures__gt=0
            ).values_list("id", flat=True)[:50]
        )
        if not ids:
            return Response({"queued": [], "skipped_missing_or_healthy": [], "detail": "No failed archive states."})
        try:
            result = enqueue_archive_retries(ids, request.user.email)
        except RetryBlocked as err:
            return Response({"detail": err.detail}, status=err.status)
        return Response(result)


class AdminAssetRecomputeIntegrityView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, key):
        if not request.data.get("confirm"):
            return Response({"detail": "confirm must be true."}, status=400)
        payload, symbols = _asset_symbols(key)
        if payload is None:
            return Response({"detail": "Asset not found."}, status=404)

        results = [update_symbol_integrity(symbol) for symbol in symbols]
        WorkflowOutcome(
            "ops_recompute_integrity",
            endpoint="symbol_integrity",
            symbol=symbols[0] if symbols else "",
            source="ops",
            destination_table="SymbolIntegrity",
        ).finish(
            WorkflowRun.Outcome.SUCCESS,
            rows_accepted=len(results),
            metadata={"symbols": symbols, "actor": request.user.email},
        )
        invalidate_ops_cache()
        return Response({"results": results})


class AdminAssetRefreshView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, key):
        if not request.data.get("confirm"):
            return Response({"detail": "confirm must be true."}, status=400)
        payload, symbols = _asset_symbols(key)
        if payload is None:
            return Response({"detail": "Asset not found."}, status=404)
        ids = list(
            ArchiveFetchState.objects.filter(symbol__in=symbols).values_list("id", flat=True)[:50]
        )
        if not ids:
            return Response({"queued": [], "detail": "No archive states for this symbol."})
        try:
            result = enqueue_archive_retries(ids, request.user.email, require_failed=False)
        except RetryBlocked as err:
            return Response({"detail": err.detail}, status=err.status)

        WorkflowOutcome(
            "ops_refresh",
            endpoint="archive_refresh",
            symbol=symbols[0] if symbols else "",
            source="ops",
            destination_table="ArchiveFetchState",
        ).finish(
            WorkflowRun.Outcome.SUCCESS,
            rows_accepted=len(result.get("queued") or []),
            metadata={"queued": result.get("queued"), "actor": request.user.email},
        )
        return Response(result)


# Mounted at /api/admin/ by config/urls.py.
from django.urls import path  # noqa: E402

urlpatterns = [
    path("overview/", AdminOverviewView.as_view(), name="admin-ops-overview"),
    path("workflows/", AdminWorkflowListView.as_view(), name="admin-ops-workflows"),
    path("archive-states/", AdminArchiveStateListView.as_view(), name="admin-ops-archive-states"),
    path("archive-states/retry/", AdminArchiveRetryView.as_view(), name="admin-ops-archive-retry"),
    path("assets/", AdminAssetListView.as_view(), name="admin-ops-assets"),
    path("assets/<str:key>/evidence/", AdminAssetEvidenceView.as_view(), name="admin-ops-asset-evidence"),
    path("assets/<str:key>/retry/", AdminAssetRetryView.as_view(), name="admin-ops-asset-retry"),
    path("assets/<str:key>/recompute-integrity/", AdminAssetRecomputeIntegrityView.as_view(), name="admin-ops-asset-recompute"),
    path("assets/<str:key>/refresh/", AdminAssetRefreshView.as_view(), name="admin-ops-asset-refresh"),
]
