"""Unit tests for the raw-storage ingest policy and the to_toman() live-price helper.

Unit tests, not integration: pure conversion-logic assertions (no DB, no ORM)
plus a handful of narrow ingest-boundary checks, so they run fast and pin
down exactly one thing each.
"""
from decimal import Decimal

import pytest

from marketdata.currency import to_toman

pytestmark = pytest.mark.django_db


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


def test_ingest_daily_history_stores_provider_value_verbatim():
    """TSE daily history: provider's raw number lands in storage untouched."""
    from marketdata import ingest
    from marketdata.models import DailyStockHistory

    payload = [{
        "date": "1403-10-19", "pmin": 8490, "pmax": 8680, "py": 8430, "pf": 8570,
        "pl": 8500, "plc": 70, "pc": 8570, "pcc": 140, "tval": 1108829664180,
    }]
    created, skipped = ingest.ingest_daily_history("تست", payload, is_adjusted=False)
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


def test_ingest_crypto_history_stores_price_toman_verbatim():
    """Provider's price_toman field lands in close_price_toman untouched."""
    from marketdata import ingest
    from marketdata.models import CryptoHistory

    payload = [{
        "name_en": "TestCoin", "date": "1404-03-21", "price": 1.0,
        "price_toman": 632000, "volume_24h": 100, "market_cap": 1000,
    }]
    created, _ = ingest.ingest_crypto_history("testcoin", payload)
    assert created == 1
    row = CryptoHistory.objects.get(symbol="TestCoin", date="1404-03-21")
    assert row.close_price_toman == Decimal("632000")
