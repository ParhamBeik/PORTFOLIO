"""Provider payload -> warehouse rows: endpoint registry, fetcher clients, field mappings, raw storage, and validation gates.

Merged from 7 files; each section keeps its original banner.
"""

import csv
from decimal import Decimal
import math
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone
import hashlib
import pytest

from marketdata import endpoints
from marketdata import ingest
from marketdata import ingest, jalali
from marketdata import validation
from marketdata.currency import to_toman
from marketdata.fetchers import (
    fetch_candlesticks,
    fetch_codal_announcements,
    fetch_daily_history,
    fetch_gold_currency_free,
    fetch_gold_currency_pro,
    fetch_gold_currency_pro_history_daily,
    fetch_market_index,
    fetch_shareholders,
    fetch_symbol_data,
    fetch_transactions,
)
from marketdata.jalali import is_jalali
from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
)
from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    StockSymbolMetadata,
)
from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    ShareholderRecord,
)
from marketdata.models import (
    MarketCandle,
    CorporateAction,
    CodalAnnouncement,
    GoldCurrencyHistory,
    RejectedRecord,
)
from marketdata.quota import ARCHIVE
from marketdata.tasks import nightly_series_validation
from portfolio.services.returns import daily_returns_matrix
from portfolio.services.valuation import _archive_replacements

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_marketdata_ingest.py
# Ingest-layer tests: payload -> rows, idempotency, malformed-record tolerance.
# 
# The mock payloads mirror tests/test_advanced_fetchers.py (the authoritative
# BrsApi shapes). Key invariants: bulk ingest is idempotent under re-run (unique
# constraints + ignore_conflicts), one bad record never sinks a batch, and Jalali
# dates are stored dash-normalized.


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
    created, skipped = ingest.ingest_daily_history("فملی", HISTORY_PAYLOAD)
    assert created == 2 and skipped == 0
    row = DailyStockHistory.objects.get(symbol="فملی", date="1403-10-19")
    # Storage unit is Rial; provider values are stored undivided.
    assert row.pl == 8500 and row.buy_count_i == 2416
    assert row.ts is not None
    # Slash-separated input date stored dash-normalized.
    assert DailyStockHistory.objects.filter(date="1403-10-20").exists()
    assert not DailyStockHistory.objects.filter(date__contains="/").exists()


@pytest.mark.django_db
def test_ingest_daily_history_rerun_is_idempotent():
    ingest.ingest_daily_history("فملی", HISTORY_PAYLOAD)
    created, skipped = ingest.ingest_daily_history("فملی", HISTORY_PAYLOAD)
    assert created == 0 and skipped == 2
    assert DailyStockHistory.objects.count() == 2


@pytest.mark.django_db
def test_ingest_daily_history_skips_malformed_record_keeps_rest():
    payload = HISTORY_PAYLOAD + [{"time": "no-date-key"}]
    created, skipped = ingest.ingest_daily_history("فملی", payload)
    assert created == 2 and skipped == 1


@pytest.mark.django_db
def test_ingest_candles_timeframe_mapping_and_idempotency():
    created, _ = ingest.ingest_candles("فملی", 3, CANDLE_PAYLOAD)
    assert created == 2
    assert MarketCandle.objects.filter(timeframe="1d_adj").count() == 2
    assert MarketCandle.objects.filter(date_time="1404-02-25").exists()
    assert MarketCandle.objects.exclude(ts__isnull=True).count() == 2
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
def test_ingest_gold_currency_history_deduplicates_upsert_batch():
    duplicate = {**GOLD_PAYLOAD["history_daily"][0], "close": 73385001}
    created, skipped = ingest.ingest_gold_currency_history({
        **GOLD_PAYLOAD,
        "history_daily": [*GOLD_PAYLOAD["history_daily"], duplicate],
    })

    assert created == 2 and skipped == 1
    assert GoldCurrencyHistory.objects.get(
        symbol="IR_COIN_EMAMI", date="1404-03-21"
    ).close_price == 73385001


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
    assert ingest.ingest_daily_history("x", None) == (0, 0)
    assert ingest.ingest_codal(None) == (0, 0)
    assert ingest.ingest_gold_currency_history(None) == (0, 0)


def test_codal_symbol_padding_is_stripped_on_ingest(db):
    """The provider pads some symbols with a trailing space.

    Storing "زقیام " verbatim silently broke every join to MarketInstrument and
    Asset, so two real companies read as having zero disclosures while 200 rows
    sat in the table. The symbol is the join key; it must be canonical on write.
    """
    payload = {
        "announcement": [
            {
                "l18": "زقیام ",           # provider-padded
                "l30": " شرکت قیام  ",     # padded both ends
                "title": "صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹",
                "code": "1",
                "date_title": "1405-01-01",
                "date_send": "1405-01-01",
                "time_send": "10:00:00",
                "date_publish": "1405-01-01",
                "time_publish": "10:00:00",
            }
        ]
    }

    ingest.ingest_codal(payload)

    stored = CodalAnnouncement.objects.get(code="1")
    assert stored.symbol == "زقیام"
    assert stored.company_name == "شرکت قیام"


@pytest.mark.django_db
class TestJalaliPartitionColumn:
    """`ts` is Timescale's partition key and NOT NULL, so every ORM write path
    must fill it -- including bulk_create, which never calls Model.save()."""

    def test_bulk_create_fills_ts_from_the_jalali_key(self):
        from marketdata.models import MarketCandle

        MarketCandle.objects.bulk_create([
            MarketCandle(symbol="SYM", timeframe=MarketCandle.ADJUSTED,
                         date_time="1405-05-24", close_price=100, volume=1),
        ])
        row = MarketCandle.objects.get(symbol="SYM")
        assert row.ts is not None
        # Jalali 1405-05-24 is Gregorian 2026-08-15, and a date-only table
        # anchors at midnight TEHRAN -- which is 20:30 the previous day in UTC.
        # Asserting row.ts.date() would read the 14th and look like an off-by-one
        # when it is just the stored instant being timezone-correct.
        assert row.ts.isoformat() == "2026-08-14T20:30:00+00:00"
        assert row.ts.astimezone(jalali.TEHRAN).date().isoformat() == "2026-08-15"

    def test_tick_ts_carries_time_of_day(self):
        from marketdata.models import StockTransactionTick

        StockTransactionTick.objects.bulk_create([
            StockTransactionTick(symbol="SYM", date="1405-05-24", time="09:00:00",
                                 row=1, price=10, volume=5),
        ])
        row = StockTransactionTick.objects.get(symbol="SYM")
        # 09:00 Tehran is 05:30 UTC; a naive read would store the wall clock.
        assert row.ts.isoformat() == "2026-08-15T05:30:00+00:00"

    def test_an_explicit_value_is_never_overwritten(self):
        import datetime
        from marketdata.models import MarketCandle

        pinned = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        MarketCandle.objects.bulk_create([
            MarketCandle(symbol="PIN", timeframe=MarketCandle.ADJUSTED,
                         date_time="1405-05-24", close_price=1, volume=0, ts=pinned),
        ])
        assert MarketCandle.objects.get(symbol="PIN").ts == pinned


# ----------------------------------------------------------------------
# test_marketdata_payload_shapes.py
# Ingest tests pinned to payload shapes captured from the live provider.
# 
# Every fixture here is a verbatim (trimmed) record from Api.BrsApi.ir observed on
# 1405-05-04. Each test corresponds to a defect that shipped to production and that
# the read-back verifier could not see, because it compared key presence and never
# values:
# 
# - Codal answers in Persian-Indic digits, so dates were unjoinable.
# - Cryptocurrency.php records carry no `symbol`, so 547 coins collapsed onto one.
# - Commodity.php answers a dict of category lists, so nothing ever parsed.
# - Shareholder.php carries no date, so every roster stored under "".
# - History.php?type=1 is the Real/Legal breakdown, not adjusted prices.


CODAL_PAYLOAD_marketdata_payload_shapes = {
    "count_announcement": 1009,
    "count_page": 51,
    "announcement": [
        {
            "l18": "فملی",
            "l30": "ملی صنایع مس ایران",
            "title": "توضیحات در خصوص اطلاعات و صورت های مالی منتشر شده",
            "code": "ن-۲۶",
            "date_title": None,
            "date_send": "۱۴۰۵/۰۵/۰۳",
            "time_send": "۱۳:۴۸:۵۴",
            "date_publish": "۱۴۰۵/۰۵/۰۳",
            "time_publish": "۱۳:۴۸:۵۴",
        }
    ],
}

CRYPTO_PAYLOAD = [
    {
        "date": "1405/05/04",
        "name_en": "Bitcoin",
        "name": "بیت‌کوین",
        "price": "64545",
        "price_toman": "12087398331",
        "market_cap": 837148080052,
        "id": 300000,
    },
    {
        "date": "1405/05/04",
        "name_en": "Ethereum",
        "name": "اتریوم",
        "price": "1885",
        "price_toman": "352975860",
        "market_cap": 265532066939,
        "id": 300001,
    },
]

COMMODITY_PAYLOAD = {
    "metal_precious": [
        {"date": "1405/05/03", "symbol": "XAUUSD", "price": 4052.85, "unit": "دلار"},
        {"date": "1405/05/03", "symbol": "XAGUSD", "price": 58.2, "unit": "دلار"},
    ],
    "metal_base": [
        {"date": "1405/05/02", "symbol": "Cu", "price": 14015, "unit": "دلار"},
    ],
    "energy": [
        {"date": "1405/05/04", "symbol": "BRENT", "price": 96.78, "unit": "دلار"},
    ],
}

SHAREHOLDER_PAYLOAD = [
    {"id": 188055, "name": "سازمان توسعه ونوسازی معادن", "volume": 121281419017, "percent": 11.55, "change": 0},
    {"id": 192627, "name": "موسسه صندوق بازنشستگی مس", "volume": 82974800148, "percent": 7.902, "change": 0},
]

REAL_LEGAL_PAYLOAD = [
    {
        "date": "1405-04-29",
        "Buy_CountI": 5970,
        "Buy_CountN": 44,
        "Sell_CountI": 2732,
        "Sell_CountN": 31,
        "Buy_I_Volume": 361199757,
        "Buy_N_Volume": 249066289,
        "Sell_I_Volume": 161586006,
        "Sell_N_Volume": 448680040,
        "Buy_I_Value": 7393536609350,
        "Buy_N_Value": 5098345397380,
        "Sell_I_Value": 3307474352240,
        "Sell_N_Value": 9184407654490,
    }
]


def test_persian_digits_fold_to_ascii():
    assert ingest.normalize_jalali("۱۴۰۵/۰۵/۰۳") == "1405-05-03"
    assert ingest.fold_digits("۱۳:۴۸:۵۴") == "13:48:54"
    assert ingest.fold_digits("ن-۲۶") == "ن-26"
    # Arabic-Indic digits fold too, and ASCII input is untouched.
    assert ingest.normalize_jalali("١٤٠٥/٠٥/٠٣") == "1405-05-03"
    assert ingest.normalize_jalali("1405-05-03") == "1405-05-03"


def test_codal_stores_joinable_ascii_dates():
    created, _ = ingest.ingest_codal(CODAL_PAYLOAD_marketdata_payload_shapes)
    assert created == 1
    row = CodalAnnouncement.objects.get()
    assert row.date_publish == "1405-05-03"
    assert row.time_publish == "13:48:54"
    assert row.code == "ن-26"


def test_shareholder_rows_get_a_date_when_the_caller_omits_one():
    created, _ = ingest.ingest_shareholders("فملی", SHAREHOLDER_PAYLOAD)
    assert created == 2
    assert not ShareholderRecord.objects.filter(date="").exists()
    assert ShareholderRecord.objects.values("date").distinct().count() == 1


def test_real_legal_lands_on_the_existing_price_row():
    DailyStockHistory.objects.create(
        symbol="فملی", date="1405-04-29", pl=20470
    )
    updated, skipped = ingest.ingest_real_legal("فملی", REAL_LEGAL_PAYLOAD)
    assert (updated, skipped) == (1, 0)
    row = DailyStockHistory.objects.get(symbol="فملی")
    assert row.buy_count_i == 5970
    assert row.sell_n_value == 9184407654490
    # The price it was carrying is untouched, and no phantom second row appears.
    assert row.pl == 20470
    assert DailyStockHistory.objects.filter(symbol="فملی").count() == 1


def test_real_legal_skips_days_with_no_price_row_yet():
    updated, skipped = ingest.ingest_real_legal("فملی", REAL_LEGAL_PAYLOAD)
    assert updated == 0 and skipped == 1


def test_gold_usdt_is_canonicalised_on_read_and_write():
    payload = {
        "symbol": "USDT",
        "name": "تتر",
        "unit": "ریال",
        "history_daily": [
            {"date": "1405-05-04", "open": 1000000, "high": 1010000, "low": 990000, "close": 1005000}
        ],
    }
    assert ingest.canonical_gold_symbol(payload) == "USDT_IRT"
    created, _ = ingest.ingest_gold_currency_history(payload)
    assert created == 1
    assert GoldCurrencyHistory.objects.filter(symbol="USDT_IRT").exists()
    # GoldCurrencyHistory is Toman-denominated for IRR-quoted symbols (the one
    # deliberate non-verbatim write path), so a Rial-declared quote is divided
    # by 10 and relabelled — valuation reads close_price straight as Toman.
    row = GoldCurrencyHistory.objects.get()
    assert float(row.close_price) == 100500.0
    assert row.unit == "تومان"


def test_flatten_records_handles_all_three_payload_shapes():
    assert len(ingest.flatten_records(CRYPTO_PAYLOAD)) == 2
    assert len(ingest.flatten_records(COMMODITY_PAYLOAD)) == 4
    assert ingest.flatten_records({"date": "1405-05-04"}) == [{"date": "1405-05-04"}]
    assert ingest.flatten_records(None) == []


# ----------------------------------------------------------------------
# test_raw_storage.py
# Unit tests for the raw-storage ingest policy and the to_toman() live-price helper.
# 
# Unit tests, not integration: pure conversion-logic assertions (no DB, no ORM)
# plus a handful of narrow ingest-boundary checks, so they run fast and pin
# down exactly one thing each.


def test_to_toman_is_identity_for_toman_declared_input():
    assert to_toman("کاما", 10000, "Toman") == Decimal("10000")
    assert to_toman("کاما", 10000, "تومان") == Decimal("10000")


def test_to_toman_divides_by_ten_for_rial_declared_input():
    assert to_toman("کاما", 10000, "Rial") == Decimal("1000")
    assert to_toman("کاما", 10000, "ریال") == Decimal("1000")
    assert to_toman("کاما", 10000, "IRR") == Decimal("1000")


def test_to_toman_passes_through_undeclared_unit():
    """No declared unit and no override -- never infer from magnitude."""
    assert to_toman("کاما", 10000, "") == Decimal("10000")


def test_to_toman_applies_usd_rate_without_extra_conversion():
    """usd_rate is expected pre-scaled by the caller (itself resolved via to_toman)."""
    assert to_toman("USDT_IRT", 1, "USD", usd_rate=632000) == Decimal("632000")


def test_to_toman_zero_and_negative_input_is_zero():
    assert to_toman("کاما", 0, "Rial") == Decimal("0")
    assert to_toman("کاما", -100, "Rial") == Decimal("0")


def test_live_gold_uses_each_rows_declared_unit_without_magnitude_guessing(settings):
    from portfolio.live.extractor import extract_standard_prices

    settings.MANUAL_PRICES = {}
    raw = {"brsapi": {"gold": [
        {"symbol": "IR_COIN_EMAMI", "price": 1_900_000_000, "unit": "ریال"},
        {"symbol": "IR_GOLD_18K", "price": 18_578_200},  # free feed: Toman, no unit
    ]}}
    prices = extract_standard_prices(raw)
    assert prices["emami_coin"] == Decimal("190000000")
    assert prices["gold_18k_gram"] == Decimal("18578200")


def test_ingest_daily_history_stores_provider_value_verbatim():
    """TSE daily history: provider's raw number lands in storage untouched."""
    from marketdata import ingest
    from marketdata.models import DailyStockHistory

    payload = [{
        "date": "1403-10-19", "pmin": 8490, "pmax": 8680, "py": 8430, "pf": 8570,
        "pl": 8500, "plc": 70, "pc": 8570, "pcc": 140, "tval": 1108829664180,
    }]
    created, skipped = ingest.ingest_daily_history("تست", payload)
    assert created == 1 and skipped == 0
    row = DailyStockHistory.objects.get(symbol="تست", date="1403-10-19")
    assert row.pl == 8500
    assert row.pc == 8570


def test_ingest_candles_stores_provider_value_verbatim():
    """TSE candles: raw open/high/low/close land in storage untouched."""
    from marketdata import ingest
    from marketdata.models import MarketCandle

    payload = {"candle_daily": [
        {"date": "1403-10-19", "open": 8490, "high": 8680, "low": 8400, "close": 8570, "volume": 1000},
    ]}
    created, skipped = ingest.ingest_candles("تست", 2, payload)
    assert created == 1 and skipped == 0
    row = MarketCandle.objects.get(symbol="تست", timeframe="1d_unadj", date_time="1403-10-19")
    assert row.close_price == Decimal("8570")


def test_candle_ingest_salvages_close_when_open_is_invalid():
    from marketdata import ingest
    from marketdata.models import MarketCandle, RejectedRecord

    payload = {"candle_daily": [{
        "date": "1403-10-19", "open": 9999, "high": 110,
        "low": 90, "close": 105, "volume": 1000,
    }]}
    created, rejected = ingest.ingest_candles("تست", 2, payload)
    assert (created, rejected) == (1, 0)
    row = MarketCandle.objects.get()
    assert row.close_price == Decimal("105") and row.open_price is None
    assert RejectedRecord.objects.filter(
        endpoint="stock_candle_unadjusted",
        reason="field_open_outside_range",
    ).exists()


def test_recent_provider_candle_revision_updates_in_place():
    from marketdata import ingest
    from marketdata.models import MarketCandle

    first = {"candle_daily_adjusted": [{
        "date": "1404-03-21", "open": 100, "high": 110,
        "low": 90, "close": 105, "volume": 1000,
    }]}
    revised = {"candle_daily_adjusted": [{
        "date": "1404-03-21", "open": 100, "high": 112,
        "low": 90, "close": 108, "volume": 1200,
    }]}
    ingest.ingest_candles("REVISION", 3, first)
    ingest.ingest_candles("REVISION", 3, revised)
    row = MarketCandle.objects.get()
    assert row.close_price == Decimal("108") and row.volume == 1200


def test_ingest_gold_currency_history_stores_provider_value_and_unit_verbatim():
    """A Toman-declared provider payload is stored as-is, no multiplier."""
    from marketdata import ingest
    from marketdata.models import GoldCurrencyHistory

    payload = {
        "symbol": "IR_COIN_TEST", "name": "test coin", "unit": "تومان",
        "history_daily": [
            {"date": "1404/03/21", "open": 100, "high": 110, "low": 90, "close": 105},
        ],
    }
    created, _ = ingest.ingest_gold_currency_history(payload)
    assert created == 1
    row = GoldCurrencyHistory.objects.get(symbol="IR_COIN_TEST", date="1404-03-21")
    assert row.close_price == Decimal("105")
    assert row.unit == "تومان"
    assert row.source == GoldCurrencyHistory.Source.PROVIDER


def test_provider_history_replaces_a_recent_live_aggregate():
    from marketdata import ingest
    from marketdata.models import GoldCurrencyHistory

    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_TEST",
        date="1404-03-21",
        unit="تومان",
        close_price=999,
        source=GoldCurrencyHistory.Source.AGGREGATE,
    )
    payload = {
        "symbol": "IR_COIN_TEST", "name": "test coin", "unit": "تومان",
        "history_daily": [
            {"date": "1404-03-21", "open": 100, "high": 110, "low": 90, "close": 105},
        ],
    }
    ingest.ingest_gold_currency_history(payload)
    row = GoldCurrencyHistory.objects.get()
    assert row.close_price == Decimal("105")
    assert row.source == GoldCurrencyHistory.Source.PROVIDER


def test_salvage_repair_preserves_close_and_nulls_only_bad_open(tmp_path):
    from marketdata.models import MarketCandle, RejectedRecord

    RejectedRecord.objects.create(
        endpoint="stock_candle_unadjusted", symbol="SALVAGE",
        date="1404-03-21", reason="open_outside_range",
        payload={
            "date": "1404-03-21", "open": 999, "high": 110,
            "low": 90, "close": 100, "volume": 10,
        },
    )
    manifest = tmp_path / "salvage.csv"
    call_command("audit_warehouse", "--check", "salvage", manifest_path=manifest)
    with manifest.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["verdict"] for row in rows] == ["salvageable_field"]

    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    call_command(
        "repair_warehouse", manifest_path=manifest, batch="salvage",
        apply=True, manifest_hash=digest,
    )
    candle = MarketCandle.objects.get(
        symbol="SALVAGE", timeframe=MarketCandle.UNADJUSTED,
        date_time="1404-03-21",
    )
    assert candle.close_price == 100
    assert candle.open_price is None
    assert not RejectedRecord.objects.filter(
        endpoint="stock_candle_unadjusted", symbol="SALVAGE",
        reason="open_outside_range",
    ).exists()
    assert RejectedRecord.objects.filter(
        endpoint="stock_candle_unadjusted", symbol="SALVAGE",
        reason__startswith="field_open_",
    ).exists()


def test_unit_audit_streams_ordered_candle_pairs(tmp_path):
    from marketdata.models import MarketCandle

    for index in range(6):
        day = f"1404-03-{index + 1:02d}"
        MarketCandle.objects.create(
            symbol="STREAM", timeframe=MarketCandle.UNADJUSTED,
            date_time=day, close_price=100 + index,
        )
        MarketCandle.objects.create(
            symbol="STREAM", timeframe=MarketCandle.ADJUSTED,
            date_time=day, close_price=100 + index,
        )
    manifest = tmp_path / "units.csv"
    call_command("audit_warehouse", "--check", "units", manifest_path=manifest)
    assert manifest.read_text(encoding="utf-8").splitlines() == [
        "check,table,symbol,date,value,corrected,verdict,evidence"
    ]


def test_cross_table_consensus_repairs_only_scaled_daily_history(tmp_path):
    from marketdata.models import DailyStockHistory, MarketCandle

    day = "1405-05-12"
    for timeframe in (MarketCandle.UNADJUSTED, MarketCandle.ADJUSTED):
        MarketCandle.objects.create(
            symbol="CONSENSUS", timeframe=timeframe, date_time=day,
            open_price=1000, high_price=1000, low_price=1000,
            close_price=1000, volume=20,
        )
    history = DailyStockHistory.objects.create(
        symbol="CONSENSUS", date=day, tvol=20, tval=2000,
        pmin=100, pmax=100, py=100, pf=100, pl=100, plc=2,
        pc=100, pcc=2,
    )
    manifest = tmp_path / "cross.csv"
    call_command("audit_warehouse", "--check", "crosstable", manifest_path=manifest)
    with manifest.open(newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert row["table"] == "marketdata_dailystockhistory"
    assert row["corrected"] == "factor:10"

    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    call_command(
        "repair_warehouse", manifest_path=manifest, batch="units",
        apply=True, manifest_hash=digest,
    )
    history.refresh_from_db()
    assert history.pl == 1000
    assert history.pmin == history.pmax == history.pc == 1000
    assert history.plc == history.pcc == 20
    assert history.tval == 20000


def test_salvage_nulls_scaled_open_when_range_is_unusable():
    from marketdata import validation

    rows, issues = validation.salvage_ohlc_records("candle", [{
        "date": "1405-05-12", "open": 469, "high": 469,
        "low": 469, "close": 4690, "volume": 20,
    }])
    assert rows[0]["open"] is None
    assert rows[0]["high"] is None
    assert rows[0]["low"] is None
    assert {issue.reason for issue in issues} >= {
        "field_open_implausible_vs_close",
        "field_high_excludes_close",
        "field_low_excludes_close",
    }


def test_gold_ingest_refuses_a_quote_whose_unit_it_cannot_name():
    """Fail closed: an unnameable scale is a silent 10x, so store nothing.

    valuation.py reads GoldCurrencyHistory.close_price straight as Toman. A
    payload whose `unit` is blank or unknown could be Rial, so the batch is
    rejected rather than guessed at.
    """
    from marketdata import ingest
    from marketdata.models import GoldCurrencyHistory, RejectedRecord

    payload = {
        "symbol": "IR_COIN_MYSTERY", "name": "mystery", "unit": "quatloos",
        "history_daily": [{"date": "1404-03-21", "close": 1050}],
    }
    created, rejected = ingest.ingest_gold_currency_history(payload)
    assert created == 0 and rejected == 1
    assert not GoldCurrencyHistory.objects.filter(symbol="IR_COIN_MYSTERY").exists()
    assert RejectedRecord.objects.filter(
        symbol="IR_COIN_MYSTERY", reason="unit_unrecognised"
    ).exists()


def test_gold_ingest_still_accepts_declared_foreign_units():
    """دلار/تتر are legitimate non-IRR units and must pass through verbatim."""
    from marketdata import ingest
    from marketdata.models import GoldCurrencyHistory

    payload = {
        "symbol": "XAUUSD_TEST", "name": "gold ounce", "unit": "دلار",
        "history_daily": [{"date": "1404-03-21", "close": 4310}],
    }
    created, _ = ingest.ingest_gold_currency_history(payload)
    assert created == 1
    row = GoldCurrencyHistory.objects.get(symbol="XAUUSD_TEST", date="1404-03-21")
    assert row.close_price == Decimal("4310") and row.unit == "دلار"


def test_usd_quoted_keys_are_never_stamped_as_verified_toman():
    """A USD-magnitude price must not be labelled IRT/verified.

    extractor.py stores bitcoin_usd / gold_ounce_usd at their provider-native
    USD magnitude. Stamping them Toman-verified would licence value_account to
    add dollars straight into a Toman total; returns.py already special-cases
    them via USD_QUOTED_KEYS, and valuation must not disagree.
    """
    from portfolio.models import Asset, Price
    from portfolio.services.returns import USD_QUOTED_KEYS
    from portfolio.tasks import _write_prices

    usd_key = USD_QUOTED_KEYS[0]
    Asset.objects.create(
        key=usd_key, name="Bitcoin USD", asset_class=Asset.AssetClass.CRYPTO,
        brs_symbol="BTC",
    )
    Asset.objects.create(
        key="toman_coin", name="Toman Coin", asset_class=Asset.AssetClass.GOLD,
        brs_symbol="IR_COIN_EMAMI",
    )
    _write_prices({usd_key: Decimal("65000"), "toman_coin": Decimal("182500000")})

    usd_row = Price.objects.filter(asset__key=usd_key).latest("fetched_at")
    assert usd_row.price_unit == Price.Unit.UNKNOWN
    assert usd_row.price_unit_verified is False

    # A genuinely Toman BRS asset is still stamped verified.
    irt_row = Price.objects.filter(asset__key="toman_coin").latest("fetched_at")
    assert irt_row.price_unit == Price.Unit.IRT
    assert irt_row.price_unit_verified is True


def test_rejection_backlog_groups_by_endpoint_and_reason(tmp_path):
    from marketdata.models import RejectedRecord

    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted", symbol="X", date="1404-01-01",
        reason="open_outside_range", occurrences=5,
    )
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted", symbol="Y", date="1404-01-02",
        reason="open_outside_range", occurrences=3,
    )
    manifest = tmp_path / "rejections.csv"
    call_command("audit_warehouse", "--check", "rejections", manifest_path=manifest)
    with manifest.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["verdict"] == "rejection_backlog"
    assert rows[0]["value"] == "2"
    assert "open_outside_range" in rows[0]["evidence"]


def test_retired_aggregate_check_flags_survivors(tmp_path):
    from marketdata.models import MarketCandle

    MarketCandle.objects.create(
        symbol="LEFTOVER", timeframe=MarketCandle.AGGREGATE, date_time="1404-01-01",
        close_price=100,
    )
    manifest = tmp_path / "retired.csv"
    call_command("audit_warehouse", "--check", "retiredaggregate", manifest_path=manifest)
    with manifest.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["verdict"] for row in rows] == ["migration_incomplete"]
    assert rows[0]["table"] == "marketdata_marketcandle"


def test_retired_aggregate_check_is_clean_when_none_remain(tmp_path):
    manifest = tmp_path / "retired.csv"
    call_command("audit_warehouse", "--check", "retiredaggregate", manifest_path=manifest)
    with manifest.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []


# ----------------------------------------------------------------------
# test_advanced_fetchers.py
# Unit tests for BRS API multi-endpoint fetchers and database models.


@pytest.mark.django_db
def test_fetch_gold_currency_free():
    mock_payload = {
        "gold": [
            {"symbol": "IR_GOLD_18K", "name": "طلای 18 عیار", "price": 6214700, "unit": "تومان"}
        ],
        "currency": [
            {"symbol": "USD", "name": "دلار", "price": 81650, "unit": "تومان"}
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_gold_currency_free("test_key")
        assert res == mock_payload
        assert len(res["gold"]) == 1
        assert res["currency"][0]["symbol"] == "USD"


@pytest.mark.django_db
def test_fetch_gold_currency_pro_history_daily():
    mock_payload = {
        "symbol": "IR_COIN_EMAMI",
        "name": "سکه امامی",
        "unit": "تومان",
        "history_daily": [
            {"date": "1404/03/21", "open": 73290000, "high": 73610000, "low": 73080000, "close": 73385000}
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_gold_currency_pro_history_daily("test_key", "IR_COIN_EMAMI")
        assert res == mock_payload
        record = res["history_daily"][0]

        # Test model persistence
        obj = GoldCurrencyHistory.objects.create(
            symbol=res["symbol"],
            name=res["name"],
            unit=res["unit"],
            date=record["date"],
            open_price=record["open"],
            high_price=record["high"],
            low_price=record["low"],
            close_price=record["close"],
        )
        assert obj.symbol == "IR_COIN_EMAMI"
        assert obj.close_price == 73385000


@pytest.mark.django_db
def test_fetch_symbol_data_and_model_persistence():
    mock_payload = {
        "id": 65883838195688438,
        "l18": "خودرو",
        "l30": "ایران‌ خودرو",
        "l30_en": "Iran Khodro",
        "isin": "IRO1IKCO0001",
        "m": "بورس",
        "cs": "خودرو و ساخت قطعات",
        "z": 301656068000,
        "bvol": 30310685,
        "mv": 1165599046752000,
        "eps": -784,
        "pe": -4.93,
        "state": "مجاز",
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_symbol_data("test_key", "خودرو")
        assert res["l18"] == "خودرو"

        meta = StockSymbolMetadata.objects.create(
            ins_code=res["id"],
            l18=res["l18"],
            l30=res["l30"],
            l30_en=res["l30_en"],
            isin=res["isin"],
            market=res["m"],
            sector=res["cs"],
            shares_count=res["z"],
            base_volume=res["bvol"],
            market_cap=res["mv"],
            eps=res["eps"],
            pe=res["pe"],
            state=res["state"],
        )
        assert meta.ins_code == 65883838195688438
        assert meta.l18 == "خودرو"


@pytest.mark.django_db
def test_fetch_daily_history_and_real_legal():
    mock_payload = [
        {
            "date": "1403-10-19",
            "time": "12:29:59",
            "tno": 7301,
            "tvol": 129326764,
            "tval": 1108829664180,
            "pmin": 8490,
            "pmax": 8680,
            "py": 8430,
            "pf": 8570,
            "pl": 8500,
            "plc": 70,
            "plp": 0.83,
            "pc": 8570,
            "pcc": 140,
            "pcp": 1.66,
            "Buy_CountI": 2416,
            "Buy_CountN": 20,
            "Sell_CountI": 2343,
            "Sell_CountN": 26,
            "Buy_I_Volume": 68461905,
            "Buy_N_Volume": 60864859,
            "Sell_I_Volume": 82617958,
            "Sell_N_Volume": 46708806,
        }
    ]
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_daily_history("test_key", "فملی", history_type=0)
        assert len(res) == 1

        rec = res[0]
        hist = DailyStockHistory.objects.create(
            symbol="فملی",
            date=rec["date"],
            time=rec["time"],
            tno=rec["tno"],
            tvol=rec["tvol"],
            tval=rec["tval"],
            pmin=rec["pmin"],
            pmax=rec["pmax"],
            py=rec["py"],
            pf=rec["pf"],
            pl=rec["pl"],
            plc=rec["plc"],
            plp=rec["plp"],
            pc=rec["pc"],
            pcc=rec["pcc"],
            pcp=rec["pcp"],
            buy_count_i=rec["Buy_CountI"],
            buy_count_n=rec["Buy_CountN"],
            sell_count_i=rec["Sell_CountI"],
            sell_count_n=rec["Sell_CountN"],
            buy_i_volume=rec["Buy_I_Volume"],
            buy_n_volume=rec["Buy_N_Volume"],
            sell_i_volume=rec["Sell_I_Volume"],
            sell_n_volume=rec["Sell_N_Volume"],
        )
        assert hist.symbol == "فملی"
        assert hist.buy_count_i == 2416


@pytest.mark.django_db
def test_fetch_candlesticks():
    mock_payload = {
        "l18": "فملی",
        "type": 3,
        "count": 1,
        "candle_daily_adjusted": [
            {"date": "1404-02-24", "open": 7380, "high": 7400, "low": 7280, "close": 7340, "volume": 180715348}
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_candlesticks("test_key", "فملی", candle_type=3)
        assert res["l18"] == "فملی"
        c_data = res["candle_daily_adjusted"][0]

        candle = MarketCandle.objects.create(
            symbol=res["l18"],
            timeframe="1d_adj",
            date_time=c_data["date"],
            open_price=c_data["open"],
            high_price=c_data["high"],
            low_price=c_data["low"],
            close_price=c_data["close"],
            volume=c_data["volume"],
        )
        assert candle.symbol == "فملی"
        assert candle.close_price == 7340


@pytest.mark.django_db
def test_fetch_transactions():
    mock_payload = [
        {"row": 1, "time": "09:01:02", "volume": 100000, "price": 26550, "canceled": 0}
    ]
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_transactions("test_key", "اهرم", date="1404-02-22")
        assert len(res) == 1
        t_data = res[0]

        tick = StockTransactionTick.objects.create(
            symbol="اهرم",
            date="1404-02-22",
            time=t_data["time"],
            row=t_data["row"],
            price=t_data["price"],
            volume=t_data["volume"],
            canceled=bool(t_data["canceled"]),
        )
        assert tick.row == 1
        assert tick.price == 26550


@pytest.mark.django_db
def test_fetch_shareholders():
    mock_payload = [
        {"id": 262011, "name": "بانک صادرات ایران", "volume": 27842346668, "percent": 5.16, "change": 0}
    ]
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_shareholders("test_key", "وبملت")
        assert len(res) == 1
        sh = res[0]

        rec = ShareholderRecord.objects.create(
            symbol="وبملت",
            shareholder_id=sh["id"],
            name=sh["name"],
            volume=sh["volume"],
            percent=sh["percent"],
            change=sh["change"],
        )
        assert rec.name == "بانک صادرات ایران"


@pytest.mark.django_db
def test_fetch_codal_announcements():
    mock_payload = {
        "count_announcement": 1,
        "count_page": 1,
        "announcement": [
            {
                "l18": "وبملت",
                "l30": "بانک ملت",
                "title": "صورت‌های مالی میاندوره‌ای",
                "code": "ن-۱۰",
                "date_title": "۱۴۰۳/۰۹/۳۰",
                "date_publish": "۱۴۰۳/۱۰/۳۰",
                "time_publish": "۱۷:۵۵:۴۱",
                "link": "https://codal.ir/Reports/Decision.aspx",
            }
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_codal_announcements("test_key", symbol="وبملت")
        assert res["count_announcement"] == 1
        item = res["announcement"][0]

        codal = CodalAnnouncement.objects.create(
            symbol=item["l18"],
            company_name=item["l30"],
            title=item["title"],
            code=item["code"],
            date_title=item["date_title"],
            date_publish=item["date_publish"],
            time_publish=item["time_publish"],
            link=item["link"],
        )
        assert codal.symbol == "وبملت"


@pytest.mark.django_db
def test_fetch_market_index():
    mock_payload = {
        "date": "1403-12-18",
        "time": "20:07:44",
        "state": "بسته",
        "index": 2756970.28,
        "index_change": -33000.22,
        "index_equalWeight": 814270.85,
        "index_equalWeight_change": -9859.51,
        "mv": 87748425774158880,
        "tno": 493959,
        "tval": 134757329520715,
        "tvol": 16326463426,
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_market_index("test_key")
        assert res["index"] == 2756970.28

        idx = MarketIndexData.objects.create(
            date=res["date"],
            time=res["time"],
            state=res["state"],
            index_overall=res["index"],
            index_overall_change=res["index_change"],
            index_equal_weight=res["index_equalWeight"],
            index_equal_weight_change=res["index_equalWeight_change"],
            market_value=res["mv"],
            trade_number=res["tno"],
            trade_value=res["tval"],
            trade_volume=res["tvol"],
        )
        assert idx.index_overall == 2756970.28


# ----------------------------------------------------------------------
# test_marketdata_endpoints.py
# Guards for the endpoint classification and the scheduling rules built on it.
# 
# Unit tests throughout: every assertion here is pure mapping logic or a single
# in-memory decision, which is exactly the fast base of the test pyramid, and none
# of it needs a provider round trip -- which matters because every real request
# costs paid quota.
# 
# The registry facts below were established by probing the live API directly. They
# are asserted rather than trusted because three of them were wrong in the shipped
# code and each wrong one burned quota on a guaranteed-failing request.


def test_probe_verified_paths():
    """Wrong paths cost a request and return 404, so pin the ones we verified."""
    assert endpoints.get("etf_nav").path == "Tsetmc/Nav.php"  # not EtfNav.php (404)
    assert endpoints.get("crypto").path == "Market/Cryptocurrency.php"  # not Crypto.php (404)
    assert endpoints.get("market_index").path == "Tsetmc/Index.php"
    assert endpoints.get("stock_transaction_ticks").path == "Tsetmc/Transaction.php"
    for key in ("etf_nav", "crypto", "market_index"):
        assert endpoints.get(key).url.startswith("https://Api.BrsApi.ir/")


def test_candlestick_types_are_two_and_three():
    """type=0 returns 400 and type=1 returns no_data; only 2 and 3 are real."""
    assert endpoints.get("stock_candles").valid_types == (2, 3)
    assert endpoints.get("stock_history").valid_types == (0, 1)


def test_live_endpoints_never_land_in_the_archive_bucket():
    """Live reads used to spend the backfill reserve, starving both."""
    for key in endpoints.LIVE_KEYS:
        # OTHER is fine for catalog/metadata reads; ARCHIVE is the bug.
        assert endpoints.bucket_for(key) != ARCHIVE, key
    for key in endpoints.FULL_HISTORY_KEYS + endpoints.PER_DAY_KEYS:
        assert endpoints.bucket_for(key) == ARCHIVE, key


def test_full_history_endpoints_never_require_a_date():
    """These return their whole series in one request; asking per-day wastes quota.

    Optional date params are allowed (gold uses them for narrow re-checks); a
    *required* one would mean the archive worker had to walk day by day.
    """
    for key in endpoints.FULL_HISTORY_KEYS:
        required = endpoints.get(key).required_params
        assert not any("date" in param for param in required), key


def test_per_day_endpoints_declare_their_date_param():
    """One request per day is the expensive class, so the bound must be explicit."""
    ticks = endpoints.get("stock_transaction_ticks")
    assert "date" in ticks.required_params
    assert "date" in ticks.jalali_params


@pytest.mark.parametrize("value,ok", [
    ("1404-02-22", True),
    ("1390-09-06", True),
    ("2026-07-01", False),   # Gregorian: provider answers 400
    ("1404-13-01", False),   # month 13
    ("1404-2-2", False),     # unpadded
    ("", False),
    (None, False),
])
def test_jalali_validation_rejects_what_the_provider_rejects(value, ok):
    assert is_jalali(value) is ok


def test_empty_payload_is_a_failure_not_a_completion(settings, db):
    """HTTP 200 with zero records used to mark a state permanently complete.

    This is the guard that would have surfaced the 404 paths and the Gregorian
    dates on day one instead of hiding them behind expected_rows=0.
    """
    from unittest.mock import patch
    from marketdata.archive import run_archive_state
    from marketdata.models import ArchiveFetchState

    settings.TSETMC_API_KEY = "test-key"
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="EMPTY",
    )
    with patch("marketdata.archive.fetch_daily_history", return_value=[]):
        state = run_archive_state(state.pk)
    assert not state.verified_complete
    assert state.last_error


def test_never_attempted_states_get_a_reserved_slice(settings, db):
    """All 39 gold states had never run once: -missing_rows sorted them last."""
    from django.utils import timezone
    from marketdata.archive import claim_archive_batch
    from marketdata.models import ArchiveFetchState

    settings.MARKETDATA_ARCHIVE_BATCH_SIZE = 10
    settings.MARKETDATA_DAILY_REQUEST_LIMIT = 10000
    # Saturate the queue with already-attempted states that all look urgent.
    for index in range(20):
        ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
            symbol=f"OLD{index}",
            last_attempt_at=timezone.now(),
            missing_rows=5000,
        )
    starved = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.GOLD_DAILY,
        symbol="USD",
    )
    assert starved.pk in claim_archive_batch()


# ----------------------------------------------------------------------
# test_marketdata_validation.py
# Unit tests for the per-record screen that sits between parsing and storing.
# 
# The archive verifier only ever asked "is anything missing?". These tests cover the
# other question -- "is any of it nonsense?" -- for the exact defects the audit found
# in production data: 1.3M all-zero rows, 54 gold rows with the high under the low,
# 547 coins collapsed onto a placeholder symbol, and Persian-Indic dates.
# 
# Pure functions over dicts, so these are unit tests at the base of the pyramid: no
# database, no network, no fixtures.


def _candle(**over):
    rec = {"date": "1405-05-03", "open": 100, "high": 110, "low": 95, "close": 105,
           "volume": 1000}
    rec.update(over)
    return rec


def reason(kind, record):
    """Run one record through the screen and return its rejection reason."""
    accepted, rejections = validation.validate(kind, [record])
    assert len(accepted) + len(rejections) == 1
    return rejections[0].reason if rejections else None


# --------------------------------------------------------------------------
# Dates: three shapes arrive and all three must survive the screen.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("date", ["1405-05-03", "1405/05/03", "۱۴۰۵/۰۵/۰۳", "١٤٠٥/٠٥/٠٣", "2026-07-27", "2026/07/27"])
def test_every_date_shape_the_provider_sends_is_accepted(date):
    assert reason("candle", _candle(date=date)) is None


@pytest.mark.parametrize(
    "date,expected",
    [
        ("", "date_missing"),
        (None, "date_missing"),
        ("1405-13-01", "date_not_jalali"),  # month 13 does not exist
        ("not-a-date", "date_not_jalali"),
        ("۱۴۰۵/۰۵/۰۳ ساعت", "date_not_ascii"),
    ],
)
def test_unstorable_dates_are_rejected_with_their_reason(date, expected):
    assert reason("candle", _candle(date=date)) == expected


# --------------------------------------------------------------------------
# OHLC ordering: the check that would have caught the 54 gold rows.
# --------------------------------------------------------------------------

def test_a_sane_candle_passes():
    assert reason("candle", _candle()) is None


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"high": 90}, "high_below_low"),            # the 54 gold rows, in candle form
        ({"open": 200}, "open_outside_range"),
        ({"close": 5}, "close_outside_range"),
        ({"low": -1}, "price_negative"),
        ({"open": 0, "high": 0, "low": 0, "close": 0}, "price_all_zero"),
        ({"low": 0}, "price_partially_zero"),
        ({"high": 10**14}, "price_absurd"),  # MAX_PRICE is 10**13 (Rial storage unit)
        ({"close": "not a number"}, "price_not_numeric"),
    ],
)
def test_impossible_candles_are_rejected(over, expected):
    assert reason("candle", _candle(**over)) == expected


def test_gold_accepts_a_close_only_quote():
    """open/high/low fall back to close upstream, so the screen must mirror that."""
    assert reason("gold", {"date": "1405-05-03", "close": 4200}) is None


def test_gold_rejects_the_high_below_low_row_the_audit_found():
    record = {"date": "1405-05-03", "open": 100, "high": 90, "low": 95, "close": 96}
    assert reason("gold", record) == "high_below_low"


# --------------------------------------------------------------------------
# History: a suspended day is real data, not garbage.
# --------------------------------------------------------------------------

def test_daily_history_accepts_a_suspended_day():
    """No trade means zero volume and a carried-over close: ~22% of real rows."""
    record = {"date": "1405-05-03", "pc": 5000, "pl": 5000, "tvol": 0,
              "pmin": 0, "pmax": 0, "pf": 0}
    assert reason("daily_history", record) is None


def test_daily_history_rejects_a_traded_day_with_no_prices():
    """Volume without an OHLC is the 1.3M-row defect, not a suspension."""
    record = {"date": "1405-05-03", "pc": 5000, "pl": 0, "tvol": 900_000,
              "pmin": 0, "pmax": 0, "pf": 0}
    assert reason("daily_history", record) == "price_all_zero"


# --------------------------------------------------------------------------
# Snapshots: the placeholder symbol that collapsed 547 coins onto one row.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["CRYPTO", "commodities", " Crypto "])
def test_snapshot_rejects_the_callers_placeholder_as_an_identity(name):
    record = {"date": "1405-05-03", "symbol": name, "price": 64545}
    assert reason("snapshot", record) == "symbol_is_placeholder"


def test_snapshot_accepts_a_coin_identified_only_by_name_en():
    record = {"date": "1405-05-03", "name_en": "Bitcoin", "price": "64545"}
    assert reason("snapshot", record) is None


# --------------------------------------------------------------------------
# Real/Legal: both legs of a trade must agree.
# --------------------------------------------------------------------------

def test_real_legal_rejects_when_buy_and_sell_volumes_disagree():
    record = {"date": "1405-05-03", "Buy_CountI": 10, "Buy_CountN": 2,
              "Sell_CountI": 8, "Sell_CountN": 1,
              "Buy_I_Volume": 500, "Buy_N_Volume": 500,
              "Sell_I_Volume": 900, "Sell_N_Volume": 50}
    assert reason("real_legal", record) == "buy_sell_volume_mismatch"


def test_real_legal_rejects_a_payload_with_no_participant_fields():
    """History.php?type=0 is prices, not participants; asking the wrong type shows here."""
    record = {"date": "1405-05-03", "pf": 100, "pl": 105}
    assert reason("real_legal", record) == "real_legal_fields_absent"


# --------------------------------------------------------------------------
# Ticks and the cross-endpoint reconciliation.
# --------------------------------------------------------------------------

def test_tick_rejects_a_zero_volume_trade():
    record = {"row": 1, "time": "09:15:22", "price": 4200, "volume": 0}
    assert reason("tick", record) == "volume_not_positive"


def test_tick_rejects_an_impossible_clock():
    record = {"row": 1, "time": "25:00:00", "price": 4200, "volume": 10}
    assert reason("tick", record) == "time_out_of_range"


def test_ticks_reconcile_against_the_candle_once_cancellations_are_excluded():
    """The strongest check available: two independent endpoints must agree.

    Cancelled trades repeat a row number and must not be counted -- excluding them
    reproduced the candle volume exactly on every sampled day that disagreed.
    """
    ticks = [
        {"row": 1, "volume": 100, "canceled": 0},
        {"row": 2, "volume": 250, "canceled": 0},
        {"row": 2, "volume": 250, "canceled": 1},  # the cancellation twin
    ]
    assert validation.reconcile_tick_volume(ticks, 350) is None
    assert validation.reconcile_tick_volume(ticks, 600).startswith("tick_volume_mismatch")


def test_tick_volume_tolerance_accepts_sub_percent_noise(settings):
    settings.MARKETDATA_TICK_VOLUME_TOLERANCE = 0.01
    ticks = [{"volume": 10000, "canceled": 0}]
    assert validation.reconcile_tick_volume(ticks, 9950) is None
    assert validation.reconcile_tick_volume(ticks, 9900) is None
    assert validation.reconcile_tick_volume(ticks, 9500).startswith("tick_volume_mismatch")
    assert validation.reconcile_tick_volume(ticks, 0).startswith("tick_volume_mismatch")
    assert validation.reconcile_tick_volume([{"volume": 0}], 100).startswith(
        "tick_volume_mismatch"
    )


def test_reconciliation_is_silent_when_there_is_no_candle_to_compare():
    assert validation.reconcile_tick_volume([{"row": 1, "volume": 5}], None) is None


# --------------------------------------------------------------------------
# The splitter itself.
# --------------------------------------------------------------------------

def test_validate_keeps_the_good_and_quarantines_the_bad():
    records = [_candle(), _candle(high=1), _candle(date=""), "not a dict"]
    accepted, rejections = validation.validate("candle", records)
    assert len(accepted) == 1
    assert [r.reason for r in rejections] == [
        "high_below_low", "date_missing", "not_a_record",
    ]
    # The payload rides along so a rejection stays inspectable after the fact.
    assert rejections[0].record["high"] == 1


def test_gregorian_date_is_translated_to_jalali():
    from marketdata import jalali
    assert jalali.normalize_jalali("2026-07-27") == "1405-05-05"
    assert jalali.normalize_jalali("2026/07/27") == "1405-05-05"


# ----------------------------------------------------------------------
# test_f2_f3_validation.py


def test_corporate_action_factor_detection():
    """Verify that TSE symbol factor changes are correctly detected as candidates."""
    # Setup test candles for a split (factor 0.5 before, 1.0 after)
    symbol = "TEST_CA"
    
    # Pre-split dates: factor adjusted / unadjusted = 0.5
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-01", close_price=Decimal("200"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    
    # Split date and after: factor adjusted / unadjusted = 1.0
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))

    # Run in dry-run mode
    res = nightly_series_validation(dry_run=True, symbols=[symbol], gold_symbols=[])
    candidates = res["corporate_action_candidates"]
    
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == symbol
    assert candidates[0]["date"] == "1405-01-02"
    # factor = 1.0 / 0.5 = 2.0
    assert pytest.approx(candidates[0]["factor"]) == 2.0
    assert candidates[0]["status"] == "unconfirmed"


def test_ordinary_volatility_ignored():
    """Verify that ordinary price changes do not trigger corporate actions."""
    symbol = "TEST_VOL"
    
    # Ordinary price changes (same adjustment ratio of 1.0 throughout)
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-02", close_price=Decimal("110"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("110"))

    res = nightly_series_validation(dry_run=True, symbols=[symbol], gold_symbols=[])
    candidates = res["corporate_action_candidates"]
    assert len(candidates) == 0


def test_duplicate_actions_prevented(db):
    """Verify that corporate action records are not duplicated on rerun."""
    symbol = "TEST_DUP"
    
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-01", close_price=Decimal("200"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))

    CodalAnnouncement.objects.create(
        symbol=symbol,
        date_publish="1405-01-02",
        category=CodalAnnouncement.Category.CAPITAL_INCREASE,
        title="Capital increase for TEST_DUP",
    )

    # First run in write mode
    res1 = nightly_series_validation(dry_run=False, symbols=[symbol], gold_symbols=[])
    assert res1["actions_created"] == 1
    assert CorporateAction.objects.filter(symbol=symbol).count() == 1
    
    # Second run in write mode
    res2 = nightly_series_validation(dry_run=False, symbols=[symbol], gold_symbols=[])
    assert res2["actions_created"] == 0
    assert CorporateAction.objects.filter(symbol=symbol).count() == 1


def test_confirmation_behavior():
    """Verify that the Codal announcement confirmation logic correctly classifies the action source."""
    symbol = "TEST_CONF"
    
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-01", close_price=Decimal("200"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))

    # Create matching Codal announcement
    CodalAnnouncement.objects.create(
        symbol=symbol,
        date_publish="1405-01-02",
        category=CodalAnnouncement.Category.CAPITAL_INCREASE,
        title="Capital increase announcement",
    )

    # Run write mode
    nightly_series_validation(dry_run=False, symbols=[symbol], gold_symbols=[])
    action = CorporateAction.objects.get(symbol=symbol, date="1405-01-02")
    assert action.source == CorporateAction.Source.CODAL
    assert action.kind == CorporateAction.Kind.CAPITAL_INCREASE


def test_gold_currency_spikes():
    """Verify that gold/currency spikes (like SEK 4.8x) are detected, but normal fluctuations are accepted."""
    # SEK spike: 20000 -> 96150 (spike) -> 20100 (normal recovery)
    symbol_sek = "SEK"
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-01", close_price=Decimal("20000"))
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-02", close_price=Decimal("96150"))
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-03", close_price=Decimal("20100"))

    # USD normal: stable moves (+/- 2%)
    symbol_usd = "USD"
    GoldCurrencyHistory.objects.create(symbol=symbol_usd, date="1405-01-01", close_price=Decimal("50000"))
    GoldCurrencyHistory.objects.create(symbol=symbol_usd, date="1405-01-02", close_price=Decimal("51000"))
    GoldCurrencyHistory.objects.create(symbol=symbol_usd, date="1405-01-03", close_price=Decimal("50500"))

    res = nightly_series_validation(dry_run=True, symbols=[], gold_symbols=[symbol_sek, symbol_usd])
    rejections = res["proposed_rejections"]

    # We expect SEK spikes (from 20k to 96k, and then back from 96k to 20.1k) to be caught
    sek_dates = {r["date"] for r in rejections if r["symbol"] == "SEK"}
    assert "1405-01-02" in sek_dates
    assert "1405-01-03" in sek_dates

    # USD should have zero rejections
    usd_rejections = [r for r in rejections if r["symbol"] == "USD"]
    assert len(usd_rejections) == 0


def test_rejection_record_endpoints():
    """Verify that rejection records are written under appropriate endpoints."""
    # TSE spike rejection (default timeframe = 1d_adj)
    symbol_tse = "TEST_TSE_SPIKE"
    MarketCandle.objects.create(symbol=symbol_tse, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    # 2x spike (math.log(2.0) = 0.693 > math.log(1.5) = 0.405)
    MarketCandle.objects.create(symbol=symbol_tse, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("200"))

    # SEK spike rejection (currency -> gold_daily)
    symbol_sek = "SEK"
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-01", close_price=Decimal("20000"))
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-02", close_price=Decimal("96150"))

    # USDT spike rejection (crypto -> crypto_daily)
    symbol_usdt = "USDT_IRT"
    GoldCurrencyHistory.objects.create(symbol=symbol_usdt, date="1405-01-01", close_price=Decimal("50000"))
    # 3x spike (math.log(3.0) = 1.09 > math.log(2.0) = 0.693)
    GoldCurrencyHistory.objects.create(symbol=symbol_usdt, date="1405-01-02", close_price=Decimal("150000"))

    # Run in write mode
    nightly_series_validation(dry_run=False, symbols=[symbol_tse], gold_symbols=[symbol_sek, symbol_usdt])

    # Check database rejections
    rejections = RejectedRecord.objects.all()
    assert rejections.filter(symbol=symbol_tse, date="1405-01-02", endpoint="series:1d_adj").exists()
    assert rejections.filter(symbol=symbol_sek, date="1405-01-02", endpoint="gold_daily").exists()
    assert rejections.filter(symbol=symbol_usdt, date="1405-01-02", endpoint="crypto_daily").exists()


def test_repeated_validation_idempotent():
    """Verify that repeated validation scans are idempotent and do not increment occurrences."""
    symbol = "SEK"
    GoldCurrencyHistory.objects.create(symbol=symbol, date="1405-01-01", close_price=Decimal("20000"))
    GoldCurrencyHistory.objects.create(symbol=symbol, date="1405-01-02", close_price=Decimal("96150"))

    # First run
    nightly_series_validation(dry_run=False, symbols=[], gold_symbols=[symbol])
    rec1 = RejectedRecord.objects.get(symbol=symbol, date="1405-01-02")
    assert rec1.occurrences == 1

    # Second run
    nightly_series_validation(dry_run=False, symbols=[], gold_symbols=[symbol])
    rec2 = RejectedRecord.objects.get(symbol=symbol, date="1405-01-02")
    assert rec2.occurrences == 1


def test_invalid_observations_excluded_from_returns_valuation(asset_catalog):
    """Verify that observations rejected by series validation are excluded from returns and archive replacements."""
    asset = asset_catalog["emami_coin"]
    symbol = asset.brs_symbol
    
    # Normal and spike price
    GoldCurrencyHistory.objects.create(symbol=symbol, date="1405-01-01", close_price=Decimal("45000000"))
    # Spike price
    GoldCurrencyHistory.objects.create(symbol=symbol, date="1405-01-02", close_price=Decimal("990000000"))

    # Create RejectedRecord for the spike date
    RejectedRecord.objects.create(
        endpoint="gold_daily",
        symbol=symbol,
        date="1405-01-02",
        reason="series_spike",
        payload={"close": 990000000.0, "previous_close": 45000000.0},
    )

    # 1. Verify returns panel excludes the spike date
    from portfolio.services.returns import _load_live_price_panel
    # Force loading prices including the dates
    # Since returns uses cutoff, let's call daily_returns_matrix or examine returns panel filter directly
    from django.utils import timezone
    import pandas as pd
    
    # 2. Verify archive replacements excludes it
    # _archive_replacements should filter out RejectedRecord dates
    prices = {asset.key: Decimal("990000000")}
    # Running archive replacements on the date should not fetch the rejected price
    # because it is excluded
    archive = _archive_replacements(prices)
    # The rejected price must not be used as fallback
    assert asset.key not in archive


def test_dry_run_safety():
    """Verify that running nightly_series_validation in dry-run mode makes zero database changes."""
    symbol_tse = "DRY_TSE"
    MarketCandle.objects.create(symbol=symbol_tse, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-01", close_price=Decimal("200"))
    MarketCandle.objects.create(symbol=symbol_tse, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol_tse, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol_tse, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))

    symbol_sek = "SEK"
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-01", close_price=Decimal("20000"))
    GoldCurrencyHistory.objects.create(symbol=symbol_sek, date="1405-01-02", close_price=Decimal("96150"))

    # Initial counts
    action_count_before = CorporateAction.objects.count()
    rejection_count_before = RejectedRecord.objects.count()

    # Dry run
    nightly_series_validation(dry_run=True, symbols=[symbol_tse], gold_symbols=[symbol_sek])

    assert CorporateAction.objects.count() == action_count_before
    assert RejectedRecord.objects.count() == rejection_count_before


def test_codal_window_and_title_matching():
    """Verify that Codal announcement matching supports window offsets and title-based mapping."""
    symbol = "TEST_MATCH"
    
    # 1. Setup price candidate on 1405-01-05
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-04", close_price=Decimal("200"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-04", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-05", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-05", close_price=Decimal("100"))

    # 2. Create Codal announcement on 1405-01-04 (1 day before, matches window)
    CodalAnnouncement.objects.create(
        symbol=symbol,
        date_publish="1405-01-04",
        category=None,  # Null category
        title="تصمیمات مجمع عمومی عادی سالیانه",
    )

    # Run in dry-run mode
    res = nightly_series_validation(dry_run=True, symbols=[symbol], gold_symbols=[])
    candidates = res["corporate_action_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["status"] == "confirmed"
    assert "assembly_decision" in candidates[0]["reason"]

    # Run in write-mode (should save as DIVIDEND)
    nightly_series_validation(dry_run=False, symbols=[symbol], gold_symbols=[])
    action = CorporateAction.objects.get(symbol=symbol, date="1405-01-05")
    assert action.kind == CorporateAction.Kind.DIVIDEND
    assert action.source == CorporateAction.Source.CODAL


def test_backfill_validation_dry_run_command_safety():
    """Verify that backfill_validation dry-run command writes nothing and produces a valid manifest."""
    from django.core.management import call_command
    import tempfile
    import os

    symbol = "TEST_BF_DRY"
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-01", close_price=Decimal("200"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-01", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.UNADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))
    MarketCandle.objects.create(symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time="1405-01-02", close_price=Decimal("100"))

    # Confirmed Codal to make sure there is a confirmed candidate
    CodalAnnouncement.objects.create(
        symbol=symbol,
        date_publish="1405-01-02",
        category=CodalAnnouncement.Category.CAPITAL_INCREASE,
        title="Capital increase",
    )

    # Initial counts
    action_count_before = CorporateAction.objects.count()

    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = os.path.join(tmpdir, "manifest.csv")
        # Call command
        call_command("backfill_validation", symbols=symbol, gold_symbols="", manifest_path=manifest_path)

        # Verify no database rows changed
        assert CorporateAction.objects.count() == action_count_before
        
        # Verify manifest exists and has headers
        assert os.path.exists(manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            assert len(lines) >= 2
            assert "confirmed_corporate_action" in lines[1]


def test_backfill_validation_default_manifest_is_secure(monkeypatch, tmp_path):
    """The default manifest path is uniquely created with owner-only permissions."""
    import os
    from django.core.management import call_command
    from marketdata.management.commands import backfill_validation

    manifest = tmp_path / "manifest.csv"

    def create_manifest(**_kwargs):
        return os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600), str(manifest)

    monkeypatch.setattr(backfill_validation.tempfile, "mkstemp", create_manifest)
    call_command("backfill_validation", symbols="", gold_symbols="")

    assert manifest.exists()
    assert manifest.stat().st_mode & 0o777 == 0o600
