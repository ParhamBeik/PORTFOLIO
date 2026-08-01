"""Quota, provider classification, and DB-verified archive progress."""
from unittest import mock
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from marketdata.archive import run_archive_state
from marketdata.catalog import is_ordinary_stock, sync_provider_catalog
from marketdata.fetchers.base import (
    PermanentMarketDataError,
    TransientMarketDataError,
    fetch_json,
)
from marketdata.models import (
    ApiRequestQuota,
    ArchiveFetchState,
    DailyStockHistory,
    MarketInstrument,
)
from marketdata.quota import (
    ARCHIVE,
    LIVE,
    QuotaExhausted,
    reconcile_account,
    remaining_requests,
    reserve_request,
)
from portfolio.models import Asset

pytestmark = pytest.mark.django_db


def test_live_floor_survives_an_archive_burst(settings):
    """Archive must never consume the requests reserved for customer-facing prices."""
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 4
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    settings.MARKETDATA_ARCHIVE_REQUEST_BUDGET = 10
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 10
    for _ in range(6):
        reserve_request(ARCHIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)
    for _ in range(4):
        reserve_request(LIVE)
    row = ApiRequestQuota.objects.get()
    assert row.used == 10
    assert row.archive_used == 6
    assert row.live_used == 4


def test_bucket_budget_caps_a_single_bucket(settings):
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 100
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 2
    reserve_request(LIVE)
    reserve_request(LIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(LIVE)


def test_provider_account_reconciles_local_counter(settings):
    """The provider's usage_today is truth; a missing block must not reset it."""
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10000
    reserve_request(ARCHIVE)
    assert reconcile_account({"usage_today": 4021, "request_block": 120}) == 120
    assert ApiRequestQuota.objects.get().used == 4021
    assert reconcile_account(None) == 0
    assert ApiRequestQuota.objects.get().used == 4021


def test_permanent_http_error_uses_one_call_without_retry(settings):
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10
    # Zero the whole live bucket: the reserve is bounded by floor + headroom, so
    # leaving the headroom set would hold back more than this 10-request day has.
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    with patch("marketdata.fetchers.base.requests.get") as get:
        get.return_value.status_code = 400
        get.return_value.json.side_effect = ValueError
        with pytest.raises(PermanentMarketDataError):
            fetch_json("https://example.test", retries=2)
    assert get.call_count == 1
    assert ApiRequestQuota.objects.get().used == 1


def test_transient_http_error_does_not_leak_api_key(settings, caplog):
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    secret = "provider-secret"
    from requests.exceptions import RequestException
    with patch(
        "marketdata.fetchers.base.requests.get",
        side_effect=RequestException(f"failed https://example.test/?key={secret}"),
    ):
        with pytest.raises(TransientMarketDataError) as exc:
            fetch_json("https://example.test", params={"key": secret}, retries=0)
    assert secret not in caplog.text
    assert secret not in str(exc.value)


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
    assert MarketInstrument.objects.filter(source="brs", symbol="BTC", eligible=True).exists()


# STOCK_HISTORY_ADJUSTED is History.php?type=1, which returns the Real/Legal
# participant breakdown and no price fields at all. These fixtures use that real
# shape; a price-shaped fixture used to pass while production stored 1.3M
# all-zero rows, because the verifier only compared date sets.
REAL_LEGAL_PAYLOAD = [
    {"date": "1404-01-01", "Buy_CountI": 12, "Buy_I_Volume": 500, "Sell_CountN": 3},
    {"date": "1404-01-02", "Buy_CountI": 15, "Buy_I_Volume": 700, "Sell_CountN": 4},
]


def _seed_price_days(symbol="TEST"):
    for date in ("1404-01-01", "1404-01-02"):
        DailyStockHistory.objects.create(
            symbol=symbol, date=date, is_adjusted=False, pl=100
        )


def test_archive_state_is_complete_only_after_rows_exist(settings):
    settings.TSETMC_API_KEY = "test-key"
    _seed_price_days()
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD):
        state = run_archive_state(state.pk)
    assert state.verified_complete
    assert state.expected_rows == state.stored_rows == 2
    assert state.missing_rows == 0
    assert (
        DailyStockHistory.objects.filter(
            symbol="TEST", is_adjusted=False, buy_count_i__isnull=False
        ).count()
        == 2
    )


def test_real_legal_without_price_rows_never_verifies(settings):
    """No price row for the day means nothing to attach the breakdown to."""
    settings.TSETMC_API_KEY = "test-key"
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD):
        state = run_archive_state(state.pk)
    assert not state.verified_complete
    assert state.missing_rows == 2


def test_archive_state_records_missing_rows_instead_of_claiming_success(settings):
    settings.TSETMC_API_KEY = "test-key"
    _seed_price_days()
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with (
        patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD),
        patch("marketdata.archive.ingest.ingest_real_legal", return_value=(0, 2)),
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


def test_5m_window_rate_limit(settings, monkeypatch):
    """We choose a unit test because verifying 5-minute rolling window rate limits tests fast, isolated business rules at the base of the test pyramid."""
    from marketdata import quota
    from marketdata.quota import get_quota_status
    monkeypatch.setattr(quota, "get_redis", lambda: None)
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 100
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    settings.MARKETDATA_ARCHIVE_REQUEST_BUDGET = 100
    settings.MARKETDATA_REQUIRE_SHARED_WINDOW = False
    # The window is per bucket, and with no Redis in the suite the degraded
    # per-process window applies, so back out both divisors to land on 3.
    settings.MARKETDATA_WINDOW_LIMIT = int(
        3 * quota._DEGRADED_PROCESS_DIVISOR / quota._WINDOW_SHARE[ARCHIVE]
    )
    settings.MARKETDATA_WINDOW_SECONDS = 300
    quota._LOCAL_WINDOWS.clear()

    reserve_request(ARCHIVE)
    reserve_request(ARCHIVE)
    reserve_request(ARCHIVE)

    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)

    status = get_quota_status()
    assert status["window_by_bucket"][ARCHIVE] >= 3


def test_window_quota_uses_one_atomic_redis_operation(settings, monkeypatch):
    from marketdata import quota

    settings.MARKETDATA_WINDOW_LIMIT = 3
    settings.MARKETDATA_WINDOW_SECONDS = 300
    client = mock.Mock()
    client.eval.return_value = 1
    monkeypatch.setattr(quota, "get_redis", lambda: client)

    quota._check_and_record_window()

    client.eval.assert_called_once()


def test_ensure_archive_states_covers_all_endpoints():
    """We choose a unit test because verifying archive state generation across all provider endpoints tests pure data warehouse mapping logic at the base of the test pyramid."""
    from marketdata.archive import ensure_archive_states
    ensure_archive_states(stock_symbols=["KAMA"], gold_symbols=["USD"])
    states = ArchiveFetchState.objects.filter(symbol="KAMA")
    endpoints = set(states.values_list("endpoint", flat=True))
    assert ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS in endpoints
    assert ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS in endpoints
    assert ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS in endpoints
    # 7, not 10: market_index_daily, etf_nav_daily and option_contract_daily are
    # live snapshots the provider cannot serve for a past date, so they no longer
    # get per-symbol backfill rows (migration 0008 deleted the existing ones).
    assert len(endpoints) == 7
    assert ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY not in endpoints
    assert ArchiveFetchState.Endpoint.ETF_NAV_DAILY not in endpoints


def test_archive_state_for_codal_shareholder_and_ticks(settings):
    """We choose an integration test because testing run_archive_state for Codal, Shareholder, and Ticks verifies fetcher response handling and database ingestion boundary logic."""
    from marketdata.archive import run_archive_state
    from marketdata.models import CodalAnnouncement, ShareholderRecord, StockTransactionTick

    settings.TSETMC_API_KEY = "test-key"

    codal_state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
        symbol="KAMA",
    )
    codal_payload = {
        "announcement": [
            {
                "l18": "KAMA",
                "title": "گزارش مالی",
                "code": "C001",
                "date_publish": "1404-01-01",
                "time_publish": "10:00:00",
            }
        ]
    }
    with patch("marketdata.archive.fetch_codal_announcements", return_value=codal_payload):
        run_archive_state(codal_state.pk)

    assert CodalAnnouncement.objects.filter(symbol="KAMA", code="C001").exists()

    sh_state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
        symbol="KAMA",
    )
    sh_payload = [{"id": 99, "name": "Bank Test", "volume": 1000, "percent": 5.0, "date": "1404-01-01"}]
    with patch("marketdata.archive.fetch_shareholders", return_value=sh_payload):
        run_archive_state(sh_state.pk)

    assert ShareholderRecord.objects.filter(symbol="KAMA", shareholder_id=99).exists()

    tick_state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="KAMA",
    )
    # Ticks cost one request per day, so the worker only asks for days it knows
    # were trading days -- which it learns from already-ingested daily candles.
    # Without a candle there is no calendar and the fetch is refused by design.
    from marketdata import jalali
    from marketdata.models import MarketCandle
    tick_day = jalali.today()
    MarketCandle.objects.create(
        symbol="KAMA", timeframe="1d_unadj", date_time=tick_day,
    )
    tick_payload = [{"row": 1, "price": 1500, "volume": 100, "time": "09:30:00", "date": tick_day}]
    with patch("marketdata.archive.fetch_transactions", return_value=tick_payload):
        run_archive_state(tick_state.pk)

    assert StockTransactionTick.objects.filter(symbol="KAMA", row=1).exists()


def test_archive_state_transient_error_reschedules_quickly_without_failure_increment(settings):
    from marketdata.fetchers.base import TransientMarketDataError
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST_TRANSIENT",
    )
    with patch("marketdata.archive._fetch_and_ingest", side_effect=TransientMarketDataError("Rate limited", status_code=429)):
        updated_state = run_archive_state(state.pk)
    
    assert updated_state.consecutive_failures == 0
    assert "Transient rate limit or network error" in updated_state.last_error
    # Should reschedule in 2 minutes
    diff = updated_state.next_attempt_at - updated_state.last_attempt_at
    assert 119 <= diff.total_seconds() <= 121


def test_archive_borrows_quota_the_live_bucket_never_spent(settings):
    """The fixed daily allowance should not go unused because a bucket capped out."""
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 20
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 4
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 2
    settings.MARKETDATA_ARCHIVE_REQUEST_BUDGET = 10
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 2

    for _ in range(10):
        reserve_request(ARCHIVE)
    row = ApiRequestQuota.objects.get()
    assert row.archive_used == 10  # own budget spent

    # 20 total - 10 archive - 6 live still spendable - 2 unspent other = 2 to borrow.
    # The live term is the reserve, not the bare floor: with a whole day left the
    # 5-minute cadence needs far more cycles than this bucket holds, so the
    # reserve saturates at everything live could still spend (4 floor + 2 headroom).
    assert remaining_requests(ARCHIVE) == 2
    for _ in range(2):
        reserve_request(ARCHIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)

    row.refresh_from_db()
    assert row.archive_used == 12
    # The live bucket and the other allowance are still intact.
    assert row.limit - row.used == 8
    for _ in range(4):
        reserve_request(LIVE)
    assert ApiRequestQuota.objects.get().live_used == 4


def test_borrowing_never_eats_the_live_floor(settings):
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 6
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    settings.MARKETDATA_ARCHIVE_REQUEST_BUDGET = 2
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 0

    for _ in range(4):
        reserve_request(ARCHIVE)  # 2 own + 2 borrowed
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)
    for _ in range(6):
        reserve_request(LIVE)
    assert ApiRequestQuota.objects.get().live_used == 6


def test_archive_state_permanent_error_exponential_backoff(settings):
    from marketdata.fetchers.base import PermanentMarketDataError
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST_PERMANENT",
    )
    with patch("marketdata.archive._fetch_and_ingest", side_effect=PermanentMarketDataError("Not found", status_code=404)):
        updated_state = run_archive_state(state.pk)
    
    assert updated_state.consecutive_failures == 1
    assert "PermanentMarketDataError" in updated_state.last_error
    # Should back off exponentially (1 hour for first failure)
    diff = updated_state.next_attempt_at - updated_state.last_attempt_at
    assert 3599 <= diff.total_seconds() <= 3601

