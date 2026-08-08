"""Unit tests for the raw-storage ingest policy and the to_toman() live-price helper.

Unit tests, not integration: pure conversion-logic assertions (no DB, no ORM)
plus a handful of narrow ingest-boundary checks, so they run fast and pin
down exactly one thing each.
"""
from decimal import Decimal
import csv
import hashlib

import pytest
from django.core.management import call_command

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


def test_nightly_aggregate_never_overwrites_provider_history():
    from marketdata.models import GoldCurrencyHistory
    from marketdata.tasks import aggregate_daily_gold_currency_history
    from portfolio.models import Asset, Price

    asset = Asset.objects.create(
        key="provider_coin", name="Provider Coin", asset_class=Asset.AssetClass.GOLD,
        brs_symbol="IR_COIN_PROVIDER",
    )
    Price.objects.create(asset=asset, price=999)
    GoldCurrencyHistory.objects.create(
        symbol=asset.brs_symbol,
        date="1404-03-21",
        unit="تومان",
        close_price=105,
        source=GoldCurrencyHistory.Source.PROVIDER,
    )
    aggregate_daily_gold_currency_history("1404-03-21")
    assert GoldCurrencyHistory.objects.get().close_price == Decimal("105")


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


def test_crypto_usd_storage_preserves_sub_cent_precision():
    from marketdata import ingest
    from marketdata.models import CryptoHistory

    payload = [{
        "name_en": "TinyCoin", "date": "1404-03-21",
        "price": "0.000000123456", "price_toman": "0.0233",
    }]
    created, rejected = ingest.ingest_crypto_history("CRYPTO", payload)
    assert (created, rejected) == (1, 0)
    assert CryptoHistory.objects.get().close_price_usd == Decimal("0.000000123456")


def test_crypto_repair_is_dry_run_safe_and_manifest_hash_locked(tmp_path):
    from marketdata.models import CryptoHistory

    CryptoHistory.objects.create(
        symbol="USD_ANCHOR", date="1404-03-21",
        close_price_usd=1, close_price_toman=200000,
    )
    tiny = CryptoHistory.objects.create(
        symbol="TINY", date="1404-03-21",
        close_price_usd=0, close_price_toman=2,
    )
    manifest = tmp_path / "crypto.csv"
    call_command("audit_warehouse", "--check", "crypto", manifest_path=manifest)

    call_command("repair_warehouse", manifest_path=manifest, batch="crypto")
    tiny.refresh_from_db()
    assert tiny.close_price_usd == 0

    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    call_command(
        "repair_warehouse", manifest_path=manifest, batch="crypto",
        apply=True, manifest_hash=digest,
    )
    tiny.refresh_from_db()
    assert tiny.close_price_usd == Decimal("0.000010000000")


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
        symbol="CONSENSUS", date=day, is_adjusted=False, tvol=20, tval=2000,
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
