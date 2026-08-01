"""Guards for the endpoint classification and the scheduling rules built on it.

Unit tests throughout: every assertion here is pure mapping logic or a single
in-memory decision, which is exactly the fast base of the test pyramid, and none
of it needs a provider round trip -- which matters because every real request
costs paid quota.

The registry facts below were established by probing the live API directly. They
are asserted rather than trusted because three of them were wrong in the shipped
code and each wrong one burned quota on a guaranteed-failing request.
"""
import pytest

from marketdata import endpoints
from marketdata.jalali import is_jalali
from marketdata.quota import ARCHIVE


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
