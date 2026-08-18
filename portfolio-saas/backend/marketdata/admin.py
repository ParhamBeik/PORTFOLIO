"""Admin for the warehouse tables — browse/filter by symbol and date.

Dates here are source-native Jalali strings (CharFields), so no date_hierarchy;
plain ordering + search covers the browse cases.
"""
from django.contrib import admin
from django.db.models import F, Sum
from django.utils import timezone

from .models import (
    ApiRequestQuota,
    ArchiveFetchState,
    CodalAnnouncement,
    CodalArtifact,
    CodalFact,
    CodalReport,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    MarketInstrument,
    OperationalMetricSnapshot,
    RejectedRecord,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
    SymbolIntegrity,
    SystemLogEvent,
    WorkflowRun,
)


# ---------------------------------------------------------------------------
# ArchiveFetchState — the core telemetry admin replaces AdminStatusView
# ---------------------------------------------------------------------------

@admin.register(ArchiveFetchState)
class ArchiveFetchStateAdmin(admin.ModelAdmin):
    list_display = (
        "symbol",
        "endpoint",
        "progress_display",
        "stored_rows",
        "expected_rows",
        "missing_rows",
        "known_gap_rows",
        "consecutive_failures",
        "verified_complete",
        "last_attempt_at",
    )
    list_filter = ("endpoint", "verified_complete", "consecutive_failures")
    search_fields = ("symbol",)
    ordering = ("verified_complete", "-missing_rows", "-consecutive_failures")
    readonly_fields = (
        "symbol", "endpoint", "stored_rows", "expected_rows", "missing_rows",
        "known_gap_rows",
        "first_date", "last_date", "verified_complete", "consecutive_failures",
        "last_error", "last_attempt_at", "last_success_at", "next_attempt_at",
    )
    list_per_page = 50
    actions = ["retry_selected_jobs"]

    @admin.display(description="Progress")
    def progress_display(self, obj):
        if obj.expected_rows == 0:
            return "—"
        pct = round(
            (obj.stored_rows + obj.known_gap_rows) / obj.expected_rows * 100, 1
        )
        return f"{pct}%"

    @admin.action(description="Retry selected backfill jobs")
    def retry_selected_jobs(self, request, queryset):
        from .admin_api import RetryBlocked, enqueue_archive_retries

        ids = list(queryset.values_list("id", flat=True)[:50])
        if not ids:
            self.message_user(request, "No archive states selected.")
            return
        try:
            result = enqueue_archive_retries(ids, request.user.email)
        except RetryBlocked as err:
            self.message_user(request, err.detail, level=40)
            return
        queued = result.get("queued") or []
        skipped = result.get("skipped_missing_or_healthy") or []
        self.message_user(
            request,
            f"Enqueued {len(queued)} job(s) for retry"
            + (f"; skipped {len(skipped)} healthy/missing." if skipped else "."),
        )


# ---------------------------------------------------------------------------
# ApiRequestQuota — read-only quota browsing
# ---------------------------------------------------------------------------

@admin.register(ApiRequestQuota)
class ApiRequestQuotaAdmin(admin.ModelAdmin):
    list_display = ("day", "used", "limit", "remaining_display", "archive_used", "live_used", "other_used", "updated_at")
    ordering = ("-day",)
    readonly_fields = ("day", "used", "limit", "archive_used", "live_used", "other_used", "updated_at")
    list_per_page = 30

    @admin.display(description="Remaining")
    def remaining_display(self, obj):
        return max(0, obj.limit - obj.used)


# ---------------------------------------------------------------------------
# MarketInstrument — enhanced from bare register
# ---------------------------------------------------------------------------

@admin.register(MarketInstrument)
class MarketInstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "source", "eligible", "updated_at")
    list_filter = ("source", "eligible")
    search_fields = ("symbol",)
    ordering = ("symbol",)


# ---------------------------------------------------------------------------
# SystemLogEvent — replaces get_recent_logs() in AdminStatusView
# ---------------------------------------------------------------------------

@admin.register(SystemLogEvent)
class SystemLogEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "level", "category", "service", "message_truncated")
    list_filter = ("level", "category", "service")
    search_fields = ("message", "category")
    ordering = ("-timestamp",)
    readonly_fields = ("timestamp", "level", "category", "logger_name", "message", "service")
    list_per_page = 100

    @admin.display(description="Message")
    def message_truncated(self, obj):
        return obj.message[:120] + "…" if len(obj.message) > 120 else obj.message


@admin.register(WorkflowRun)
class WorkflowRunAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "workflow", "endpoint", "symbol", "outcome",
        "rows_accepted", "rows_rejected", "duration_ms", "error_code",
    )
    list_filter = ("workflow", "endpoint", "symbol", "outcome", "created_at")
    search_fields = ("task_id", "correlation_id", "symbol", "error_code")
    ordering = ("-created_at",)
    readonly_fields = tuple(field.name for field in WorkflowRun._meta.fields)


class StaffReadOnlyAdmin(admin.ModelAdmin):
    """Operational evidence is visible to staff and never editable here."""

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OperationalMetricSnapshot)
class OperationalMetricSnapshotAdmin(StaffReadOnlyAdmin):
    list_display = ("captured_at", "tracked_table_count")
    ordering = ("-captured_at",)
    readonly_fields = (
        "captured_at", "database_counts", "table_bytes", "archive", "quota",
        "queues", "codal_status", "workflow_15m", "workers", "disk",
    )

    @admin.display(description="Tracked tables")
    def tracked_table_count(self, obj):
        return len(obj.database_counts)


@admin.register(RejectedRecord)
class RejectedRecordAdmin(StaffReadOnlyAdmin):
    list_display = (
        "last_seen", "endpoint", "symbol", "reason", "occurrences", "disposition",
    )
    list_filter = ("endpoint", "reason", "disposition")
    search_fields = ("symbol", "reason")
    ordering = ("-last_seen",)
    readonly_fields = tuple(field.name for field in RejectedRecord._meta.fields)


@admin.register(SymbolIntegrity)
class SymbolIntegrityAdmin(StaffReadOnlyAdmin):
    list_display = (
        "symbol", "source", "coverage_ratio", "max_gap_days", "rejected_count",
        "passes_gate", "computed_at",
    )
    list_filter = ("passes_gate", "source")
    search_fields = ("symbol", "reason")
    ordering = ("passes_gate", "symbol")
    readonly_fields = tuple(field.name for field in SymbolIntegrity._meta.fields)


class CodalArtifactInline(admin.TabularInline):
    model = CodalArtifact
    extra = 0
    readonly_fields = ("kind", "source_url", "s3_key", "checksum_sha256", "content_type", "size_bytes", "fetch_status", "error_code")


@admin.register(CodalReport)
class CodalReportAdmin(admin.ModelAdmin):
    list_display = ("id", "symbol", "category", "report_type", "period_end", "status", "quality", "updated_at")
    list_filter = ("category", "status", "quality", "is_audited", "is_consolidated", "is_correction")
    search_fields = ("announcement__symbol", "announcement__title", "letter_type")
    inlines = (CodalArtifactInline,)

    @admin.display(ordering="announcement__symbol")
    def symbol(self, obj):
        return obj.announcement.symbol


@admin.register(CodalFact)
class CodalFactAdmin(admin.ModelAdmin):
    list_display = ("fact_code", "symbol", "period_end", "numeric_value", "unit", "quality", "confidence")
    list_filter = ("fact_code", "quality", "report__category")
    search_fields = ("report__announcement__symbol", "fact_code", "text_value")

    @admin.display(ordering="report__announcement__symbol")
    def symbol(self, obj):
        return obj.report.announcement.symbol


# ---------------------------------------------------------------------------
# Existing model admins (unchanged)
# ---------------------------------------------------------------------------

@admin.register(StockSymbolMetadata)
class StockSymbolMetadataAdmin(admin.ModelAdmin):
    list_display = ("l18", "l30", "sector", "market", "pe", "eps", "market_cap", "updated_at")
    list_filter = ("market", "sector")
    search_fields = ("l18", "l30", "isin")


@admin.register(DailyStockHistory)
class DailyStockHistoryAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "pl", "pc", "tvol", "plp")
    list_filter = ("symbol",)
    search_fields = ("symbol",)


@admin.register(MarketCandle)
class MarketCandleAdmin(admin.ModelAdmin):
    list_display = ("symbol", "timeframe", "date_time", "open_price", "close_price", "volume")
    list_filter = ("symbol", "timeframe")
    search_fields = ("symbol",)


@admin.register(StockTransactionTick)
class StockTransactionTickAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "time", "row", "price", "volume", "canceled")
    list_filter = ("symbol", "canceled")
    search_fields = ("symbol",)


@admin.register(ShareholderRecord)
class ShareholderRecordAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "name", "percent", "volume", "change")
    list_filter = ("symbol",)
    search_fields = ("symbol", "name")


@admin.register(CodalAnnouncement)
class CodalAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("symbol", "title", "code", "date_publish", "time_publish")
    list_filter = ("symbol",)
    search_fields = ("symbol", "title")


@admin.register(GoldCurrencyHistory)
class GoldCurrencyHistoryAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "close_price", "open_price", "unit")
    list_filter = ("symbol",)
    search_fields = ("symbol", "name")


@admin.register(MarketIndexData)
class MarketIndexDataAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "index_overall", "index_overall_change", "trade_value")
    search_fields = ("date",)
