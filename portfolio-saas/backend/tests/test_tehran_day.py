"""`tehran_day` is the one owner of "what day is it in the market?".

These tests FREEZE the clock inside the 20:30-24:00 UTC window where the server
date and the Tehran date disagree. A test run at 15:00 would never see the bug.
"""
import datetime as dt

import pytest

from marketdata import jalali

UTC = dt.timezone.utc


def test_after_midnight_tehran_is_the_next_day_than_the_server_sees():
    # 01:00 Wednesday in Tehran is 21:30 Tuesday on the server.
    moment = dt.datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    assert moment.date() == dt.date(2026, 11, 10)  # what the server's date says
    assert jalali.tehran_day(moment) == dt.date(2026, 11, 11)  # the market's day


def test_boundary_is_20_30_utc():
    assert jalali.tehran_day(dt.datetime(2026, 11, 10, 20, 29, tzinfo=UTC)) == dt.date(2026, 11, 10)
    assert jalali.tehran_day(dt.datetime(2026, 11, 10, 20, 30, tzinfo=UTC)) == dt.date(2026, 11, 11)


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        jalali.tehran_day(dt.datetime(2026, 11, 10, 21, 30))


def test_default_reads_the_replaceable_clock(monkeypatch):
    monkeypatch.setattr(jalali, "_now", lambda: dt.datetime(2026, 11, 10, 21, 30, tzinfo=UTC))
    assert jalali.tehran_day() == dt.date(2026, 11, 11)


def test_research_budget_day_follows_tehran(monkeypatch):
    from research import budget

    monkeypatch.setattr(jalali, "_now", lambda: dt.datetime(2026, 11, 10, 21, 30, tzinfo=UTC))
    assert budget._today() == dt.date(2026, 11, 11)
