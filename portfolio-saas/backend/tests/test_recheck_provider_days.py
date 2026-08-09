"""Re-fetch before judging: a third observation arbitrates what two cannot.

The audit can see that the candle table and the history table disagree, but its
arbiter is the adjusted candle, and when that matches neither side the row is
correctly left unrepaired. Asking the provider again supplies the missing vote.
"""
import pytest

from marketdata.management.commands.recheck_provider_days import Command

pytestmark = pytest.mark.django_db


def _judge(stored_candle, stored_history, fresh_candle, fresh_history):
    return Command()._judge(
        "کاما", "1402-01-07", stored_candle, stored_history, fresh_candle, fresh_history
    )


def test_fresh_data_backing_the_candle_condemns_the_history_row():
    row = _judge(stored_candle=21430, stored_history=2143,
                 fresh_candle=21430, fresh_history=21430)

    assert row["verdict"] == "stored_wrong"
    assert row["corrected_table"] == "marketdata_dailystockhistory"
    assert row["corrected_value"] == 21430


def test_fresh_data_backing_the_history_condemns_the_candle():
    row = _judge(stored_candle=895.5, stored_history=8955,
                 fresh_candle=8955, fresh_history=8955)

    assert row["verdict"] == "stored_wrong"
    assert row["corrected_table"] == "marketdata_marketcandle"


def test_provider_contradicting_itself_is_never_copied_in():
    """This is the case that must not become a repair.

    If the endpoint's own two views disagree today, neither is evidence, and
    writing either one would launder a provider defect into the warehouse.
    """
    row = _judge(stored_candle=21430, stored_history=2143,
                 fresh_candle=21430, fresh_history=2143)

    assert row["verdict"] == "provider_broken"
    assert row["corrected_value"] == ""


def test_a_provider_that_changed_its_own_history_matches_neither():
    row = _judge(stored_candle=21430, stored_history=2143,
                 fresh_candle=5914, fresh_history=5914)

    assert row["verdict"] == "provider_changed"
    assert row["corrected_value"] == 5914


def test_agreement_is_left_alone():
    row = _judge(stored_candle=1234, stored_history=1234,
                 fresh_candle=1234, fresh_history=1234)

    assert row["verdict"] == "no_data"
    assert row["corrected_table"] == ""


def test_a_day_the_provider_dropped_is_reported_not_repaired():
    row = _judge(stored_candle=21430, stored_history=2143,
                 fresh_candle=None, fresh_history=None)

    assert row["verdict"] == "no_data"
    assert row["corrected_value"] == ""


def test_rounding_does_not_count_as_disagreement():
    """The disputes are 10x, 6.7x and 5x; the bar only has to exclude noise."""
    row = _judge(stored_candle=1000, stored_history=1000.004,
                 fresh_candle=1000, fresh_history=1000)

    assert row["verdict"] == "no_data"
