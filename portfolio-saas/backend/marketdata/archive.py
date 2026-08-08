"""Gap-driven archive worker that verifies provider rows landed in PostgreSQL."""
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.conf import settings
from django.db import transaction
from django.db.models import F, Max, Q, Sum
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
from .fetchers.base import MarketDataFetchError
from .fetchers.expanded import (
    fetch_commodity_prices,
    fetch_crypto_prices,
    fetch_option_contracts,
)
from .models import (
    ArchiveFetchState,
    CodalAnnouncement,
    CommodityHistory,
    CryptoHistory,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    OptionContractHistory,
    RealLegalHistory,
    RejectedRecord,
    ShareholderRecord,
    StockTransactionTick,
)
from .quota import ARCHIVE, QuotaExhausted, remaining_requests, increment_historical_full_used


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

# Cost-ordered classes: drain HISTORICAL_FULL first (~4,600 rows/request, the
# best rows-per-quota-unit available), then the paged RANGE class, then the
# expensive PER_DAY class last. Applied as an annotation so the cheap classes
# always outrank per-day ones regardless of the secondary sort that follows.
_FULL_HISTORY = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.GOLD_DAILY,
)
def _expected_gain_qs(qs):
    """Estimate new rows bought by the state's next provider request."""
    from django.db.models import Case, IntegerField, Value, When

    return qs.annotate(
        _expected_gain=Case(
            # Per-day endpoints can only advance one day regardless of the
            # state's total gap. Production's median tick-day yield is 17 rows.
            When(
                endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
                then=Value(17),
            ),
            When(
                endpoint=ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
                then=Value(20),
            ),
            When(
                endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
                then=Value(30),
            ),
            # A full-history request can close every currently known gap.
            When(endpoint__in=_FULL_HISTORY, missing_rows__gt=0, then=F("missing_rows")),
            When(endpoint__in=_FULL_HISTORY, last_success_at__isnull=True, then=Value(3000)),
            When(endpoint__in=_FULL_HISTORY, stored_rows=0, then=Value(3000)),
            default=Value(1),
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
_RETIRED_ARCHIVE_ENDPOINTS = frozenset({
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY,
    ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY,
})


def ensure_archive_states(stock_symbols=None, gold_symbols=None):
    from .models import MarketInstrument
    from .catalog import sync_provider_catalog

    if not MarketInstrument.objects.filter(eligible=True).exists():
        try:
            sync_provider_catalog()
        except Exception:
            pass

    if stock_symbols is None or not stock_symbols:
        from .tasks import tracked_tse_symbols
        stock_symbols = tracked_tse_symbols()
    if gold_symbols is None or not gold_symbols:
        from .tasks import tracked_brs_symbols
        gold_symbols = tracked_brs_symbols()

    import re
    rows = []
    for symbol in stock_symbols:
        is_derivative = bool(re.search(r"\d$", symbol))
        for endpoint in STOCK_ENDPOINTS:
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
    if endpoint in _FULL_HISTORY:
        increment_historical_full_used()
    if endpoint == ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED:
        payload = fetch_daily_history(settings.TSETMC_API_KEY, symbol, history_type=0)
        result = ingest.ingest_daily_history(symbol, payload, is_adjusted=False)
        expected = _record_dates(payload)
        stored = set(DailyStockHistory.objects.filter(
            symbol=symbol, is_adjusted=False, date__in=expected
        ).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED:
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
            symbol=symbol, is_adjusted=False, date__in=expected,
        ).exists():
            raise MarketDataFetchError(
                "Real/legal payload has no matching daily price rows; the "
                "unadjusted history pass must land first."
            )
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED:
        payload = fetch_candlesticks(settings.TSETMC_API_KEY, symbol, candle_type=2)
        result = ingest.ingest_candles(symbol, 2, payload)
        expected = _candle_dates(payload)
        stored = set(MarketCandle.objects.filter(
            symbol=symbol, timeframe="1d_unadj", date_time__in=expected
        ).values_list("date_time", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED:
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
            RejectedRecord.objects.update_or_create(
                endpoint="stock_transaction_ticks",
                symbol=symbol[:64],
                date=day[:10],
                reason=mismatch.split(":")[0][:64],
                defaults={"payload": {"day": day, "detail": mismatch}},
            )
            raise MarketDataFetchError(f"{day}: {mismatch}")

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
    from .candles import actual_trading_days

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


def _tick_days_unreconciled(symbol, days):
    """Stored tick days whose traded volume disagrees with the daily candle.

    Cancelled trades are excluded, which is the whole point: including them
    inflated volume by up to 14% and is why 370 stored stock-days disagreed with
    their own candles. A day that fails here is re-fetched, not patched.
    """
    if not days:
        return set()
    tick_totals = dict(
        StockTransactionTick.objects.filter(
            symbol=symbol, date__in=days, canceled=False
        )
        .values_list("date")
        .annotate(total=Sum("volume"))
    )
    candle_totals = _symbol_candle_volumes(symbol, days)
    return {
        day
        for day, candle_volume in candle_totals.items()
        if day in tick_totals and int(tick_totals[day] or 0) != int(candle_volume or 0)
    }


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
    """
    trading = _tick_trading_days(symbol, window_days)
    stored = _tick_dates_stored(symbol)
    missing = trading - stored
    broken = _tick_days_unreconciled(symbol, trading & stored)
    return sorted(missing, reverse=True) + sorted(broken, reverse=True)


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


def run_archive_state(state_id):
    state = ArchiveFetchState.objects.get(pk=state_id)
    now = timezone.now()
    logger.debug("Processing archive state %s (%s).", state.symbol, state.endpoint)
    try:
        (created, _), expected, stored = _fetch_and_ingest(state)
    except QuotaExhausted as exc:
        state.last_attempt_at = now
        state.next_attempt_at = now + timedelta(minutes=1)
        state.last_error = "Daily quota unavailable."
        state.save(update_fields=["last_attempt_at", "next_attempt_at", "last_error"])
        logger.debug("Archive quota unavailable for %s (%s): %s", state.symbol, state.endpoint, exc)
        raise
    except MarketDataFetchError as exc:
        from .fetchers.base import TransientMarketDataError
        is_transient = isinstance(exc, TransientMarketDataError) or getattr(exc, "status_code", None) == 429

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
            logger.debug("Archive request retry scheduled for %s (%s) in %dm: %s", state.symbol, state.endpoint, delay, exc)
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
        logger.debug("Archive fetch retry scheduled for %s (%s): %s", state.symbol, state.endpoint, exc)
        return state

    previous_missing = state.missing_rows
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
        logger.debug(
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
        # `created` was truthy on every pass because ingest_real_legal re-updates
        # rows it has already written, so the old fast-retry branch rescheduled
        # non-converging states every 60s forever (one symbol ran 126x in 5h).
        # Only real progress -- a gap that actually shrank -- earns the fast path.
        if len(missing) < previous_missing:
            state.consecutive_failures = 0
            state.next_attempt_at = now + timedelta(minutes=1)
        else:
            state.consecutive_failures += 1
            state.next_attempt_at = now + timedelta(
                hours=min(2 ** max(state.consecutive_failures - 1, 0), 24)
            )
        logger.debug("Archive state incomplete for %s (%s): stored=%d expected=%d missing=%d.", state.symbol, state.endpoint, state.stored_rows, state.expected_rows, state.missing_rows)
    state.save()
    return state


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


def claim_recent_refresh(limit=None):
    """Lease post-close history/candle refreshes for held then liquid symbols."""
    from portfolio.models import Holding

    request_limit = min(
        limit or settings.MARKETDATA_RECENT_REFRESH_REQUEST_BUDGET,
        max(remaining_requests(ARCHIVE), 0),
    )
    symbol_limit = request_limit // 2
    if not symbol_limit:
        return []

    held = list(
        Holding.objects.filter(quantity__gt=0)
        .exclude(asset__tse_symbol="")
        .values_list("asset__tse_symbol", flat=True)
        .distinct()
    )
    latest_day = MarketCandle.objects.filter(
        timeframe=MarketCandle.UNADJUSTED, volume__gt=0
    ).aggregate(day=Max("date_time"))["day"]
    liquid = list(
        MarketCandle.objects.filter(
            timeframe=MarketCandle.UNADJUSTED,
            date_time=latest_day,
            volume__gt=0,
        )
        .order_by("-volume")
        .values_list("symbol", flat=True)[:symbol_limit]
    ) if latest_day else []
    symbols = list(dict.fromkeys([*held, *liquid]))[:symbol_limit]
    if not symbols:
        return []

    endpoints = (
        ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
        ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    )
    now = timezone.now()
    due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    with transaction.atomic():
        states = list(
            ArchiveFetchState.objects.select_for_update(skip_locked=True).filter(
                due, symbol__in=symbols, endpoint__in=endpoints
            )
        )
        by_key = {(state.symbol, state.endpoint): state for state in states}
        ordered = [
            by_key[(symbol, endpoint)]
            for symbol in symbols
            for endpoint in endpoints
            if (symbol, endpoint) in by_key
        ][:request_limit]
        ArchiveFetchState.objects.filter(pk__in=[s.pk for s in ordered]).update(
            next_attempt_at=now + timedelta(hours=3)
        )
    return [state.pk for state in ordered]


def claim_archive_batch(limit=None):
    now = timezone.now()
    batch_size = min(
        limit or settings.MARKETDATA_ARCHIVE_BATCH_SIZE,
        max(remaining_requests(ARCHIVE), 0),
    )
    if not batch_size:
        return []
    due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    with transaction.atomic():
        base = ArchiveFetchState.objects.select_for_update(skip_locked=True).filter(
            due, verified_complete=False
        )
        tick_endpoint = ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
        # Reserve at most two jobs for high-yield/critical non-tick repair. The
        # rest advances breadth-first tick coverage; completed rows never churn
        # through this normal batch.
        non_tick = list(
            _coverage_rank_qs(_expected_gain_qs(base.exclude(endpoint=tick_endpoint)))
            .order_by("_coverage_rank", "-_expected_gain", _LAST_ATTEMPT_FIRST)[:2]
        )
        states = non_tick
        remaining_slots = batch_size - len(states)
        if remaining_slots > 0:
            states += list(
                base.filter(endpoint=tick_endpoint)
                .order_by("target_window_days", "stored_rows", _LAST_ATTEMPT_FIRST)
                [:remaining_slots]
            )
        remaining_slots = batch_size - len(states)
        if remaining_slots > 0:
            states += list(
                _coverage_rank_qs(_expected_gain_qs(
                    base.exclude(pk__in=[state.pk for state in states])
                    .exclude(endpoint=tick_endpoint)
                ))
                .order_by("_coverage_rank", "-_expected_gain", _LAST_ATTEMPT_FIRST)
                [:remaining_slots]
            )
        claim_until = now + timedelta(minutes=10)
        ArchiveFetchState.objects.filter(pk__in=[state.pk for state in states]).update(
            next_attempt_at=claim_until
        )
    return [state.pk for state in states]


def promote_priority_tick_windows(liquid_limit=100):
    """Begin the 365-day phase only after every 90-day tick state completes."""
    tick_endpoint = ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
    phase_one = ArchiveFetchState.objects.filter(
        endpoint=tick_endpoint, target_window_days=90
    )
    if not phase_one.exists() or phase_one.filter(verified_complete=False).exists():
        return 0

    from portfolio.models import Holding

    held = list(
        Holding.objects.filter(quantity__gt=0)
        .exclude(asset__tse_symbol="")
        .values_list("asset__tse_symbol", flat=True)
        .distinct()
    )
    latest_day = MarketCandle.objects.filter(
        timeframe=MarketCandle.UNADJUSTED, volume__gt=0
    ).aggregate(day=Max("date_time"))["day"]
    liquid = list(
        MarketCandle.objects.filter(
            timeframe=MarketCandle.UNADJUSTED,
            date_time=latest_day,
            volume__gt=0,
        )
        .order_by("-volume")
        .values_list("symbol", flat=True)[:liquid_limit]
    ) if latest_day else []
    symbols = list(dict.fromkeys([*held, *liquid]))
    if not symbols:
        return 0
    return ArchiveFetchState.objects.filter(
        endpoint=tick_endpoint, symbol__in=symbols, target_window_days=90
    ).update(
        target_window_days=365,
        verified_complete=False,
        next_attempt_at=timezone.now(),
    )


def claim_archive_maintenance(limit=2):
    """Low-rate leases for completed disclosure/shareholder reverification."""
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
            .order_by(_LAST_ATTEMPT_FIRST)[:limit]
        )
        ArchiveFetchState.objects.filter(pk__in=[state.pk for state in states]).update(
            next_attempt_at=now + timedelta(hours=3)
        )
    return [state.pk for state in states]
