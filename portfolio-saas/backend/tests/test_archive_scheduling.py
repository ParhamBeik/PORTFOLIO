"""Archive claim ordering and state convergence.

Three defects let the backfill burn ~38% of a day's quota re-fetching symbols it
could never finish, while 858 symbols were never fetched once:

1. `-missing_rows` DESC sorted never-attempted states (missing_rows=0) last.
2. `elif created:` rescheduled non-converging states every 60s forever.
3. The rejected-record lookup queried the ArchiveFetchState endpoint name, but
   ingest labels rejections by writer (`real_legal_history`, `series:1d_adj`).
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from marketdata.archive import claim_archive_batch, run_archive_state
from marketdata.models import ArchiveFetchState, RejectedRecord

pytestmark = pytest.mark.django_db

Endpoint = ArchiveFetchState.Endpoint


def _state(symbol, endpoint=Endpoint.STOCK_HISTORY_ADJUSTED, **kwargs):
    return ArchiveFetchState.objects.create(symbol=symbol, endpoint=endpoint, **kwargs)


def test_claim_fills_historical_full_before_ticks(monkeypatch):
    monkeypatch.setattr("marketdata.archive._archive_prereqs_ready", lambda state: True)
    histories = [
        _state(f"h{i}", Endpoint.STOCK_HISTORY_UNADJUSTED) for i in range(8)
    ]
    ticks = [
        _state(f"t{i}", Endpoint.STOCK_TRANSACTION_TICKS) for i in range(8)
    ]
    batch = claim_archive_batch(limit=5)
    history_ids = {row.pk for row in histories}
    tick_ids = {row.pk for row in ticks}
    assert set(batch) <= history_ids
    assert not (set(batch) & tick_ids)


def test_claim_order_is_coverage_first():
    """A symbol with no data must be claimed before one missing 6 of 3,028 rows."""
    from marketdata.models import DailyStockHistory

    now = timezone.now()
    full = _state("has_data_small_gap", stored_rows=3022, expected_rows=3028,
                  missing_rows=6, last_success_at=now, last_attempt_at=now)
    empty = _state("fetched_but_empty", stored_rows=0, last_success_at=now,
                   last_attempt_at=now)
    never = _state("never_fetched")
    for symbol in ("has_data_small_gap", "fetched_but_empty", "never_fetched"):
        DailyStockHistory.objects.create(
            symbol=symbol, date="1405-01-01", pl=100,
        )

    batch = claim_archive_batch(limit=3)

    assert batch.index(never.pk) < batch.index(empty.pk) < batch.index(full.pk), (
        "coverage must outrank gap size: never-fetched, then empty, then top-ups"
    )


def test_recency_gap_tier_reopens_verified_complete_states(monkeypatch):
    """A verified_complete state whose post-close reverify came due must be
    claimable again -- without this tier it is permanently invisible to
    claim_archive_batch (its `base` queryset only ever sees
    verified_complete=False), so the gap between last_date and today never
    closes on its own."""
    monkeypatch.setattr("marketdata.archive._archive_prereqs_ready", lambda state: True)
    now = timezone.now()
    stale = _state(
        "already_complete", Endpoint.STOCK_HISTORY_UNADJUSTED,
        verified_complete=True, last_date="1404-01-01",
        next_attempt_at=now - timedelta(hours=1),
    )
    growing_tick = _state(
        "tick_symbol", Endpoint.STOCK_TRANSACTION_TICKS,
        target_window_days=90, stored_rows=10,
    )

    batch = claim_archive_batch(limit=1)

    assert batch == [stale.pk], (
        "the recency tier sits between the general coverage tier and tick "
        "depth growth, so a due reverify claims ahead of a fresh tick state"
    )
    assert growing_tick.pk not in batch


def test_recency_gap_tier_never_reopens_ticks():
    """Ticks reopen only through grow_tick_windows (which also widens the
    window); the general recency tier must leave verified_complete tick
    states alone or the two mechanisms would fight over the same rows."""
    now = timezone.now()
    done_tick = _state(
        "finished_tick", Endpoint.STOCK_TRANSACTION_TICKS,
        verified_complete=True, next_attempt_at=now - timedelta(hours=1),
    )

    batch = claim_archive_batch(limit=5)

    assert done_tick.pk not in batch


def test_rejected_rows_under_the_writer_label_are_forgiven():
    """ingest_real_legal labels rejections `real_legal_history`, not the endpoint.

    Querying `endpoint=state.endpoint` matched nothing, so ~726 states sat one
    or two permanently-rejected rows short of complete, forever.
    """
    state = _state("خزر", missing_rows=8)
    RejectedRecord.objects.create(
        endpoint="stock_history_unadjusted", symbol="خزر",
        date="1403-10-19", reason="price_negative",
    )
    RejectedRecord.objects.create(
        endpoint="real_legal_history", symbol="خزر",
        date="1403-10-20", reason="outlier_deviation",
    )

    with patch(
        "marketdata.archive._fetch_and_ingest",
        return_value=((1, 0), {"1403-10-19", "1403-10-20", "1403-10-21"}, {"1403-10-21"}),
    ):
        run_archive_state(state.pk)

    state.refresh_from_db()
    assert state.missing_rows == 0
    assert state.verified_complete is True


def test_no_progress_backs_off_instead_of_retrying_every_minute():
    # stored_rows starts at 1 to match the row the stub reports as already held.
    # Otherwise the first pass is genuine progress (0 -> 1 stored) and correctly
    # earns the fast path, which is a different case from "converges never".
    state = _state("قصفها", missing_rows=2, stored_rows=1)
    stuck = ((1, 0), {"a", "b", "c"}, {"c"})  # 2 missing, unchanged, every pass

    with patch("marketdata.archive._fetch_and_ingest", return_value=stuck):
        for _ in range(3):
            run_archive_state(state.pk)

    state.refresh_from_db()
    assert state.verified_complete is False
    assert state.consecutive_failures == 3
    # 2 ** (3-1) = 4h, nowhere near the old flat 1-minute reschedule.
    assert state.next_attempt_at - timezone.now() > timedelta(hours=3)


def test_real_progress_keeps_the_fast_retry_path():
    state = _state("لپارس3", missing_rows=5, consecutive_failures=2)

    with patch(
        "marketdata.archive._fetch_and_ingest",
        return_value=((1, 0), {"a", "b", "c"}, {"a", "b"}),  # 5 -> 1 missing
    ):
        run_archive_state(state.pk)

    state.refresh_from_db()
    assert state.missing_rows == 1
    assert state.consecutive_failures == 0
    assert state.next_attempt_at - timezone.now() < timedelta(minutes=2)


def test_codal_reverifies_weekly_not_daily():
    codal = _state("شیراز", endpoint=Endpoint.CODAL_ANNOUNCEMENTS)
    prices = _state("شیراز", endpoint=Endpoint.STOCK_HISTORY_UNADJUSTED)
    complete = ((1, 0), {"a"}, {"a"})

    with patch("marketdata.archive._fetch_and_ingest", return_value=complete):
        run_archive_state(codal.pk)
        run_archive_state(prices.pk)

    codal.refresh_from_db()
    prices.refresh_from_db()
    assert codal.verified_complete and prices.verified_complete
    assert codal.next_attempt_at - timezone.now() > timedelta(days=6)
    # Daily series re-verify at the next post-close after +0d. From a Thursday
    # morning that can be ~2.1 days out (Sat close); still far below weekly.
    assert prices.next_attempt_at < codal.next_attempt_at
    assert prices.next_attempt_at - timezone.now() < timedelta(days=4)


def test_repeated_transients_escalate():
    from marketdata.fetchers import TransientMarketDataError

    state = _state("ونفت")
    with patch(
        "marketdata.archive._fetch_and_ingest",
        side_effect=TransientMarketDataError("ReadTimeout"),
    ):
        for _ in range(4):
            run_archive_state(state.pk)

    state.refresh_from_db()
    assert state.consecutive_failures == 4
    # Was a flat 2 minutes on every attempt, forever.
    assert state.next_attempt_at - timezone.now() > timedelta(minutes=10)


def test_quota_exhausted_defers_to_tehran_day_rollover_not_one_minute():
    from marketdata.archive import next_quota_day_start
    from marketdata.quota import QuotaExhausted

    now = timezone.now()
    state = _state("quota-sym")
    sibling = _state(
        "quota-sib",
        next_attempt_at=now + timedelta(minutes=5),
    )
    with patch(
        "marketdata.archive._fetch_and_ingest",
        side_effect=QuotaExhausted("archive budget gone"),
    ):
        with pytest.raises(QuotaExhausted):
            run_archive_state(state.pk)

    state.refresh_from_db()
    sibling.refresh_from_db()
    rollover = next_quota_day_start(now)
    assert state.last_error == "Daily quota unavailable."
    assert state.next_attempt_at >= rollover - timedelta(seconds=2)
    assert abs((state.next_attempt_at - rollover).total_seconds()) < 2
    assert sibling.next_attempt_at >= rollover - timedelta(seconds=2)
    assert sibling.last_error == "Daily quota unavailable."


def test_grow_tick_windows_widens_completed_states_without_limit():
    from marketdata.archive import grow_tick_windows

    done = _state(
        "grown_symbol", Endpoint.STOCK_TRANSACTION_TICKS,
        verified_complete=True, target_window_days=90,
    )
    not_done = _state(
        "still_working", Endpoint.STOCK_TRANSACTION_TICKS,
        verified_complete=False, target_window_days=90,
    )

    updated = grow_tick_windows(step_days=90)

    assert updated == 1
    done.refresh_from_db()
    not_done.refresh_from_db()
    assert done.target_window_days == 180
    assert done.verified_complete is False
    assert not_done.target_window_days == 90, "an incomplete state is not this function's job"


def test_grow_tick_windows_stops_at_the_symbol_listing_date():
    from marketdata.archive import grow_tick_windows
    from marketdata.models import InstrumentListingHistory

    InstrumentListingHistory.objects.create(
        symbol="old_symbol", first_seen="1404-01-01", last_seen="1405-01-01",
    )
    already_at_listing = _state(
        "old_symbol", Endpoint.STOCK_TRANSACTION_TICKS,
        verified_complete=True, target_window_days=3650,
    )

    updated = grow_tick_windows(step_days=90)

    assert updated == 0
    already_at_listing.refresh_from_db()
    assert already_at_listing.target_window_days == 3650
    assert already_at_listing.verified_complete is True, (
        "already backfilled to the symbol's own listing date -- stays done"
    )
