"""Celery tasks for the real-time price loop.

`fetch_and_publish` is the single heartbeat: fetch global prices, persist them,
and bust the cache. The fetch_prices management
command calls the same body so GitHub Actions (the dead-man's switch) and Celery
beat stay in lockstep.

The task keeps its historical name: it once also broadcast the price map over Redis
pub/sub for SSE clients, but nothing ever subscribed, so that half was removed.
Clients read `/api/valuation/` on a poll instead.
"""
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP

from celery import shared_task
from django.db import transaction

from accounts.models import User
from .models import (
    Account,
    Asset,
    DailyPriceAverage,
    Holding,
    LedgerEntry,
    Price,
    Snapshot,
    USD_QUOTED_KEYS,
    positive_price_q,
)
from portfolio.services import asset_value, get_latest_prices, invalidate_prices_cache
from portfolio.services.valuation import (
    _archive_replacements,
    current_market_state,
    fetch_sessions,
    guard_price_map,
)
from portfolio.live.extractor import apply_instrument_prices, extract_standard_prices
from portfolio.live.fetcher import api_settings_from_django, fetch_all_markets
from portfolio.live.redis_client import get_redis
import datetime as dt

from django.conf import settings
from django.utils import timezone

from portfolio.optimization_models import save_current_optimization
from django.core.cache import cache
from marketdata.models import GoldCurrencyHistory, WorkflowRun
from marketdata.workflows import WorkflowOutcome
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

# `portfolio.services.optimization` imports cvxpy, which drags in scs and its
# bundled OpenBLAS. Importing it here put that library into every process that
# loads this module -- including the live price worker, which never solves
# anything. Both optimizer tasks below import it inside the function instead.
# See the OPENBLAS_CORETYPE note in docker-compose.prod.yml for why that
# library is hazardous on the deployed vCPU.

logger = logging.getLogger(__name__)


def _overlay_usdt_irt_from_warehouse(prices: dict) -> dict:
    """Use archived USDT/IRT when the free feed only echoed the USD peg."""
    from decimal import Decimal

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


def _source_for(key, archive_replacements):
    """Which of the three things a price actually is.

    The Swiss bars have no provider, so `extract_standard_prices` fills them from
    `settings.MANUAL_PRICES` -- a constant, injected into the live map on every
    cycle like any quote. Labelling that "API" made an operator-typed number read
    as a working feed: the Ops console showed the bars sourced API and 244s old,
    against a dashboard that called the same holdings a 3-day-old manual
    valuation. Worse, a constant can never look stale, so the freshness panel
    could not have reported those two assets going dark.
    """
    if key in archive_replacements:
        return "ARCHIVE"
    if key in settings.MANUAL_PRICES:
        return "MANUAL"
    return "API"


def _persistable_prices(live_prices, resolved_prices, archive_replacements):
    """Return fresh provider/archive observations, excluding forward-filled values."""
    priced = {
        key: value
        for key, value in resolved_prices.items()
        if value > 0 and (Decimal(str(live_prices[key])) > 0 or key in archive_replacements)
    }
    return priced, {key: _source_for(key, archive_replacements) for key in priced}


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
        prices = apply_instrument_prices(
            raw,
            Asset.objects.filter(is_active=True, is_house=False).values_list(
                "key", "tse_symbol", "brs_symbol"
            ),
            prices,
        )
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
            live_fetched_at=fetch_sessions(live_prices, fetched_at=timezone.now()),
            market_state=current_state,
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
        public_priced = {key: float(value) for key, value in priced.items()}

        written = False
        if priced and not dry_run:
            with transaction.atomic():
                _write_prices(
                    priced, sources=sources,
                    normalized_foreign_keys=set(priced) & set(USD_QUOTED_KEYS),
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


def _write_prices(
    priced: dict, *, sources: dict | None = None,
    normalized_foreign_keys: set[str] | None = None,
) -> None:
    sources = sources or {}
    normalized_foreign_keys = normalized_foreign_keys or set()
    assets = {
        a.key: a
        for a in Asset.objects.filter(key__in=priced.keys(), is_active=True)
    }
    latest_rows = {
        row.asset.key: row
        for row in Price.objects.select_related("asset")
        .filter(positive_price_q(), asset__key__in=priced.keys())
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    }
    latest_prices = {key: Decimal(str(row.price)) for key, row in latest_rows.items()}
    rows = []
    for key, value in priced.items():
        if key not in assets:
            continue
        source = sources.get(key, "API")
        # The owner's own mark outranks the settings default. `MANUAL_PRICES` is
        # a fallback for a bar nobody has priced by hand, but the extractor
        # injects it into the live map on EVERY cycle, so editing a Swiss bar on
        # the dashboard saved and was overwritten by the constant about four
        # minutes later -- the edit box worked and the number would not stay.
        # `record_manual_price` writes source "manual"; this path writes
        # "MANUAL", so the two are distinguishable.
        held = latest_rows.get(key)
        if source == "MANUAL" and held is not None and held.source == "manual":
            continue
        # A repeat of a price nobody re-observed is not an observation. Archive
        # replays and manual constants both re-present the same number every
        # cycle, so writing a row for them restamped `fetched_at` and made the
        # age column mean "when we last looped" rather than "when this price last
        # moved" -- which is the whole question the freshness panel asks.
        if source in ("ARCHIVE", "MANUAL") and latest_prices.get(key) == value:
            previous = latest_rows.get(key)
            if not (
                source == "ARCHIVE"
                and key in USD_QUOTED_KEYS
                and previous is not None
                and not (
                    previous.price_unit == Price.Unit.IRT
                    and previous.price_unit_verified
                )
            ):
                continue
        asset = assets[key]
        # BRS gold/FX and manuals are Toman. TSE stocks are stored as **Rial**
        # (see extractor._price_from_tsetmc_record); valuation divides the
        # quantity-times-price product by ten. Seed foreign keys are Toman only when
        # the fetch path explicitly normalized their declared provider unit.
        if key in USD_QUOTED_KEYS:
            verified = source == "ARCHIVE" or key in normalized_foreign_keys
            unit = Price.Unit.IRT if verified else Price.Unit.UNKNOWN
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


def _write_snapshots(prices: dict, *, day, session_close_keys: set[str]) -> int:
    """Write one completed Tehran day per scope; retries update the same rows."""
    from zoneinfo import ZoneInfo

    close_at = dt.datetime.combine(
        day, dt.time.max, tzinfo=ZoneInfo("Asia/Tehran")
    ).astimezone(dt.timezone.utc)
    users = (
        User.objects.filter(accounts__isnull=False)
        .distinct()
        .prefetch_related("accounts__holdings__asset", "accounts__liabilities")
    )
    written = 0
    for user in users.iterator(chunk_size=200):
        user_total = Decimal("0")
        account_close_flags = []
        for account in user.accounts.all():
            account_total = Decimal("0")
            held_tse_keys = {
                holding.asset.key for holding in account.holdings.all()
                if holding.asset.tse_symbol
            }
            is_close = bool(held_tse_keys) and held_tse_keys <= session_close_keys
            if held_tse_keys:
                account_close_flags.append(is_close)
            for holding in account.holdings.all():
                account_total += asset_value(holding, prices.get(holding.asset.key))
            # Same net worth as value_account: holdings + cash - debt.
            account_total += account.cash_balance_tomans or Decimal("0")
            for liability in account.liabilities.all():
                account_total -= liability.outstanding_tomans()
            account_total = account_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            user_total += account_total
            Snapshot.objects.update_or_create(
                user=user, account=account, day=day,
                defaults={
                    "timestamp": close_at, "total_value_tomans": account_total,
                    "is_estimated": False, "is_session_close": is_close,
                },
            )
            written += 1
        Snapshot.objects.update_or_create(
            user=user, account=None, day=day,
            defaults={
                "timestamp": close_at, "total_value_tomans": user_total,
                "is_estimated": False,
                "is_session_close": bool(account_close_flags) and all(account_close_flags),
            },
        )
        written += 1
    return written


@shared_task(ignore_result=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def write_daily_net_worth_snapshot():
    """At 00:01 Tehran, seal the previous day; today's point stays dynamic."""
    from zoneinfo import ZoneInfo

    day = timezone.localtime(timezone.now(), ZoneInfo("Asia/Tehran")).date() - dt.timedelta(days=1)
    latest = get_latest_prices()
    verified_close_keys = set()
    replacements = _archive_replacements(
        latest,
        live_fetched_at=fetch_sessions(latest, fetched_at=timezone.now()),
        market_state=current_market_state(),
        verified_close_keys=verified_close_keys,
    )
    prices = guard_price_map(latest, archive_replacements=replacements)
    count = _write_snapshots(prices, day=day, session_close_keys=verified_close_keys)
    logger.info("Sealed %d net-worth snapshots for Tehran day %s.", count, day)
    return {"day": day.isoformat(), "rows": count}




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

    outcome = WorkflowOutcome(
        "aggregate_daily_price_averages", destination_table="DailyPriceAverage"
    )
    today_jalali = date_str or jdatetime.date.today().strftime("%Y-%m-%d")
    since = timezone.now() - timedelta(hours=24)

    written = 0
    for asset in Asset.objects.filter(is_active=True, is_house=False):
        ticks = Price.objects.filter(asset=asset, source="API", fetched_at__gte=since)
        if asset.key in USD_QUOTED_KEYS:
            ticks = ticks.filter(price_unit=Price.Unit.IRT, price_unit_verified=True)
        stats = ticks.aggregate(
            avg=Avg("price_foreign" if asset.key in USD_QUOTED_KEYS else "price_iranian"),
            n=Count("id"),
        )
        if not stats["n"]:
            continue
        DailyPriceAverage.objects.update_or_create(
            asset=asset, date=today_jalali,
            defaults={"avg_price": stats["avg"], "sample_count": stats["n"]},
        )
        written += 1
    outcome.finish(WorkflowRun.Outcome.SUCCESS, rows_accepted=written)
    try:
        sweep_my_optimal_snapshots.delay()
    except Exception as exc:
        logger.warning("Could not enqueue sweep_my_optimal_snapshots: %s", exc)
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
        from portfolio.services.optimization import optimize
        from .services.valuation import get_latest_prices, value_account
        from .models import Account

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
            snap = save_current_optimization(
                account=None,
                scenario="max_sharpe",
                window_days=180,
                payload=global_result,
                price_version=global_result.get("price_version", ""),
                as_of=None,
            )
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
                # Weights are shares of what is invested, not of net worth:
                # `total` also carries cash and debt, which the optimizer
                # does not hold as assets.
                total = sum(
                    (Decimal(str(i["value"])) for i in valuation.get("items", [])
                     if i.get("value") is not None),
                    Decimal("0"),
                )
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

                snap = save_current_optimization(
                    account=account,
                    scenario=result.get("scenario", "max_sharpe"),
                    window_days=180,
                    payload=result,
                    price_version=result.get("price_version", ""),
                    as_of=None,
                    created_by=None,
                )
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
GUIDANCE_SCENARIO = "risk_parity"


@shared_task(ignore_result=True)
def run_best_overall_snapshots():
    """One global (account=None) OptimizationSnapshot per (window, scenario)."""
    from marketdata.universe import get_candidate_universe
    from portfolio.services.optimization import (
        MixedUnitUniverseBlocked,
        SolverError,
        UniverseTooSmall,
        optimize,
    )

    universe, _ = get_candidate_universe()
    if len(universe) < 3:
        logger.warning("Candidate universe too small (%d); skipping.", len(universe))
        return {"ok": False, "reason": "universe_too_small"}

    written = []
    for window_days in WINDOWS_DAYS:
        for scenario in (*SCENARIOS, *((GUIDANCE_SCENARIO,) if window_days in (365, 1095) else ())):
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
            snap = save_current_optimization(
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

@shared_task(ignore_result=True)
def prune_snapshots():
    """Compatibility no-op: daily net-worth history is retained indefinitely."""
    return {"enabled": False, "retained": Snapshot.objects.count()}


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


@shared_task(ignore_result=True)
def prune_expired_refresh_tokens():
    """Delete refresh tokens whose own expiry has already passed.

    `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` mean every refresh
    writes an `OutstandingToken` row and blacklists the one it replaced. An
    access token lives 30 minutes, so an active user mints dozens of rows a day
    and nothing ever removed them: two append-only tables on a box with a disk
    budget, and `accounts.views._revoke_all` walking every row a user has ever
    held to blacklist tokens that expired months ago.

    Deleting a token past `expires_at` is not a retention policy decision --
    the token is already refused by `RefreshToken()` on the way in, so the row
    can only cost storage and work. That is why this one has no
    `*_PRUNE_ENABLED` gate: `prune_snapshots` and `prune_prices` destroy the
    only copy of real user history and must be signed off; this destroys
    credentials that stopped working before the sweep ran.

    The delete cascades to `BlacklistedToken` (its FK to `OutstandingToken` is
    the primary key), so both tables are bounded by one statement.
    """

    deleted, _ = OutstandingToken.objects.filter(
        expires_at__lt=timezone.now()
    ).delete()
    logger.info("Deleted %d expired refresh tokens.", deleted)
    result = {"deleted": deleted}
    _ledger_prune("prune_expired_refresh_tokens", "OutstandingToken", result)
    return result


def _ledger_prune(workflow, table, metadata):

    WorkflowOutcome(workflow, destination_table=table).finish(
        WorkflowRun.Outcome.SUCCESS,
        metadata=metadata,
    )


# -----------------------------------------------------------------------------
# Per-account MyOptimal snapshot precompute & background refresh
# -----------------------------------------------------------------------------
def refresh_if_stale(account, basis: str = "real_toman", *, force: bool = False):
    """Compute and persist an OptimizationSnapshot for an account if stale.

    Idempotent: uses a cache lock so concurrent triggers cannot run duplicate
    optimizations. Skips if the latest snapshot is younger than 15 minutes
    and the scoped price/ledger fingerprint matches.
    """
    from django.utils import timezone
    from portfolio.optimization_models import OptimizationSnapshot
    from portfolio.services.returns import _price_version_fingerprint
    # From the concern module, not the package root: `views/__init__` is a
    # compatibility layer that re-exports the PUBLIC surface, and this helper is
    # private to the analytics views.
    from portfolio.views.analytics import _compute_my_optimal_payload

    holding_q = Holding.objects.filter(account=account)
    ledger_q = LedgerEntry.objects.filter(account=account)
    max_ledger_id = ledger_q.order_by("-id").values_list("id", flat=True).first() or 0
    max_holding_id = holding_q.order_by("-id").values_list("id", flat=True).first() or 0
    hidden_fp = "-".join(
        str(i) for i in sorted(
            holding_q.filter(is_hidden=True).values_list("id", flat=True)
        )
    )
    current_fp = (
        f"{_price_version_fingerprint(holding_q.values_list('asset__key', flat=True))}"
        f":{max_ledger_id}:{max_holding_id}:{hidden_fp}"
    )

    snap = (
        OptimizationSnapshot.objects
        .filter(account=account, scenario="my_optimal", basis=basis)
        .order_by("-created_at")
        .first()
    )

    now = timezone.now()
    if not force and snap is not None:
        if (now - snap.created_at).total_seconds() < 900 and snap.price_version == current_fp:
            return snap

    lock_key = f"lock:my_optimal_refresh:{account.id}:{basis}"
    if not cache.add(lock_key, 1, timeout=60):
        return snap

    try:
        payload = _compute_my_optimal_payload(account.user, account, basis)
        if not payload or not isinstance(payload, dict) or "windows" not in payload:
            return None
        if not any(w.get("status") == "ok" for w in payload.get("windows", [])):
            return None

        payload["as_of"] = now.isoformat()
        snap = save_current_optimization(
            account=account,
            scenario="my_optimal",
            basis=basis,
            window_days=0,
            payload=payload,
            price_version=current_fp,
            as_of=now,
            created_by=account.user,
        )
        cache_key = f"my_optimal:{account.user_id}:{account.id}:{basis}:nall:vany:{current_fp}"
        cache.set(cache_key, payload, 300)
        return snap
    finally:
        cache.delete(lock_key)


@shared_task(ignore_result=True)
def refresh_my_optimal_snapshot(account_id: int, basis: str = "real_toman", force: bool = False):
    """Asynchronously refresh the default MyOptimal snapshot for one account."""
    account = Account.objects.select_related("user").filter(id=account_id).first()
    if account:
        refresh_if_stale(account, basis=basis, force=force)


@shared_task(ignore_result=True)
def sweep_my_optimal_snapshots():
    """Nightly sweep refreshing stale MyOptimal snapshots for all active accounts."""
    accounts = (
        Account.objects.filter(holdings__isnull=False)
        .distinct()
        .values_list("id", flat=True)
    )
    for account_id in accounts:
        for basis in ("real_toman", "nominal_toman"):
            refresh_my_optimal_snapshot.delay(account_id=account_id, basis=basis)


def debounce_my_optimal_refresh(account_id: int):
    """Debounce triggering a MyOptimal refresh for an account after ledger/holding change."""
    from django.conf import settings
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return
    key = f"debounce:my_optimal:{account_id}"
    if cache.add(key, 1, timeout=5):
        try:
            for basis in ("real_toman", "nominal_toman"):
                refresh_my_optimal_snapshot.apply_async(
                    kwargs={"account_id": account_id, "basis": basis},
                    countdown=5,
                )
        except Exception as exc:
            logger.warning("Could not enqueue debounced my_optimal refresh: %s", exc)
