"""Backfill command tests: mocked HTTP -> rows written; --dry-run writes none."""
from unittest.mock import patch

import pytest
from django.core.management import call_command

from marketdata.models import DailyStockHistory

pytestmark = pytest.mark.django_db

HISTORY_PAYLOAD = [
    {"date": "1403-10-19", "time": "12:29:59", "tno": 1, "tvol": 100, "tval": 1000,
     "pmin": 8490, "pmax": 8680, "py": 8430, "pf": 8570, "pl": 8500, "plc": 70,
     "plp": 0.83, "pc": 8570, "pcc": 140, "pcp": 1.66},
]


def _mock_get(mock):
    mock.return_value.status_code = 200
    mock.return_value.json.return_value = HISTORY_PAYLOAD
    mock.return_value.raise_for_status.return_value = None


def test_backfill_writes_history_rows(settings):
    settings.TSETMC_API_KEY = "test-key"
    with patch("marketdata.fetchers.base.requests.get") as mock:
        _mock_get(mock)
        call_command("backfill_market_data", "--symbol", "کاما",
                     "--kinds", "history", "--sleep", "0")
    # type 0 (unadjusted) + type 1 (adjusted) of the same payload = 2 rows.
    assert DailyStockHistory.objects.filter(symbol="کاما").count() == 2
    assert DailyStockHistory.objects.filter(is_adjusted=True).count() == 1


def test_backfill_dry_run_writes_nothing(settings):
    settings.TSETMC_API_KEY = "test-key"
    with patch("marketdata.fetchers.base.requests.get") as mock:
        _mock_get(mock)
        call_command("backfill_market_data", "--symbol", "کاما",
                     "--kinds", "history", "--sleep", "0", "--dry-run")
    assert DailyStockHistory.objects.count() == 0


def test_backfill_rejects_unknown_kind(settings):
    settings.TSETMC_API_KEY = "test-key"
    with pytest.raises(Exception, match="Unknown kinds"):
        call_command("backfill_market_data", "--symbol", "x", "--kinds", "bogus")
