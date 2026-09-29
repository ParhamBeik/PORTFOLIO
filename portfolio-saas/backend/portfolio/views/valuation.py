"""What the portfolio is worth -- now, on a date, and over time.

Live valuation, the net-worth series, the price screen and performance.
All of it reads; none of it writes."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from django.conf import settings
from django.db.models import Avg
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from ..models import Holding, LedgerEntry, Price, Snapshot, Transaction, USD_QUOTED_KEYS
from ..services import get_latest_prices, value_account, value_user
from ..services.catalog import resolve_asset_key
from ..services.valuation import (
    HIDDEN_ADJUSTMENT_MAX_DAYS,
    SYNTHETIC_HISTORY_MAX_DAYS,
    compute_dynamic_net_worth_series,
)
from ..services.visibility import hidden_asset_ids
from ..services.deflator import cpi_for_date, normalize_basis
from ..services.performance import account_performance
from ._common import _express_real_toman, _express_usd_real, _fx_rate, _int_param, _scope, _with_usd
from marketdata import jalali
from marketdata.currency import TOMAN_QUOTE_UNITS, is_tse_priced, to_toman
from marketdata.integrity import compute_symbol_integrity
from marketdata.jalali import from_gregorian, to_gregorian
from marketdata.models import (
    ArchiveFetchState,
    GoldCurrencyHistory,
    MarketCandle,
    RejectedRecord,
    SymbolIntegrity,
)
from marketdata.provenance import BRS_SERIES_ENDPOINTS, daily_bar_price, rejected_pairs, toman_rate_kwargs, toman_rate_tables


def _dated_fx_rates(days, symbols):
    """Accepted provider rates for chart days, carried at most five calendar days.

    One warehouse read per symbol avoids a query for every point of a full-history
    chart. Unknown-origin legacy rows cannot establish a verified USD value.
    """
    if not days:
        return {}
    first = from_gregorian(min(days) - timedelta(days=5))
    last = from_gregorian(max(days))
    rejected = rejected_pairs(symbols, BRS_SERIES_ENDPOINTS, since=first)
    rows = GoldCurrencyHistory.objects.filter(
        symbol__in=symbols, date__gte=first, date__lte=last,
        close_price__gt=0, source=GoldCurrencyHistory.Source.PROVIDER,
    ).exclude(origin=GoldCurrencyHistory.Origin.UNKNOWN).order_by("date")
    by_symbol = {symbol: [] for symbol in symbols}
    for row in rows:
        if (row.symbol, row.date) in rejected:
            continue
        if str(row.unit or "").strip().casefold() not in TOMAN_QUOTE_UNITS:
            continue
        observed = to_gregorian(row.date)
        if observed is not None:
            by_symbol[row.symbol].append((observed, Decimal(row.close_price)))
    rates = {}
    for symbol, observations in by_symbol.items():
        cursor = 0
        latest = None
        for day in sorted(days):
            while cursor < len(observations) and observations[cursor][0] <= day:
                latest = observations[cursor]
                cursor += 1
            rates[(symbol, day)] = (
                latest[1] if latest and (day - latest[0]).days <= 5 else None
            )
    return rates


def _invalid_snapshot_days(rows, accounts):
    """A close is stale if a later ledger edit affects a date on or before it."""
    if not rows or not accounts:
        return set()
    changes = sorted(
        (
            timezone.localtime(entry["timestamp"], ZoneInfo("Asia/Tehran")).date(),
            entry["updated_at"]
        )
        for entry in LedgerEntry.objects.filter(account__in=accounts).values("timestamp", "updated_at")
    )
    stale = set()
    cursor = 0
    latest_change = None
    for row in rows:
        day = row["day"]
        while cursor < len(changes) and changes[cursor][0] <= day:
            changed_at = changes[cursor][1]
            if latest_change is None or changed_at > latest_change:
                latest_change = changed_at
            cursor += 1
        computed_at = row.get("computed_at") or row.get("timestamp")
        if latest_change and computed_at and latest_change > computed_at:
            stale.add(day)
    return stale


class AccountPerformanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Account not found."}, status=404)
        try:
            payload = account_performance(
                account, basis=request.query_params.get("basis")
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(payload)


class AccountDataQualityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, account_id):

        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            raise NotFound("Account not found.")
        holdings = list(account.holdings.filter(is_hidden=False).select_related("asset"))
        symbols = [h.asset.tse_symbol or h.asset.brs_symbol for h in holdings]
        states = {
            (row.symbol, row.endpoint): row for row in ArchiveFetchState.objects.filter(symbol__in=symbols)
        }
        assets = []
        for holding in holdings:
            asset = holding.asset
            symbol = asset.tse_symbol or asset.brs_symbol
            if asset.is_house or asset.is_manual or not symbol:
                assets.append({
                    "asset_key": asset.key,
                    "symbol": symbol or None,
                    "passes_gate": None,
                    "quality_status": "manual",
                    "reason_codes": ["manual_valuation"],
                })
                continue
            try:
                result = compute_symbol_integrity(
                    symbol,
                    start=request.query_params.get("from"),
                    end=request.query_params.get("to"),
                )
            except (TypeError, ValueError) as exc:
                return Response({"detail": str(exc)}, status=400)
            endpoint = (
                ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED if asset.tse_symbol else
                ArchiveFetchState.Endpoint.CRYPTO_DAILY if asset.asset_class == "Crypto" else
                ArchiveFetchState.Endpoint.GOLD_DAILY
            )
            state = states.get((symbol, endpoint))
            repair_state = (
                "not_scheduled" if state is None else
                "blocked" if state.blacklisted or state.suspended_at else
                "verified" if state.verified_complete and not result["missing_count"] else
                "retrying" if state.consecutive_failures else "pending"
            )
            assets.append({
                "asset_key": asset.key, **result,
                "repair_state": repair_state,
                "next_repair_at": state.next_attempt_at.isoformat() if state and state.next_attempt_at else None,
                "archive_last_success_at": state.last_success_at.isoformat() if state and state.last_success_at else None,
            })

        assessed = [item for item in assets if item["passes_gate"] is not None]
        payload = {
            "account_id": account.id,
            "assets": assets,
            "passing_assets": sum(bool(item["passes_gate"]) for item in assessed),
            "assessed_assets": len(assessed),
            "quality_status": (
                "complete" if assessed and all(item["passes_gate"] for item in assessed)
                else "partial" if assessed
                else "unavailable"
            ),
            "known_limits": [
                {
                    "code": "no_iranian_holiday_calendar",
                    "detail": "Clock-based market state can poll on unobserved holidays until provider state refreshes.",
                },
                {
                    "code": "codal_page_cap",
                    "detail": "Codal verification is complete only to the configured five-page archive cap.",
                },
                {
                    "code": "market_state_cache_ttl",
                    "detail": "Provider market state is cached for 3600 seconds and has no external fallback.",
                },
            ],
        }
        # Warehouse coverage is operator telemetry (row counts across the
        # whole store). A member asking "can I trust MY symbols" does not
        # need it, and computing it is the expensive half of this view.
        if request.user.role == "admin":
            from marketdata.coverage_report import build_warehouse_coverage
            payload["warehouse_coverage"] = build_warehouse_coverage()
        return Response(payload)


class ValuationView(APIView):
    """Current valuation, optionally scoped to one portfolio via `?account=`.

    No `?account=` (or "All portfolios") aggregates every account the user owns.
    """

    def get(self, request):
        as_of = request.query_params.get("as_of")
        basis = request.query_params.get("basis") or "nominal_toman"
        account = _scope(request)

        # Historical as-of (any basis) must use the warehouse path.
        # Live requests keep the live price path; usd_real is applied after.
        if as_of:
            from portfolio.services.valuation import value_as_of
            res = value_as_of(request.user, account=account, as_of=as_of, basis=basis)
            return Response(res)

        if account is not None:
            valuation = value_account(account)
            # value_account omits the price map; attach it so _with_usd can
            # convert to USD like the aggregate path does.
            valuation["prices"] = get_latest_prices()
        else:
            valuation = value_user(request.user)
        valuation = _with_usd(valuation)
        if basis in {"usd_real", "usd_denominated", "usdt_denominated"}:
            valuation = _express_usd_real(valuation, basis)
        elif basis == "real_toman":
            valuation = _express_real_toman(valuation)
        else:
            # Stamped on every path, not just the converted ones. The client keeps
            # the previous payload on screen while the next one loads, so a basis
            # switch briefly rendered Toman figures under a dollar sign -- reading
            # the basis off the DATA rather than off the picker keeps the number
            # and its currency describing the same thing.
            valuation["basis"] = "nominal_toman"
        return Response(valuation)


class AccountValuationView(APIView):
    """Current valuation for one account."""

    def get(self, request, account_id):
        account = request.user.accounts.filter(pk=account_id).first()
        if account is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        as_of = request.query_params.get("as_of")
        basis = request.query_params.get("basis") or "nominal_toman"
        if as_of:
            from portfolio.services.valuation import value_as_of
            res = value_as_of(request.user, account=account, as_of=as_of, basis=basis)
            return Response({
                "id": account.id,
                "name": account.name,
                **res
            })

        result = value_account(account)
        result["prices"] = get_latest_prices()
        result = _with_usd(result)
        if basis in {"usd_real", "usd_denominated", "usdt_denominated"}:
            result = _express_usd_real(result, basis)
        elif basis == "real_toman":
            result = _express_real_toman(result)
        else:
            result["basis"] = "nominal_toman"
        return Response({
            "id": account.id,
            "name": account.name,
            **result,
        })


def _subtract_hidden_holdings(user, account, series, now) -> None:
    """Net switched-off holdings out of a recorded net-worth series, in place.

    Stored snapshots are a faithful record of everything owned; the tick that
    hides an asset is a view preference applied at read time. Subtracting here --
    over the whole window rather than from the moment the box was unticked --
    is what keeps the line continuous instead of putting a cliff in it on the day
    the user changed their mind.

    The subtrahend comes from the same routine that draws the synthetic series,
    so both sides of the arithmetic resolve a past price identically. Days where
    a hidden asset had no real close and its live price stood in are marked
    `approximated` rather than silently adjusted, and the result is floored at
    zero: a stale snapshot paired with a since-appreciated property could
    otherwise subtract past the total and draw a negative net worth.

    Recomputation is bounded at HIDDEN_ADJUSTMENT_MAX_DAYS. Points older than
    that reuse the oldest adjustment that WAS computed rather than going
    unadjusted: leaving them alone would put the hidden asset back into the far
    end of the line and draw exactly the cliff this function exists to avoid,
    only at the bound instead of at the day the box was unticked. Carrying the
    value back is an estimate, and says so.
    """
    if not series:
        return
    accounts = [account] if account is not None else list(user.accounts.all())
    if not hidden_asset_ids(accounts):
        return
    earliest = min(row["date"] for row in series)
    span = (now.date() - datetime.strptime(earliest, "%Y-%m-%d").date()).days + 1
    span = max(1, min(span, HIDDEN_ADJUSTMENT_MAX_DAYS))
    hidden = compute_dynamic_net_worth_series(
        user, account=account, days=span, only_hidden=True, max_days=span
    )
    if not hidden:
        return
    # Both sides key on a "%Y-%m-%d" string: the snapshot rows via TruncDate, the
    # subtrahend via strftime on an aware datetime. They agree only because
    # settings.TIME_ZONE is UTC. Change that and this join silently misses,
    # leaving totals unadjusted (and flagged approximate) on the shifted days.
    by_date = {row["date"]: row for row in hidden}
    oldest = min(by_date)
    for row in series:
        adjustment = by_date.get(row["date"])
        beyond_reach = adjustment is None
        if beyond_reach:
            adjustment = by_date[oldest]
        snap_total = Decimal(row["total"])
        hidden_total = Decimal(adjustment["total"])
        # A photograph taken before those holdings existed cannot contain them.
        # Subtracting anyway floors every such day at zero -- the Aug 2026 hole.
        if snap_total < hidden_total:
            continue
        net = snap_total - hidden_total
        row["total"] = str(max(net, Decimal("0")))
        row["approximated"] = beyond_reach or bool(adjustment["approximated"])


def _cpi_window_provenance(series: list[dict]) -> dict:
    """The inflation this chart actually divided by, and where it came from.

    "After inflation" is a claim about a rate, and until now the rate itself
    never appeared: the reader saw two diverging lines and had to take the size
    of the gap on faith. A projection running at triple the published pace looks
    exactly like a projection running at the right one.

    `applied_annual_rate` is the pace implied by the CPI at the two ends of the
    drawn window, annualized -- not the configured knob, which is only one of
    the inputs (a window spanning Nowruz interpolates between two anchors, one
    of which may be a published figure). That is the number the line is made of,
    so that is the number to show.
    """
    provenance = {
        "source": settings.CPI_SOURCE,
        "base_jalali_year": min(settings.CPI_BY_JALALI_YEAR),
        "verified_through_jalali_year": settings.CPI_VERIFIED_THROUGH_YEAR,
        "estimated_jalali_years": sorted(settings.CPI_ESTIMATED_YEARS),
        "applied_annual_rate": None,
    }
    if len(series) < 2:
        return provenance
    first = datetime.strptime(series[0]["date"], "%Y-%m-%d").date()
    last = datetime.strptime(series[-1]["date"], "%Y-%m-%d").date()
    span_days = (last - first).days
    if span_days <= 0:
        return provenance
    start_cpi = cpi_for_date(first)
    end_cpi = cpi_for_date(last)
    if start_cpi > 0 and end_cpi > 0:
        provenance["applied_annual_rate"] = (end_cpi / start_cpi) ** (365.0 / span_days) - 1.0
    return provenance


class SnapshotListView(APIView):
    """Completed Tehran-day closes plus today's derived point and trade markers."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        raw_days = request.query_params.get("days", "30")
        show_all = raw_days == "all"
        if show_all:
            days = None
        else:
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(days, 3650))
        now = timezone.now()
        today = timezone.localtime(now, ZoneInfo("Asia/Tehran")).date()
        account = _scope(request)
        basis = request.query_params.get("basis") or "nominal_toman"
        if request.query_params.get("view") == "return":
            # The replay computes same-quantity day pairs; a stored value-only
            # snapshot cannot distinguish a price gain from a new contribution.
            from portfolio.services.comparison import BENCHMARK_MAX_DAYS

            requested_days = days if days is not None else BENCHMARK_MAX_DAYS
            replay_days = min(requested_days, BENCHMARK_MAX_DAYS)
            dynamic = compute_dynamic_net_worth_series(
                request.user, account=account, days=replay_days,
                max_days=BENCHMARK_MAX_DAYS,
            )
            level = Decimal("100")
            started = False
            broken = False
            return_series = []
            for row in dynamic:
                reason = None
                if row.get("unpriced_assets"):
                    reason = "missing_asset_price"
                elif row.get("approximated"):
                    reason = "unverified_asset_price"
                elif broken:
                    reason = "earlier_history_gap"
                else:
                    total = Decimal(row["total"])
                    if not started and total > 0:
                        started = True
                    elif started:
                        flow = Decimal(row["total_ex_flows"])
                        base = Decimal(row["total_ex_flows_base"])
                        if base <= 0 or flow < 0:
                            reason = "unreconstructable_return"
                        else:
                            level *= flow / base
                if reason:
                    broken = True
                return_series.append({
                    "date": row["date"],
                    "index": float(level) if started and not broken else None,
                    "gap_reason": reason,
                })
            return Response({
                "series": return_series,
                "basis": "return_index",
                "window_truncated": show_all or requested_days > replay_days,
                "days_replayed": replay_days,
            })
        snapshots = Snapshot.objects.filter(user=request.user, day__lt=today)
        if not show_all:
            snapshots = snapshots.filter(day__gte=today - timedelta(days=days - 1))
        if account is not None:
            snapshots = snapshots.filter(account=account)
        else:
            snapshots = snapshots.filter(account=None)
        prices = get_latest_prices()
        usd_rate, _ = _fx_rate(prices, "usd_denominated")

        daily = list(snapshots.values(
            "day", "timestamp", "computed_at", "total_value_tomans", "is_estimated", "is_session_close"
        ).order_by("day"))

        accounts = [account] if account is not None else list(request.user.accounts.all())
        holdings_only = bool(accounts) and not LedgerEntry.objects.filter(
            account__in=accounts,
            kind__in=[LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL],
        ).exists()
        has_holdings = Holding.objects.filter(account__in=accounts).exists() if accounts else False
        stale_days = _invalid_snapshot_days(daily, accounts)

        short_window = not show_all and days <= SYNTHETIC_HISTORY_MAX_DAYS
        # Only synthesise when there is genuinely nothing recorded to draw.
        # This used to compare the number of observed days against the number of
        # CALENDAR days in the window; markets are shut on Thursday and Friday,
        # so that comparison was permanently true for every holdings-only
        # account and threw away real snapshots on every single request in
        # favour of a recomputed estimate. Recorded history always wins.
        use_synthetic = has_holdings and holdings_only and len(daily) < 2

        if use_synthetic:
            synth_days = days if short_window else SYNTHETIC_HISTORY_MAX_DAYS
            dynamic = compute_dynamic_net_worth_series(
                request.user, account=account, days=synth_days
            )
            series = [
                {
                    "timestamp": row["date"],
                    "date": row["date"],
                    "total": None if row.get("unpriced_assets") else row["total"],
                    "total_usd": row["total_usd"],
                    "is_estimated": True,
                    "is_session_close": False,
                    "cache_status": "price_gap" if row.get("unpriced_assets") else "estimated",
                }
                for row in dynamic
            ]
        else:
            current_valuation = (
                value_account(account, include_hidden=True)
                if account else value_user(request.user, include_hidden=True)
            )
            current_total = current_valuation["total"]
            current_incomplete = bool(current_valuation.get("excluded"))
            daily.append({
                "day": today, "total_value_tomans": current_total,
                "is_estimated": False, "is_session_close": False,
            })

            series = []
            for row in daily:
                total = Decimal(row["total_value_tomans"] or 0)
                day_str = row["day"].strftime("%Y-%m-%d")
                series.append({
                    "timestamp": day_str,
                    "date": day_str,
                    "total": str(total),
                    "total_usd": None,
                    "is_estimated": bool(row["is_estimated"]),
                    "is_session_close": bool(row["is_session_close"]),
                    "cache_status": "stale" if row["day"] in stale_days else "ledger_checked",
                })
            # Snapshots record everything owned, so anything switched off has to
            # come back out here -- across the whole window, not from today
            # forward, or the chart would step down on the day the box was
            # unticked. USD is derived after the subtraction for the same reason.
            _subtract_hidden_holdings(request.user, account, series, now)
            if current_incomplete:
                series[-1]["total"] = None
                series[-1]["cache_status"] = "price_gap"
            if stale_days:
                from portfolio.services.comparison import BENCHMARK_MAX_DAYS

                replay_days = min(BENCHMARK_MAX_DAYS, (today - min(stale_days)).days + 1)
                rebuilt = {
                    row["date"]: row for row in compute_dynamic_net_worth_series(
                        request.user, account=account, days=replay_days,
                        max_days=BENCHMARK_MAX_DAYS,
                    )
                }
                stale_day_strings = {day.isoformat() for day in stale_days}
                for row in series:
                    if row["date"] not in stale_day_strings:
                        continue
                    replacement = rebuilt.get(row["date"])
                    if replacement and not replacement.get("approximated") and not replacement.get("unpriced_assets"):
                        row["total"] = replacement["total"]
                        row["cache_status"] = "rebuilt_from_ledger"
                        row["is_estimated"] = True
                    else:
                        row["total"] = None
                        row["cache_status"] = "rebuild_gap"

            # The writer is scheduled daily. A missing stored day is an
            # unobserved value, not permission to connect the neighbouring
            # closes with a smooth line. Keep that date in the response so the
            # chart breaks and the repair state remains visible.
            if series:
                known = {row["date"]: row for row in series}
                first_day = datetime.strptime(series[0]["date"], "%Y-%m-%d").date()
                series = []
                day = first_day
                while day <= today:
                    day_str = day.isoformat()
                    series.append(known.get(day_str, {
                        "date": day_str, "timestamp": day_str,
                        "total": None, "total_usd": None,
                        "is_estimated": False, "is_session_close": False,
                        "cache_status": "missing_snapshot",
                    }))
                    day += timedelta(days=1)

        chart_days = [datetime.strptime(row["date"], "%Y-%m-%d").date() for row in series]
        historic_days = {day for day in chart_days if day < today}
        symbols = ("USD", "USDT_IRT") if basis == "usdt_denominated" else ("USD",)
        historic_rates = _dated_fx_rates(historic_days, symbols)
        for row, day in zip(series, chart_days):
            if row["total"] is None:
                row["total_usd"] = None
                row["usd_rate_status"] = "gap"
                continue
            total = Decimal(str(row["total"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            row["total"] = str(total)
            cash_rate = usd_rate if day == today else historic_rates.get(("USD", day))
            row["total_usd"] = str(round(total / cash_rate, 2)) if cash_rate and cash_rate > 0 else None
            row["usd_rate_status"] = "verified_current_quote" if day == today and cash_rate else "verified_history" if cash_rate else "gap"

        trades = (
            Transaction.objects.filter(
                account__user=request.user,
                asset__isnull=False,
                kind__in=[LedgerEntry.Kind.BUY, LedgerEntry.Kind.SELL],
            )
            .select_related("asset")
            .order_by("timestamp")
        )
        if not show_all:
            trades = trades.filter(timestamp__gte=now - timedelta(days=days))
        if account is not None:
            trades = trades.filter(account=account)
        markers = [
            {
                "timestamp": t.timestamp.isoformat(),
                "date": timezone.localtime(t.timestamp, ZoneInfo("Asia/Tehran")).date().isoformat(),
                "side": t.side,
                "asset_key": t.asset.key,
                "asset_name": t.asset.name,
                "quantity": str(t.quantity),
                "price_tomans": str(t.price_tomans),
            }
            for t in trades
        ]
        # Every denominated point uses its own day's accepted rate. Missing FX
        # stays a gap, never a Toman value labelled as dollars.
        if basis in ("usd_denominated", "usdt_denominated"):
            symbol = "USD" if basis == "usd_denominated" else "USDT_IRT"
            live_rate, _source = _fx_rate(prices, normalize_basis(basis))
            divisor = lambda row: (
                live_rate if row["date"] == today.isoformat()
                else historic_rates.get((symbol, datetime.strptime(row["date"], "%Y-%m-%d").date()))
            )
        elif basis == "real_toman":
            divisor = lambda row: Decimal(  # noqa: E731
                str(cpi_for_date(row.get("date") or row.get("timestamp")))
            ) / Decimal("100")
        else:
            divisor = None
        if divisor:
            for row in series:
                rate = divisor(row)
                if not rate or rate <= 0:
                    row["total"] = None
                    row["total_usd"] = None
                    row["fx_gap"] = True
                else:
                    if row["total"] is not None:
                        row["total"] = float(Decimal(str(row["total"])) / rate)
                    # total_usd always denotes the date's cash USD equivalent,
                    # regardless of the selected chart basis.
        # The basis these points are actually IN, which is not always the one that
        # was asked for: with no FX rate available `divisor` is None and the series
        # stays in Toman, and the chart would have gone on labelling it dollars.
        # It also closes the same race the valuation payload carries this field
        # for -- the client keeps the previous series on screen while the next one
        # loads, so a basis switch drew Toman under a dollar axis until it landed.
        applied_basis = basis if divisor else "nominal_toman"
        payload = {
            "series": series,
            "trades": markers,
            "basis": applied_basis,
            "history_repair_state": "partial" if any(row.get("cache_status") in {"rebuild_gap", "missing_snapshot", "price_gap"} for row in series)
                else "rebuilt" if stale_days else "ledger_checked",
        }
        if applied_basis == "real_toman":
            payload["cpi"] = _cpi_window_provenance(series)
        return Response(payload)


class LatestPricesView(APIView):
    """The shared global price map everyone reads. Cached and global."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        prices = get_latest_prices()
        return Response({k: float(v) for k, v in prices.items()})


#: Widest window the price-history screen may ask for, in days.


PRICE_HISTORY_MAX_DAYS = 1825

#: Rejection verdicts covering the two tables this screen reads directly. A day
#: the warehouse already judged bad must not be drawn as a fact; the daily-bar
#: branch gets the same filter for free from `provenance.daily_bar_price`.


_PRICE_HISTORY_REJECTIONS = (
    "stock_candle_unadjusted",
    "stock_candle_adjusted",
    "stock_history_unadjusted",
    "stock_history_adjusted",
    "gold_daily",
)


def _rejected_dates(symbol, since_jalali):

    return set(
        RejectedRecord.objects.filter(
            symbol=symbol,
            date__gte=since_jalali,
            endpoint__in=_PRICE_HISTORY_REJECTIONS,
        ).values_list("date", flat=True)
    )


def _tse_price_history(asset, since_jalali):
    """TSE daily closes, Rial and provider-verbatim like the rest of the table."""

    rejected = _rejected_dates(asset.tse_symbol, since_jalali)
    for timeframe in (MarketCandle.UNADJUSTED, MarketCandle.ADJUSTED):
        rows = (
            MarketCandle.objects.filter(
                symbol=asset.tse_symbol,
                timeframe=timeframe,
                close_price__gt=0,
                date_time__gte=since_jalali,
            )
            .order_by("date_time")
            .values_list("date_time", "close_price")
        )
        # `date_time` is stored BOTH bare ("1405-05-09") and suffixed with a
        # time, so one session can arrive as two rows; key by day and let the
        # later one win rather than plotting the same close twice.
        by_day = {
            str(stamp).split()[0]: float(close)
            for stamp, close in rows
            if str(stamp).split()[0] not in rejected
        }
        if by_day:
            return (
                [{"date": day, "price": by_day[day]} for day in sorted(by_day)],
                "TSETMC daily close",
            )
    return [], ""


def _brs_price_history(asset, since_jalali):
    """Provider daily closes for a BRS-quoted asset, in Toman.

    The warehouse stores this table provider-verbatim: IRR-quoted symbols were
    converted on ingest, but XAUUSD stays in dollars and BTC in Tether, each
    row carrying its own declared `unit`. Convert at the matching rate of the
    row's OWN date and refuse a row whose unit will not resolve — a foreign
    number drawn on a Toman axis is off by five orders of magnitude, and the
    unit is declared precisely so it never has to be guessed.
    """

    rejected = _rejected_dates(asset.brs_symbol, since_jalali)
    rows = [
        row
        for row in GoldCurrencyHistory.objects.filter(
            symbol=asset.brs_symbol, close_price__gt=0, date__gte=since_jalali
        )
        .order_by("date")
        .values("date", "close_price", "unit")
        if row["date"] not in rejected
    ]
    if rows:
        cash_rates, tether_rates = toman_rate_tables(
            [row["unit"] for row in rows], [row["date"] for row in rows],
        )
        points = []
        for row in rows:
            # An unlabelled row on a foreign-quoted asset is a refusal, not a
            # pass-through: `to_toman` hands an unlabelled number back
            # unchanged, which is right for a Toman quote and catastrophic here.
            if not row["unit"] and (asset.key == "usd_cash" or asset.key in USD_QUOTED_KEYS):
                continue
            price = to_toman(
                asset.brs_symbol,
                row["close_price"],
                row["unit"],
                **toman_rate_kwargs(
                    row["unit"], row["date"], cash_rates, tether_rates,
                ),
            )
            if price > 0:
                points.append({"date": row["date"], "price": float(price)})
        if points:
            return points, "provider daily close"
    # Crypto and commodities have no provider history endpoint, so their only
    # close series is MarketDailyBar -- where `daily_bar_price` is THE reader:
    # it guards the asset class (a coin and a commodity can both be "BTC"),
    # drops rejected days, and resolves the unit at each row's own rate.
    bars = daily_bar_price([asset], since=since_jalali)
    points = [
        {"date": date, "price": float(close)} for _symbol, date, close in bars
    ]
    return points, "daily bar from live snapshots" if points else ""


def _live_price_history(asset, since):
    """One point per day from the recorded ticks, for an asset with no warehouse row.

    `Price` is a two-minute tick table, so a 90-day window is ~65k rows that all
    land on 90 x-values. The mean is taken in the database and one point comes
    back per day, matching what `DailyPriceAverage` records nightly.

    ARCHIVE-tagged rows are excluded for the same reason that rollup excludes
    them: they are the warehouse read back through the live table, so they would
    restate the branch above rather than add anything. Manual marks stay in --
    for a manually valued asset they ARE the series, and nothing else has one.
    """
    ticks = Price.objects.filter(asset=asset, fetched_at__gte=since).exclude(
        source="ARCHIVE"
    )
    if asset.key in USD_QUOTED_KEYS:
        ticks = ticks.filter(price_unit=Price.Unit.IRT, price_unit_verified=True)
    rows = (
        ticks
        .annotate(day=TruncDate("fetched_at"))
        .values("day")
        .annotate(avg_price=Avg(
            "price_foreign" if asset.key in USD_QUOTED_KEYS else "price_iranian"
        ))
        .order_by("day")
    )
    return [
        {"date": row["day"].isoformat(), "price": float(row["avg_price"])}
        for row in rows
    ]


def _to_gregorian_points(points):
    """Jalali-dated warehouse points -> ISO Gregorian, the axis the charts read.

    Every date formatter in the frontend is `new Date(iso)` against an en-GB
    locale, so a Jalali string reaches it as a year-1405 CE date: the whole
    series renders ~621 years early. An unparseable date yields no point rather
    than a wrong one.
    """

    out = []
    for point in points:
        day = jalali.to_gregorian(point["date"])
        if day is not None:
            out.append({"date": day.isoformat(), "price": point["price"]})
    return out


class PriceHistoryView(APIView):
    """Historical closes for one asset, for the single-asset price screen."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        asset_key = request.query_params.get("asset")
        if not asset_key:
            return Response({"detail": "asset query param required."}, status=400)
        # The window is a span of DAYS, not a row count. A shared row cap told
        # two different stories: ~one row a day out of the warehouse, but one
        # every two minutes out of the live table, where "1Y" was half a day.
        days, error = _int_param(
            request, "days", 365, clamp=(1, PRICE_HISTORY_MAX_DAYS)
        )
        if error:
            return error
        # Deliberately no `is_active` filter: a screen may deactivate a
        # candidate but never a holding, and a user still holding a delisted
        # asset must keep being able to see its history. Owner scoping is NOT
        # optional though -- an owner-minted row is somebody's house, and
        # answering for one the caller does not own hands back its name and
        # class to anyone who learns the key.
        asset = resolve_asset_key(request.user, asset_key, active_only=False)
        if asset is None:
            return Response({"detail": "Unknown asset."}, status=404)


        since = timezone.now() - timedelta(days=days)
        since_jalali = from_gregorian(since)
        points, source = [], ""
        if asset.tse_symbol:
            points, source = _tse_price_history(asset, since_jalali)
        elif asset.brs_symbol:
            points, source = _brs_price_history(asset, since_jalali)
        points = _to_gregorian_points(points)
        if not points:
            # `source` is decided HERE and not before the branch: labelling
            # live ticks "TSETMC daily close" is the exact lie this field
            # exists to prevent.
            points = _live_price_history(asset, since)
            source = "recorded daily average" if points else ""

        # True by construction now, not inferred: the TSE branch is the only one
        # that returns a provider-verbatim Rial close, and every other branch
        # converts to the Toman every non-TSE price in this codebase is quoted in.
        unit = "Rial" if is_tse_priced(asset) else "Toman"

        symbol_key = asset.tse_symbol or asset.brs_symbol or asset.key
        integrity = SymbolIntegrity.objects.filter(symbol=symbol_key).first()
        caveats = []
        if integrity:
            if not integrity.passes_gate and integrity.reason:
                caveats.append(integrity.reason)
            if integrity.coverage_ratio and integrity.coverage_ratio < 0.70:
                caveats.append("low_coverage")
            if integrity.max_gap_days and integrity.max_gap_days > 14:
                caveats.append("price_gap_exceeded")

        return Response({
            "asset": {
                "key": asset.key,
                "name": asset.name,
                "name_fa": asset.name_fa,
                "symbol": asset.tse_symbol or asset.brs_symbol or asset.key,
                "asset_class": asset.asset_class,
                "unit": unit,
            },
            "source": source,
            "caveats": caveats,
            "coverage_ratio": integrity.coverage_ratio if integrity else None,
            "points": points,
            "earliest_date": points[0]["date"] if points else None,
            "latest_date": points[-1]["date"] if points else None,
        })


class PerformanceView(APIView):
    """One-release compatibility wrapper for account-scoped performance."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = _scope(request)
        if account is None:
            return Response(
                {"detail": "account query param is required."}, status=400
            )
        try:
            return Response(
                account_performance(
                    account, basis=request.query_params.get("basis")
                )
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
