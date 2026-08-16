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
from marketdata.quota import get_quota_status


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


class AdminLogListView(APIView):
    permission_classes = [IsAdminUser]

    ORDERING = {
        "timestamp": "timestamp",
        "-timestamp": "-timestamp",
        "level": "level",
        "-level": "-level",
    }

    def get(self, request):
        qs = SystemLogEvent.objects.all()
        level = request.query_params.get("level")
        if level:
            qs = qs.filter(level=level)
        service = request.query_params.get("service")
        if service:
            qs = qs.filter(service=service)
        category = request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(Q(message__icontains=search) | Q(logger_name__icontains=search))
        start = _parse_dt(request.query_params.get("start"))
        end = _parse_dt(request.query_params.get("end"))
        if start:
            qs = qs.filter(timestamp__gte=start)
        if end:
            qs = qs.filter(timestamp__lte=end)
        ordering = self.ORDERING.get(request.query_params.get("ordering", "-timestamp"), "-timestamp")
        qs = qs.order_by(ordering)
        paginator = OpsPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "level": r.level,
                "category": r.category,
                "logger_name": r.logger_name,
                "message": r.message,
                "service": r.service,
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
                "last_error": r.last_error,
                "last_attempt_at": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
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

    quota = get_quota_status()
    remaining = None
    if isinstance(quota, dict):
        remaining = quota.get("remaining_daily")
        if remaining is None and "limit" in quota and "used" in quota:
            remaining = int(quota["limit"]) - int(quota["used"])
    if remaining is not None and remaining <= 0:
        raise RetryBlocked(503, "Provider quota exhausted; retry blocked.")

    from django.conf import settings
    from redis import Redis

    try:
        Redis.from_url(settings.CELERY_BROKER_URL).ping()
    except Exception as exc:
        raise RetryBlocked(503, "Broker unavailable; retry blocked.") from exc

    from marketdata.tasks import retry_archive_job_task
    from marketdata.workflows import WorkflowOutcome

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
        from marketdata.integrity import update_symbol_integrity
        from marketdata.workflows import WorkflowOutcome

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
        from marketdata.workflows import WorkflowOutcome

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
