"""Gap-driven archive worker that verifies provider rows landed in PostgreSQL."""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import ingest
from .fetchers import (
    fetch_candlesticks,
    fetch_daily_history,
    fetch_gold_currency_pro_history_daily,
)
from .fetchers.base import MarketDataFetchError
from .models import ArchiveFetchState, DailyStockHistory, GoldCurrencyHistory, MarketCandle
from .quota import QuotaExhausted, remaining_requests


STOCK_ENDPOINTS = (
    ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
    ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
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
    else:
        payload = fetch_gold_currency_pro_history_daily(settings.BRS_API_KEY, symbol)
        result = ingest.ingest_gold_currency_history(payload)
        expected = _gold_dates(payload)
        stored = set(GoldCurrencyHistory.objects.filter(
            symbol=symbol, date__in=expected
        ).values_list("date", flat=True))
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


def run_archive_state(state_id):
    state = ArchiveFetchState.objects.get(pk=state_id)
    now = timezone.now()
    try:
        (created, _), expected, stored = _fetch_and_ingest(state)
    except QuotaExhausted:
        state.last_attempt_at = now
        state.next_attempt_at = now + timedelta(minutes=1)
        state.last_error = "Daily quota unavailable."
        state.save(update_fields=["last_attempt_at", "next_attempt_at", "last_error"])
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
        return state

    missing = expected - stored
    state.expected_rows = len(expected)
    state.stored_rows = len(stored)
    state.missing_rows = len(missing)
    state.first_date = min(expected) if expected else ""
    state.last_date = max(expected) if expected else ""
    state.verified_complete = bool(expected) and not missing
    state.last_attempt_at = now
    state.last_success_at = now if expected else state.last_success_at
    state.last_error = "" if expected else "Provider returned no archive rows."
    state.consecutive_failures = 0 if expected else state.consecutive_failures + 1
    if state.verified_complete:
        state.next_attempt_at = now + timedelta(hours=20)
    elif created:
        state.next_attempt_at = now + timedelta(minutes=1)
    else:
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
