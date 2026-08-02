"""Gap-driven archive worker that verifies provider rows landed in PostgreSQL."""
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
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
from .quota import ARCHIVE, QuotaExhausted, remaining_requests


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
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY,
)
_RANGE_HISTORY = (ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,)


def _cost_rank_qs(qs):
    from django.db.models import Case, IntegerField, Value, When
    return qs.annotate(
        _cost_rank=Case(
            When(endpoint__in=_FULL_HISTORY, then=Value(0)),
            When(endpoint__in=_RANGE_HISTORY, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    )


# Tuple consumed by the existing `.order_by(*_COST_ORDER)` call sites.
_COST_ORDER = ("_cost_rank",)

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
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
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
    rows += [
        ArchiveFetchState(endpoint=ArchiveFetchState.Endpoint.COMMODITY_DAILY, symbol="COMMODITIES"),
        ArchiveFetchState(endpoint=ArchiveFetchState.Endpoint.CRYPTO_DAILY, symbol="CRYPTO"),
        ArchiveFetchState(endpoint=ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY, symbol="TEDPIX"),
    ]
    if rows:
        ArchiveFetchState.objects.bulk_create(rows, ignore_conflicts=True)


def _fetch_and_ingest(state):
    endpoint = state.endpoint
    symbol = state.symbol
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
        stored_real_legal = set(RealLegalHistory.objects.filter(
            symbol=symbol, date__in=expected,
        ).values_list("date", flat=True))
        stored_prices = set(DailyStockHistory.objects.filter(
            symbol=symbol, is_adjusted=False, date__in=expected,
        ).values_list("date", flat=True))
        stored = stored_real_legal.intersection(stored_prices)
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
    elif endpoint == ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY:
        payload = fetch_option_contracts(symbol)
        result = ingest.ingest_option_contracts(symbol, payload)
        expected = _generic_dates(payload)
        stored = set(OptionContractHistory.objects.filter(symbol=symbol, date__in=expected).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.COMMODITY_DAILY:
        # One request returns every commodity for one day, so the unit of
        # verification is symbol+date -- keying on date alone would pass with a
        # single row stored out of fourteen.
        payload = fetch_commodity_prices()
        result = ingest.ingest_commodity_history(symbol, payload)
        expected = _snapshot_keys(payload)
        stored = _stored_snapshot_keys(CommodityHistory, expected)
    elif endpoint == ArchiveFetchState.Endpoint.CRYPTO_DAILY:
        payload = fetch_crypto_prices()
        result = ingest.ingest_crypto_history(symbol, payload)
        expected = _snapshot_keys(payload, name_keys=("name_en", "symbol"))
        stored = _stored_snapshot_keys(CryptoHistory, expected)
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
        pending = _tick_dates_needed(symbol)
        if not pending:
            # Nothing pending means one of two opposite things. With no daily
            # candles there is nothing to fetch against and the candle pass must
            # run first. With candles, every trading day is already stored and
            # reconciled -- that is completion. Treating both as an error flipped
            # finished symbols back to incomplete on every tick, so they never
            # converged and burned a retry slot forever.
            if not _tick_trading_days(symbol):
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
        # A day only reaches here twice if its stored ticks failed to reconcile.
        # bulk_create(ignore_conflicts=True) cannot correct existing rows, so the
        # day is cleared first and rebuilt from the fresh payload.
        StockTransactionTick.objects.filter(symbol=symbol, date=day).delete()
        result = ingest.ingest_transactions(symbol, day, payload)

        candle_volume = (
            MarketCandle.objects.filter(
                symbol=symbol, timeframe="1d_unadj", date_time=day
            ).values_list("volume", flat=True).first()
        )
        mismatch = validation.reconcile_tick_volume(
            payload if isinstance(payload, list) else [], candle_volume
        )
        if mismatch:
            # The provider's own daily bar disagrees with its own trade list, so
            # the day is not usable. Keep it out of `stored` and let it retry
            # rather than silently banking a wrong total.
            raise MarketDataFetchError(f"{day}: {mismatch}")

        # Progress is measured across the whole window, not this one day, so the
        # state stays incomplete and reschedules until the window is covered.
        expected = set(pending) | _tick_dates_stored(symbol)
        stored = _tick_dates_stored(symbol)
    elif endpoint == ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY:
        from .fetchers.index import fetch_market_index
        payload = fetch_market_index(settings.TSETMC_API_KEY)
        result = ingest.ingest_market_index(payload)
        expected = {payload.get("date")} if (payload and isinstance(payload, dict) and payload.get("date")) else set()
        stored = set(MarketIndexData.objects.filter(date__in=expected).values_list("date", flat=True))
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
    window = set(jalali.recent_days(window_days or TICK_WINDOW_DAYS))
    counts = list(
        MarketCandle.objects.filter(timeframe="1d_unadj", date_time__in=window)
        .values("date_time")
        .annotate(n=Count("symbol", distinct=True))
    )
    if not counts:
        return set()
    busiest = max(row["n"] for row in counts)
    # A real session has most of the universe quoting. A stray handful of
    # candles on a closed day (late corrections, off-market prints) should not
    # promote that day into the calendar.
    floor = max(1, busiest // 5)
    return {row["date_time"] for row in counts if row["n"] >= floor}


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
    candle_totals = dict(
        MarketCandle.objects.filter(
            symbol=symbol, timeframe="1d_unadj", date_time__in=days
        ).values_list("date_time", "volume")
    )
    return {
        day
        for day, candle_volume in candle_totals.items()
        if day in tick_totals and int(tick_totals[day] or 0) != int(candle_volume or 0)
    }


def _tick_trading_days(symbol):
    """Trading days this symbol actually has a daily candle for.

    Empty means the candle pass has not reached this symbol yet. That is a
    different condition from "every tick day is already stored", and callers
    must not conflate the two.
    """
    return market_trading_days() & set(
        MarketCandle.objects.filter(
            symbol=symbol, timeframe=MarketCandle.UNADJUSTED
        ).values_list("date_time", flat=True)
    )


def _tick_dates_needed(symbol):
    """Trading days in the trailing window still owing a correct set of ticks.

    Two kinds of work: days never fetched, and days whose stored ticks do not
    add up to the candle. The second kind is the self-repair path -- the check
    that finds them is the same one that proves a fresh fetch is right.
    """
    trading = _tick_trading_days(symbol)
    stored = _tick_dates_stored(symbol)
    missing = trading - stored
    broken = _tick_days_unreconciled(symbol, trading & stored)
    return sorted(missing | broken, reverse=True)


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
    logger.info("[INGEST] Processing backfill for %s (%s)...", state.symbol, state.endpoint)
    try:
        (created, _), expected, stored = _fetch_and_ingest(state)
    except QuotaExhausted as exc:
        state.last_attempt_at = now
        state.next_attempt_at = now + timedelta(minutes=1)
        state.last_error = "Daily quota unavailable."
        state.save(update_fields=["last_attempt_at", "next_attempt_at", "last_error"])
        logger.warning("[QUOTA] Daily API quota exhausted while processing %s (%s): %s", state.symbol, state.endpoint, exc)
        raise
    except MarketDataFetchError as exc:
        from .fetchers.base import TransientMarketDataError
        is_transient = isinstance(exc, TransientMarketDataError) or getattr(exc, "status_code", None) == 429

        if is_transient:
            state.last_attempt_at = now
            state.next_attempt_at = now + timedelta(minutes=2)
            state.last_error = f"Transient rate limit or network error: {exc}"[:500]
            state.save(update_fields=["last_attempt_at", "next_attempt_at", "last_error"])
            logger.warning("[TRANSIENT_ERROR] Temporary rate-limit or network error on %s (%s): %s. Rescheduling in 2m.", state.symbol, state.endpoint, exc)
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
        logger.error("[ARCHIVE_FETCH_ERROR] Failed backfill fetch for %s (%s): %s", state.symbol, state.endpoint, exc)
        return state

    missing = expected - stored
    # Records the validator permanently rejects (bad OHLC, volume mismatch)
    # will never land in stored. Without this, the state re-fetches forever.
    # ponytail: DB query only runs when there are actual missing records.
    if missing:
        rejected_dates = set(
            RejectedRecord.objects.filter(
                endpoint=state.endpoint, symbol=state.symbol,
            ).values_list("date", flat=True)
        )
        # Handles both date keys ("1405-05-03") and composite snapshot
        # keys ("Bitcoin|1405-05-03") in one pass.
        if rejected_dates:
            missing = {
                k for k in missing
                if k not in rejected_dates
                and k.split("|")[-1] not in rejected_dates
            }
    state.expected_rows = len(expected)
    state.stored_rows = len(stored)
    state.missing_rows = len(missing)
    state.first_date = min(expected) if expected else ""
    state.last_date = max(expected) if expected else ""
    state.verified_complete = not missing
    state.last_attempt_at = now
    state.last_success_at = now
    state.last_error = ""
    state.consecutive_failures = 0
    
    if state.verified_complete:
        logger.info("[INGEST] Successfully backfilled %s (%s): verified complete (stored %d rows).", state.symbol, state.endpoint, state.stored_rows)
        # Not a flat +20h: that drifts across the clock, so a state that verified
        # at midday re-verified the same stale history the next midday and never
        # picked up the day it had just missed.
        state.next_attempt_at = market_state.next_post_close(now)
    elif created:
        logger.info("[INGEST] Backfilled %s (%s): incomplete (stored %d/%d, %d missing).", state.symbol, state.endpoint, state.stored_rows, state.expected_rows, state.missing_rows)
        state.next_attempt_at = now + timedelta(minutes=1)
    else:
        logger.info("[INGEST] Backfilled %s (%s): incomplete (stored %d/%d, %d missing).", state.symbol, state.endpoint, state.stored_rows, state.expected_rows, state.missing_rows)
        state.next_attempt_at = now + timedelta(
            hours=min(2 ** max(state.consecutive_failures - 1, 0), 24)
        )
    state.save()
    return state


_ENDPOINT_PRIORITY = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.GOLD_DAILY,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
    ArchiveFetchState.Endpoint.COMMODITY_DAILY,
    ArchiveFetchState.Endpoint.CRYPTO_DAILY,
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
)


def release_archive_claims(state_ids):
    """Immediately make states claimed but not started by a bounded tick due again."""
    if state_ids:
        ArchiveFetchState.objects.filter(pk__in=state_ids).update(next_attempt_at=timezone.now())


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
        base = ArchiveFetchState.objects.select_for_update(skip_locked=True).filter(due)
        # One state from every due endpoint makes starvation impossible. The
        # remaining slots keep the user-facing historical-data priority.
        states = []
        for endpoint in _ENDPOINT_PRIORITY:
            state = _cost_rank_qs(base.filter(endpoint=endpoint)).order_by(
                "last_attempt_at", "verified_complete", "-missing_rows"
            ).first()
            if state:
                states.append(state)
                if len(states) == batch_size:
                    break
        remaining_slots = batch_size - len(states)
        if remaining_slots > 0:
            states += list(
                _cost_rank_qs(base.exclude(pk__in=[state.pk for state in states]))
                .order_by("verified_complete", *_COST_ORDER, "-missing_rows", "last_attempt_at")[:remaining_slots]
            )
        claim_until = now + timedelta(minutes=10)
        ArchiveFetchState.objects.filter(pk__in=[state.pk for state in states]).update(
            next_attempt_at=claim_until
        )
    return [state.pk for state in states]
