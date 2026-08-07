"""Ingest-layer tests: payload -> rows, idempotency, malformed-record tolerance.

The mock payloads mirror tests/test_advanced_fetchers.py (the authoritative
BrsApi shapes). Key invariants: bulk ingest is idempotent under re-run (unique
constraints + ignore_conflicts), one bad record never sinks a batch, and Jalali
dates are stored dash-normalized.
"""
import pytest

from marketdata import ingest
from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    StockSymbolMetadata,
)

HISTORY_PAYLOAD = [
    {
        "date": "1403-10-19", "time": "12:29:59", "tno": 7301, "tvol": 129326764,
        "tval": 1108829664180, "pmin": 8490, "pmax": 8680, "py": 8430, "pf": 8570,
        "pl": 8500, "plc": 70, "plp": 0.83, "pc": 8570, "pcc": 140, "pcp": 1.66,
        "Buy_CountI": 2416, "Buy_CountN": 20, "Sell_CountI": 2343, "Sell_CountN": 26,
        "Buy_I_Volume": 68461905, "Buy_N_Volume": 60864859,
        "Sell_I_Volume": 82617958, "Sell_N_Volume": 46708806,
    },
    {
        "date": "1403/10/20", "time": "12:29:59", "tno": 5000, "tvol": 90000000,
        "tval": 800000000000, "pmin": 8500, "pmax": 8700, "py": 8500, "pf": 8600,
        "pl": 8650, "plc": 150, "plp": 1.76, "pc": 8640, "pcc": 140, "pcp": 1.65,
    },
]

CANDLE_PAYLOAD = {
    "l18": "فملی", "type": 3, "count": 2,
    "candle_daily_adjusted": [
        {"date": "1404-02-24", "open": 7380, "high": 7400, "low": 7280, "close": 7340, "volume": 180715348},
        {"date": "1404/02/25", "open": 7340, "high": 7500, "low": 7300, "close": 7450, "volume": 150000000},
    ],
}

CODAL_PAYLOAD = {
    "count_announcement": 1, "count_page": 1,
    "announcement": [
        {
            "l18": "وبملت", "l30": "بانک ملت", "title": "صورت‌های مالی میاندوره‌ای",
            "code": "ن-۱۰", "date_title": "1403/09/30", "date_publish": "1403/10/30",
            "time_publish": "17:55:41", "link": "https://codal.ir/Reports/Decision.aspx",
        }
    ],
}

GOLD_PAYLOAD = {
    "symbol": "IR_COIN_EMAMI", "name": "سکه امامی", "unit": "تومان",
    "history_daily": [
        {"date": "1404/03/21", "open": 73290000, "high": 73610000, "low": 73080000, "close": 73385000},
        {"date": "1404/03/22", "open": 73385000, "high": 74000000, "low": 73300000, "close": 73900000},
    ],
}


@pytest.mark.django_db
def test_ingest_daily_history_maps_fields_and_normalizes_dates():
    created, skipped = ingest.ingest_daily_history("فملی", HISTORY_PAYLOAD, is_adjusted=False)
    assert created == 2 and skipped == 0
    row = DailyStockHistory.objects.get(symbol="فملی", date="1403-10-19")
    # Storage unit is Rial; provider values are stored undivided.
    assert row.pl == 8500 and row.buy_count_i == 2416 and row.is_adjusted is False
    # Slash-separated input date stored dash-normalized.
    assert DailyStockHistory.objects.filter(date="1403-10-20").exists()
    assert not DailyStockHistory.objects.filter(date__contains="/").exists()


@pytest.mark.django_db
def test_ingest_daily_history_rerun_is_idempotent():
    ingest.ingest_daily_history("فملی", HISTORY_PAYLOAD, is_adjusted=False)
    created, skipped = ingest.ingest_daily_history("فملی", HISTORY_PAYLOAD, is_adjusted=False)
    assert created == 0 and skipped == 2
    assert DailyStockHistory.objects.count() == 2


@pytest.mark.django_db
def test_ingest_daily_history_skips_malformed_record_keeps_rest():
    payload = HISTORY_PAYLOAD + [{"time": "no-date-key"}]
    created, skipped = ingest.ingest_daily_history("فملی", payload, is_adjusted=False)
    assert created == 2 and skipped == 1


@pytest.mark.django_db
def test_ingest_candles_timeframe_mapping_and_idempotency():
    created, _ = ingest.ingest_candles("فملی", 3, CANDLE_PAYLOAD)
    assert created == 2
    assert MarketCandle.objects.filter(timeframe="1d_adj").count() == 2
    assert MarketCandle.objects.filter(date_time="1404-02-25").exists()
    created, skipped = ingest.ingest_candles("فملی", 3, CANDLE_PAYLOAD)
    assert created == 0 and skipped == 2


@pytest.mark.django_db
def test_ingest_codal_idempotent():
    created, _ = ingest.ingest_codal(CODAL_PAYLOAD)
    assert created == 1
    ann = CodalAnnouncement.objects.get()
    assert ann.symbol == "وبملت" and ann.date_publish == "1403-10-30"
    created, skipped = ingest.ingest_codal(CODAL_PAYLOAD)
    assert created == 0 and skipped == 1


@pytest.mark.django_db
def test_ingest_gold_currency_history_idempotent():
    created, _ = ingest.ingest_gold_currency_history(GOLD_PAYLOAD)
    assert created == 2
    row = GoldCurrencyHistory.objects.get(symbol="IR_COIN_EMAMI", date="1404-03-21")
    # Raw-storage policy: the provider's declared-Toman value is stored verbatim.
    assert row.close_price == 73385000
    created, skipped = ingest.ingest_gold_currency_history(GOLD_PAYLOAD)
    assert created == 0 and skipped == 2


@pytest.mark.django_db
def test_ingest_symbol_metadata_updates_in_place():
    payload = {"id": 65883838195688438, "l18": "خودرو", "l30": "ایران‌ خودرو",
               "m": "بورس", "cs": "خودرو", "z": 100, "bvol": 5, "mv": 1000,
               "eps": -784, "pe": -4.93, "state": "مجاز"}
    created, _ = ingest.ingest_symbol_metadata(payload)
    assert created == 1
    payload["pe"] = -5.10
    created, skipped = ingest.ingest_symbol_metadata(payload)
    assert created == 0 and skipped == 1  # update, not a new row
    assert StockSymbolMetadata.objects.count() == 1
    assert float(StockSymbolMetadata.objects.get().pe) == -5.10


@pytest.mark.django_db
def test_ingest_handles_none_payload():
    assert ingest.ingest_daily_history("x", None, is_adjusted=False) == (0, 0)
    assert ingest.ingest_codal(None) == (0, 0)
    assert ingest.ingest_gold_currency_history(None) == (0, 0)
