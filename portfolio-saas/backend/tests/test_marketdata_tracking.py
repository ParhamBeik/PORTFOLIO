"""Quota, provider classification, and DB-verified archive progress."""
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from marketdata.archive import run_archive_state
from marketdata.catalog import is_ordinary_stock, sync_provider_catalog
from marketdata.fetchers.base import PermanentMarketDataError, fetch_json
from marketdata.models import (
    ApiRequestQuota,
    ArchiveFetchState,
    DailyStockHistory,
    MarketInstrument,
)
from marketdata.quota import ARCHIVE, LIVE, QuotaExhausted, reserve_request
from portfolio.models import Asset

pytestmark = pytest.mark.django_db


def test_quota_reserves_ninety_percent_for_archive(settings):
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10
    settings.MARKETDATA_ARCHIVE_REQUEST_RESERVE = 9
    reserve_request(LIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(LIVE)
    for _ in range(9):
        reserve_request(ARCHIVE)
    row = ApiRequestQuota.objects.get()
    assert row.used == 10
    assert row.archive_used == 9


def test_permanent_http_error_uses_one_call_without_retry(settings):
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10
    settings.MARKETDATA_ARCHIVE_REQUEST_RESERVE = 0
    with patch("marketdata.fetchers.base.requests.get") as get:
        get.return_value.status_code = 400
        with pytest.raises(PermanentMarketDataError):
            fetch_json("https://example.test", retries=2)
    assert get.call_count == 1
    assert ApiRequestQuota.objects.get().used == 1


def test_catalog_accepts_shares_and_rejects_rights_and_funds():
    assert is_ordinary_stock({"isin": "IRO1TEST0001"})
    assert is_ordinary_stock({"isin": "IRO3TEST0001"})
    assert not is_ordinary_stock({"isin": "IRR1TEST0101"})
    assert not is_ordinary_stock({"isin": "IRT1TEST0001"})


def test_catalog_accepts_all_provider_currencies(settings):
    """We choose an integration test because provider catalog sync crosses fetcher payload parsing and DB persistence."""
    settings.TSETMC_API_KEY = "test-key"
    settings.BRS_API_KEY = "test-key"
    with (
        patch("marketdata.catalog.fetch_all_symbols", return_value=[]),
        patch("marketdata.catalog.fetch_gold_currency_free", return_value={
            "currency": [
                {"symbol": "USD", "name": "Dollar"},
                {"symbol": "EUR", "name": "Euro"},
            ],
            "crypto": [{"symbol": "BTC", "name": "Bitcoin"}],
        }),
    ):
        sync_provider_catalog()

    assert MarketInstrument.objects.filter(source="brs", symbol="USD", eligible=True).exists()
    assert MarketInstrument.objects.filter(source="brs", symbol="EUR", eligible=True).exists()
    assert MarketInstrument.objects.filter(source="brs", symbol="BTC", eligible=False).exists()


def test_archive_state_is_complete_only_after_rows_exist(settings):
    settings.TSETMC_API_KEY = "test-key"
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    payload = [
        {"date": "1404-01-01", "pl": 100},
        {"date": "1404-01-02", "pl": 101},
    ]
    with patch("marketdata.archive.fetch_daily_history", return_value=payload):
        state = run_archive_state(state.pk)
    assert state.verified_complete
    assert state.expected_rows == state.stored_rows == 2
    assert state.missing_rows == 0
    assert DailyStockHistory.objects.filter(symbol="TEST", is_adjusted=True).count() == 2


def test_archive_state_records_missing_rows_instead_of_claiming_success(settings):
    settings.TSETMC_API_KEY = "test-key"
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    payload = [
        {"date": "1404-01-01", "pl": 100},
        {"date": "1404-01-02", "pl": 101},
    ]
    with (
        patch("marketdata.archive.fetch_daily_history", return_value=payload),
        patch("marketdata.archive.ingest.ingest_daily_history", return_value=(0, 2)),
    ):
        state = run_archive_state(state.pk)
    assert not state.verified_complete
    assert state.missing_rows == 2


def test_active_asset_must_exist_in_verified_catalog():
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS,
        symbol="IR_GOLD_18K",
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )
    Asset.objects.create(
        key="verified_gold",
        name="Verified Gold",
        asset_class=Asset.AssetClass.GOLD,
        brs_symbol="IR_GOLD_18K",
    )
    with pytest.raises(ValidationError):
        Asset.objects.create(
            key="invented_gold",
            name="Invented Gold",
            asset_class=Asset.AssetClass.GOLD,
            brs_symbol="NOT_REAL",
        )


def test_5m_window_rate_limit(settings):
    """We choose a unit test because verifying 5-minute rolling window rate limits tests fast, isolated business rules at the base of the test pyramid."""
    from marketdata import quota
    from marketdata.quota import get_quota_status
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 100
    settings.MARKETDATA_ARCHIVE_REQUEST_RESERVE = 0
    settings.MARKETDATA_WINDOW_LIMIT = 3
    settings.MARKETDATA_WINDOW_SECONDS = 300
    quota._LOCAL_WINDOW.clear()
    client = quota.get_redis()
    if client is not None:
        client.delete("quota:window:5m")

    reserve_request(ARCHIVE)
    reserve_request(ARCHIVE)
    reserve_request(ARCHIVE)

    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)

    status = get_quota_status()
    assert status["window_used"] >= 3
    assert status["remaining_window"] == 0
