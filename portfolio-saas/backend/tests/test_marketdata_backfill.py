"""Backfill command tests: mocked HTTP -> rows written; --dry-run writes none."""
from unittest.mock import patch

import pytest
from django.core.management import call_command

from marketdata import ingest
from marketdata.models import DailyStockHistory, RealLegalHistory

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
    # The fixture is a price payload; type=1 real/legal data rejects it rather
    # than writing fictional adjusted prices.
    assert DailyStockHistory.objects.filter(symbol="کاما").count() == 1
    assert not RealLegalHistory.objects.exists()


def test_real_legal_is_retained_without_a_matching_price_row():
    payload = [{
        "date": "1403-10-19", "Buy_CountI": 10, "Buy_CountN": 2,
        "Sell_CountI": 8, "Sell_CountN": 1, "Buy_I_Volume": 500,
        "Buy_N_Volume": 500, "Sell_I_Volume": 450, "Sell_N_Volume": 550,
        "Buy_I_Value": 1_000, "Buy_N_Value": 1_000, "Sell_I_Value": 900,
        "Sell_N_Value": 1_100,
    }]
    created, skipped = ingest.ingest_real_legal("کاما", payload)
    assert (created, skipped) == (1, 0)
    assert RealLegalHistory.objects.filter(symbol="کاما", date="1403-10-19").exists()


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
