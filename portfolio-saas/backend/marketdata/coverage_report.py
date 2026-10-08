"""Granular data-coverage breakdown for the /ops console.

Classifies live portfolio pricing separately from warehouse archive jobs.
States are validated against DB fields — not a binary complete/incomplete flag.
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from portfolio.models import Asset, Holding, Price, newest_prices, owner_display_names, positive_price_q

from .archive import _RETIRED_ARCHIVE_ENDPOINTS
from .evidence import LIVE_PRICE_FRESH_SECONDS
from .models import ArchiveFetchState, SymbolIntegrity

ARCHIVE_STATUSES = (
    "complete", "refresh_due", "partial", "failed", "awaiting_data", "not_tried",
    # Added 2026-09-06. `blacklisted` and `suspended_at` have been on the model
    # for weeks but no status named them, so a symbol the archive had given up
    # on was counted under whatever its stale row counts happened to say --
    # usually "failed", sometimes "complete". "How many symbols can we not
    # fetch?" was therefore unanswerable from the console, which is the one
    # question that decides whether a coverage number is a bug or a ceiling.
    "unfetchable", "suspended",
)
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
    """Six states, because "not verified" is four different situations.

    `verified_complete` is deliberately re-armed to False whenever a state is
    due for another pass -- new sessions print every day, so a payload that was
    complete last night is stale tonight. That is the design working, not a
    defect, and it must not read as damage.

    This used to be a four-way ladder whose last rung was a bare `return
    "partial"`, labelled "tried -- gaps remain". Everything that was not
    complete, not untried and not failing fell into it, so a state re-armed for
    refresh and then parked by the daily quota ceiling -- attempted, zero
    failures, zero missing rows -- was reported as carrying gaps. 5,978 of
    6,383 "partial" jobs (52.8% of the whole warehouse) were in that position:
    stored_rows == expected_rows exactly, nothing missing, waiting only for
    tomorrow's budget.

    So the two honest questions are asked separately: does this state owe rows
    (`missing_rows`), and has it ever landed a payload (`last_success_at`)?
    Failures are tested before either, because a failing state is the operator's
    problem whatever its row counts say.

    Unfetchability is tested before everything, including `verified_complete`.
    A blacklisted or suspended state is not going to be fetched no matter what
    its row counts say, and reporting it under any other label overstates both
    the coverage we have and the backlog we can still work through.
    """
    if state.blacklisted:
        return "unfetchable"
    if state.suspended_at is not None:
        return "suspended"
    if state.verified_complete:
        return "complete"
    if state.consecutive_failures > 0:
        return "failed"
    if state.last_attempt_at is None:
        return "not_tried"
    if state.last_success_at is None:
        return "awaiting_data"
    if state.missing_rows == 0:
        return "refresh_due"
    return "partial"


def state_stale_days(state: ArchiveFetchState, *, now) -> int | None:
    """Whole days since this state last landed a payload, or None if never.

    This is the number the refresh queue is ordered by: the point of re-arming
    a complete state is to fetch whichever symbol has gone longest without an
    update, so "how far behind is it" has to be a visible quantity rather than
    an implicit consequence of `next_attempt_at`.
    """
    if state.last_success_at is None:
        return None
    return max(0, (now - state.last_success_at).days)


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
    # Was a Python walk over every positive tick in the retention window to
    # keep the first per asset -- 12 s on the admin asset list in production.
    return {row.asset_id: row for row in newest_prices(positive_price_q())}


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
    owned_labels = owner_display_names()

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


def _refresh_backlog(states, *, now, limit: int = 10) -> dict:
    """How far behind the refresh queue is, and which symbols are worst.

    Only states that have landed a payload can be "behind" -- one that has never
    succeeded is not stale, it is unstarted, and mixing the two would put a
    brand-new symbol at the top of a queue meant to surface neglected ones.
    """
    aged = [
        (state, days)
        for state in states
        for days in (state_stale_days(state, now=now),)
        if days is not None
    ]
    if not aged:
        return {"tracked": 0, "median_days": 0, "max_days": 0, "oldest": []}
    aged.sort(key=lambda pair: pair[1], reverse=True)
    days_sorted = sorted(days for _, days in aged)
    return {
        "tracked": len(aged),
        "median_days": days_sorted[len(days_sorted) // 2],
        "max_days": aged[0][1],
        "oldest": [
            {
                "symbol": state.symbol,
                "endpoint": state.endpoint,
                "stale_days": days,
                "status": classify_archive_state(state),
            }
            for state, days in aged[:limit]
        ],
    }


def build_warehouse_coverage() -> dict:
    now = timezone.now()
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
            # "Healthy" is complete plus refresh-due: both hold every row they
            # expect, and the only difference is whether another pass is owed.
            "healthy_pct": _pct(counts["complete"] + counts["refresh_due"], endpoint_total),
            "refresh_due_pct": _pct(counts["refresh_due"], endpoint_total),
            "partial_pct": _pct(counts["partial"], endpoint_total),
            "failed_pct": _pct(counts["failed"], endpoint_total),
            "awaiting_data_pct": _pct(counts["awaiting_data"], endpoint_total),
            "not_tried_pct": _pct(counts["not_tried"], endpoint_total),
            "stored_rows": endpoint_stored,
            "expected_rows": endpoint_expected,
            "missing_rows": endpoint_missing,
            "row_fill_pct": _pct(endpoint_stored, endpoint_expected) if endpoint_expected else None,
            "refresh_backlog": _refresh_backlog(bucket, now=now, limit=5),
        })

    by_endpoint.sort(key=lambda row: (-row["total"], row["label"]))

    return {
        "total_jobs": total,
        "counts": overall,
        "complete_pct": _pct(overall["complete"], total),
        "healthy_pct": _pct(overall["complete"] + overall["refresh_due"], total),
        "refresh_due_pct": _pct(overall["refresh_due"], total),
        "partial_pct": _pct(overall["partial"], total),
        "failed_pct": _pct(overall["failed"], total),
        "awaiting_data_pct": _pct(overall["awaiting_data"], total),
        "not_tried_pct": _pct(overall["not_tried"], total),
        "stored_rows": stored_rows,
        "expected_rows": expected_rows,
        "missing_rows": missing_rows,
        "known_gap_rows": known_gaps,
        "row_fill_pct": _pct(stored_rows, expected_rows) if expected_rows else None,
        "by_endpoint": by_endpoint,
        "live_sourced": live_sourced,
        "refresh_backlog": _refresh_backlog(states, now=now),
        "symbol_census": build_symbol_census(states),
        "status_labels": {
            "complete": "Verified complete",
            "refresh_due": "Complete — refresh due",
            "partial": "Rows still missing",
            "failed": "Failing — needs attention",
            "awaiting_data": "Attempted — no payload yet",
            "not_tried": "Never attempted",
            "unfetchable": "Blacklisted — will not be fetched",
            "suspended": "Suspended — auto-probed weekly",
        },
    }


def build_symbol_census(states=None) -> dict:
    """How many distinct SYMBOLS the archive has, has given up on, or has never touched.

    Deliberately counted per symbol, not per job. Every other number in this
    module counts `(symbol, endpoint)` states, so a symbol with eight endpoints
    contributes eight rows and "1,331 pending" reads as a symbol count when it
    is not. The question this answers -- how much of the universe do we actually
    hold, and how much can we never hold -- is a question about symbols.

    These buckets are exclusive. A symbol whose every endpoint is blacklisted
    or suspended is `unfetchable`, even if an earlier pass once landed rows --
    the question is what we can still work, not what we once held. A symbol
    counts as `fetched` only when at least one endpoint is still live and has
    landed a payload. One blocked endpoint out of eight is
    `partially_unfetchable`, not a lost symbol.
    """
    if states is None:
        states = list(ArchiveFetchState.objects.all())

    by_symbol: dict[str, list] = {}
    for state in states:
        by_symbol.setdefault(state.symbol, []).append(state)

    census = {
        "symbols_total": len(by_symbol),
        "fetched": 0,
        "never_fetched": 0,
        "unfetchable": 0,
        "partially_unfetchable": 0,
        "attempted_never_landed": 0,
    }
    for rows in by_symbol.values():
        blocked = [r for r in rows if r.blacklisted or r.suspended_at is not None]
        if len(blocked) == len(rows):
            census["unfetchable"] += 1
            continue
        if blocked:
            census["partially_unfetchable"] += 1
        if any(r.last_success_at is not None for r in rows):
            census["fetched"] += 1
        elif any(r.last_attempt_at is not None for r in rows):
            # Tried and came back with nothing -- a different problem from
            # never having been reached, and the two were previously merged.
            census["attempted_never_landed"] += 1
        else:
            census["never_fetched"] += 1

    census["fetched_pct"] = _pct(census["fetched"], census["symbols_total"])
    return census


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

ARCHIVE_STATUS_PRIORITY = {
    "failed": 0, "partial": 1,
    # Unknown statuses used to default to 99, which is *better* than complete.
    # A symbol with one blacklisted job and one verified job then rolled up as
    # complete in the asset catalog.
    "unfetchable": 2, "suspended": 3,
    "awaiting_data": 4, "not_tried": 5,
    "refresh_due": 6, "complete": 7,
}
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
    owned_labels = owner_display_names()

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
