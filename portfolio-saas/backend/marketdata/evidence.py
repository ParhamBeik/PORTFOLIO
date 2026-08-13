"""Assemble per-asset evidence for the staff ops inspector.

Joins portfolio identity (Asset.key) to warehouse identity (tse_symbol /
brs_symbol) without adding FKs. Live `compute_symbol_integrity` is used so
missing dates exist; archive completeness stays payload-relative.
"""
from __future__ import annotations

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from .candles import candle_close_qs
from .integrity import compute_symbol_integrity
from .models import (
    ArchiveFetchState,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketInstrument,
    RejectedRecord,
    WorkflowRun,
)

LIVE_PRICE_FRESH_SECONDS = 300
WORKFLOW_LIMIT = 25
REJECT_SAMPLE = 15


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _num(value):
    if value is None:
        return None
    return str(value)


def _warehouse_symbols(asset, lookup: str) -> list[str]:
    symbols = []
    if asset is not None:
        for value in (asset.tse_symbol, asset.brs_symbol):
            if value and value not in symbols:
                symbols.append(value)
    if lookup and lookup not in symbols:
        if MarketInstrument.objects.filter(symbol=lookup).exists() or ArchiveFetchState.objects.filter(symbol=lookup).exists():
            symbols.append(lookup)
    return symbols


def _resolve_asset(lookup: str):
    from portfolio.models import Asset

    asset = Asset.objects.filter(key=lookup).first()
    if asset is not None:
        return asset
    return Asset.objects.filter(Q(tse_symbol=lookup) | Q(brs_symbol=lookup)).first()


def _displayed_value(asset):
    from portfolio.models import Price

    if asset is None:
        return None
    row = (
        Price.objects.filter(asset=asset, price__gt=0)
        .order_by("-fetched_at", "-id")
        .first()
    )
    now = timezone.now()
    if row is None:
        return {
            "price_id": None,
            "price": None,
            "unit": None,
            "source": None,
            "priced_at": None,
            "age_seconds": None,
            "quality_status": "unavailable",
            "archive_record": _latest_archive_close(asset),
        }
    age = max(0, int((now - row.fetched_at).total_seconds()))
    return {
        "price_id": row.id,
        "price": _num(row.price),
        "unit": row.price_unit,
        "source": row.source,
        "priced_at": _iso(row.fetched_at),
        "age_seconds": age,
        "quality_status": "live" if age <= LIVE_PRICE_FRESH_SECONDS else "stale",
        "archive_record": _latest_archive_close(asset),
    }


def _latest_archive_close(asset):
    if asset is None:
        return None
    if asset.tse_symbol:
        row = (
            candle_close_qs(asset.tse_symbol)
            .order_by("-date_time")
            .values("id", "date_time", "timeframe", "close_price")
            .first()
        )
        if row:
            return {
                "id": row["id"],
                "date": str(row["date_time"]).split()[0],
                "timeframe": row["timeframe"],
                "table": "MarketCandle",
                "close": _num(row["close_price"]),
            }
    if asset.brs_symbol:
        row = (
            GoldCurrencyHistory.objects.filter(symbol=asset.brs_symbol, close_price__gt=0)
            .order_by("-date")
            .values("id", "date", "close_price")
            .first()
        )
        if row:
            return {
                "id": row["id"],
                "date": row["date"],
                "timeframe": None,
                "table": "GoldCurrencyHistory",
                "close": _num(row["close_price"]),
            }
    return None


def _series_span(symbols: list[str]) -> dict:
    candles = MarketCandle.objects.filter(symbol__in=symbols, timeframe=MarketCandle.ADJUSTED).aggregate(
        first=Min("date_time"), last=Max("date_time"), n=Count("id")
    )
    history = DailyStockHistory.objects.filter(symbol__in=symbols, is_adjusted=False).aggregate(
        first=Min("date"), last=Max("date"), n=Count("id")
    )
    gold = GoldCurrencyHistory.objects.filter(symbol__in=symbols).aggregate(
        first=Min("date"), last=Max("date"), n=Count("id")
    )
    return {
        "candles": {"first": candles["first"], "last": candles["last"], "rows": candles["n"] or 0},
        "daily_history": {"first": history["first"], "last": history["last"], "rows": history["n"] or 0},
        "gold_currency": {"first": gold["first"], "last": gold["last"], "rows": gold["n"] or 0},
    }


def _claims(*, displayed, archive_states, integrity) -> list[dict]:
    core = [
        s for s in archive_states
        if s["endpoint"] in (
            ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
            ArchiveFetchState.Endpoint.GOLD_DAILY,
        )
    ]
    archive_pass = bool(core) and all(s["verified_complete"] for s in core)
    live = displayed or {}
    live_pass = live.get("quality_status") == "live"
    gate_pass = bool(integrity and integrity.get("passes_gate"))
    return [
        {
            "id": "live_price_present",
            "label": "Live price present",
            "passed": live_pass,
            "definition": "Latest Price row exists and is ≤300 seconds old. Dashboard coverage uses this, not warehouse fill.",
        },
        {
            "id": "archive_payload_verified",
            "label": "Archive payload verified",
            "passed": archive_pass,
            "definition": "Last provider payload's keys are stored or quarantined as known gaps. Not full listing history.",
        },
        {
            "id": "analytics_gate_179d",
            "label": "179-day analytics gate",
            "passed": gate_pass,
            "definition": "Coverage ≥90%, max gap ≤5 sessions, freshness ≤5, rejection ratio ≤1% over 179 days.",
        },
    ]


def assemble_asset_evidence(lookup: str) -> dict | None:
    lookup = (lookup or "").strip()
    if not lookup:
        return None
    asset = _resolve_asset(lookup)
    symbols = _warehouse_symbols(asset, lookup)
    if asset is None and not symbols:
        return None

    instrument = None
    if symbols:
        instrument = MarketInstrument.objects.filter(symbol__in=symbols).first()

    states = list(ArchiveFetchState.objects.filter(symbol__in=symbols).order_by("endpoint"))
    archive_rows = [
        {
            "id": s.id,
            "endpoint": s.endpoint,
            "symbol": s.symbol,
            "stored_rows": s.stored_rows,
            "expected_rows": s.expected_rows,
            "missing_rows": s.missing_rows,
            "known_gap_rows": s.known_gap_rows,
            "first_date": s.first_date,
            "last_date": s.last_date,
            "verified_complete": s.verified_complete,
            "consecutive_failures": s.consecutive_failures,
            "last_error": s.last_error,
            "last_attempt_at": _iso(s.last_attempt_at),
            "last_success_at": _iso(s.last_success_at),
            "next_attempt_at": _iso(s.next_attempt_at),
        }
        for s in states
    ]

    integrity_symbol = (asset.tse_symbol if asset and asset.tse_symbol else None) or (
        asset.brs_symbol if asset and asset.brs_symbol else None
    ) or (symbols[0] if symbols else None)
    integrity = compute_symbol_integrity(integrity_symbol) if integrity_symbol else None

    rejected_qs = RejectedRecord.objects.filter(symbol__in=symbols)
    reason_counts = list(
        rejected_qs.values("reason").annotate(count=Count("id")).order_by("-count")[:12]
    )
    rejected_sample = [
        {
            "id": r.id,
            "endpoint": r.endpoint,
            "symbol": r.symbol,
            "date": r.date,
            "reason": r.reason,
            "occurrences": r.occurrences,
            "disposition": r.disposition,
            "last_seen": _iso(r.last_seen),
        }
        for r in rejected_qs.order_by("-occurrences", "-last_seen")[:REJECT_SAMPLE]
    ]

    workflows = [
        {
            "id": r.id,
            "created_at": _iso(r.created_at),
            "workflow": r.workflow,
            "outcome": r.outcome,
            "endpoint": r.endpoint,
            "symbol": r.symbol,
            "rows_accepted": r.rows_accepted,
            "rows_rejected": r.rows_rejected,
            "error_code": r.error_code,
            "correlation_id": r.correlation_id,
            "task_id": r.task_id,
        }
        for r in WorkflowRun.objects.filter(symbol__in=symbols).order_by("-created_at")[:WORKFLOW_LIMIT]
    ]

    displayed = _displayed_value(asset)
    claims = _claims(displayed=displayed, archive_states=archive_rows, integrity=integrity)
    archive_ok = next((c["passed"] for c in claims if c["id"] == "archive_payload_verified"), False)
    gate_ok = next((c["passed"] for c in claims if c["id"] == "analytics_gate_179d"), False)
    suggested_cli = ""
    if archive_ok and not gate_ok and integrity_symbol:
        suggested_cli = f"python manage.py resync_symbol_from_provider --symbol {integrity_symbol}"

    return {
        "lookup": lookup,
        "identity": {
            "key": asset.key if asset else None,
            "name": asset.name if asset else None,
            "asset_class": asset.asset_class if asset else None,
            "tse_symbol": asset.tse_symbol if asset else None,
            "brs_symbol": asset.brs_symbol if asset else None,
            "is_manual": bool(asset.is_manual) if asset else False,
            "is_house": bool(asset.is_house) if asset else False,
            "warehouse_symbols": symbols,
            "instrument": None if instrument is None else {
                "symbol": instrument.symbol,
                "source": instrument.source,
                "eligible": instrument.eligible,
            },
        },
        "displayed_value": displayed,
        "claims": claims,
        "archive_states": archive_rows,
        "integrity": integrity,
        "rejected": {
            "count": rejected_qs.count(),
            "reasons": reason_counts,
            "sample": rejected_sample,
        },
        "workflows": workflows,
        "series": _series_span(symbols) if symbols else {
            "candles": {"first": None, "last": None, "rows": 0},
            "daily_history": {"first": None, "last": None, "rows": 0},
            "gold_currency": {"first": None, "last": None, "rows": 0},
        },
        "suggested_cli": suggested_cli,
    }
