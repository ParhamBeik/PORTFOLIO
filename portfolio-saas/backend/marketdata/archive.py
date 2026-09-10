"""Gap-driven archive worker that verifies provider rows landed in PostgreSQL."""
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.conf import settings
from django.db import transaction
from django.db.models import F, IntegerField, Max, Q, Sum, Value
from django.db.models.functions import Least
from django.utils import timezone

from . import ingest, jalali, market_state, validation
from .fetchers import (
    fetch_candlesticks,
    fetch_codal_announcements,
    fetch_daily_history,
    fetch_gold_currency_pro_history_daily,
    fetch_shareholders,
    fetch_transactions,
)
from .fetchers import MarketDataFetchError
from .models import (
    ArchiveFetchState,
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    RealLegalHistory,
    RejectedRecord,
    ShareholderRecord,
    StockTransactionTick,
)
from .quota import (
    BRS,
    REASON_LIVE_RESERVED,
    TSETMC,
    QuotaExhausted,
    archive_capacity,
    increment_historical_full_used,
    require_archive_room,
)


STOCK_ENDPOINTS = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
)
# Options are a LIVE snapshot (registry: option_contracts, bucket=LIVE), so they
# never get an ArchiveFetchState row and were removed from STOCK_ENDPOINTS.

# Per-day endpoints (Transaction.php, Shareholder.php) cost one request per
# calendar day, so the trailing window is bounded to keep cost finite. Trading
# days only -- a non-trading day has no daily candle, so it is never requested.
TICK_WINDOW_DAYS = getattr(settings, "MARKETDATA_TICK_WINDOW_DAYS", 90)

# Hard ceiling for `grow_tick_windows`. The TSE's oldest listings reach back to
# the mid-1370s (کاما's first candle is 1382-12-26), so ~33 years covers every
# symbol that has ever traded with room to spare. It must also stay well inside
# ArchiveFetchState.target_window_days' PositiveSmallIntegerField range (32,767)
# -- exceeding that is a database error, not a saturating add.
MAX_TICK_WINDOW_DAYS = 12_000

# Endpoints that return an entire history in one request. Used to tag the
# per-day `historical_full` counter, which is how "we spent the day re-downloading
# histories" shows up in the quota status.
_FULL_HISTORY = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.GOLD_DAILY,
)


def _starvation_rank_qs(qs):
    """Annotate the "how badly is this state owed data?" sort keys.

    Used as `_coverage_rank ASC, _deficit DESC, last_success_at ASC`, which is one
    ordering in place of what used to be two hard tiers ("incomplete" above
    "complete and due for refresh"). Tiering by completeness meant that at the
    14:30 post-close, when every finished full-history state comes due at once,
    ~5,500 refreshes outranked the one endpoint with a real backlog -- the day's
    quota went on re-downloading histories that gained nothing while intraday
    ticks sat at 35% coverage.

    Coverage still leads, because `missing_rows` cannot express "holds nothing":
    a state that has never stored a row reports missing_rows=0 for the same reason
    a never-attempted one does -- nothing has run to populate it. Ranking on the
    deficit alone would sort those behind symbols already holding years of data.

    Within a coverage rank the deficit decides, and a complete state scores 0 and
    sinks to the back, where it competes only with other complete states and is
    ordered by longest-since-success. The daily refresh still happens; it just
    stops outranking work that has never been done.
    """
    from django.db.models import Case, IntegerField, Value, When

    return _coverage_rank_qs(qs).annotate(
        _deficit=Case(
            When(verified_complete=True, then=Value(0)),
            default=F("missing_rows"),
            output_field=IntegerField(),
        )
    )


def _coverage_rank_qs(qs):
    """Rank by how much of the symbol exists at all, not by the size of its gap.

    Sorting on `-missing_rows` looked like "worst first" but was the opposite: a
    never-attempted state has missing_rows=0 because nothing has ever run to
    populate it, so it sorted behind every symbol that already had years of data
    and was short a couple of rows. Coverage has to outrank gap size, or the
    first fetch for a symbol never happens.
    """
    from django.db.models import Case, IntegerField, Value, When
    return qs.annotate(
        _coverage_rank=Case(
            When(last_attempt_at__isnull=True, then=Value(0)),  # never attempted
            When(last_success_at__isnull=True, then=Value(1)),  # attempted, never succeeded
            When(stored_rows=0, then=Value(2)),                 # succeeded, holds nothing
            default=Value(3),                                   # has data, closing a gap
            output_field=IntegerField(),
        )
    )


# Endpoints where a parsed-zero-records response proves a bug, not a quiet day.
# Transaction.php in particular returns [] with HTTP 200 when the date is wrong,
# which used to mark the state verified_complete with zero rows.
EMPTY_IS_FAILURE = frozenset({
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
    ArchiveFetchState.Endpoint.GOLD_DAILY,
})

# RejectedRecord rows are labelled by the *writer*, which does not use the
# ArchiveFetchState endpoint name. Verifying `stock_history_adjusted` against the
# label "stock_history_adjusted" matched nothing, so permanently-rejected rows
# were never forgiven and ~726 states re-fetched forever over 1-2 missing rows.
# STOCK_HISTORY_ADJUSTED needs both labels because its `stored` set is the
# intersection of real/legal rows and the unadjusted price rows.
_REJECTION_LABELS = {
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED: ("stock_history_unadjusted",),
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED: (
        "real_legal_history", "stock_history_unadjusted",
    ),
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED: (
        "stock_candle_adjusted", "series:1d_adj",
    ),
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED: (
        "stock_candle_unadjusted", "series:1d_unadj",
    ),
    # Ticks need no entry: the writer label and the endpoint name are identical,
    # so the `(state.endpoint,)` default below already finds their rejections.
}

# Extra wait before a *completed* state re-verifies, on top of the next
# post-close. Disclosure filings and shareholder rosters do not change daily, so
# re-checking all 1,508 of them every day spent ~1,290 requests/day (13% of the
# quota) to learn nothing. Everything else stays on the daily post-close refresh.
_REVERIFY_INTERVAL = {
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS: timedelta(days=7),
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS: timedelta(days=7),
}

# Covered by live organic ingest (portfolio.tasks); archive rows are retired.
# Which provider wallet each archive endpoint spends.
#
# Declared here rather than read from `endpoints.REGISTRY` because only three of
# these names are registry keys; the rest are archive-level jobs whose fetcher is
# picked by a branch in `_fetch_and_ingest`, so the wallet is whichever API key
# that branch passes. `None` means the endpoint issues no provider request at
# all, so no wallet can exhaust it.
#
# `tests/test_archive.py` pins that every Endpoint choice appears here: a new
# endpoint that silently defaulted would be claimed against a spent plan forever.
_ENDPOINT_PLAN = {
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED: TSETMC,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED: TSETMC,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED: TSETMC,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED: TSETMC,
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS: TSETMC,
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS: TSETMC,
    # Codal/* bills the same subscription as Tsetmc/*.
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS: TSETMC,
    ArchiveFetchState.Endpoint.GOLD_DAILY: BRS,
    # Not retired and not branched for, so it reaches the final `else` and is
    # fetched as gold history off the BRS key. No state has ever been created for
    # it; the mapping records where it would spend, not an endorsement.
    ArchiveFetchState.Endpoint.ETF_NAV_DAILY: BRS,
    ArchiveFetchState.Endpoint.COMMODITY_DAILY: None,
    ArchiveFetchState.Endpoint.CRYPTO_DAILY: None,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY: None,
    ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY: None,
}


_RETIRED_ARCHIVE_ENDPOINTS = frozenset({
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY,
    ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY,
})


#: Where each archive endpoint's rows actually land.
#:
#: The ledger used to record `ArchiveFetchState` as the destination for every
#: archive workflow. That is the bookkeeping row the job updates, not the table
#: the data is written to, so the one column that answers "where did these rows
#: go" named the same table for all eight endpoints and told an operator
#: nothing. Kept beside `_fetch_and_ingest`, whose branches these mirror.
ENDPOINT_DESTINATIONS = {
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED: "DailyStockHistory",
    # Misnamed enum (see `_fetch_and_ingest`): type=1 is the real/legal
    # participant breakdown, so it writes RealLegalHistory, not prices.
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED: "RealLegalHistory",
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED: "MarketCandle",
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED: "MarketCandle",
    ArchiveFetchState.Endpoint.GOLD_DAILY: "GoldCurrencyHistory",
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS: "CodalAnnouncement",
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS: "ShareholderRecord",
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS: "StockTransactionTick",
}


def destination_for(endpoint):
    """The table `endpoint` writes rows into, for the ledger and the log line."""
    return ENDPOINT_DESTINATIONS.get(endpoint, "")


# Archive job names are not registry keys (`stock_candle_adjusted` vs
# `stock_candles`). Map to the registry key so `endpoints.source_for` can name
# the provider path instead of echoing the enum.
_ENDPOINT_REGISTRY_KEY = {
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED: "stock_history",
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED: "stock_history",
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED: "stock_candles",
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED: "stock_candles",
    ArchiveFetchState.Endpoint.GOLD_DAILY: "gold_currency_history",
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS: "codal_announcements",
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS: "shareholder_records",
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS: "stock_transaction_ticks",
}


def source_for(endpoint):
    """Provider path the archive job actually calls, or empty if it calls none."""
    from . import endpoints as endpoint_registry

    return endpoint_registry.source_for(_ENDPOINT_REGISTRY_KEY.get(endpoint, ""))


def disabled_endpoints():
    """Endpoints no state may be claimed or created for right now.

    Distinct from `_RETIRED_ARCHIVE_ENDPOINTS`, which marks a state
    `verified_complete` because another path genuinely covers it. A disabled
    endpoint is not covered by anything -- it is switched off -- so its states
    stay exactly as they are, claimed by nobody, and resume untouched when the
    flag flips back.
    """
    if settings.CODAL_ENABLED:
        return frozenset()
    return frozenset({ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS})


def ensure_archive_states(stock_symbols=None, gold_symbols=None):
    from .models import MarketInstrument
    from .catalog import sync_provider_catalog

    if not MarketInstrument.objects.filter(eligible=True).exists():
        # This branch is the cold-start bootstrap: with no eligible instrument
        # on file, nothing downstream has a universe to work from. Swallowing
        # the failure silently meant a provider outage here looked identical to
        # a healthy empty catalog -- the archive went on to build states from
        # whatever `tracked_*_symbols()` could scrape instead, and the only
        # symptom was a warehouse that never grew. Still non-fatal (the caller
        # can proceed on tracked symbols alone), but never again invisible.
        try:
            sync_provider_catalog()
        except Exception:
            logger.warning(
                "Catalog bootstrap failed and no eligible instrument exists; "
                "archive states will be built from tracked symbols only.",
                exc_info=True,
            )

    if stock_symbols is None or not stock_symbols:
        from .tasks import tracked_tse_symbols
        stock_symbols = tracked_tse_symbols()
    if gold_symbols is None or not gold_symbols:
        from .tasks import tracked_brs_symbols
        gold_symbols = tracked_brs_symbols()

    import re
    rows = []
    disabled = disabled_endpoints()
    for symbol in stock_symbols:
        is_derivative = bool(re.search(r"\d$", symbol))
        for endpoint in STOCK_ENDPOINTS:
            if endpoint in disabled:
                continue
            # Skip creating codal_announcements and shareholder_records for digit-suffixed symbols
            if is_derivative and endpoint in (
                ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
                ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
            ):
                continue
            rows.append(ArchiveFetchState(endpoint=endpoint, symbol=symbol))

    rows += [
        ArchiveFetchState(
            endpoint=ArchiveFetchState.Endpoint.GOLD_DAILY,
            symbol=symbol,
        )
        for symbol in gold_symbols
    ]
    if rows:
        existing = set(
            ArchiveFetchState.objects.values_list("endpoint", "symbol")
        )
        missing = [row for row in rows if (row.endpoint, row.symbol) not in existing]
        ArchiveFetchState.objects.bulk_create(missing, ignore_conflicts=True)


def _fetch_and_ingest(state):
    endpoint = state.endpoint
    symbol = state.symbol
    if endpoint in _RETIRED_ARCHIVE_ENDPOINTS:
        # No request; empty expected/stored marks verified_complete and stops retries.
        return (0, 0), set(), set()
    if endpoint == ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED:
        if endpoint in _FULL_HISTORY:
            increment_historical_full_used()
        payload = fetch_daily_history(settings.TSETMC_API_KEY, symbol, history_type=0)
        result = ingest.ingest_daily_history(symbol, payload)
        expected = _record_dates(payload)
        stored = set(DailyStockHistory.objects.filter(
            symbol=symbol, date__in=expected
        ).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED:
        if endpoint in _FULL_HISTORY:
            increment_historical_full_used()
        # Misnamed enum kept for DB compatibility: type=1 is the Real/Legal
        # participant breakdown, not adjusted prices (those live in MarketCandle
        # "1d_adj"). Verified on the breakdown columns, not on date presence, so
        # a payload that lands nothing can no longer report complete.
        payload = fetch_daily_history(settings.TSETMC_API_KEY, symbol, history_type=1)
        result = ingest.ingest_real_legal(symbol, payload)
        expected = _record_dates(payload)
        stored = set(RealLegalHistory.objects.filter(
            symbol=symbol, date__in=expected,
        ).values_list("date", flat=True))
        # Intersecting with the price table made this state's completion depend
        # on a *different* endpoint's date coverage: any day the price validator
        # rejected left a permanent hole here that no amount of re-fetching this
        # endpoint could fill. Verify what this endpoint writes; still require
        # that prices landed at all, which is what the intersection was really
        # guarding against (a payload that stores nothing reporting complete).
        if not DailyStockHistory.objects.filter(
            symbol=symbol, date__in=expected,
        ).exists():
            raise MarketDataFetchError(
                "Real/legal payload has no matching daily price rows; the "
                "unadjusted history pass must land first."
            )
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED:
        if endpoint in _FULL_HISTORY:
            increment_historical_full_used()
        payload = fetch_candlesticks(settings.TSETMC_API_KEY, symbol, candle_type=2)
        result = ingest.ingest_candles(symbol, 2, payload)
        expected = _candle_dates(payload)
        stored = set(MarketCandle.objects.filter(
            symbol=symbol, timeframe="1d_unadj", date_time__in=expected
        ).values_list("date_time", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED:
        if endpoint in _FULL_HISTORY:
            increment_historical_full_used()
        payload = fetch_candlesticks(settings.TSETMC_API_KEY, symbol, candle_type=3)
        result = ingest.ingest_candles(symbol, 3, payload)
        expected = _candle_dates(payload)
        stored = set(MarketCandle.objects.filter(
            symbol=symbol, timeframe="1d_adj", date_time__in=expected
        ).values_list("date_time", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS:
        payload, result, expected, stored = _fetch_codal_pages(symbol)
    elif endpoint == ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS:
        day = jalali.today()
        payload = fetch_shareholders(settings.TSETMC_API_KEY, symbol=symbol)
        result = ingest.ingest_shareholders(symbol, payload, date=day)
        expected = _shareholder_keys(payload, day)
        stored = {
            f"{sid}_{dt}"
            for sid, dt in ShareholderRecord.objects.filter(
                symbol=symbol, date=day
            ).values_list("shareholder_id", "date")
        } & expected
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS:
        # Every other branch reserves quota on its first line, so a refusal costs
        # nothing. This one has to work out WHICH day to ask for before it can
        # ask, and that work -- the trading calendar, the stored-day set, the
        # unreconciled set, the reversal labels -- is the most expensive in the
        # module. Doing it and then being refused is pure waste, and it was the
        # dominant cost on the archive pool: 8,461 paced refusals in one hour at
        # 536ms each is 4,539 seconds of compute per wall-clock hour, against 465
        # fetches that actually happened.
        #
        # Advisory only. `reserve_request` immediately below is still the real,
        # atomic gate; this just declines to prepare a request the plan already
        # has no room for.
        # Ticks always bill TSETMC (`Tsetmc/Transaction.php`, and the registry
        # entry takes the default plan), so the plan is a constant here rather
        # than something to look up.
        require_archive_room(TSETMC)
        pending = _tick_dates_needed(symbol, state.target_window_days)
        if not pending:
            # Nothing pending means one of two opposite things. With no daily
            # candles there is nothing to fetch against and the candle pass must
            # run first. With candles, every trading day is already stored and
            # reconciled -- that is completion. Treating both as an error flipped
            # finished symbols back to incomplete on every tick, so they never
            # converged and burned a retry slot forever.
            if not _tick_trading_days(symbol, state.target_window_days):
                raise MarketDataFetchError(
                    "No trading days known for this symbol yet; daily candles must be "
                    "backfilled first to know which dates have ticks."
                )
            # No request was made, so no quota was spent. Return early: the
            # payload guards below have nothing to check.
            covered = _tick_dates_stored(symbol)
            return (0, 0), covered, covered
        day = pending[0]
        payload = fetch_transactions(settings.TSETMC_API_KEY, symbol=symbol, date=day)
        accepted_ticks, rejected_ticks = ingest.screen(
            "tick",
            payload if isinstance(payload, list) else [],
            "stock_transaction_ticks",
            symbol,
            default_date=day,
        )
        if rejected_ticks:
            raise MarketDataFetchError(
                f"{day}: provider returned {rejected_ticks} invalid tick record(s); "
                "existing ticks were retained."
            )
        candle_volume = _symbol_candle_volumes(symbol, [day]).get(day)
        mismatch = validation.reconcile_tick_volume(
            accepted_ticks, candle_volume
        )
        if mismatch:
            # The provider's own daily bar disagrees with its own trade list, so
            # the day is not usable. Keep it out of `stored` rather than silently
            # banking a wrong total -- but record it, because a day the provider
            # will never serve consistently is a permanent gap, not a retry. Left
            # unrecorded it re-fetched forever (24 states, none able to complete)
            # and, because promotion to the 365-day window demands that *every*
            # tick state be complete, a single such day blocked the whole phase.
            #
            # Do not raise: the failure path never reaches known_gaps forgiveness,
            # so the same day stayed in `_tick_dates_needed` forever. Return the
            # window so run_archive_state can credit the RejectedRecord as a gap.
            RejectedRecord.objects.update_or_create(
                endpoint="stock_transaction_ticks",
                symbol=symbol[:64],
                date=day[:10],
                reason=mismatch.split(":")[0][:64],
                defaults={"payload": {"day": day, "detail": mismatch}},
            )
            trading = _tick_trading_days(symbol, state.target_window_days)
            stored = _tick_dates_stored(symbol)
            return (0, 0), trading | stored | {day}, stored

        # Validate before replacing, then swap atomically. A malformed payload
        # or failed insert must leave the previously stored day intact.
        result = ingest.ingest_transactions(
            symbol, day, payload, replace=True, require_all_valid=True
        )
        if result[1]:
            raise MarketDataFetchError(
                f"{day}: provider returned {result[1]} invalid tick record(s); "
                "existing ticks were retained."
            )

        # Progress is measured across the whole window, not this one day, so the
        # state stays incomplete and reschedules until the window is covered.
        expected = set(pending) | _tick_dates_stored(symbol)
        stored = _tick_dates_stored(symbol)
    else:
        direct_payload = None
        if getattr(settings, "TGJU_ENABLED", False):
            from .sources import tgju
            try:
                direct_payload = tgju.gold_history_payload(symbol)
                if direct_payload is not None:
                    logger.info(
                        "Using TGJU history for %s (%s rows); skipping BrsApi.",
                        symbol, len(direct_payload["history_daily"]),
                    )
            except Exception as exc:  # noqa: BLE001 - paid fallback is intentional
                logger.warning(
                    "TGJU history failed for %s; using BrsApi fallback: %s",
                    symbol, exc,
                )
        if direct_payload is not None:
            payload = direct_payload
        else:
            increment_historical_full_used()
            payload = fetch_gold_currency_pro_history_daily(settings.BRS_API_KEY, symbol)
        result = ingest.ingest_gold_currency_history(payload)
        expected = _gold_dates(payload)
        stored = set(GoldCurrencyHistory.objects.filter(
            symbol=ingest.canonical_gold_symbol(payload, symbol), date__in=expected
        ).values_list("date", flat=True))
    if payload is None:
        raise MarketDataFetchError("Provider returned empty/None payload.")
    if endpoint in EMPTY_IS_FAILURE and not _parsed_any(payload):
        # HTTP 200 with zero records used to mark the state verified_complete
        # with expected_rows=0 and defer it 20h, hiding wrong URLs and bad dates.
        raise MarketDataFetchError(
            "Provider returned zero records where records were expected."
        )
    if not expected:
        if isinstance(payload, dict) and payload.get("status") == "no_data":
            # Explicitly returned 'no_data', meaning the symbol is valid but has no records.
            # We return empty sets so it marks complete with 0 rows instead of failing.
            return result, set(), set()
        if isinstance(payload, list) and len(payload) == 0:
            # Clean empty list returned for full history endpoints (e.g. inactive block trade symbols)
            if endpoint in (
                ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
                ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
            ):
                return result, set(), set()
        # `verified_complete = not (expected - stored)` is vacuously true for an
        # empty expected set, so a payload our parser could not read verified
        # against an empty table and deferred 20h. Commodity sat in that state
        # for months with zero rows stored. A parseable payload always yields at
        # least one key; an empty set means the parser and the payload disagree.
        raise MarketDataFetchError(
            "Payload parsed to zero verifiable keys; parser and payload disagree."
        )
    return result, expected, stored


CODAL_PAGE_SIZE = 20


def _codal_stored_keys(symbol):
    return {
        f"{code}_{dp}_{tp}"
        for code, dp, tp in CodalAnnouncement.objects.filter(symbol=symbol).values_list(
            "code", "date_publish", "time_publish"
        )
    }


def _fetch_codal_pages(symbol):
    """Walk the newest Codal pages for one symbol; returns (payload, result, expected, stored).

    Announcement.php pages 20 records at a time and reports `count_page`; a
    mature symbol has ~50 pages. Only page 1 was ever requested, so 2% of the
    history landed -- and it verified complete because the expected set was built
    from that same page. Page 1 is always refreshed for new filings; the deeper
    pages are only walked while the symbol sits below its target, so the first
    pass costs MAX_PAGES requests per symbol and steady state costs one.

    Provider symbols are canonical (`l18`), so a state keyed `سامان2` stores rows
    under `سامان`; read back by the keys the ingest actually wrote.
    """
    key = settings.TSETMC_API_KEY
    first = fetch_codal_announcements(key, symbol=symbol, page=1)
    created, skipped = ingest.ingest_codal(first)
    expected = _codal_keys(first)
    total = 0
    if isinstance(first, dict):
        try:
            total = int(first.get("count_announcement") or 0)
        except (TypeError, ValueError):
            total = 0
    target = min(total, settings.MARKETDATA_CODAL_MAX_PAGES * CODAL_PAGE_SIZE)

    written_symbols = {
        rec.get("l18")
        for rec in (first.get("announcement") or [] if isinstance(first, dict) else [])
        if isinstance(rec, dict) and rec.get("l18")
    } or {symbol}

    def stored_now():
        keys = set()
        for sym in written_symbols:
            keys |= _codal_stored_keys(sym)
        return keys

    have = stored_now()
    last_page = min(
        settings.MARKETDATA_CODAL_MAX_PAGES,
        -(-total // CODAL_PAGE_SIZE) if total else 1,
    )
    if len(have) < target:
        for page in range(2, last_page + 1):
            payload = fetch_codal_announcements(key, symbol=symbol, page=page)
            page_created, page_skipped = ingest.ingest_codal(payload)
            created += page_created
            skipped += page_skipped
            expected |= _codal_keys(payload)
        have = stored_now()

    return first, (created, skipped), expected, have & expected


def _parsed_any(payload):
    if isinstance(payload, list):
        return bool(payload)
    if isinstance(payload, dict):
        return any(
            bool(value)
            for key, value in payload.items()
            if key not in ("account", "status", "successful")
        )
    return bool(payload)


def _tick_dates_stored(symbol):
    return set(
        StockTransactionTick.objects.filter(symbol=symbol)
        .values_list("date", flat=True)
        .distinct()
    )


def market_trading_days(window_days=None):
    """Days the exchange actually traded, taken from the data rather than a rule.

    A weekday calendar cannot know Iranian public holidays, but the market can:
    on a holiday no symbol produces a candle. Counting distinct symbols per day
    separates "the exchange was shut" from "this one stock was suspended", which
    a per-symbol view cannot tell apart. Requesting ticks for a closed day costs
    a request and returns nothing, so this is a direct quota saving.
    """
    from .calendars import actual_trading_days

    return actual_trading_days(window_days=window_days or TICK_WINDOW_DAYS)


def _symbol_candle_volumes(symbol, days=None):
    """Traded volume per day for one symbol, from whichever candle pass has it.

    Volume is adjustment-invariant -- a split rescales price, not shares traded --
    so an adjusted bar answers this exactly as well as an unadjusted one, and the
    unadjusted row still wins where both exist.

    Reading only `1d_unadj` silently stranded 35 symbols. The provider serves them
    no unadjusted candles at all (اتکای has 2,570 adjusted bars and zero unadjusted),
    so their trading calendar came back empty, their tick state raised "no trading
    days known" on every pass, and it never converged -- 4-7 consecutive failures
    each, burning a request per cycle forever. The candle pass had not "not reached
    them yet"; it was never going to.
    """
    queryset = MarketCandle.objects.filter(
        symbol=symbol,
        timeframe__in=(MarketCandle.UNADJUSTED, MarketCandle.ADJUSTED),
    )
    if days is not None:
        queryset = queryset.filter(date_time__in=days)
    volumes = {}
    for timeframe, day, volume in queryset.values_list(
        "timeframe", "date_time", "volume"
    ):
        if timeframe == MarketCandle.UNADJUSTED or day not in volumes:
            volumes[day] = volume
    return volumes


def _tick_rejected_dates(symbol):
    return set(
        RejectedRecord.objects.filter(
            endpoint="stock_transaction_ticks",
            symbol=symbol,
        ).values_list("date", flat=True)
    )


def _tick_days_unreconciled(symbol, days):
    """Stored tick days whose traded volume disagrees with the daily candle.

    Cancelled trades are excluded, which is the whole point: including them
    inflated volume by up to 14% and is why 370 stored stock-days disagreed with
    their own candles. A day that fails here is re-fetched, not patched.
    Permanently quarantined days are left alone -- re-fetching them loops forever.
    """
    if not days:
        return set()
    rejected = _tick_rejected_dates(symbol)
    tick_totals = dict(
        StockTransactionTick.objects.filter(
            symbol=symbol, date__in=days, canceled=False
        )
        .values_list("date")
        .annotate(total=Sum("volume"))
    )
    candle_totals = _symbol_candle_volumes(symbol, days)
    broken = set()
    for day, candle_volume in candle_totals.items():
        if day in rejected or day not in tick_totals:
            continue
        if validation.reconcile_tick_volume(
            [{"volume": tick_totals[day], "canceled": 0}],
            candle_volume,
        ):
            broken.add(day)
    return broken


def _tick_trading_days(symbol, window_days=TICK_WINDOW_DAYS):
    """Trading days this symbol actually has a daily candle for.

    Empty means no candle pass has produced a bar for this symbol on any timeframe.
    That is a different condition from "every tick day is already stored", and
    callers must not conflate the two.
    """
    return market_trading_days(window_days) & set(_symbol_candle_volumes(symbol))


def _tick_dates_needed(symbol, window_days=TICK_WINDOW_DAYS):
    """Trading days in the trailing window still owing a correct set of ticks.

    Two kinds of work: days never fetched (missing), and days whose stored ticks
    do not add up to the candle (broken). Missing days are prioritized first
    to establish a complete timeline before spending quota on self-repair.
    Quarantined mismatch days are never re-requested.

    Within `missing`, the days the reversal model actually needs come first (see
    `reversal.priority_tick_days`). Ordering by date alone spends one request per
    symbol-day across a ~2.1M-day universe in whatever order the calendar hands
    them over -- which is how 96,602 days were bought and only 3,172 of them were
    days anything wanted to look at. Everything else still follows, newest first,
    so a symbol with no labelled days behaves exactly as before.
    """
    trading = _tick_trading_days(symbol, window_days)
    stored = _tick_dates_stored(symbol)
    rejected = _tick_rejected_dates(symbol)
    missing = trading - stored - rejected
    broken = _tick_days_unreconciled(symbol, trading & stored)

    try:
        from . import reversal

        wanted = [day for day in reversal.priority_tick_days(symbol) if day in missing]
    except Exception:  # noqa: BLE001 -- labelling must never block a fetch
        logger.warning("reversal labelling unavailable for %s", symbol, exc_info=True)
        wanted = []
    rest = sorted(missing.difference(wanted), reverse=True)
    return wanted + rest + sorted(broken, reverse=True)


def _record_dates(payload):
    if not isinstance(payload, list):
        return set()
    return {
        ingest.normalize_jalali(record.get("date"))
        for record in payload
        if isinstance(record, dict) and record.get("date")
    }


def _candle_dates(payload):
    if not isinstance(payload, dict):
        return set()
    records = (
        payload.get("candle_daily")
        or payload.get("candle_daily_adjusted")
        or payload.get("candle_intraday")
        or []
    )
    return {
        ingest.normalize_jalali(record.get("date"))
        for record in records
        if isinstance(record, dict) and record.get("date")
    }


def _gold_dates(payload):
    records = payload.get("history_daily", []) if isinstance(payload, dict) else []
    return {
        ingest.normalize_jalali(record.get("date"))
        for record in records
        if isinstance(record, dict) and record.get("date")
    }


def _generic_dates(payload):
    return {
        ingest.normalize_jalali(rec.get("date") or rec.get("d"))
        for rec in ingest.flatten_records(payload)
        if rec.get("date") or rec.get("d")
    }


def _snapshot_keys(payload, name_keys=("symbol",)):
    """symbol|date keys for the multi-symbol snapshot endpoints."""
    keys = set()
    for rec in ingest.flatten_records(payload):
        day = ingest.normalize_jalali(rec.get("date") or rec.get("d"))
        name = next((rec[k] for k in name_keys if rec.get(k)), None)
        if name is None and rec.get("id") is not None:
            name = f"ID_{rec['id']}"
        if day and name:
            keys.add(f"{str(name)[:64]}|{day}")
    return keys


def _stored_snapshot_keys(model, expected):
    if not expected:
        return set()
    days = {key.split("|", 1)[1] for key in expected}
    return {
        f"{sym}|{day}"
        for sym, day in model.objects.filter(date__in=days).values_list("symbol", "date")
    } & expected


def _codal_keys(payload):
    records = payload.get("announcement") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return set()
    # Keys must be built the same way ingest_codal builds the stored row, digit
    # folding included, or every announcement reads back as missing.
    return {
        f"{ingest.fold_digits(rec.get('code', ''))}"
        f"_{ingest.normalize_jalali(rec.get('date_publish', ''))}"
        f"_{ingest.fold_digits(rec.get('time_publish', ''))}"
        for rec in records
        if isinstance(rec, dict)
    }


def _shareholder_keys(payload, day):
    """Roster keys for `day`; the payload itself carries no date field."""
    if not isinstance(payload, list):
        return set()
    return {
        f"{rec['id']}_{day}"
        for rec in payload
        if isinstance(rec, dict) and rec.get("id") is not None
    }


def _transaction_keys(payload):
    """Kept for the management command; the archive path keys ticks by date now."""
    if not isinstance(payload, list):
        return set()
    return {
        f"{rec.get('row')}_{ingest.normalize_jalali(rec.get('date', ''))}"
        for rec in payload
        if isinstance(rec, dict) and rec.get("row") is not None
    }


_PREREQ_ERROR_MARKERS = (
    "No trading days known for this symbol yet",
    "Real/legal payload has no matching daily price rows",
)

# A prereq that has not landed yet is not the state's fault, so the retry stays a
# flat 3h rather than escalating to the 24h cap -- but it is still a pass that did
# not converge, and that has to be counted.
PREREQ_DEFER_INTERVAL = timedelta(hours=3)

# The one declaration of "this state is waiting on a SIBLING endpoint, not on the
# provider". `suspension` reads it to keep those states out of its peer sample:
# the wait says the scheduler has not reached the prerequisite yet, which is a
# statement about our own fetch order, never about whether this symbol is
# servable. Distinct from the `_PREREQ_ERROR_MARKERS` defers above, which carry
# an exception message and ARE the symbol's own problem.
PREREQ_WAIT_ERROR = "Waiting on prerequisite endpoint."


def _defer_for_prereq(state, *, now, error=""):
    """Hold a state whose dependency endpoint has not landed, and count the pass.

    Everything that can notice a stuck state -- the Ops "Wedged" tile, the
    `wedged-archive-states` alert and `suspension`'s outlier detector -- reads
    `consecutive_failures` and nothing else. This branch used to leave it alone,
    on the reasoning that waiting for candles is not a failure, which made a
    symbol whose prereq NEVER lands invisible to all three: it retried on the
    flat 3h cadence, eight provider calls a day, forever, with a counter frozen
    at zero saying it was fine.

    Counting it is safe precisely because suspension is peer-relative. On a cold
    warehouse every state on the endpoint is waiting, so the outage guard
    (`OUTAGE_SHARE_THRESHOLD`) suspends nobody; when six symbols out of hundreds
    are the only ones still waiting, that is the outlier it is meant to catch.
    Either way the count clears the moment the state stores a row.
    """
    state.consecutive_failures += 1
    state.last_attempt_at = now
    state.next_attempt_at = now + PREREQ_DEFER_INTERVAL
    state.last_error = error[:500]
    state.verified_complete = False
    state.save(update_fields=[
        "consecutive_failures", "last_attempt_at", "next_attempt_at",
        "last_error", "verified_complete",
    ])


def next_quota_day_start(now=None):
    """UTC datetime of the next Tehran midnight (provider quota day boundary)."""
    from datetime import datetime, timezone as dt_timezone

    now = now or timezone.now()
    local = now.astimezone(market_state.TEHRAN)
    nxt = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return nxt.astimezone(dt_timezone.utc)


def spread_over_next_quota_day(now=None, *, rng=None):
    """A wakeup jittered across the next quota day's spending window.

    Everything that hit a genuinely spent wallet used to be stamped with the bare
    `next_quota_day_start`, so the entire backlog came due in the same second.
    Tehran midnight is the worst possible moment for that: the paced allowance is
    at its floor, so the herd immediately re-refused itself and (before the
    reason branch below existed) parked itself for another 24 hours.

    Jittering keeps the wallet fed evenly instead of in one doomed burst. The
    spread stops at `MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR` because a state that
    wakes after the ramp closes has no allowance left to claim that day.
    """
    import random

    rng = rng or random
    start = next_quota_day_start(now)
    full_by = max(1, min(24, int(
        getattr(settings, "MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR", 24)
    )))
    return start + timedelta(seconds=rng.uniform(0, full_by * 3600))


def run_archive_state(state_id):
    state = ArchiveFetchState.objects.get(pk=state_id)
    now = timezone.now()
    if state.endpoint in disabled_endpoints():
        # Last line of defence, and the only one that covers a task already
        # sitting in the broker when the flag flipped. Deliberately touches
        # nothing on the row: a disabled endpoint resumes exactly where it was.
        #
        # Returns `state`, like every other exit from this function. Returning
        # None instead crashes the Celery wrapper, which reads state.last_error
        # OUTSIDE its try/except (tasks.py) -- so the guard meant to protect the
        # task would have killed it and dropped the ledger row with it.
        logger.info("Skipping %s (%s): endpoint disabled.", state.symbol, state.endpoint)
        return state
    logger.info("Processing archive state %s (%s).", state.symbol, state.endpoint)
    # Per-run yield, carried on the instance for the Celery wrapper's ledger row.
    # `rows_received`/`rows_accepted` there are the state's CUMULATIVE totals, so
    # they cannot answer "what did this request buy?" -- summing them over a day
    # produced "1.26M rows accepted, 0 created", which reads as a fleet of wasted
    # requests and is really just an unset field. Transient by design: nothing
    # persists it, and a state re-read from the DB correctly reports None.
    state.run_rows_created = 0
    try:
        (created, _), expected, stored = _fetch_and_ingest(state)
        state.run_rows_created = int(created or 0)
    except QuotaExhausted as exc:
        # Branch on WHY the claim was refused. Treating all four reasons as "the
        # wallet is spent" is what stalled the archive from 2026-08-27: a pacing
        # wait of a few minutes parked the state until the next Tehran midnight,
        # where the whole herd re-refused itself and parked again. 6,863 states
        # sat on `last_error="Daily quota unavailable."` while ~4,000 TSETMC
        # requests a day went unspent.
        if exc.is_pacing:
            # Refused by something that clears on its own before rollover. Come
            # back later and leave the row otherwise untouched: no failure count
            # (nothing failed), and no alarming `last_error` that would make a
            # healthy queue read as a broken one.
            #
            # The two reasons clear on different timescales, so they get
            # different waits. Pacing moves with the ramp, in minutes. A live
            # reservation only releases as the live lane's *remaining* cadence
            # shrinks, and the step that matters is the 13:00 Tehran session
            # close -- retrying that every three minutes would spin all morning
            # for a condition that changes a few times a day.
            import random

            if exc.reason == REASON_LIVE_RESERVED:
                base = int(getattr(settings, "MARKETDATA_LIVE_RESERVED_RETRY_SECONDS", 1800))
            else:
                base = int(getattr(settings, "MARKETDATA_ARCHIVE_PACED_RETRY_SECONDS", 180))
            state.last_attempt_at = now
            state.next_attempt_at = now + timedelta(
                seconds=random.uniform(base * 0.5, base * 1.5)
            )
            state.save(update_fields=["last_attempt_at", "next_attempt_at"])
            logger.debug(
                "Archive deferred for %s (%s): %s; retrying in ~%ds.",
                state.symbol, state.endpoint, exc.reason, base,
            )
            raise

        # Genuinely day-scoped: plan_blocked and bucket_exhausted. Nothing this
        # state can do clears either before rollover.
        state.last_attempt_at = now
        state.next_attempt_at = spread_over_next_quota_day(now)
        state.last_error = f"Daily quota unavailable ({exc.reason})."
        state.save(update_fields=["last_attempt_at", "next_attempt_at", "last_error"])
        # Other states leased in this tick still wake within ~10m; push them past
        # rollover so the queue does not spin once the day budget is really gone.
        # Each gets its own jittered wakeup -- a single shared timestamp is what
        # built the midnight herd in the first place. Deliberately NOT reached on
        # a pacing refusal: one paced state used to drag its whole batch with it.
        siblings = list(
            ArchiveFetchState.objects.filter(
                verified_complete=False,
                next_attempt_at__gt=now,
                next_attempt_at__lte=now + timedelta(minutes=10),
            ).exclude(pk=state.pk)
        )
        for sibling in siblings:
            sibling.next_attempt_at = spread_over_next_quota_day(now)
            sibling.last_error = f"Daily quota unavailable ({exc.reason})."
        if siblings:
            ArchiveFetchState.objects.bulk_update(
                siblings, ["next_attempt_at", "last_error"], batch_size=500
            )
        logger.info(
            "Archive quota unavailable for %s (%s) reason=%s: %s",
            state.symbol, state.endpoint, exc.reason, exc,
        )
        raise
    except MarketDataFetchError as exc:
        from .fetchers import TransientMarketDataError
        is_transient = isinstance(exc, TransientMarketDataError) or getattr(exc, "status_code", None) == 429
        message = str(exc)

        if any(marker in message for marker in _PREREQ_ERROR_MARKERS):
            # Candles / unadj history must land first, so the cadence stays flat
            # instead of escalating to the 24h cap -- but this pass already spent
            # a provider call and converged on nothing, so it counts.
            _defer_for_prereq(state, now=now, error=f"{type(exc).__name__}: {exc}")
            logger.info(
                "Archive prereq defer for %s (%s): %s",
                state.symbol, state.endpoint, exc,
            )
            return state

        if is_transient:
            # A flat 2m retry never escalated, so a symbol that always times out
            # consumed a batch slot every 2 minutes indefinitely. Escalate like
            # any other failure; a genuine blip still retries fast.
            failures = state.consecutive_failures + 1
            delay = min(2 ** failures, 60)
            state.consecutive_failures = failures
            state.last_attempt_at = now
            state.next_attempt_at = now + timedelta(minutes=delay)
            state.last_error = f"Transient rate limit or network error: {exc}"[:500]
            state.save(update_fields=[
                "consecutive_failures", "last_attempt_at", "next_attempt_at", "last_error",
            ])
            logger.info("Archive request retry scheduled for %s (%s) in %dm: %s", state.symbol, state.endpoint, delay, exc)
            return state

        failures = state.consecutive_failures + 1
        state.consecutive_failures = failures
        state.last_attempt_at = now
        state.next_attempt_at = now + timedelta(hours=min(2 ** (failures - 1), 24))
        state.last_error = f"{type(exc).__name__}: {exc}"[:500]
        state.verified_complete = False
        state.save(update_fields=[
            "consecutive_failures", "last_attempt_at", "next_attempt_at",
            "last_error", "verified_complete",
        ])
        logger.info("Archive fetch retry scheduled for %s (%s): %s", state.symbol, state.endpoint, exc)
        return state

    previous_missing = state.missing_rows
    previous_stored = state.stored_rows
    missing = expected - stored
    known_gaps = set()
    # Records the validator permanently rejects (bad OHLC, volume mismatch)
    # will never land in stored. Without this, the state re-fetches forever.
    # ponytail: DB query only runs when there are actual missing records.
    if missing:
        rejected_dates = set(
            RejectedRecord.objects.filter(
                endpoint__in=_REJECTION_LABELS.get(state.endpoint, (state.endpoint,)),
                symbol=state.symbol,
            ).values_list("date", flat=True)
        )
        # Handles both date keys ("1405-05-03") and composite snapshot
        # keys ("Bitcoin|1405-05-03") in one pass.
        if rejected_dates:
            known_gaps = {
                key for key in missing
                if key in rejected_dates or key.split("|")[-1] in rejected_dates
            }
            missing -= known_gaps
    state.expected_rows = len(expected)
    state.stored_rows = len(stored)
    state.missing_rows = len(missing)
    state.known_gap_rows = len(known_gaps)
    state.first_date = min(expected) if expected else ""
    state.last_date = max(expected) if expected else ""
    state.verified_complete = not missing
    state.last_attempt_at = now
    state.last_success_at = now
    state.last_error = ""

    if state.verified_complete:
        state.consecutive_failures = 0
        logger.info(
            "Archive state complete for %s (%s): stored=%d known_gaps=%d.",
            state.symbol, state.endpoint, state.stored_rows, state.known_gap_rows,
        )
        # Not a flat +20h: that drifts across the clock, so a state that verified
        # at midday re-verified the same stale history the next midday and never
        # picked up the day it had just missed.
        state.next_attempt_at = market_state.next_post_close(
            now + _REVERIFY_INTERVAL.get(state.endpoint, timedelta(0))
        )
    else:
        _reschedule(
            state,
            stored_count=len(stored),
            missing_count=len(missing),
            previous_stored=previous_stored,
            previous_missing=previous_missing,
            now=now,
        )
        logger.info("Archive state incomplete for %s (%s): stored=%d expected=%d missing=%d.", state.symbol, state.endpoint, state.stored_rows, state.expected_rows, state.missing_rows)
    state.save()
    # A suspended state only reaches here via the weekly probe. If the fetch
    # came back clean, lift the suspension so it rejoins normal scheduling.
    if state.suspended_at is not None:
        from . import suspension

        suspension.try_recover(state)
    return state


def _reschedule(state, *, stored_count, missing_count, previous_stored,
                previous_missing, now=None):
    """Decide when an incomplete state tries again, from whether it advanced.

    `created` was truthy on every pass because ingest_real_legal re-updates rows
    it has already written, so an older fast-retry branch rescheduled
    non-converging states every 60s forever (one symbol ran 126x in 5h). Only
    real progress earns the fast path.

    Progress is measured on `stored`, not only on `missing`. For ticks `expected`
    is a *moving* 90-day window: as each new trading day opens it gains a day, so
    banking one day leaves `missing` flat -- and the symbol was punished for
    succeeding, backing off 1h, 2h, 4h ... to the 24h cap while steadily storing
    data. That throttled the largest remaining backlog in the warehouse. A gap
    that shrank still counts; so does a row that landed.
    """
    now = now or timezone.now()
    if stored_count > previous_stored or missing_count < previous_missing:
        state.consecutive_failures = 0
        state.next_attempt_at = now + timedelta(minutes=1)
    else:
        state.consecutive_failures += 1
        state.next_attempt_at = now + timedelta(
            hours=min(2 ** max(state.consecutive_failures - 1, 0), 24)
        )


_ENDPOINT_PRIORITY = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.GOLD_DAILY,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
)

# Postgres sorts NULLs last on ASC, so a never-attempted state (last_attempt_at
# IS NULL) sorted *behind* every state that had ever run -- the never-fetched
# work was permanently last in line. Oldest-first must mean never-run-first.
_LAST_ATTEMPT_FIRST = F("last_attempt_at").asc(nulls_first=True)


def release_archive_claims(state_ids):
    """Immediately make states claimed but not started by a bounded tick due again."""
    if state_ids:
        ArchiveFetchState.objects.filter(pk__in=state_ids).update(next_attempt_at=timezone.now())


def _archive_prereqs_ready(state):
    """Whether dependency endpoints have landed enough for this state to spend quota."""
    if state.endpoint == ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS:
        return bool(_tick_trading_days(state.symbol, state.target_window_days))
    if state.endpoint == ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED:
        return DailyStockHistory.objects.filter(symbol=state.symbol).exists()
    return True


def _pick_ready_states(candidates, limit, now, deferred_pks):
    """Take up to `limit` prereq-ready states; soft-defer the rest without failing."""
    ready = []
    for state in candidates:
        if len(ready) >= limit:
            break
        if state.pk in deferred_pks:
            continue
        if _archive_prereqs_ready(state):
            ready.append(state)
        else:
            deferred_pks.add(state.pk)
            # The same hold as the post-fetch prereq branch, decided before the
            # call rather than after it. It costs no quota, but a state parked
            # here is just as stuck and was just as invisible, so it is counted
            # the same way -- one rule, not two that drift.
            _defer_for_prereq(state, now=now, error=PREREQ_WAIT_ERROR)
    return ready


_GAP_REFETCHABLE_ENDPOINTS = (
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.GOLD_DAILY,
)


def reopen_states_with_gaps(symbols=None):
    """Put symbols with missing sessions back in the fetch queue.

    `symbols=None` means every symbol, which is what a market-wide ingest
    outage calls for.

    A state marks itself complete once it has stored everything the provider's
    last payload contained, which says nothing about history outside that
    payload. `claim_archive_batch` then skips it forever, so a hole in the
    warehouse is self-sealing: the only component that can see missing sessions
    is `compute_symbol_integrity`, and it had no way to ask for a re-fetch.
    This is that way.
    """
    states = ArchiveFetchState.objects.filter(
        endpoint__in=_GAP_REFETCHABLE_ENDPOINTS, verified_complete=True
    )
    if symbols is not None:
        symbols = list(symbols)
        if not symbols:
            return 0
        states = states.filter(symbol__in=symbols)
    return states.update(
        verified_complete=False, next_attempt_at=None, consecutive_failures=0
    )


#: Endpoints whose convergence gates the tick lane. These are the per-symbol
#: daily series (one request buys the WHOLE history), so they finish in about one
#: symbol-count of requests. Ticks are per symbol-day and effectively unbounded,
#: so letting them run first means the cheap, finite work waits behind the
#: expensive, infinite work.
_DAILY_GATE_ENDPOINTS = (
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
)


def _tick_share_now():
    """The tick lane's share of a batch, held down until the dailies converge.

    "Dailies first, ticks after" as an explicit gate rather than a fixed split,
    because the two are not comparable work. A daily endpoint costs ONE request
    per symbol for its entire history, so all three finish in ~5,900 requests --
    well under a day of the meter. Ticks cost one request per symbol-day against
    a ~2.1M-day universe. A static share lets the unbounded lane hold up the
    bounded one indefinitely.

    Cached briefly: this runs on every claim and the count is a full scan of the
    state table. A stale answer only mis-sizes one batch.
    """
    from django.core.cache import cache

    full = float(getattr(settings, "MARKETDATA_TICK_QUOTA_SHARE", 0.25))
    gated = float(getattr(settings, "MARKETDATA_TICK_SHARE_WHILE_DAILY_GAPS", 0.05))
    target = float(getattr(settings, "MARKETDATA_DAILY_GATE_COMPLETENESS", 0.95))
    key = "marketdata:daily_gate_ratio"
    try:
        ratio = cache.get(key)
    except Exception:
        ratio = None
    if ratio is None:
        rows = ArchiveFetchState.objects.filter(endpoint__in=_DAILY_GATE_ENDPOINTS)
        total = rows.count()
        # No daily states at all means nothing to gate on -- do not starve ticks
        # on the strength of an empty table.
        ratio = (rows.filter(verified_complete=True).count() / total) if total else 1.0
        try:
            cache.set(key, ratio, timeout=300)
        except Exception:
            pass
    return full if ratio >= target else gated


def claim_archive_batch(limit=None):
    """Lease the next batch of states to fetch, in the operator's priority order.

    Three rules, applied in this order:

    1. Anything that has never had a single successful fetch goes first, at any
       cost, across every endpoint. A symbol we hold nothing for is worse than a
       symbol that is merely out of date.
    2. Intraday ticks -- the one endpoint with an unbounded backlog -- take a
       fixed majority share of what is left. A share rather than strict priority
       on purpose: ticks buy one symbol-day per request and `grow_tick_windows`
       keeps reopening finished windows, so strict priority would starve every
       other endpoint for years rather than days.
    3. Everything else fills the remainder, most-starved first.

    Either lane hands its unused slots to the other, so a quiet tick queue never
    leaves quota unspent.
    """
    now = timezone.now()
    # Sized against the roomiest wallet, not the sum. Summing would over-claim;
    # taking the minimum would let a spent TSETMC plan stop the handful of gold
    # states, which is the whole cross-plan failure this work removed.
    capacity = archive_capacity()
    batch_size = min(
        limit or settings.MARKETDATA_ARCHIVE_BATCH_SIZE,
        max([*capacity.values(), 0]),
    )
    if not batch_size:
        return []
    # ...but size is not enough on its own. 11,719 of the 11,759 states bill
    # TSETMC and 38 bill BRS, so once TSETMC was spent for the day, BRS's idle
    # 1,500 kept `archive_capacity()` non-zero and this went on claiming full
    # batches out of a pool that is 99.7% TSETMC. Every one of them was refused
    # by `reserve_request`, at 11,053 refusals an hour, each writing a ledger
    # row -- the earlier comment here justified that as harmless because "a
    # batch is claimed before anyone knows which plan each state bills", which
    # is not true: the wallet is a static property of the endpoint.
    spent = {plan for plan, room in capacity.items() if room <= 0}
    blocked_endpoints = [
        endpoint for endpoint, plan in _ENDPOINT_PLAN.items()
        if plan is not None and plan in spent
    ]
    due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    deferred_pks = set()
    tick_endpoint = ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
    with transaction.atomic():
        # Suspended states are excluded here or suspension does nothing: they
        # would keep being claimed by the normal path and keep burning quota on
        # a symbol already proven to return bad data. They come back only via
        # the weekly probe (claim_probe_batch) or an operator force_retry.
        base = ArchiveFetchState.objects.select_for_update(skip_locked=True).filter(
            due, suspended_at__isnull=True
        ).exclude(endpoint__in=disabled_endpoints()).exclude(
            endpoint__in=blocked_endpoints
        )
        states = []

        def take(candidates, n):
            if n <= 0:
                return
            picked = _pick_ready_states(candidates, n, now, deferred_pks)
            deferred_pks.update(state.pk for state in picked)
            states.extend(picked)

        def starved(qs, n, extra=8):
            """Least-covered first, then most-owed, then longest-since-success."""
            return list(
                _starvation_rank_qs(qs.exclude(pk__in=deferred_pks)).order_by(
                    "_coverage_rank",
                    "-_deficit",
                    F("last_success_at").asc(nulls_first=True),
                )[: n + extra]
            )

        # 1. Never succeeded anywhere. Ordered by _coverage_rank so a state that
        #    has not even been attempted leads the ones that tried and failed.
        take(
            list(
                _coverage_rank_qs(
                    base.filter(last_success_at__isnull=True)
                ).order_by("_coverage_rank", _LAST_ATTEMPT_FIRST)[: batch_size + 8]
            ),
            batch_size,
        )

        # 2/3. Split the remainder between the tick lane and everything else,
        #      each lane taking whatever the other cannot use.
        #
        # A *complete* tick state is deliberately not claimable here: reopening
        # one means widening its window, which is `grow_tick_windows`'s job, and
        # re-fetching the same window would buy nothing. Every other endpoint's
        # complete states stay eligible -- that is how the daily refresh happens,
        # now at the back of the queue instead of ahead of it.
        ticks = base.filter(endpoint=tick_endpoint, verified_complete=False)
        # Intraday detail is only ever trained on for symbols liquid enough to
        # trade, so buying one request per historical day for the illiquid tail
        # is pure spend. Daily endpoints are untouched by this -- every symbol
        # still gets its candles and history; this bounds the per-day endpoint
        # alone, which is the only one whose cost scales with calendar length.
        if getattr(settings, "MARKETDATA_TICK_UNIVERSE_ONLY", False):
            try:
                from . import reversal

                universe = reversal.liquid_symbol_set()
                if universe:
                    ticks = ticks.filter(symbol__in=universe)
            except Exception:  # noqa: BLE001 -- never block the claim
                logger.warning("tick universe unavailable; claiming unrestricted",
                               exc_info=True)
        remaining = batch_size - len(states)
        tick_slots = int(remaining * _tick_share_now())
        take(starved(ticks, tick_slots, extra=16), tick_slots)

        remaining = batch_size - len(states)
        take(starved(base.exclude(endpoint=tick_endpoint), remaining), remaining)

        # Carryover: the general lane could not fill the slots the tick share left
        # it, so let ticks have them back rather than under-spending the batch.
        remaining = batch_size - len(states)
        take(starved(ticks, remaining, extra=16), remaining)

        claim_until = now + timedelta(minutes=10)
        ArchiveFetchState.objects.filter(pk__in=[state.pk for state in states]).update(
            next_attempt_at=claim_until
        )
    return [state.pk for state in states]


def get_deep_tier_symbols(all_symbols: set[str] | None = None) -> set[str]:
    """Symbols eligible for deep tick history: held symbols + the liquid core.

    Shallow-tier symbols stay capped at their initial 90-day seed window and never
    widen, conserving the metered API budget for assets users actually hold or
    that represent the market's liquid core.

    Liquidity is measured as median daily turnover from candles we already own,
    NOT from `StockSymbolMetadata.market_cap`. That column is filled by the weekly
    metadata sync and on 2026-09-04 held a value for **86 of 1,969 symbols**, so
    "top N by market cap" silently meant "the 86 we happen to know" -- a cap of
    100 could never bind, and the tier was decided by which symbols the sync had
    reached rather than by liquidity. Turnover covers 1,156 symbols, costs no
    request, and is the better proxy anyway: market cap counts shares that never
    trade, and only tradeable names are worth deep intraday history.
    """
    from portfolio.models import Holding
    from . import reversal

    held = set(
        Holding.objects.filter(quantity__gt=0)
        .exclude(asset__tse_symbol="")
        .values_list("asset__tse_symbol", flat=True)
    )
    n = getattr(settings, "MARKETDATA_DEEP_TIER_N", 100)
    liquid = set(reversal.liquid_symbols(n))
    if not held and not liquid and all_symbols:
        return all_symbols
    return held | liquid


def grow_tick_windows(step_days=90):
    """Widen fully-backfilled tick windows by another `step_days` for deep tier symbols.

    Tiered growth: only held symbols and top-N liquid stocks (deep tier) widen
    back to their listing date. Shallow-tier symbols remain capped at their initial
    seed window and do not grow.

    Clamped to MAX_TICK_WINDOW_DAYS to prevent PositiveSmallIntegerField overflow.
    """
    from .models import InstrumentListingHistory

    tick_endpoint = ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
    done = list(
        ArchiveFetchState.objects.filter(
            endpoint=tick_endpoint, verified_complete=True
        ).values_list("symbol", "target_window_days")
    )
    if not done:
        return 0

    deep_tier = get_deep_tier_symbols(all_symbols={symbol for symbol, _ in done})
    first_seen = dict(
        InstrumentListingHistory.objects.filter(
            symbol__in=[symbol for symbol, _ in done]
        ).values_list("symbol", "first_seen")
    )
    today = jalali.to_gregorian(jalali.today())
    to_grow = []
    for symbol, window in done:
        if symbol not in deep_tier:
            continue  # shallow tier: 90-day seed, no growth
        if window >= MAX_TICK_WINDOW_DAYS:
            continue  # at the clamp: no wider window exists to fetch into
        listed = first_seen.get(symbol)
        if listed:
            greg_listed = jalali.to_gregorian(listed)
            if greg_listed is not None:
                span_to_listing = (today - greg_listed).days
                if window >= span_to_listing:
                    continue  # already backfilled to this symbol's own listing date
        to_grow.append(symbol)
    if not to_grow:
        return 0
    return ArchiveFetchState.objects.filter(
        endpoint=tick_endpoint, symbol__in=to_grow, verified_complete=True
    ).update(
        target_window_days=Least(
            F("target_window_days") + step_days,
            Value(MAX_TICK_WINDOW_DAYS, output_field=IntegerField()),
        ),
        verified_complete=False,
        next_attempt_at=timezone.now(),
    )


def claim_archive_maintenance(limit=2):
    """Low-rate leases for completed disclosure/shareholder reverification.

    Honours `disabled_endpoints()` for the same reason `claim_archive_batch`
    does, and it is easier to forget: it runs unconditionally from its own beat
    entry and selects `verified_complete=True` rows -- which is exactly what the
    758 finished Codal states in production are. Without the filter, switching
    Codal off still fetched two of them a day against an unreachable origin.

    There are THREE lease paths, not two: this one, `claim_archive_batch`, and
    `suspension.claim_probe_batch`. All three must filter.
    """
    now = timezone.now()
    due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    with transaction.atomic():
        states = list(
            ArchiveFetchState.objects.select_for_update(skip_locked=True)
            .filter(
                due,
                verified_complete=True,
                endpoint__in=(
                    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
                    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
                ),
            )
            .exclude(endpoint__in=disabled_endpoints())
            .order_by(_LAST_ATTEMPT_FIRST)[:limit]
        )
        ArchiveFetchState.objects.filter(pk__in=[state.pk for state in states]).update(
            next_attempt_at=now + timedelta(hours=3)
        )
    return [state.pk for state in states]
