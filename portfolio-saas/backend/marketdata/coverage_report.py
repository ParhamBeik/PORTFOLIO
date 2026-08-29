"""Granular data-coverage breakdown for the /ops console.

Classifies live portfolio pricing separately from warehouse archive jobs.
States are validated against DB fields — not a binary complete/incomplete flag.
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from portfolio.models import Asset, Holding, Price

from .archive import _RETIRED_ARCHIVE_ENDPOINTS
from .evidence import LIVE_PRICE_FRESH_SECONDS
from .models import ArchiveFetchState, SymbolIntegrity

ARCHIVE_STATUSES = ("complete", "partial", "failed", "not_tried")
LIVE_STATUSES = ("fresh", "stale", "missing", "manual", "formula", "no_source")

LIVE_TABLE_KEYS = ("prices", "snapshots")
WAREHOUSE_TABLE_KEYS = (
    "market_instruments",
    "stock_history_rows",
    "gold_currency_rows",
    "candles",
    "stock_transaction_ticks",
    "announcements",
    "shareholders",
)


def classify_archive_state(state: ArchiveFetchState) -> str:
    if state.verified_complete:
        return "complete"
    if state.last_attempt_at is None:
        return "not_tried"
    if state.consecutive_failures > 0:
        return "failed"
    return "partial"


def _count_statuses(rows, classifier) -> dict[str, int]:
    totals = {key: 0 for key in ARCHIVE_STATUSES}
    for row in rows:
        status = classifier(row)
        totals[status] = totals.get(status, 0) + 1
    return totals


def _pct(part: int, whole: int) -> float:
    """Percentage that reserves 100 for actually complete.

    Plain rounding turned 99.96% into "100%", so the endpoint table reported
    100% row fill on the same line as 1,313 jobs still carrying gaps -- which
    reads as a contradiction rather than as "nearly there". Only part == whole
    earns 100; anything short of it stops at 99.9.
    """
    if not whole:
        return 0.0
    if part >= whole:
        return 100.0
    return min(round(part / whole * 100, 1), 99.9)


def _latest_prices_by_asset() -> dict[int, Price]:
    out: dict[int, Price] = {}
    for row in Price.objects.filter(price__gt=0).order_by("asset_id", "-fetched_at", "-id"):
        if row.asset_id not in out:
            out[row.asset_id] = row
    return out


def _owner_labels() -> dict[int, str]:
    """Nicknames for owner-minted assets, keyed by asset id.

    A property is minted per owner and its catalog `name` is the asset class, so
    the console listed someone's house as "Real Estate" while every other screen
    called it خونه کرج. For an owned row the nickname is the only name that says
    WHICH property, which is what an operator needs on an account holding two.
    Shared catalog rows are untouched: they have no owner and no nickname.
    """
    return dict(
        Holding.objects.filter(asset__owner__isnull=False)
        .exclude(display_name="")
        .values_list("asset_id", "display_name")
    )


def classify_live_asset(asset: Asset, price: Price | None, *, now) -> str:
    if asset.is_house:
        return "formula"
    if asset.is_manual:
        return "manual"
    if not asset.tse_symbol and not asset.brs_symbol:
        return "no_source"
    if price is None:
        return "missing"
    age = max(0, int((now - price.fetched_at).total_seconds()))
    return "fresh" if age <= LIVE_PRICE_FRESH_SECONDS else "stale"


def build_live_coverage(*, held_only: bool = False) -> dict:
    now = timezone.now()
    assets_qs = Asset.objects.filter(is_active=True)
    if held_only:
        held_ids = Holding.objects.values_list("asset_id", flat=True).distinct()
        assets_qs = assets_qs.filter(id__in=held_ids)
    assets = list(assets_qs.order_by("asset_class", "name"))
    latest = _latest_prices_by_asset()
    owned_labels = _owner_labels()

    by_status: dict[str, list] = {k: [] for k in LIVE_STATUSES}
    rows = []
    for asset in assets:
        price = latest.get(asset.id)
        status = classify_live_asset(asset, price, now=now)
        age_seconds = None
        if price is not None:
            age_seconds = max(0, int((now - price.fetched_at).total_seconds()))
        entry = {
            "key": asset.key,
            "name": owned_labels.get(asset.id) or asset.name_fa or asset.name,
            "asset_class": asset.asset_class,
            "status": status,
            "tse_symbol": asset.tse_symbol or "",
            "brs_symbol": asset.brs_symbol or "",
            "price": str(price.price) if price else None,
            "source": price.source if price else None,
            "age_seconds": age_seconds,
            "fetched_at": price.fetched_at.isoformat() if price else None,
        }
        by_status[status].append(entry)
        rows.append(entry)

    totals = {status: len(by_status[status]) for status in LIVE_STATUSES}
    total = len(rows)
    fetchable = total - totals["manual"] - totals["formula"] - totals["no_source"]
    priced_ok = totals["fresh"]

    symbols = {
        sym
        for asset in assets
        for sym in (asset.tse_symbol, asset.brs_symbol)
        if sym
    }
    integrity_rows = SymbolIntegrity.objects.filter(symbol__in=symbols)
    integrity = integrity_rows.aggregate(
        assessed=Count("id"),
        passing=Count("id", filter=Q(passes_gate=True)),
    )
    assessed = integrity["assessed"] or 0
    passing = integrity["passing"] or 0
    integrity_summary = {
        "assessed": assessed,
        "passing": passing,
        "failing": assessed - passing,
        "not_assessed": max(0, len(symbols) - assessed),
    }

    return {
        "scope": "held" if held_only else "catalog",
        "total_assets": total,
        "fetchable_assets": fetchable,
        "fresh_assets": priced_ok,
        "fresh_pct": _pct(priced_ok, fetchable),
        "totals": totals,
        "integrity": integrity_summary,
        "assets": rows,
        "status_labels": {
            "fresh": "Fresh live price (≤5 min)",
            "stale": "Stale live price (>5 min)",
            "missing": "No live price row",
            "manual": "Manual / settings priced",
            "formula": "Formula priced (real estate)",
            "no_source": "No API symbol configured",
        },
    }


def build_warehouse_coverage() -> dict:
    states = list(ArchiveFetchState.objects.all())
    overall = _count_statuses(states, classify_archive_state)
    total = len(states)

    missing_rows = sum(s.missing_rows for s in states)
    known_gaps = sum(s.known_gap_rows for s in states)
    stored_rows = sum(s.stored_rows for s in states)
    expected_rows = sum(s.expected_rows for s in states)

    by_endpoint = []
    live_sourced = []
    grouped: dict[str, list] = {}
    for state in states:
        grouped.setdefault(state.endpoint, []).append(state)

    for endpoint, label in ArchiveFetchState.Endpoint.choices:
        if endpoint in _RETIRED_ARCHIVE_ENDPOINTS:
            live_sourced.append({
                "endpoint": endpoint,
                "label": label,
                "note": "Live price loop — history accumulates from repeated snapshots, not per-symbol archive jobs.",
            })
            continue
        bucket = grouped.get(endpoint, [])
        endpoint_total = len(bucket)
        if endpoint_total == 0:
            continue
        counts = _count_statuses(bucket, classify_archive_state)
        endpoint_missing = sum(s.missing_rows for s in bucket)
        endpoint_stored = sum(s.stored_rows for s in bucket)
        endpoint_expected = sum(s.expected_rows for s in bucket)
        by_endpoint.append({
            "endpoint": endpoint,
            "label": label,
            "total": endpoint_total,
            "counts": counts,
            "complete_pct": _pct(counts["complete"], endpoint_total),
            "partial_pct": _pct(counts["partial"], endpoint_total),
            "failed_pct": _pct(counts["failed"], endpoint_total),
            "not_tried_pct": _pct(counts["not_tried"], endpoint_total),
            "stored_rows": endpoint_stored,
            "expected_rows": endpoint_expected,
            "missing_rows": endpoint_missing,
            "row_fill_pct": _pct(endpoint_stored, endpoint_expected) if endpoint_expected else None,
        })

    by_endpoint.sort(key=lambda row: (-row["total"], row["label"]))

    return {
        "total_jobs": total,
        "counts": overall,
        "complete_pct": _pct(overall["complete"], total),
        "partial_pct": _pct(overall["partial"], total),
        "failed_pct": _pct(overall["failed"], total),
        "not_tried_pct": _pct(overall["not_tried"], total),
        "stored_rows": stored_rows,
        "expected_rows": expected_rows,
        "missing_rows": missing_rows,
        "known_gap_rows": known_gaps,
        "row_fill_pct": _pct(stored_rows, expected_rows) if expected_rows else None,
        "by_endpoint": by_endpoint,
        "live_sourced": live_sourced,
        "status_labels": {
            "complete": "Payload verified complete",
            "partial": "Tried — gaps remain (not verified)",
            "failed": "Tried — consecutive failures",
            "not_tried": "Never attempted",
        },
    }


def build_table_coverage(database_rows: list[dict]) -> dict:
    by_key = {row["key"]: row for row in database_rows}
    live = [by_key[k] for k in LIVE_TABLE_KEYS if k in by_key]
    warehouse = [by_key[k] for k in WAREHOUSE_TABLE_KEYS if k in by_key]
    return {
        "live_tables": live,
        "warehouse_tables": warehouse,
        "live_row_total": sum(int(r.get("count") or 0) for r in live),
        "warehouse_row_total": sum(int(r.get("count") or 0) for r in warehouse),
    }


def build_coverage_report(*, database_rows: list[dict]) -> dict:
    return {
        "generated_at": timezone.now().isoformat(),
        "live": {
            "catalog": build_live_coverage(held_only=False),
            "held": build_live_coverage(held_only=True),
        },
        "warehouse": build_warehouse_coverage(),
        "tables": build_table_coverage(database_rows),
    }

ARCHIVE_STATUS_PRIORITY = {"failed": 0, "partial": 1, "not_tried": 2, "complete": 3}
LIVE_STATUS_SORT = {"fresh": 0, "stale": 1, "missing": 2, "manual": 3, "formula": 4, "no_source": 5}
# Spelled out rather than abbreviated: the console renders reason codes straight
# through `humanize()`, which turned "n_a" into the meaningless "N a" in the
# 179-day gate column. Its sibling here was already "not_assessed".
INTEGRITY_SORT = {"fail": 0, "not_assessed": 1, "pass": 2, "not_applicable": 3}


def _asset_symbols(asset: Asset) -> list[str]:
    symbols = []
    for value in (asset.tse_symbol, asset.brs_symbol):
        if value and value not in symbols:
            symbols.append(value)
    return symbols


def _worst_archive_status(states: list[ArchiveFetchState]) -> str | None:
    if not states:
        return None
    statuses = [classify_archive_state(state) for state in states]
    return min(statuses, key=lambda status: ARCHIVE_STATUS_PRIORITY.get(status, 99))


def _integrity_status(symbols: list[str], integrity_map: dict[str, SymbolIntegrity]) -> str:
    if not symbols:
        return "not_applicable"
    rows = [integrity_map[sym] for sym in symbols if sym in integrity_map]
    if not rows:
        return "not_assessed"
    if all(row.passes_gate for row in rows):
        return "pass"
    if any(not row.passes_gate for row in rows):
        return "fail"
    return "not_assessed"


def list_ops_assets(
    *,
    asset_class: str | None = None,
    search: str | None = None,
    live_status: str | None = None,
    held_only: bool = False,
    integrity: str | None = None,
    ordering: str = "name",
) -> dict:
    """Paginate-ready asset catalog rows for the ops Asset browser."""
    now = timezone.now()
    qs = Asset.objects.filter(is_active=True)
    if asset_class and asset_class != "all":
        qs = qs.filter(asset_class=asset_class)
    if search:
        term = search.strip()
        if term:
            qs = qs.filter(
                Q(key__icontains=term)
                | Q(name__icontains=term)
                | Q(name_fa__icontains=term)
                | Q(tse_symbol__icontains=term)
                | Q(brs_symbol__icontains=term)
            )
    assets = list(qs.order_by("asset_class", "name"))
    held_ids = set(Holding.objects.values_list("asset_id", flat=True).distinct())
    latest = _latest_prices_by_asset()
    owned_labels = _owner_labels()

    symbols = {
        sym
        for asset in assets
        for sym in _asset_symbols(asset)
    }
    integrity_map = {
        row.symbol: row
        for row in SymbolIntegrity.objects.filter(symbol__in=symbols)
    }
    archive_by_symbol: dict[str, list[ArchiveFetchState]] = {}
    for state in ArchiveFetchState.objects.filter(symbol__in=symbols):
        archive_by_symbol.setdefault(state.symbol, []).append(state)

    rows = []
    for asset in assets:
        if held_only and asset.id not in held_ids:
            continue
        price = latest.get(asset.id)
        status = classify_live_asset(asset, price, now=now)
        if live_status and live_status != "all" and status != live_status:
            continue
        age_seconds = None
        if price is not None:
            age_seconds = max(0, int((now - price.fetched_at).total_seconds()))
        symbols_for_asset = _asset_symbols(asset)
        archive_states = [
            state
            for sym in symbols_for_asset
            for state in archive_by_symbol.get(sym, [])
        ]
        gate = _integrity_status(symbols_for_asset, integrity_map)
        if integrity and integrity != "all" and gate != integrity:
            continue
        rows.append({
            "key": asset.key,
            "name": asset.name,
            "name_fa": asset.name_fa or "",
            "display_name": owned_labels.get(asset.id) or asset.name_fa or asset.name,
            "asset_class": asset.asset_class,
            "tse_symbol": asset.tse_symbol or "",
            "brs_symbol": asset.brs_symbol or "",
            "live_status": status,
            "price": str(price.price) if price else None,
            "source": price.source if price else None,
            "age_seconds": age_seconds,
            "fetched_at": price.fetched_at.isoformat() if price else None,
            "integrity_status": gate,
            "archive_status": _worst_archive_status(archive_states),
            "archive_jobs": len(archive_states),
            "held": asset.id in held_ids,
            "is_manual": asset.is_manual,
            "is_house": asset.is_house,
        })

    reverse = ordering.startswith("-")
    field = ordering.lstrip("-")
    if field == "live_status":
        rows.sort(
            key=lambda row: LIVE_STATUS_SORT.get(row["live_status"], 99),
            reverse=reverse,
        )
    elif field == "integrity_status":
        rows.sort(
            key=lambda row: INTEGRITY_SORT.get(row["integrity_status"], 99),
            reverse=reverse,
        )
    elif field == "age_seconds":
        rows.sort(
            key=lambda row: row["age_seconds"] if row["age_seconds"] is not None else 10**9,
            reverse=reverse,
        )
    elif field in {"name", "key", "asset_class", "display_name"}:
        rows.sort(key=lambda row: (row.get(field) or "").lower(), reverse=reverse)
    else:
        rows.sort(key=lambda row: (row["display_name"] or row["name"] or "").lower())

    class_counts: dict[str, int] = {}
    for asset in Asset.objects.filter(is_active=True):
        class_counts[asset.asset_class] = class_counts.get(asset.asset_class, 0) + 1

    return {
        "generated_at": now.isoformat(),
        "count": len(rows),
        "asset_classes": [
            {"value": "all", "label": "All classes", "count": sum(class_counts.values())},
            *[
                {"value": value, "label": label, "count": class_counts.get(value, 0)}
                for value, label in Asset.AssetClass.choices
            ],
        ],
        "results": rows,
    }

