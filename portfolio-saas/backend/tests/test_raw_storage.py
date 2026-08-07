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


def test_toman_to_tse_close_round_trips_the_read_boundary():
    """The write-side inverse must undo tse_close_to_toman exactly.

    aggregate_daily_stock_history's tickless fallback relies on this: a Toman
    `Price` goes back to Rial before landing in MarketCandle, so the read side's
    divide-by-ten returns the original number instead of a tenth of it.
    """
    from marketdata.currency import toman_to_tse_close, tse_close_to_toman

    for rial in ("3610", "70.5", "192880", "0.1"):
        assert toman_to_tse_close(tse_close_to_toman(rial)) == Decimal(rial)
    assert toman_to_tse_close(None) is None
    assert toman_to_tse_close("") is None


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


def test_no_brs_symbol_is_ever_written_into_the_rial_candle_table():
    """MarketCandle is Rial TSE data; BRS gold/FX quotes are Toman.

    A Toman row here makes candle_close_qs(symbol) match for a gold asset, which
    routes readers down the TSE path and exposes them to tse_close_to_toman()'s
    divide-by-ten. Guards the aggregate_daily_gold_currency_history regression.
    """
    from marketdata.models import GoldCurrencyHistory, MarketCandle
    from marketdata.tasks import aggregate_daily_gold_currency_history
    from portfolio.models import Asset, Price

    asset = Asset.objects.create(
        key="guard_coin", name="Guard Coin", asset_class=Asset.AssetClass.GOLD,
        brs_symbol="IR_COIN_GUARD",
    )
    Price.objects.create(asset=asset, price=Decimal("182500000"))

    aggregate_daily_gold_currency_history("1404-03-21")

    # The Toman series of record is written...
    assert GoldCurrencyHistory.objects.filter(symbol="IR_COIN_GUARD").exists()
    # ...but nothing lands in the Rial candle table.
    assert not MarketCandle.objects.filter(symbol="IR_COIN_GUARD").exists()


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
