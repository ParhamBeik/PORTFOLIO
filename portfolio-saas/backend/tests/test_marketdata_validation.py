"""Unit tests for the per-record screen that sits between parsing and storing.

The archive verifier only ever asked "is anything missing?". These tests cover the
other question -- "is any of it nonsense?" -- for the exact defects the audit found
in production data: 1.3M all-zero rows, 54 gold rows with the high under the low,
547 coins collapsed onto a placeholder symbol, and Persian-Indic dates.

Pure functions over dicts, so these are unit tests at the base of the pyramid: no
database, no network, no fixtures.
"""
import pytest

from marketdata import validation


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
