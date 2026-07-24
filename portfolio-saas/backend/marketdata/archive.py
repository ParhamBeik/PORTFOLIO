"""Gap-driven archive worker that verifies provider rows landed in PostgreSQL."""
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import ingest
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
    fetch_commodity_history,
    fetch_crypto_history,
    fetch_etf_nav_history,
    fetch_index_history,
    fetch_option_contracts,
    fetch_transaction_ticks,
)
from .models import (
    ArchiveFetchState,
    CodalAnnouncement,
    CommodityHistory,
    CryptoHistory,
    DailyStockHistory,
    EtfNavHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    OptionContractHistory,
    ShareholderRecord,
    StockTransactionTick,
)
from .quota import QuotaExhausted, remaining_requests


STOCK_ENDPOINTS = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
    ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY,
    ArchiveFetchState.Endpoint.ETF_NAV_DAILY,
    ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY,
    ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
    ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
    ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
)


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

    rows = [
        ArchiveFetchState(endpoint=endpoint, symbol=symbol)
        for symbol in stock_symbols
        for endpoint in STOCK_ENDPOINTS
    ]
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
        payload = fetch_daily_history(settings.TSETMC_API_KEY, symbol, history_type=1)
        result = ingest.ingest_daily_history(symbol, payload, is_adjusted=True)
        expected = _record_dates(payload)
        stored = set(DailyStockHistory.objects.filter(
            symbol=symbol, is_adjusted=True, date__in=expected
        ).values_list("date", flat=True))
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
    elif endpoint == ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY:
        payload = fetch_index_history(symbol)
        result = ingest.ingest_market_index(payload)
        expected = {ingest.normalize_jalali(payload.get("date"))} if isinstance(payload, dict) and "date" in payload else set()
        stored = set(MarketIndexData.objects.filter(date__in=expected).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.ETF_NAV_DAILY:
        payload = fetch_etf_nav_history(symbol)
        result = ingest.ingest_etf_nav(symbol, payload)
        expected = _generic_dates(payload)
        stored = set(EtfNavHistory.objects.filter(symbol=symbol, date__in=expected).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.OPTION_CONTRACT_DAILY:
        payload = fetch_option_contracts(symbol)
        result = ingest.ingest_option_contracts(symbol, payload)
        expected = _generic_dates(payload)
        stored = set(OptionContractHistory.objects.filter(symbol=symbol, date__in=expected).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.COMMODITY_DAILY:
        payload = fetch_commodity_history(symbol)
        result = ingest.ingest_commodity_history(symbol, payload)
        expected = _generic_dates(payload)
        stored = set(CommodityHistory.objects.filter(date__in=expected).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.CRYPTO_DAILY:
        payload = fetch_crypto_history(symbol)
        result = ingest.ingest_crypto_history(symbol, payload)
        expected = _generic_dates(payload)
        stored = set(CryptoHistory.objects.filter(date__in=expected).values_list("date", flat=True))
    elif endpoint == ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS:
        payload = fetch_codal_announcements(settings.TSETMC_API_KEY, symbol=symbol)
        result = ingest.ingest_codal(payload)
        expected = _codal_keys(payload)
        stored = {
            f"{code}_{dp}_{tp}"
            for code, dp, tp in CodalAnnouncement.objects.filter(
                symbol=symbol
            ).values_list("code", "date_publish", "time_publish")
        } & expected
    elif endpoint == ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS:
        payload = fetch_shareholders(settings.TSETMC_API_KEY, symbol=symbol)
        result = ingest.ingest_shareholders(symbol, payload)
        expected = _shareholder_keys(payload)
        stored = {
            f"{sid}_{dt}"
            for sid, dt in ShareholderRecord.objects.filter(
                symbol=symbol
            ).values_list("shareholder_id", "date")
        } & expected
    elif endpoint == ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS:
        payload = fetch_transactions(settings.TSETMC_API_KEY, symbol=symbol)
        result = ingest.ingest_transactions(symbol, "", payload)
        expected = _transaction_keys(payload)
        stored = {
            f"{row}_{dt}"
            for row, dt in StockTransactionTick.objects.filter(
                symbol=symbol
            ).values_list("row", "date")
        } & expected
    else:
        payload = fetch_gold_currency_pro_history_daily(settings.BRS_API_KEY, symbol)
        result = ingest.ingest_gold_currency_history(payload)
        expected = _gold_dates(payload)
        stored = set(GoldCurrencyHistory.objects.filter(
            symbol=symbol, date__in=expected
        ).values_list("date", flat=True))
    if payload is None:
        raise MarketDataFetchError("Provider returned empty/None payload.")
    return result, expected, stored


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
    items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
    return {
        ingest.normalize_jalali(rec.get("date") or rec.get("d"))
        for rec in items
        if isinstance(rec, dict) and (rec.get("date") or rec.get("d"))
    }


def _codal_keys(payload):
    records = payload.get("announcement") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return set()
    return {
        f"{rec.get('code', '') or ''}_{ingest.normalize_jalali(rec.get('date_publish', ''))}_{rec.get('time_publish', '') or ''}"
        for rec in records
        if isinstance(rec, dict)
    }


def _shareholder_keys(payload):
    if not isinstance(payload, list):
        return set()
    return {
        f"{rec.get('id')}_{ingest.normalize_jalali(rec.get('date', ''))}"
        for rec in payload
        if isinstance(rec, dict) and rec.get("id") is not None
    }


def _transaction_keys(payload):
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
        state.next_attempt_at = now + timedelta(hours=20)
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


def claim_archive_batch(limit=None):
    now = timezone.now()
    batch_size = min(
        limit or settings.MARKETDATA_ARCHIVE_BATCH_SIZE,
        max(remaining_requests(), 0),
    )
    if not batch_size:
        return []
    with transaction.atomic():
        states = list(
            ArchiveFetchState.objects.select_for_update(skip_locked=True)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("verified_complete", "-missing_rows", "last_attempt_at")[:batch_size]
        )
        claim_until = now + timedelta(minutes=10)
        ArchiveFetchState.objects.filter(pk__in=[state.pk for state in states]).update(
            next_attempt_at=claim_until
        )
    return [state.pk for state in states]
