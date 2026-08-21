"""Celery tasks for the real-time price loop.

`fetch_and_publish` is the single heartbeat: fetch global prices, persist them,
snapshot every user's valuation, and bust the cache. The fetch_prices management
command calls the same body so GitHub Actions (the dead-man's switch) and Celery
beat stay in lockstep.

The task keeps its historical name: it once also broadcast the price map over Redis
pub/sub for SSE clients, but nothing ever subscribed, so that half was removed.
Clients read `/api/valuation/` on a poll instead.
"""
import logging
import uuid
from decimal import Decimal

from celery import shared_task
from django.db import transaction

from accounts.models import User
from portfolio.models import Asset, DailyPriceAverage, Price, Snapshot
from portfolio.services import asset_value, invalidate_prices_cache
from portfolio.services.valuation import (
    _archive_replacements,
    current_market_state,
    guard_price_map,
    tse_market_is_closed,
)
from portfolio.live.extractor import extract_standard_prices
from portfolio.live.fetcher import api_settings_from_django, fetch_all_markets
from portfolio.live.redis_client import get_redis
import datetime as dt

from django.conf import settings
from django.db.models import Avg
from django.db.models.functions import TruncDate
from django.utils import timezone

from portfolio.optimization_models import OptimizationSnapshot
from portfolio.services.optimization import (
    MixedUnitUniverseBlocked,
    SolverError,
    UniverseTooSmall,
    optimize,
)

logger = logging.getLogger(__name__)


def _overlay_usdt_irt_from_warehouse(prices: dict) -> dict:
    """Use archived USDT/IRT when the free feed only echoed the USD peg."""
    from decimal import Decimal
    from marketdata.models import GoldCurrencyHistory

    usd = Decimal(str(prices.get("usd_cash") or 0))
    current = Decimal(str(prices.get("usdt_irt") or 0))
    if current > 0 and usd > 0 and current != usd:
        return prices

    close = (
        GoldCurrencyHistory.objects.filter(symbol="USDT_IRT", close_price__gt=0)
        .order_by("-date")
        .values_list("close_price", flat=True)
        .first()
    )
    if close:
        warehouse = Decimal(str(close))
        if warehouse > 0 and warehouse != usd:
            prices["usdt_irt"] = warehouse
    return prices


def _persistable_prices(live_prices, resolved_prices, archive_replacements):
    """Return fresh provider/archive observations, excluding forward-filled values."""
    priced = {
        key: value
        for key, value in resolved_prices.items()
        if value > 0 and (Decimal(str(live_prices[key])) > 0 or key in archive_replacements)
    }
    return priced, {
        key: "ARCHIVE" if key in archive_replacements else "API"
        for key in priced
    }


def run_price_fetch(*, dry_run=False):
    """Fetch and persist the latest price map.

    Network I/O and extraction stay OUTSIDE the transaction (C2 fix): only the
    writes are atomic, so a slow market API never holds an open DB connection.
    Returns {"priced": <float map>, "written": bool}.
    """
    redis_client = get_redis()
    lock_key = "lock:price_fetch"
    lock_token = None
    if redis_client and not dry_run:
        lock_token = uuid.uuid4().hex
        if not redis_client.set(lock_key, lock_token, ex=150, nx=True):
            logger.warning("Another price fetch is already running (failed to acquire Redis lock). Skipping.")
            # Flagged so the caller can report a skip rather than a failed fetch:
            # an empty price map alone cannot tell the two apart.
            return {"priced": {}, "written": False, "skipped": "lock_held"}

    try:
        raw = fetch_all_markets(api_settings_from_django())
        prices = extract_standard_prices(raw)
        prices = _overlay_usdt_irt_from_warehouse(prices)
        active_keys = set(
            Asset.objects.filter(is_active=True, is_house=False).values_list("key", flat=True)
        )
        live_prices = {
            key: prices.get(key, 0)
            for key in active_keys
        }
        current_state = current_market_state()
        verified_close_keys = set()
        archive_replacements = _archive_replacements(
            live_prices,
            prefer_closed_tse=tse_market_is_closed(current_state),
            verified_close_keys=verified_close_keys,
        )
        resolved_prices = guard_price_map(
            live_prices, archive_replacements=archive_replacements
        )
        # Persist provider prices and verified archive replacements. Do not stamp
        # a forward-filled prior price as if it were a fresh market observation.
        priced, sources = _persistable_prices(
            live_prices, resolved_prices, archive_replacements
        )
        snapshot_prices = resolved_prices
        public_priced = {key: float(value) for key, value in priced.items()}

        written = False
        if priced and not dry_run:
            with transaction.atomic():
                _write_prices(priced, sources=sources)
                _write_snapshots(
                    snapshot_prices,
                    session_close_keys=verified_close_keys,
                )
            invalidate_prices_cache()

            # LAZY import: avoids a circular `portfolio.tasks -> portfolio.services.returns ->
            # portfolio.models` chain at module load. Outside the transaction on
            # purpose — cache deletes are not transactional.
            from portfolio.services.returns import invalidate_returns_cache
            invalidate_returns_cache()
            written = True
        return {"priced": public_priced, "written": written}
    finally:
        if lock_token and redis_client:
            redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_token,
            )


def _write_prices(priced: dict, *, sources: dict | None = None) -> None:
    sources = sources or {}
    assets = {
        a.key: a
        for a in Asset.objects.filter(key__in=priced.keys(), is_active=True)
    }
    latest_prices = {
        row.asset.key: Decimal(str(row.price))
        for row in Price.objects.select_related("asset")
        .filter(asset__key__in=priced.keys(), price__gt=0)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    }
    rows = []
    for key, value in priced.items():
        if key not in assets:
            continue
        source = sources.get(key, "API")
        if source == "ARCHIVE" and latest_prices.get(key) == value:
            continue
        asset = assets[key]
        # BRS gold/FX and manuals are Toman. TSE stocks are stored as **Rial**
        # (see extractor._price_from_tsetmc_record) so qty×price matches the
        # 1/10-share broker hack. USD-quoted keys stay provider-native.
        from portfolio.services.returns import USD_QUOTED_KEYS

        if key in USD_QUOTED_KEYS:
            unit = Price.Unit.UNKNOWN
            verified = False
        elif asset.tse_symbol:
            unit = Price.Unit.IRR
            verified = True
        elif asset.brs_symbol or asset.is_manual:
            unit = Price.Unit.IRT
            verified = True
        else:
            unit = Price.Unit.UNKNOWN
            verified = False
        rows.append(
            Price(
                asset=asset,
                price=value,
                source=source,
                price_unit=unit,
                price_unit_verified=verified,
            )
        )
    if rows:
        Price.objects.bulk_create(rows, batch_size=500)
        logger.info("Wrote %d price rows.", len(rows))


from datetime import timedelta
from django.utils import timezone


def _write_snapshots(
    priced: dict,
    *,
    session_close_keys: set[str] | None = None,
) -> None:
    """Snapshot each active user's net worth in bulk and fill downtime gaps safely."""
    guarded_priced = guard_price_map(priced)
    prices = {k: Decimal(str(v)) for k, v in guarded_priced.items()}
    session_close_keys = session_close_keys or set()
    now = timezone.now()

    # 1. Detect downtime gaps (> 4 minutes since last snapshot), capped to 60 intervals (2 hours) per run
    last_snap_time = (
        Snapshot.objects.order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    gap_timestamps = []
    if last_snap_time and (now - last_snap_time).total_seconds() > 240:
        gap_start = max(last_snap_time + timedelta(minutes=2), now - timedelta(hours=2))
        slot = gap_start
        while slot < now - timedelta(seconds=90) and len(gap_timestamps) < 60:
            gap_timestamps.append(slot)
            slot += timedelta(minutes=2)
        if gap_timestamps:
            logger.info(
                "[DOWNTIME_GAP_FILLED] Backfilling %d missing 2-minute interval snapshots from %s to %s",
                len(gap_timestamps), gap_timestamps[0].isoformat(), gap_timestamps[-1].isoformat()
            )

    # 2. Only snapshot users who actually have accounts
    active_users_qs = (
        User.objects.filter(accounts__isnull=False)
        .distinct()
        .prefetch_related("accounts__holdings__asset", "accounts__liabilities")
    )

    total_written = 0
    chunk_size = 200
    user_batch = []

    for user in active_users_qs.iterator(chunk_size=chunk_size):
        user_batch.append(user)
        if len(user_batch) >= chunk_size:
            total_written += _flush_user_snapshots(
                user_batch, prices, gap_timestamps, session_close_keys
            )
            user_batch = []

    if user_batch:
        total_written += _flush_user_snapshots(
            user_batch, prices, gap_timestamps, session_close_keys
        )

    logger.info("Wrote %d net-worth snapshots total.", total_written)


def _flush_user_snapshots(
    users: list,
    prices: dict,
    gap_timestamps: list,
    session_close_keys: set[str],
) -> int:
    """Generate and bulk_create snapshots for a small batch of users."""
    snapshots = []

    def _build_batch_snapshots(ts=None):
        batch_snaps = []
        is_est = True if ts is not None else False
        for user in users:
            user_total = Decimal("0")
            account_close_flags = []
            for account in user.accounts.all():
                account_total = Decimal("0")
                held_tse_keys = {
                    holding.asset.key
                    for holding in account.holdings.all()
                    if holding.asset.tse_symbol
                }
                is_close = (
                    not is_est
                    and bool(held_tse_keys)
                    and held_tse_keys <= session_close_keys
                )
                if held_tse_keys:
                    account_close_flags.append(is_close)
                for holding in account.holdings.all():
                    unit_price = prices.get(holding.asset.key)
                    value = asset_value(holding, unit_price)
                    account_total += value
                # Match value_account: net mortgage / other liabilities.
                for liability in account.liabilities.all():
                    account_total -= liability.amount_tomans
                user_total += account_total
                snap = Snapshot(
                    user=user,
                    account=account,
                    total_value_tomans=account_total,
                    is_estimated=is_est,
                    is_session_close=is_close,
                )
                if ts:
                    snap.timestamp = ts
                batch_snaps.append(snap)
            snap = Snapshot(
                user=user,
                account=None,
                total_value_tomans=user_total,
                is_estimated=is_est,
                is_session_close=(
                    not is_est
                    and bool(account_close_flags)
                    and all(account_close_flags)
                ),
            )
            if ts:
                snap.timestamp = ts
            batch_snaps.append(snap)
        return batch_snaps

    for gap_ts in gap_timestamps:
        snapshots.extend(_build_batch_snapshots(ts=gap_ts))

    snapshots.extend(_build_batch_snapshots(ts=None))

    if snapshots:
        Snapshot.objects.bulk_create(snapshots, batch_size=500)
    return len(snapshots)




@shared_task(
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    time_limit=90,
    soft_time_limit=75,
)
def fetch_and_publish():
    """Celery entry point; beat ticks every minute, this decides whether to fetch.

    The cadence lives here rather than in the beat schedule because it depends on
    whether the TSE is open, which beat cannot know. Overnight the market is a
    frozen order book, so polling it every two minutes just burnt quota that the
    archive backfill needed.
    """
    from marketdata.market_state import live_interval_seconds, market_state
    from marketdata.models import WorkflowRun
    from marketdata.workflows import WorkflowOutcome

    # The live lane was the one pipeline stage with no structured record at all:
    # a plain-text line that could not be grouped, counted or queried, and whose
    # provider HTTP attempts were counted into a ContextVar with no owner and then
    # discarded. It is the lane that spends quota every minute, so it is the one
    # that most needs to be answerable in the ledger alongside the archive.
    state = market_state()
    outcome = WorkflowOutcome(
        "live_prices",
        endpoint="live_tick",
        source="brsapi.ir",
        destination_table="Price",
    )
    interval = live_interval_seconds()
    redis_client = get_redis()
    if redis_client is not None:
        # NX+EX is the whole gate: the key expires exactly one interval after the
        # last accepted run, so a failed SET means "too soon".
        if not redis_client.set("marketdata:live_tick", "1", ex=interval, nx=True):
            outcome.finish(
                WorkflowRun.Outcome.SKIPPED,
                metadata={"reason": "cadence_not_elapsed", "market_state": state,
                          "interval_seconds": interval},
            )
            return {"priced": {}, "written": False, "skipped": True}

    try:
        result = run_price_fetch()
    except Exception as err:
        outcome.finish(
            WorkflowRun.Outcome.FAILED,
            error_code=type(err).__name__,
            metadata={"reason": str(err), "market_state": state},
        )
        raise
    priced = len(result["priced"])
    if not priced:
        # No prices is not a partial success. Overnight only crypto quotes, and a
        # contended lock returns the same empty shape -- neither is a degraded
        # fetch, and calling them "partial" would make the failure rate lie.
        outcome.finish(
            WorkflowRun.Outcome.SKIPPED,
            metadata={"reason": result.get("skipped") or "no_prices_available",
                      "market_state": state},
        )
        return result
    outcome.finish(
        WorkflowRun.Outcome.SUCCESS if result["written"] else WorkflowRun.Outcome.PARTIAL,
        rows_received=priced,
        rows_accepted=priced if result["written"] else 0,
        metadata={"market_state": state, "written": result["written"]},
    )
    return result


@shared_task(ignore_result=True)
def aggregate_daily_price_averages(date_str: str | None = None):
    """Roll today's live Price ticks into one DailyPriceAverage row per asset.

    Only averages source="API" ticks -- ARCHIVE-tagged rows (guard_price_map's
    fallback-to-warehouse writes, see _persistable_prices) are not a live
    observation and must not inflate sample_count or skew the average.
    """
    import jdatetime
    from datetime import timedelta
    from django.db.models import Avg, Count
    from django.utils import timezone
    from marketdata.models import WorkflowRun
    from marketdata.workflows import WorkflowOutcome

    outcome = WorkflowOutcome(
        "aggregate_daily_price_averages", destination_table="DailyPriceAverage"
    )
    today_jalali = date_str or jdatetime.date.today().strftime("%Y-%m-%d")
    since = timezone.now() - timedelta(hours=24)

    written = 0
    for asset in Asset.objects.filter(is_active=True, is_house=False):
        stats = Price.objects.filter(
            asset=asset, source="API", fetched_at__gte=since,
        ).aggregate(avg=Avg("price"), n=Count("id"))
        if not stats["n"]:
            continue
        DailyPriceAverage.objects.update_or_create(
            asset=asset, date=today_jalali,
            defaults={"avg_price": stats["avg"], "sample_count": stats["n"]},
        )
        written += 1
    outcome.finish(WorkflowRun.Outcome.SUCCESS, rows_accepted=written)
    return written


# -----------------------------------------------------------------------------
# Optimization snapshot task
# -----------------------------------------------------------------------------
@shared_task(ignore_result=True)
def run_global_optimization_snapshot(payload: dict | None = None):
    """Run optimizations and persist snapshots.

    Behavior (MVP):
      - Run one global optimization (account=None) for market reference.
      - Run one optimization per Account that has holdings (account-scoped), using
        its current holdings to compute current_weights and total_value_tomans.
      - Persist each result into OptimizationSnapshot with account set for per-
        account runs.

    The task is intended to be triggered by the brsapi webhook after new prices
    arrive. Uses lazy imports to avoid circular load issues.
    """
    try:
        from decimal import Decimal
        from django.utils import timezone
        from portfolio.services.optimization import optimize
        from .optimization_models import OptimizationSnapshot
        from .services.valuation import get_latest_prices, value_account
        from .models import Account

        now = timezone.now()
        prices = get_latest_prices()

        results = []

        # 1) Global market snapshot (account=None)
        try:
            global_result = optimize(
                scenario="max_sharpe",
                current_weights={},
                total_value_tomans=1,
                constraints=None,
                user=None,
                history_days=180,
            )
            snap = OptimizationSnapshot(
                account=None,
                scenario="max_sharpe",
                payload=global_result,
                price_version=global_result.get("price_version", ""),
                as_of=None,
            )
            snap.save()
            logger.info("Saved global OptimizationSnapshot id=%s", snap.id)
            results.append({"account": None, "snapshot_id": snap.id})
        except Exception as exc:
            logger.exception("Global optimization failed: %s", exc)

        # 2) Per-account snapshots
        # Only iterate accounts that actually have holdings to avoid waste.
        accounts_qs = Account.objects.filter(holdings__isnull=False).distinct().prefetch_related("holdings__asset")
        for account in accounts_qs.iterator():
            try:
                valuation = value_account(account, prices=prices)
                total = valuation.get("total") or Decimal("0")
                if total <= 0:
                    logger.debug("Skipping optimization for account %s: total value = %s", account.id, str(total))
                    continue

                # Build current_weights: {asset_key: weight_fraction}
                items = valuation.get("items", [])
                current_weights = {}
                for item in items:
                    key = item.get("key") or item.get("asset") or item.get("asset_key")
                    value = item.get("value")
                    if key and value is not None:
                        try:
                            frac = float(Decimal(str(value)) / Decimal(str(total)))
                        except Exception:
                            frac = 0.0
                        if frac > 0:
                            current_weights[key] = frac

                if not current_weights:
                    logger.debug("No priced holdings for account %s, skipping optimization.", account.id)
                    continue

                # Run optimize for this account (user passed so universe/account scoping works)
                try:
                    result = optimize(
                        scenario="max_sharpe",
                        current_weights=current_weights,
                        total_value_tomans=total,
                        constraints=None,
                        user=account.user,
                        history_days=180,
                    )
                except Exception as exc:
                    logger.exception("Optimization failed for account %s: %s", account.id, exc)
                    continue

                snap = OptimizationSnapshot(
                    account=account,
                    scenario=result.get("scenario", "max_sharpe"),
                    payload=result,
                    price_version=result.get("price_version", ""),
                    as_of=None,
                    created_by=None,
                )
                snap.save()
                logger.info("Saved OptimizationSnapshot id=%s for account %s", snap.id, account.id)
                results.append({"account": account.id, "snapshot_id": snap.id})
            except Exception as exc:
                logger.exception("Unexpected error while processing account %s: %s", getattr(account, "id", None), exc)
                continue

        return {"ok": True, "results": results}
    except Exception as exc:
        logger.exception("run_global_optimization_snapshot unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------------
# Nightly precompute for the "Best Possible Portfolio Overall" page: the
# market-wide optimizer over every tracked asset across four lookback windows
# and both scenarios, so BestOverallView is a pure snapshot read with no solver
# call in the request path.

WINDOWS_DAYS = (365, 1095, 1825, 3650)
SCENARIOS = ("max_sharpe", "min_volatility")


@shared_task(ignore_result=True)
def run_best_overall_snapshots():
    """One global (account=None) OptimizationSnapshot per (window, scenario)."""
    from marketdata.universe import get_candidate_universe

    universe, _ = get_candidate_universe()
    if len(universe) < 3:
        logger.warning("Candidate universe too small (%d); skipping.", len(universe))
        return {"ok": False, "reason": "universe_too_small"}

    written = []
    for window_days in WINDOWS_DAYS:
        for scenario in SCENARIOS:
            try:
                payload = optimize(
                    scenario=scenario,
                    current_weights={},
                    total_value_tomans=Decimal("1"),
                    user=None,
                    history_days=window_days,
                    universe=universe,
                    universe_mode="market",
                )
            except (UniverseTooSmall, SolverError, MixedUnitUniverseBlocked) as exc:
                logger.info(
                    "%s/%dd not solvable yet: %s", scenario, window_days, exc
                )
                continue
            snap = OptimizationSnapshot.objects.create(
                account=None,
                scenario=scenario,
                window_days=window_days,
                payload=payload,
                price_version=payload.get("price_version", ""),
            )
            written.append(snap.id)
    logger.info("Wrote %d snapshots.", len(written))
    return {"ok": True, "snapshot_ids": written}


# ----------------------------------------------------------------------
# Retention. Both are no-ops unless their *_PRUNE_ENABLED setting is on --
# deleting rows needs explicit sign-off.

def _stale_daily_groups(cutoff):
    return (
        Snapshot.objects.filter(timestamp__lt=cutoff)
        .annotate(day=TruncDate("timestamp"))
        .values("user_id", "account_id", "day")
        .annotate(avg_total=Avg("total_value_tomans"))
        .order_by()
    )


@shared_task(ignore_result=True)
def prune_snapshots():
    """Collapse Snapshot rows older than SNAPSHOT_RETENTION_DAYS to one/day.

    # ponytail: single-pass delete over the whole table; batch by user if the
    # first production run locks too long for the table's size at that point.
    Disabled by default (SNAPSHOT_PRUNE_ENABLED=0): logs what it would do and
    returns without touching any row. This is a data-deleting operation and
    must only be enabled with explicit sign-off.
    """
    cutoff = timezone.now() - dt.timedelta(days=settings.SNAPSHOT_RETENTION_DAYS)
    groups = list(_stale_daily_groups(cutoff))
    stale_row_count = Snapshot.objects.filter(timestamp__lt=cutoff).count()

    if not settings.SNAPSHOT_PRUNE_ENABLED:
        logger.info(
            "Would collapse %d stale rows into %d daily "
            "rows (cutoff=%s). Set SNAPSHOT_PRUNE_ENABLED=1 to actually run this.",
            stale_row_count, len(groups), cutoff.isoformat(),
        )
        result = {"enabled": False, "would_collapse": stale_row_count, "would_write": len(groups)}
        _ledger_prune("prune_snapshots", "Snapshot", result)
        return result

    written = 0
    with transaction.atomic():
        for group in groups:
            Snapshot.objects.filter(
                user_id=group["user_id"],
                account_id=group["account_id"],
                timestamp__date=group["day"],
                timestamp__lt=cutoff,
            ).delete()
            day_start = timezone.make_aware(dt.datetime.combine(group["day"], dt.time.min))
            Snapshot.objects.create(
                user_id=group["user_id"],
                account_id=group["account_id"],
                total_value_tomans=group["avg_total"],
                timestamp=day_start,
                is_estimated=True,
            )
            written += 1
    logger.info(
        "Collapsed %d day-groups (%d stale rows) into %d rows.",
        written, stale_row_count, written,
    )
    result = {"enabled": True, "groups_collapsed": written, "stale_rows_seen": stale_row_count}
    _ledger_prune("prune_snapshots", "Snapshot", result)
    return result


@shared_task(ignore_result=True)
def prune_prices():
    """Drop intra-day Price rows older than PRICE_RETENTION_DAYS; keep latest per asset."""
    cutoff = timezone.now() - dt.timedelta(days=settings.PRICE_RETENTION_DAYS)
    stale = Price.objects.filter(fetched_at__lt=cutoff)
    stale_count = stale.count()
    if not settings.PRICE_PRUNE_ENABLED:
        logger.info(
            "Would delete %d price rows older than %s. "
            "Set PRICE_PRUNE_ENABLED=1 to run.",
            stale_count, cutoff.isoformat(),
        )
        result = {"enabled": False, "would_delete": stale_count}
        _ledger_prune("prune_prices", "Price", result)
        return result

    latest_ids = list(
        Price.objects.order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
        .values_list("id", flat=True)
    )
    deleted, _ = stale.exclude(id__in=latest_ids).delete()
    logger.info("Deleted %d stale price rows (kept latest per asset).", deleted)
    result = {"enabled": True, "deleted": deleted, "kept_latest": len(latest_ids)}
    _ledger_prune("prune_prices", "Price", result)
    return result


def _ledger_prune(workflow, table, metadata):
    from marketdata.models import WorkflowRun
    from marketdata.workflows import WorkflowOutcome

    WorkflowOutcome(workflow, destination_table=table).finish(
        WorkflowRun.Outcome.SUCCESS,
        metadata=metadata,
    )
