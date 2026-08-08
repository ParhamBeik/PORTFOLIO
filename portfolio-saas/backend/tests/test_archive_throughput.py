from unittest.mock import patch

import pytest
import requests

from marketdata.archive import claim_archive_batch
from marketdata.fetchers.base import TransientMarketDataError, fetch_json
from marketdata.models import ArchiveFetchState
from marketdata.quota import ARCHIVE

pytestmark = pytest.mark.django_db


def test_batch_reserves_two_non_tick_and_uses_ten_tick_slots():
    non_ticks = [
        ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
            symbol=f"repair-{index}",
        )
        for index in range(3)
    ]
    ticks = [
        ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
            symbol=f"tick-{index}",
            stored_rows=index,
        )
        for index in range(12)
    ]

    claimed = claim_archive_batch(limit=12)

    assert len(set(claimed) & {row.pk for row in non_ticks}) == 2
    assert len(set(claimed) & {row.pk for row in ticks}) == 10
    claimed_ticks = [row.pk for row in ticks[:10]]
    assert set(claimed_ticks).issubset(claimed)


def test_completed_state_is_not_claimed_by_normal_batch():
    complete = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="complete",
        verified_complete=True,
    )
    assert complete.pk not in claim_archive_batch(limit=12)


def test_archive_fetch_has_one_physical_attempt_by_default():
    with patch("marketdata.fetchers.base.reserve_request") as reserve, patch(
        "marketdata.fetchers.base.requests.get", side_effect=requests.Timeout("timeout")
    ):
        with pytest.raises(TransientMarketDataError):
            fetch_json("https://example.test", quota_bucket=ARCHIVE)
    assert reserve.call_count == 1
