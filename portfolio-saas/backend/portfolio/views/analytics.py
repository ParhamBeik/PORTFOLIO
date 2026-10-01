"""Risk, optimization and comparison -- the computed views.

These are the expensive endpoints: they carry the analytics throttle
scope and the concurrency cap, and several read a precomputed snapshot
rather than solving in the request path."""
import logging
from datetime import date
from decimal import Decimal
import numpy as np
import pandas as pd
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from ..models import Account, Holding, LedgerEntry
from ..services import value_account, value_user
from ..services.deflator import normalize_basis
from ..services.diagnostics import portfolio_diagnostics
from ..services.insights import _liquid_items, _total, build_insights
from ..services.optimization import (
    MIN_CARDINALITY,
    SCENARIOS,
    UniverseTooSmall,
    SolverError,
    NoAssetBeatsRiskFreeRate,
    MixedUnitUniverseBlocked,
    _efficient_frontier,
    _finite,
    _rebalance_trades,
    optimize,
)
from ..services.returns import daily_returns_matrix
from accounts.permissions import IsRoleAdmin
from ._common import _int_param, _scope, concurrency_cap
from django.core.cache import cache as _cache
from marketdata.models import (
    AssetMetricSnapshot,
    MarketInstrument,
    RejectedRecord,
    SymbolIntegrity,
)
from portfolio.optimization_models import OptimizationSnapshot, save_current_optimization
from portfolio.serializers import OptimizationSnapshotSerializer
import copy

# Both uses sit inside `except` handlers whose whole point is to degrade
# gracefully -- one when the broker will not take a background refresh, one when
# the snapshot write fails. Without this name those handlers raised NameError
# and turned a servable cached page into a 500, which is the opposite of what
# they were written to do.
logger = logging.getLogger(__name__)


class InsightsView(APIView):
    """Rule-based financial insights for the requested scope."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(build_insights(request.user, _scope(request)))


def _current_weights_and_total(
    user, account=None
) -> tuple[dict[str, float], Decimal, dict]:
    """Liquid weights + liquid total + the valuation they came from.

    `account=None` analyzes the whole-user portfolio; passing an account scopes
    weights to that single portfolio.

    The valuation is returned rather than recomputed by each caller:
    `portfolio_diagnostics` derives its `held_keys` from it (see
    `diagnostics._aggregate_holdings`), and calling it without one silently
    yields an EMPTY held set, which puts the user's own holdings back under the
    market-universe screening gates.
    """
    valuation = value_account(account) if account is not None else value_user(user)
    items = _liquid_items(valuation)
    total = _total(items)
    if total <= 0:
        return {}, Decimal("0"), valuation
    # Sum across portfolios: `_liquid_items` flattens every account into one list,
    # so an asset held in two of them appears twice. Keying a dict comprehension on
    # `i["key"]` kept only the LAST row and silently discarded the rest, while
    # `total` still counted them -- the weights then summed to less than 1 and the
    # optimizer rebalanced a book it believed was smaller than it is. On the family
    # account that hid 580,300,000 T (2.4%) held as usd_cash and quarter_coin in
    # both portfolios, and made every rebalance plan buy more than it sold.
    by_key: dict[str, Decimal] = {}
    for i in items:
        if i["value"] > 0:
            by_key[i["key"]] = by_key.get(i["key"], Decimal("0")) + i["value"]
    weights = {key: float(value / total) for key, value in by_key.items()}
    return weights, total, valuation


class AnalyticsView(APIView):
    """portfolio diagnostics: vol, Sharpe, drawdown, VaR, etc."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    @concurrency_cap
    def get(self, request):
        from portfolio.services.deflator import normalize_basis

        account = _scope(request)
        basis = request.query_params.get("basis") or "nominal_toman"
        window, error = _int_param(request, "window", 180, allowed=(90, 180, 365))
        if error:
            return error
        try:
            normalize_basis(basis)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        weights, total, valuation = _current_weights_and_total(request.user, account)
        return Response(
            portfolio_diagnostics(
                weights,
                total,
                user=request.user,
                history_days=window,
                basis=basis,
                valuation=valuation,
            )
        )


class OptimizationView(APIView):
    """scenario optimizer: max_sharpe / min_volatility / risk_parity / hrp."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    @concurrency_cap
    def post(self, request):
        scenario = request.data.get("scenario")
        if scenario not in SCENARIOS:
            return Response(
                {"detail": f"scenario must be one of {list(SCENARIOS)}."},
                status=400,
            )
        constraints = request.data.get("constraints")
        if isinstance(constraints, dict) and constraints.get("max_assets") is not None:
            # `optimize()` coerces this with `int()`; an unvalidated string from
            # the body would surface as a 500 instead of a 400.
            value, error = _parse_max_assets(constraints["max_assets"])
            if error:
                return Response({"detail": error}, status=400)
            constraints = {**constraints, "max_assets": value}
        weights, total, _valuation = _current_weights_and_total(request.user, _scope(request))
        try:
            payload = optimize(
                scenario=scenario,
                current_weights=weights,
                total_value_tomans=total,
                constraints=constraints,
                user=request.user,
            )
        except UniverseTooSmall as exc:
            return Response(
                {
                    "detail": "Not enough price history yet to optimize this portfolio.",
                    "eligible_assets": exc.eligible,
                },
                status=503,
            )
        except MixedUnitUniverseBlocked as exc:
            return Response(
                {
                    "detail": str(exc),
                    "tse_keys": exc.tse_keys,
                    "other_keys": exc.other_keys,
                    "policy": "docs/REFERENCE.md",
                },
                status=409,
            )
        except NoAssetBeatsRiskFreeRate as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except SolverError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(payload)


class FrontierView(APIView):
    """efficient frontier + max_sharpe / min_volatility reference points."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    @concurrency_cap
    def get(self, request):
        from ..services.returns import get_universe_by_mode

        account = _scope(request)
        weights, total, _valuation = _current_weights_and_total(request.user, account)
        window, error = _int_param(request, "window", 180, clamp=(30, 3650))
        if error:
            return error
        # Scope to the user's own book. Without a universe this drew the frontier
        # over the entire active catalog while the chart caption promised "the
        # assets you already hold" -- the line and the Max-Sharpe marker described
        # a portfolio the user cannot build.
        universe = get_universe_by_mode("held", user=request.user, account=account)
        held_keys = frozenset(weights)
        try:
            frontier = _efficient_frontier(
                n_points=30,
                history_days=window,
                universe=universe,
                held_keys=held_keys,
            )
        except MixedUnitUniverseBlocked as exc:
            return Response(
                {
                    "detail": str(exc),
                    "tse_keys": exc.tse_keys,
                    "other_keys": exc.other_keys,
                    "policy": "docs/REFERENCE.md",
                },
                status=409,
            )
        # Inject the current portfolio point, on the SAME panel and the SAME
        # annualization the frontier used -- a default-window, whole-catalog
        # matrix put the user's dot on a chart built from different data.
        returns, _ = daily_returns_matrix(
            history_days=window, universe=universe, held_keys=held_keys
        )
        frequency = float(
            frontier.get("periods_per_year")
            or returns.attrs.get("periods_per_year")
            or 252
        )
        current_point = None
        cloud = []
        if weights and not returns.empty:
            cols = [k for k in weights if k in returns.columns]
            if cols:
                sub = returns[cols].fillna(0.0).to_numpy()
                w = np.array([weights[k] for k in cols], dtype=float)
                if w.sum() > 0:
                    w = w / w.sum()
                    port = pd.Series(sub @ w, index=returns.index)
                    if port.std(ddof=1) > 0:
                        ann_ret = float(port.mean() * frequency)
                        ann_vol = float(port.std(ddof=1) * np.sqrt(frequency))
                        current_point = {
                            "return": _finite(ann_ret),
                            "volatility": _finite(ann_vol),
                            "weights": weights,
                        }
                # Random-weight cloud over the SAME held assets, so the chart shows
                # what varying the user's own mix (not the whole market) could do.
                # Pure numpy, no solver: 400 Dirichlet draws mapped through the
                # same covariance the frontier line already used.
                if len(cols) >= 2:
                    rng = np.random.default_rng()
                    draws = rng.dirichlet(np.ones(len(cols)), size=400)
                    port_returns = sub @ draws.T
                    means = port_returns.mean(axis=0) * frequency
                    stds = port_returns.std(axis=0, ddof=1) * np.sqrt(frequency)
                    cloud = [
                        {"return": _finite(float(r)), "volatility": _finite(float(v))}
                        for r, v in zip(means, stds)
                        if v > 0
                    ]
        return Response({
            "frontier": frontier["frontier"],
            "max_sharpe": frontier["max_sharpe"],
            "min_volatility": frontier["min_volatility"],
            "current": current_point,
            "cloud": cloud,
        })


def _lifetime_days(user, account=None) -> int:
    """Days since tracking started -- the same inception source
    `account_performance()` uses, so "lifetime" agrees across pages."""
    if account is not None:
        start = account.tracking_started_at or (
            account.transactions.order_by("timestamp").values_list("timestamp", flat=True).first()
        )
    else:
        starts = [a.tracking_started_at for a in user.accounts.all() if a.tracking_started_at]
        start = min(starts) if starts else (
            LedgerEntry.objects.filter(account__user=user)
            .order_by("timestamp").values_list("timestamp", flat=True).first()
        )
    if start is None:
        return 365
    return max((timezone.now() - start).days, 30)


# Upper bound on the "hold at most N assets" control. Above this the cap stops
# binding on any realistic book, and it keeps a hand-crafted query string from
# turning into a wide re-solve of every window.
MAX_ASSETS_CEILING = 40


# Risk tolerance, as annualized volatility. Below 1% no real book qualifies and
# the scenario would always degrade to minimum variance; above 200% the ceiling
# stops binding on anything.
MIN_TARGET_VOLATILITY = 0.01
MAX_TARGET_VOLATILITY = 2.0


def _parse_target_volatility(raw):
    """Validate the `target_volatility` query param -> (value|None, error|None)."""
    if raw in (None, ""):
        return None, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "target_volatility must be a number (annualized, e.g. 0.25)."
    if not MIN_TARGET_VOLATILITY <= value <= MAX_TARGET_VOLATILITY:
        return None, (
            f"target_volatility must be between {MIN_TARGET_VOLATILITY} and "
            f"{MAX_TARGET_VOLATILITY}."
        )
    return value, None


def _parse_max_assets(raw):
    """Validate the `max_assets` query param -> (value|None, error|None).

    Returns a message rather than raising so the caller answers 400 instead of
    500; `optimize()` coerces with `int()` and would blow up on a stray string.
    """
    if raw in (None, ""):
        return None, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, "max_assets must be an integer."
    if not MIN_CARDINALITY <= value <= MAX_ASSETS_CEILING:
        return None, (
            f"max_assets must be between {MIN_CARDINALITY} and {MAX_ASSETS_CEILING}."
        )
    return value, None


def _compute_my_optimal_payload(
    user, account, requested_basis="real_toman", max_assets=None, target_volatility=None, constraints=None
) -> dict | None:
    from ..services.deflator import CpiUnavailable
    from ..services.returns import get_universe_by_mode

    weights, total, valuation = _current_weights_and_total(user, account)
    if not weights:
        return None
    universe = get_universe_by_mode("held", user=user, account=account)
    held_keys = frozenset(weights)
    lifetime_days = _lifetime_days(user, account)
    requested_basis = normalize_basis(requested_basis)

    if constraints is None:
        constraints = {
            k: v for k, v in
            (("max_assets", max_assets), ("target_volatility", target_volatility))
            if v is not None
        } or None

    def _solve(scenario, window_days, basis):
        """One scenario, degrading the basis rather than the answer.

        A real-terms panel needs CPI for every Jalali year it spans. When
        that is missing the request must not silently become a nominal one:
        it falls back, but the basis actually used is reported back so the
        client never mistakes an inflation-contaminated number for a real one.
        """
        try:
            return optimize(
                scenario=scenario, current_weights=weights,
                total_value_tomans=total, user=user,
                history_days=window_days, universe=universe,
                held_keys=held_keys, basis=basis, constraints=constraints,
            ), basis
        except CpiUnavailable:
            if basis == "nominal_toman":
                raise
            return optimize(
                scenario=scenario, current_weights=weights,
                total_value_tomans=total, user=user,
                history_days=window_days, universe=universe,
                held_keys=held_keys, basis="nominal_toman",
                constraints=constraints,
            ), "nominal_toman"

    windows = []
    for label, fixed_days in MyOptimalView.WINDOWS:
        window_days = fixed_days or lifetime_days
        entry = {"label": label, "window_days": window_days}
        try:
            payload, basis_used = _solve("min_volatility", window_days, requested_basis)
            entry["min_volatility"] = payload
            entry["basis"] = basis_used
        except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate,
                MixedUnitUniverseBlocked, CpiUnavailable) as exc:
            entry["status"] = "insufficient_history"
            entry["detail"] = str(exc)
            windows.append(entry)
            continue
        basis_used = entry["basis"]
        optional = ("max_sharpe", "risk_parity", "hrp", "min_cvar")
        if target_volatility is not None:
            optional += ("efficient_risk",)
        for scenario in optional:
            try:
                entry[scenario], _ = _solve(scenario, window_days, basis_used)
            except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate,
                    MixedUnitUniverseBlocked, CpiUnavailable):
                entry[scenario] = None
        entry["actual"] = portfolio_diagnostics(
            weights, total, user=user, history_days=window_days,
            universe=universe, valuation=valuation, basis=basis_used,
        )
        for scenario_key in ("min_volatility", *optional):
            scenario_payload = entry.get(scenario_key)
            if scenario_payload:
                scenario_diagnostics = portfolio_diagnostics(
                    scenario_payload["target_weights"], total,
                    user=user, history_days=window_days,
                    universe=universe, valuation=valuation, basis=basis_used,
                )
                scenario_payload["diagnostics"] = {
                    "metrics": scenario_diagnostics.get("metrics", {})
                }
        entry["status"] = "ok"
        windows.append(entry)
    return {
        "windows": windows,
        "basis_requested": requested_basis,
        "max_assets": max_assets,
        "max_assets_range": [MIN_CARDINALITY, MAX_ASSETS_CEILING],
        "target_volatility": target_volatility,
        "target_volatility_range": [MIN_TARGET_VOLATILITY, MAX_TARGET_VOLATILITY],
    }


class MyOptimalView(APIView):
    """"if a quant had optimized MY existing assets, what would it
    look like?" -- per lookback window, max-Sharpe and min-volatility weights
    over the user's OWN held assets, next to how the portfolio actually did.

    The math is exactly `optimize()` / `portfolio_diagnostics()`; this view is
    the window loop plus per-window error containment so a short-history user
    still sees their 1Y result even when 5Y/lifetime can't solve.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    WINDOWS = (("1Y", 365), ("3Y", 1095), ("5Y", 1825), ("Lifetime", None))

    # ~40 `optimize()` solves per request (4 windows x up to 6 scenarios plus
    # diagnostics) is expensive enough to time out the request under a single
    # worker (see docker-compose.yml comment). Cache the whole response body
    # per (user, account, basis); the key rotates on any price update via
    # `_price_version_fingerprint()` and on any trade/holding edit via the
    # ledger/holding fingerprint below, so a cache hit can never serve a
    # result computed from stale weights or stale prices.
    CACHE_TTL = 300

    @staticmethod
    def _cache_key(user, account, basis, max_assets=None, target_volatility=None):
        from ..services.returns import _price_version_fingerprint

        ledger_q = LedgerEntry.objects.filter(account__user=user)
        holding_q = Holding.objects.filter(account__user=user)
        if account is not None:
            ledger_q = ledger_q.filter(account=account)
            holding_q = holding_q.filter(account=account)
        max_ledger_id = ledger_q.order_by("-id").values_list("id", flat=True).first() or 0
        max_holding_id = holding_q.order_by("-id").values_list("id", flat=True).first() or 0
        # Ticking a holding off changes the answer without inserting a row, so the
        # highest ids alone cannot see it and the cached analytics would outlive
        # the toggle for the full TTL.
        hidden_fp = "-".join(
            str(i) for i in sorted(
                holding_q.filter(is_hidden=True).values_list("id", flat=True)
            )
        )
        # This page optimizes the user's OWN assets, so an ingest for anything
        # they do not hold cannot change the answer. Scoping the price half of
        # the key to their symbols is what lets the TTL below actually hold --
        # globally it rotated on every archive insert, which is continuous.
        # The ledger/holding ids stay unscoped and exact: a trade must
        # invalidate immediately, with no staleness window at all.
        fingerprint = (
            f"{_price_version_fingerprint(holding_q.values_list('asset__key', flat=True))}"
            f":{max_ledger_id}:{max_holding_id}:{hidden_fp}"
        )
        account_key = account.id if account is not None else "all"
        # Both knobs change every target in the body, so they have to key the
        # cache too -- otherwise the first request of a TTL decides the position
        # count and the risk ceiling for every later one.
        cap_key = "all" if max_assets is None else str(max_assets)
        vol_key = "any" if target_volatility is None else f"{target_volatility:.4f}"
        return (
            f"my_optimal:{user.id}:{account_key}:{basis}:n{cap_key}:v{vol_key}:{fingerprint}",
            _cache,
        )

    @concurrency_cap
    def get(self, request):
        from ..services.returns import _price_version_fingerprint
        from django.utils import timezone

        account = _scope(request)
        requested_basis = request.query_params.get("basis") or "real_toman"
        try:
            requested_basis = normalize_basis(requested_basis)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        max_assets, error = _parse_max_assets(request.query_params.get("max_assets"))
        if error:
            return Response({"detail": error}, status=400)
        target_volatility, error = _parse_target_volatility(
            request.query_params.get("target_volatility")
        )
        if error:
            return Response({"detail": error}, status=400)

        cache_key, cache = self._cache_key(
            request.user, account, requested_basis, max_assets, target_volatility
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        is_default_knobs = (max_assets is None and target_volatility is None)

        # Build current fingerprint for staleness checks
        holding_q = Holding.objects.filter(account__user=request.user)
        ledger_q = LedgerEntry.objects.filter(account__user=request.user)
        if account is not None:
            holding_q = holding_q.filter(account=account)
            ledger_q = ledger_q.filter(account=account)
        max_ledger_id = ledger_q.order_by("-id").values_list("id", flat=True).first() or 0
        max_holding_id = holding_q.order_by("-id").values_list("id", flat=True).first() or 0
        hidden_fp = "-".join(
            str(i) for i in sorted(
                holding_q.filter(is_hidden=True).values_list("id", flat=True)
            )
        )
        fingerprint = (
            f"{_price_version_fingerprint(holding_q.values_list('asset__key', flat=True))}"
            f":{max_ledger_id}:{max_holding_id}:{hidden_fp}"
        )

        if is_default_knobs and account is not None:
            snap = (
                OptimizationSnapshot.objects
                .filter(account=account, scenario="my_optimal", basis=requested_basis)
                .order_by("-created_at")
                .first()
            )
            if snap is not None and snap.price_version == fingerprint:
                now = timezone.now()
                is_stale = (now - snap.created_at).total_seconds() > 900
                if is_stale:
                    from ..tasks import refresh_my_optimal_snapshot
                    try:
                        refresh_my_optimal_snapshot.delay(account_id=account.id, basis=requested_basis)
                    except Exception as exc:
                        logger.warning("Could not queue my_optimal background refresh: %s", exc)

                payload = dict(snap.payload)
                if "as_of" not in payload and snap.as_of:
                    payload["as_of"] = snap.as_of.isoformat()
                cache.set(cache_key, payload, self.CACHE_TTL)
                return Response(payload)

        # Cold start or non-default knobs: compute inline
        body = _compute_my_optimal_payload(
            request.user, account, requested_basis, max_assets, target_volatility
        )
        if body is None:
            return Response({"detail": "No priced holdings to optimize yet."}, status=400)

        now = timezone.now()
        body["as_of"] = now.isoformat()
        cache.set(cache_key, body, self.CACHE_TTL)

        if is_default_knobs and account is not None:
            if any(w.get("status") == "ok" for w in body.get("windows", [])):
                try:
                    save_current_optimization(
                        account=account,
                        scenario="my_optimal",
                        basis=requested_basis,
                        window_days=0,
                        payload=body,
                        price_version=fingerprint,
                        as_of=now,
                        created_by=request.user,
                    )
                except Exception as exc:
                    logger.warning("Failed to save default OptimizationSnapshot: %s", exc)

        return Response(body)


def _covers_window(payload, days):
    """Do not call a short sample a three-year recommendation."""
    window = payload.get("data_window") or {}
    try:
        start = date.fromisoformat(window["start"][:10])
        end = date.fromisoformat(window["end"][:10])
    except (KeyError, TypeError, ValueError):
        return False
    return (end - start).days >= int(days * 0.8)


class GuidanceView(APIView):
    """One personal rebalance and one market benchmark, with explicit fallback."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    @concurrency_cap
    def get(self, request):
        from ..services.deflator import CpiUnavailable
        from ..services.returns import get_universe_by_mode

        account = _scope(request)
        profile = request.user.risk_profile
        scenario = {
            "conservative": "min_volatility",
            "balanced": "risk_parity",
            "growth": "max_sharpe",
        }[profile]
        weights, total, _ = _current_weights_and_total(request.user, account)
        personal = None
        selected_days = None
        basis = "real_toman"
        if weights:
            universe = get_universe_by_mode("held", user=request.user, account=account)
            for days in (1095, 365):
                try:
                    candidate = optimize(
                        scenario=scenario, current_weights=weights,
                        total_value_tomans=total, user=request.user,
                        history_days=days, universe=universe,
                        held_keys=frozenset(weights), basis=basis,
                    )
                except CpiUnavailable:
                    basis = "nominal_toman"
                    try:
                        candidate = optimize(
                            scenario=scenario, current_weights=weights,
                            total_value_tomans=total, user=request.user,
                            history_days=days, universe=universe,
                            held_keys=frozenset(weights), basis=basis,
                        )
                    except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate,
                            MixedUnitUniverseBlocked, CpiUnavailable):
                        continue
                except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate,
                        MixedUnitUniverseBlocked):
                    continue
                if days == 1095 and not _covers_window(candidate, days):
                    continue
                personal = candidate
                selected_days = days
                break

        benchmark = None
        benchmark_days = None
        for days in (1095, 365):
            snap = (
                OptimizationSnapshot.objects.filter(
                    account__isnull=True, scenario="risk_parity", window_days=days
                ).order_by("-created_at", "-pk").first()
            )
            if snap is None or not isinstance(snap.payload, dict):
                continue
            if days == 1095 and not _covers_window(snap.payload, days):
                continue
            benchmark = {
                "target_weights": snap.payload.get("target_weights", {}),
                "metrics": snap.payload.get("metrics", {}),
                "data_window": snap.payload.get("data_window"),
                "as_of": snap.as_of or snap.created_at,
            }
            benchmark_days = days
            break

        return Response({
            "risk_profile": profile,
            "scenario": scenario,
            "personal": None if personal is None else {
                "target_weights": personal.get("target_weights", {}),
                "rebalance_trades": personal.get("rebalance_trades", []),
                "metrics": personal.get("metrics", {}),
                "data_window": personal.get("data_window"),
            },
            "personal_window_days": selected_days,
            "benchmark": benchmark,
            "benchmark_window_days": benchmark_days,
            "basis": basis,
            "fallback_disclosed": selected_days == 365 or benchmark_days == 365,
            "fallback_reason": (
                "Three years of usable history were unavailable; showing one year."
                if selected_days == 365 or benchmark_days == 365 else None
            ),
        })


class RobustnessView(APIView):
    """"how much of this allocation is signal?" -- bootstrap bands for ONE
    scenario and window.

    Separate from `my-optimal` because ~200 re-solves cannot run inside a page
    load that already does eight. The client requests this for the tab the user
    is actually looking at; the result is cached with the rest of the payload.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    @concurrency_cap
    def get(self, request):
        from ..services.returns import get_universe_by_mode

        scenario = request.query_params.get("scenario") or "min_volatility"
        if scenario not in SCENARIOS:
            return Response(
                {"detail": f"scenario must be one of {list(SCENARIOS)}."}, status=400
            )
        window, error = _int_param(request, "window", 365, clamp=(30, 3650))
        if error:
            return error
        # `efficient_risk` is the one scenario that cannot solve unasked, so the
        # ceiling has to travel with it -- otherwise resampling the tab the user
        # is looking at answers 503 for the one they chose deliberately.
        target_volatility, detail = _parse_target_volatility(
            request.query_params.get("target_volatility")
        )
        if detail:
            return Response({"detail": detail}, status=400)

        account = _scope(request)
        weights, total, _valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to optimize yet."}, status=400)
        universe = get_universe_by_mode("held", user=request.user, account=account)
        try:
            payload = optimize(
                scenario=scenario,
                current_weights=weights,
                total_value_tomans=total,
                user=request.user,
                history_days=window,
                universe=universe,
                held_keys=frozenset(weights),
                include_robustness=True,
                constraints=(
                    {"target_volatility": target_volatility}
                    if target_volatility is not None else None
                ),
            )
        except (UniverseTooSmall, SolverError, NoAssetBeatsRiskFreeRate, MixedUnitUniverseBlocked) as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response({
            "scenario": scenario,
            "window_days": window,
            "robustness": payload.get("robustness"),
            "target_weights": payload.get("target_weights"),
            "diversification": payload.get("diversification"),
            "forecast_free": payload.get("forecast_free"),
        })


class AssetReturnsView(APIView):
    """daily-returns matrix + correlation, for heatmaps and scatter plots."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        days, _ = _int_param(request, "days", 180, clamp=(1, 365), strict=False)
        df, excluded = daily_returns_matrix(history_days=days)
        if df.empty:
            return Response({
                "assets": [],
                "dates": [],
                "returns": {},
                "correlation": {"assets": [], "matrix": []},
                "excluded_assets": excluded,
            })
        assets = list(df.columns)
        dates = [d.isoformat() for d in df.index]
        returns_payload = {
            k: [None if np.isnan(v) else float(v) for v in df[k].tolist()]
            for k in assets
        }
        corr = df.corr().fillna(0.0)
        return Response({
            "assets": assets,
            "dates": dates,
            "returns": returns_payload,
            "correlation": {
                "assets": assets,
                "matrix": np.nan_to_num(corr.to_numpy(), nan=0.0).tolist(),
            },
            "excluded_assets": excluded,
        })


def _asset_class_leaders():
    """Top performers per asset class from the nightly AssetMetricSnapshot run.

    Only the 1-year window is populated today (nightly_asset_metrics' default),
    so this is independent of the window the user has selected on the page.
    """
    from marketdata.universe import get_candidate_universe

    candidates, _ = get_candidate_universe()
    instruments = {mi.symbol: mi for mi in MarketInstrument.objects.filter(symbol__in=candidates)}
    snapshots = AssetMetricSnapshot.objects.filter(symbol__in=candidates, window_days=365)
    latest = snapshots.order_by("-as_of").values_list("as_of", flat=True).first()
    leaders = {}
    for row in snapshots.filter(as_of=latest).order_by("-sharpe"):
        instrument = instruments.get(row.symbol)
        category = instrument.get_category_display() if instrument else "Other"
        leaders.setdefault(category, []).append({
            "symbol": row.symbol,
            "name": instrument.name if instrument else row.symbol,
            "sharpe": row.sharpe,
            "sortino": row.sortino,
            "calmar": row.total_return / abs(row.max_drawdown) if row.max_drawdown else 0.0,
            "expected_return_annual": row.total_return,
            "volatility_annual": row.annualized_volatility,
        })
    return leaders, latest


class BestOverallView(APIView):
    """"what is the best portfolio available across ALL tracked
    assets?" -- a pure read of the nightly `run_best_overall_snapshots`
    precompute (see `portfolio/tasks.py`). No solver call in
    the request path; a window with no snapshot yet reports its own status
    rather than leaving the whole response empty.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from ..tasks import SCENARIOS, WINDOWS_DAYS

        # Trades are per caller: the nightly snapshot is market-wide and must
        # not be mutated, and its stored trades (if any) are not this user's.
        weights, total, _valuation = _current_weights_and_total(
            request.user, _scope(request)
        )
        window_labels = {365: "1Y", 1095: "3Y", 1825: "5Y", 3650: "10Y"}
        windows = []
        latest_created = None
        for window_days in WINDOWS_DAYS:
            entry = {"window_days": window_days, "label": window_labels.get(window_days, f"{window_days}d")}
            for scenario in SCENARIOS:
                snap = (
                    OptimizationSnapshot.objects
                    .filter(account=None, window_days=window_days, scenario=scenario)
                    .order_by("-created_at")
                    .first()
                )
                if snap is None:
                    entry[scenario] = None
                else:
                    payload = copy.deepcopy(snap.payload) if isinstance(snap.payload, dict) else {}
                    payload["rebalance_trades"] = _rebalance_trades(
                        weights, payload.get("target_weights") or {}, total
                    )
                    entry[scenario] = payload
                    if latest_created is None or snap.created_at > latest_created:
                        latest_created = snap.created_at
            entry["status"] = "ok" if (entry["max_sharpe"] or entry["min_volatility"]) else "insufficient_history"
            windows.append(entry)

        leaders, leaders_as_of = _asset_class_leaders()
        return Response({
            "windows": windows,
            "leaders": leaders,
            "leaders_as_of": leaders_as_of,
            "as_of": latest_created.isoformat() if latest_created else None,
        })


class AssetRankingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):

        account = _scope(request)
        if account is None:
            return Response({"detail": "account query param is required."}, status=400)
        symbols = [
            holding.asset.tse_symbol or holding.asset.brs_symbol
            for holding in account.holdings.filter(is_hidden=False).select_related("asset")
            if holding.asset.tse_symbol or holding.asset.brs_symbol
        ]
        rows = AssetMetricSnapshot.objects.filter(
            symbol__in=symbols, window_days=365
        )
        latest = rows.order_by("-as_of").values_list("as_of", flat=True).first()
        return Response([
            {
                "symbol": row.symbol,
                "sharpe": row.sharpe,
                "sortino": row.sortino,
                "total_return": row.total_return,
                "annualized_volatility": row.annualized_volatility,
                "max_drawdown": row.max_drawdown,
            }
            for row in rows.filter(as_of=latest).order_by("-sharpe")
        ])


class DiversifierCandidatesView(APIView):
    """"What should I buy next?" ranked by diversification, not past returns.

    Scores every screened market candidate by how much portfolio volatility it
    would REMOVE if it entered the book at a small weight. Ranking by return
    picks whatever already went up; ranking by this picks what does not move
    with the book, which is the one risk reduction that costs no expected
    return. Both are returned so the frontend can plot the tradeoff rather than
    hide it.

    Advisory only: this proposes nothing and writes nothing.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from portfolio.services.deflator import CpiUnavailable, normalize_basis
        from portfolio.services.diagnostics import _portfolio_returns
        from portfolio.services.diversification import diversifier_candidates
        from portfolio.services.returns import (
            TRADING_DAYS_PER_YEAR,
            daily_returns_matrix,
            get_universe_by_mode,
        )

        account = _scope(request)
        weights, _total, valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to diversify yet."}, status=400)

        window, error = _int_param(request, "window", 365, allowed=(90, 180, 365))
        if error:
            return error
        try:
            basis = normalize_basis(request.query_params.get("basis") or "real_toman")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        held = frozenset(weights)
        candidates = get_universe_by_mode("market", user=request.user, account=account) or []
        # One panel over held + candidates: the held columns build the portfolio
        # series, the rest are scored against it. `held_keys` keeps the user's
        # own holdings out of the market-universe screening gates, which would
        # otherwise delete the very columns the portfolio is made of.
        universe = sorted(held.union(candidates))
        # A real-terms panel needs CPI for every Jalali year it spans, and the
        # table is only verified through the last published figure. Degrade the
        # BASIS rather than the answer -- same rule as MyOptimalView -- and
        # report the basis actually used, so an inflation-contaminated number is
        # never mistaken for a real one.
        basis_requested = basis
        try:
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=universe, basis=basis, held_keys=held,
            )
        except CpiUnavailable:
            if basis == "nominal_toman":
                raise
            basis = "nominal_toman"
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=universe, basis=basis, held_keys=held,
            )
        if returns.empty:
            return Response({
                "basis": basis, "basis_requested": basis_requested,
                "window": window, "candidates": [], "held": [],
            })

        frequency = float(returns.attrs.get("periods_per_year", TRADING_DAYS_PER_YEAR))
        port_series = _portfolio_returns(returns, weights)
        candidate_cols = [c for c in returns.columns if c not in held]
        rows = diversifier_candidates(
            port_series,
            returns[candidate_cols],
            periods_per_year=frequency,
        )
        return Response({
            "basis": basis,
            "basis_requested": basis_requested,
            "window": window,
            "entry_weight": 0.05,
            "periods_per_year": frequency,
            # What was ACTUALLY measured, which is rarely the window asked for.
            # `_trim_to_contiguous` starts the panel after the last ingest
            # outage, so on this warehouse 90d, 180d and 365d all resolve to the
            # same ~65 rows since the 1404-1405 reopening -- three buttons that
            # cannot change the answer, with nothing on screen saying so. Every
            # correlation here carries a standard error of about 1/sqrt(n), so
            # the observation count is not a footnote: it is what decides
            # whether the ranking's top few are distinguishable at all.
            "data_window": {
                "start": returns.index[0].date().isoformat(),
                "end": returns.index[-1].date().isoformat(),
                "observations": int(len(returns.index)),
            },
            # The current book on the same axes, so the scatter can show where
            # the holdings already sit rather than plotting candidates in a void.
            "held": diversifier_candidates(
                port_series,
                returns[[c for c in returns.columns if c in held]],
                periods_per_year=frequency,
            ),
            "candidates": rows,
        })


class BenchmarkSeriesView(APIView):
    """Your portfolio against the things you could have held instead.

    Everything is indexed to 100 at the window's first shared date, because the
    question is relative growth and the levels are not comparable -- a gold gram
    and a whole portfolio have no common scale.

    The TSE index IS included now. It was absent for a real reason -- BrsApi
    exposes the index as a live snapshot only (see marketdata/endpoints.py,
    "there is no index history here") and MarketIndexData held about two weeks
    of rows, so plotting it would have been inventing a comparison. TGJU carries
    the full daily TEDPIX series, which BrsApi does not sell at any price, so
    the benchmark is now drawn from observed closes.
    """

    permission_classes = [IsAuthenticated]

    # Asset keys standing in for "what else could I have held". The Emami coin,
    # not the 18k gram: it is the gold an Iranian household actually buys and
    # the one quoted on every evening's news, so it is the yardstick people
    # already measure themselves against.
    BENCHMARKS = (("emami_coin", "Emami coin"), ("usd_cash", "US dollar"))

    # The market itself. Kept separate from BENCHMARKS because it is not an
    # asset key -- it has no row in the returns matrix and is loaded from
    # MarketIndexData -- but it must reach `labels` all the same: the client
    # renders exactly the keys `labels` names, so a column missing from here is
    # computed and then silently never drawn.
    INDEX_BENCHMARK = ("tse_index", "TSE index (TEDPIX)")

    def get(self, request):
        from portfolio.services.deflator import CpiUnavailable, normalize_basis
        from portfolio.services.diagnostics import _load_index_returns, _portfolio_returns
        from portfolio.services.returns import daily_returns_matrix

        account = _scope(request)
        weights, _total, _valuation = _current_weights_and_total(request.user, account)
        if not weights:
            return Response({"detail": "No priced holdings to compare yet."}, status=400)

        raw_window = request.query_params.get("window", "365")
        if raw_window == "all":
            window = _lifetime_days(request.user, account)
            requested_window = "all"
        else:
            window, error = _int_param(request, "window", 365, allowed=(30, 90, 180, 365))
            if error:
                return error
            requested_window = window
        requested_window_days = window if requested_window != "all" else window
        try:
            basis = normalize_basis(request.query_params.get("basis") or "nominal_toman")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        held = frozenset(weights)
        wanted = [key for key, _label in self.BENCHMARKS]
        basis_requested = basis
        try:
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=sorted(held.union(wanted)),
                basis=basis, held_keys=held,
            )
        except CpiUnavailable:
            if basis == "nominal_toman":
                raise
            basis = "nominal_toman"
            returns, _excluded = daily_returns_matrix(
                history_days=window, universe=sorted(held.union(wanted)),
                basis=basis, held_keys=held,
            )

        port = _portfolio_returns(returns, weights)
        if port.empty:
            return Response({
                "basis": basis,
                "window": window,
                "requested_window": requested_window,
                "requested_window_days": requested_window_days,
                "series": [],
                "unavailable": [],
            })

        def indexed(series):
            """Cumulative growth from 100. NaN-safe: a benchmark that starts
            later joins the chart at its own first observation rather than
            dragging the whole series to null."""
            return (100.0 * (1.0 + series.fillna(0.0)).cumprod()).round(4)

        columns = {"portfolio": indexed(port)}
        unavailable = []
        for key, label in self.BENCHMARKS:
            if key in returns.columns:
                columns[key] = indexed(returns[key].reindex(port.index))
            else:
                unavailable.append({"key": key, "label": label,
                                    "reason": "no overlapping history in this window"})
        # The TSE index, now that there is one to draw.
        #
        # This used to be hard-coded unavailable, and correctly so: BrsApi sells
        # the index as a live snapshot only, `MarketIndexData` held ~2 weeks of
        # rows, and a benchmark drawn from that would have been invented. TGJU
        # carries the full daily TEDPIX series, so the premise is gone.
        #
        # It is loaded through the same helper that feeds beta and alpha, so the
        # line on this chart and the beta on the risk card can never disagree
        # about what the benchmark was.
        index_returns = _load_index_returns(port.index)
        if index_returns is not None and index_returns.notna().sum() >= 2:
            columns[self.INDEX_BENCHMARK[0]] = indexed(index_returns.reindex(port.index))
        else:
            unavailable.append({
                "key": self.INDEX_BENCHMARK[0], "label": self.INDEX_BENCHMARK[1],
                "reason": "no overlapping index history in this window",
            })

        rows = []
        for stamp in port.index:
            row = {"x": stamp.isoformat()}
            for name, series in columns.items():
                value = series.get(stamp)
                row[name] = None if value is None or pd.isna(value) else float(value)
            rows.append(row)

        return Response({
            "basis": basis,
            "basis_requested": basis_requested,
            "window": window,
            "requested_window": requested_window,
            "requested_window_days": requested_window_days,
            "data_window": {
                "start": port.index[0].date().isoformat(),
                "end": port.index[-1].date().isoformat(),
                "observations": int(len(port.index)),
            },
            "series": rows,
            "labels": {"portfolio": "Your portfolio",
                       **{k: v for k, v in (*self.BENCHMARKS, self.INDEX_BENCHMARK)
                          if k in columns}},
            "unavailable": unavailable,
        })


class IntegrityView(APIView):
    """Retrieve symbols integrity quality metrics and rejected records."""

    permission_classes = [IsRoleAdmin]

    def get(self, request):

        try:
            page = max(1, int(request.query_params.get("page") or 1))
            page_size = min(100, max(1, int(request.query_params.get("page_size") or 50)))
        except (TypeError, ValueError):
            return Response({"detail": "page and page_size must be integers."}, status=400)
        offset = (page - 1) * page_size

        integrities = SymbolIntegrity.objects.all().order_by("symbol")
        rejected = RejectedRecord.objects.all().order_by("-occurrences")
        integrity_data = [
            {
                "symbol": i.symbol,
                "passes_gate": i.passes_gate,
                "coverage_ratio": float(i.coverage_ratio) if i.coverage_ratio else 0.0,
                "max_gap_days": i.max_gap_days,
                "reason": i.reason,
                "computed_at": i.computed_at.isoformat() if i.computed_at else None,
            }
            for i in integrities[offset:offset + page_size]
        ]
        rejected_data = [
            {
                "id": r.id,
                "endpoint": r.endpoint,
                "symbol": r.symbol,
                "date": r.date,
                "reason": r.reason,
                "occurrences": r.occurrences,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in rejected[offset:offset + page_size]
        ]
        return Response({
            "integrity_count": integrities.count(),
            "rejected_count": rejected.count(),
            "page": page,
            "page_size": page_size,
            "integrity": integrity_data,
            "rejected": rejected_data,
        })


class OptimizationSnapshotListView(APIView):
    """List optimization snapshots for an account or global snapshots.

    Query params:
      - account_id (optional): integer. If provided, must belong to the requesting user.
      - limit (optional): integer, default 20
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):

        account_id_raw = request.query_params.get("account_id")
        try:
            raw_limit = int(request.query_params.get("limit", 20))
            limit = max(1, min(raw_limit, 200))
        except (ValueError, TypeError):
            limit = 20

        qs = OptimizationSnapshot.objects.all().order_by("-created_at")
        if account_id_raw is not None and account_id_raw != "":
            try:
                account_id = int(account_id_raw)
            except (ValueError, TypeError):
                return Response({"detail": "account_id must be an integer."}, status=400)
            account = get_object_or_404(Account, pk=account_id, user=request.user)
            qs = qs.filter(account=account)
        else:
            qs = qs.filter(account__isnull=True)

        snaps = qs[:limit]
        serializer = OptimizationSnapshotSerializer(snaps, many=True)
        return Response(serializer.data)


class OptimizationSnapshotLatestView(APIView):
    """Return the latest snapshot for a given account (or global).

    Query params: account_id (optional)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account_id_raw = request.query_params.get("account_id")
        if account_id_raw is not None and account_id_raw != "":
            try:
                account_id = int(account_id_raw)
            except (ValueError, TypeError):
                return Response({"detail": "account_id must be an integer."}, status=400)
            account = get_object_or_404(Account, pk=account_id, user=request.user)
            snap = OptimizationSnapshot.objects.filter(account=account).order_by("-created_at").first()
        else:
            # latest global snapshot
            snap = OptimizationSnapshot.objects.filter(account__isnull=True).order_by("-created_at").first()

        if not snap:
            return Response({"detail": "Not found."}, status=404)
        serializer = OptimizationSnapshotSerializer(snap)
        return Response(serializer.data)


class ComparisonView(APIView):
    """Counterfactuals: what the same money would have done somewhere else.

    `GET` with no `mode` answers what the picker can offer; with one, it runs
    that comparison. Every refusal comes back as a `reason` code the page
    renders as a sentence, because most of them are data limits the user can
    act on -- property has no market series, a position with no recorded prices
    has no amount to move, a coin's history may not reach back to 2022.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics"

    @concurrency_cap
    def get(self, request):
        from portfolio.services.comparison import (
            MAX_WINDOW_DAYS, ComparisonError, comparable_assets, compare,
        )

        account = _scope(request)
        mode = request.query_params.get("mode")
        if not mode:
            return Response(comparable_assets(request.user, account))
        days, error = _int_param(request, "days", 0, clamp=(0, MAX_WINDOW_DAYS))
        if error:
            return error
        try:
            payload = compare(
                request.user,
                account=account,
                mode=mode,
                subject=request.query_params.get("subject"),
                target=request.query_params.get("target"),
                # 0 is "as far back as my own history goes", which is the
                # answer this page is usually asked for.
                days=days or None,
            )
        except ComparisonError as exc:
            return Response(
                {"detail": exc.detail, "reason": exc.reason, **exc.extra}, status=400
            )
        payload["as_of"] = timezone.now().isoformat()
        return Response(payload)
