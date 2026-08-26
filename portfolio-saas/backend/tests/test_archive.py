"""The quota-driven backfill: scheduling, leasing, suspension, throughput, rejected-record recovery, and per-symbol resync.

Merged from 8 files; each section keeps its original banner.
"""

from datetime import timedelta
import datetime
from decimal import Decimal
from unittest import mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
import pytest
import requests

from marketdata.archive import _tick_trading_days, claim_archive_batch
from marketdata.archive import claim_archive_batch
from marketdata.archive import claim_archive_batch, run_archive_state
from marketdata.archive import run_archive_state
from marketdata.catalog import is_ordinary_stock, sync_provider_catalog
from marketdata.fetchers import (
    PermanentMarketDataError,
    TransientMarketDataError,
    fetch_json,
)
from marketdata.fetchers import TransientMarketDataError, fetch_json
from marketdata.management.commands.recheck_provider_days import Command
from marketdata.management.commands.recover_rejected_records import classify
from marketdata.models import (
    ApiRequestQuota,
    ArchiveFetchState,
    DailyStockHistory,
    MarketInstrument,
)
from marketdata.models import ArchiveFetchState
from marketdata.models import ArchiveFetchState, MarketCandle
from marketdata.models import ArchiveFetchState, RejectedRecord
from marketdata.models import DailyStockHistory, MarketCandle
from marketdata.models import RealLegalHistory, RejectedRecord
from marketdata import quota
from marketdata.quota import (
    ARCHIVE,
    LIVE,
    QuotaExhausted,
    quota_day,
    reconcile_account,
    remaining_requests,
    reserve_request,
)
from marketdata.quota import ARCHIVE
from marketdata.suspension import (
    MIN_ABSOLUTE_FAILURES,
    MIN_PEER_SAMPLE,
    PROBE_INTERVAL,
    _outlier_candidates,
    blacklist,
    claim_probe_batch,
    force_retry,
    suspend_outliers,
    try_recover,
    unblacklist,
    unsuspend,
)
from marketdata.tasks import archive_tick
from portfolio.models import Asset

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_archive_scheduling.py
# Archive claim ordering and state convergence.
# 
# Three defects let the backfill burn ~38% of a day's quota re-fetching symbols it
# could never finish, while 858 symbols were never fetched once:
# 
# 1. `-missing_rows` DESC sorted never-attempted states (missing_rows=0) last.
# 2. `elif created:` rescheduled non-converging states every 60s forever.
# 3. The rejected-record lookup queried the ArchiveFetchState endpoint name, but
#    ingest labels rejections by writer (`real_legal_history`, `series:1d_adj`).


Endpoint = ArchiveFetchState.Endpoint


def _state(symbol, endpoint=Endpoint.STOCK_HISTORY_ADJUSTED, **kwargs):
    return ArchiveFetchState.objects.create(symbol=symbol, endpoint=endpoint, **kwargs)


# Integration tests, not unit: the claim order is expressed as database
# annotations, so an in-memory fake would be testing a reimplementation of the
# ordering rather than the SQL that actually decides how the day's quota is spent.


def test_never_succeeded_outranks_everything_else(monkeypatch):
    """Rule 1: one successful fetch everywhere before anything is topped up.

    Endpoint class is irrelevant here. The old scheduler drained cheap
    full-history endpoints first, so a symbol with no ticks at all waited behind
    routine refreshes of symbols already holding years of data.
    """
    monkeypatch.setattr("marketdata.archive._archive_prereqs_ready", lambda state: True)
    now = timezone.now()
    virgin_tick = _state("NEVER", Endpoint.STOCK_TRANSACTION_TICKS)
    _state("GAP", Endpoint.STOCK_HISTORY_UNADJUSTED, missing_rows=500,
           stored_rows=2000, last_success_at=now, last_attempt_at=now)
    _state("DONE", Endpoint.STOCK_CANDLE_ADJUSTED, verified_complete=True,
           stored_rows=3000, last_success_at=now - timedelta(days=9),
           last_attempt_at=now - timedelta(days=9))

    assert claim_archive_batch(limit=1) == [virgin_tick.pk]


def test_ticks_take_the_majority_share_once_everything_has_succeeded(monkeypatch, settings):
    """Rule 2: the endpoint with the real backlog gets most of the batch.

    A share, not strict priority -- see MARKETDATA_TICK_QUOTA_SHARE. Before this
    the tick lane was the *last* of four tiers, so the post-close flood of
    completed full-history refreshes consumed whole batches ahead of it.
    """
    monkeypatch.setattr("marketdata.archive._archive_prereqs_ready", lambda state: True)
    settings.MARKETDATA_TICK_QUOTA_SHARE = 0.70
    now = timezone.now()
    for i in range(20):
        _state(f"t{i}", Endpoint.STOCK_TRANSACTION_TICKS, missing_rows=80,
               stored_rows=10, last_success_at=now, last_attempt_at=now)
        _state(f"h{i}", Endpoint.STOCK_HISTORY_UNADJUSTED, missing_rows=80,
               stored_rows=10, last_success_at=now, last_attempt_at=now)

    batch = claim_archive_batch(limit=10)
    ticks = ArchiveFetchState.objects.filter(
        pk__in=batch, endpoint=Endpoint.STOCK_TRANSACTION_TICKS
    ).count()
    assert len(batch) == 10
    assert ticks == 7, "ticks take their configured share, not the whole batch"


def test_completed_refreshes_trail_the_backlog_and_sort_by_staleness(monkeypatch):
    """Rule 3: deficit first, then longest-since-success.

    A complete state scores a deficit of 0 and sinks behind anything still owed
    rows, instead of occupying a tier of its own above the backlog.
    """
    monkeypatch.setattr("marketdata.archive._archive_prereqs_ready", lambda state: True)
    now = timezone.now()
    owed = _state("OWED", Endpoint.STOCK_HISTORY_UNADJUSTED, missing_rows=4,
                  stored_rows=3000, last_success_at=now, last_attempt_at=now)
    stale = _state("STALE_DONE", Endpoint.STOCK_CANDLE_ADJUSTED, verified_complete=True,
                   stored_rows=3000, last_success_at=now - timedelta(days=9),
                   last_attempt_at=now - timedelta(days=9))
    fresh = _state("FRESH_DONE", Endpoint.STOCK_CANDLE_UNADJUSTED, verified_complete=True,
                   stored_rows=3000, last_success_at=now - timedelta(hours=1),
                   last_attempt_at=now - timedelta(hours=1))

    batch = claim_archive_batch(limit=3)
    assert batch.index(owed.pk) < batch.index(stale.pk) < batch.index(fresh.pk)


def test_a_lane_takes_the_slots_the_other_cannot_fill(monkeypatch, settings):
    """An empty tick queue must not leave 70% of the batch -- and the quota -- unspent."""
    monkeypatch.setattr("marketdata.archive._archive_prereqs_ready", lambda state: True)
    settings.MARKETDATA_TICK_QUOTA_SHARE = 0.70
    now = timezone.now()
    for i in range(10):
        _state(f"h{i}", Endpoint.STOCK_HISTORY_UNADJUSTED, missing_rows=80,
               stored_rows=10, last_success_at=now, last_attempt_at=now)

    assert len(claim_archive_batch(limit=10)) == 10


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


def test_codal_reverifies_weekly_not_daily(settings):
    settings.CODAL_ENABLED = True
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


# ----------------------------------------------------------------------
# test_archive_suspension.py
# Peer-relative outlier suspension for ArchiveFetchState.
# 
# Detection (`_outlier_candidates`) is pure -- no DB -- so those cases are plain
# unit tests. Recovery/blacklist/probe-selection need real rows since they are
# queryset filters, so those are thin integration tests against Postgres.


def _peers(n, value, prefix="p"):
    return [(i, f"{prefix}{i}", value) for i in range(n)]


# --- unit: pure detection function --------------------------------------

def test_outlier_against_healthy_peers_is_flagged():
    # unit test: pure function, no DB
    peers = _peers(9, 0) + [(99, "sick", MIN_ABSOLUTE_FAILURES)]
    candidates = _outlier_candidates(peers)
    assert {c.state_id for c in candidates} == {99}
    assert candidates[0].sample_size == 10
    assert candidates[0].peer_median == 0


def test_same_rate_as_peers_is_not_flagged():
    # unit test: the core of the requirement -- shared bad luck isn't a bad symbol
    value = MIN_ABSOLUTE_FAILURES + 1
    peers = _peers(3, value) + _peers(7, 0)  # 30% share, below the outage guard
    candidates = _outlier_candidates(peers)
    assert candidates == []


def test_provider_wide_outage_suspends_nobody():
    # unit test: most of the endpoint failing together must never suspend anyone
    peers = _peers(8, MIN_ABSOLUTE_FAILURES + 3) + _peers(2, 0)
    assert _outlier_candidates(peers) == []


def test_small_sample_endpoint_never_flagged():
    # unit test: below MIN_PEER_SAMPLE, no distribution is trustworthy
    assert MIN_PEER_SAMPLE > 3
    peers = _peers(2, 0) + [(9, "lonely_outlier", 50)]
    assert _outlier_candidates(peers) == []


def test_below_absolute_floor_never_flagged_even_if_relatively_high():
    # unit test: 2 vs peer median 0 is "relatively worse" but under the floor
    peers = _peers(9, 0) + [(99, "barely_worse", MIN_ABSOLUTE_FAILURES - 1)]
    assert _outlier_candidates(peers) == []


# --- integration: DB-backed wrappers -------------------------------------

@pytest.mark.django_db
def test_suspend_outliers_flags_only_the_outlier_and_keeps_the_row():
    for i in range(9):
        _state(f"healthy{i}", consecutive_failures=0)
    sick = _state("sick", consecutive_failures=MIN_ABSOLUTE_FAILURES)

    suspended_ids = suspend_outliers(Endpoint.STOCK_HISTORY_ADJUSTED)

    assert suspended_ids == [sick.pk]
    sick.refresh_from_db()
    assert sick.suspended_at is not None
    assert sick.suspension_reason == "peer_outlier"
    assert sick.suspension_evidence["value"] == MIN_ABSOLUTE_FAILURES
    # never deleted
    assert ArchiveFetchState.objects.filter(pk=sick.pk).exists()


@pytest.mark.django_db
def test_suspended_state_becomes_probe_due_after_a_week_and_unsuspends_on_clean_data():
    now = timezone.now()
    state = _state(
        "recovering",
        suspended_at=now - PROBE_INTERVAL - timedelta(hours=1),
        last_probe_at=now - PROBE_INTERVAL - timedelta(hours=1),
        consecutive_failures=MIN_ABSOLUTE_FAILURES,
    )

    # not due yet a moment after suspension
    assert claim_probe_batch(now=now - PROBE_INTERVAL + timedelta(minutes=1)) == []

    due = claim_probe_batch(now=now)
    assert due == [state.pk]
    state.refresh_from_db()
    assert state.last_probe_at == now  # claimed

    # simulate a clean fetch result landing on the probed state
    state.verified_complete = True
    state.consecutive_failures = 0
    recovered = try_recover(state, now=now)

    assert recovered is True
    assert state.suspended_at is None
    assert state.suspension_reason == ""
    state.refresh_from_db()
    assert state.suspended_at is None


@pytest.mark.django_db
def test_dirty_probe_result_stays_suspended():
    now = timezone.now()
    state = _state("still_sick", suspended_at=now, consecutive_failures=MIN_ABSOLUTE_FAILURES)
    state.verified_complete = False
    assert try_recover(state, now=now) is False
    assert state.suspended_at is not None


@pytest.mark.django_db
def test_blacklisted_state_never_probed_and_never_recovers():
    now = timezone.now()
    state = _state(
        "gone_forever",
        last_probe_at=now - PROBE_INTERVAL - timedelta(days=1),
    )
    blacklist(state, now=now)
    state.refresh_from_db()
    assert state.blacklisted is True
    assert state.suspended_at is not None  # blacklist implies suspended

    assert claim_probe_batch(now=now) == []

    state.verified_complete = True
    state.consecutive_failures = 0
    assert try_recover(state, now=now) is False
    assert state.blacklisted is True

    unblacklist(state)
    state.refresh_from_db()
    assert state.blacklisted is False
    assert state.suspended_at is not None  # unblacklisting alone doesn't un-suspend


@pytest.mark.django_db
def test_manual_force_retry_clears_suspension_immediately():
    now = timezone.now()
    state = _state(
        "operator_override",
        suspended_at=now,
        suspension_reason="peer_outlier",
        suspension_evidence={"value": 9},
        consecutive_failures=9,
    )
    force_retry(state, now=now)
    state.refresh_from_db()
    assert state.suspended_at is None
    assert state.consecutive_failures == 0
    assert state.next_attempt_at == now
    assert ArchiveFetchState.objects.filter(pk=state.pk).exists()


@pytest.mark.django_db
def test_unsuspend_never_deletes_the_row():
    state = _state("delete_check", suspended_at=timezone.now(), consecutive_failures=7)
    pk = state.pk
    unsuspend(state)
    assert ArchiveFetchState.objects.filter(pk=pk).exists()


# ----------------------------------------------------------------------
# test_archive_task_lock.py


def test_archive_tick_skips_when_lock_is_held(monkeypatch):
    import marketdata.tasks as tasks

    client = mock.Mock()
    client.set.return_value = False
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    ensure = mock.Mock()
    monkeypatch.setattr(tasks, "ensure_archive_states", ensure)

    archive_tick()

    ensure.assert_not_called()


def test_archive_tick_claims_only_free_queue_slots(monkeypatch):
    import marketdata.tasks as tasks

    client = mock.Mock()
    client.set.return_value = True
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    monkeypatch.setattr(tasks, "_queue_slots", lambda *_args: (3, 1))
    monkeypatch.setattr(tasks, "grow_tick_windows", mock.Mock())
    claim = mock.Mock(return_value=[])
    monkeypatch.setattr(tasks, "claim_archive_batch", claim)

    archive_tick()

    claim.assert_called_once_with(limit=3)


def test_archive_tick_does_not_claim_when_queue_is_full(monkeypatch):
    import marketdata.tasks as tasks

    client = mock.Mock()
    client.set.return_value = True
    monkeypatch.setattr(tasks, "get_redis", lambda: client)
    monkeypatch.setattr(tasks, "_queue_slots", lambda *_args: (0, 4))
    claim = mock.Mock()
    monkeypatch.setattr(tasks, "claim_archive_batch", claim)

    archive_tick()

    claim.assert_not_called()


def test_queue_slots_uses_broker_depth_and_fails_closed(monkeypatch):
    import marketdata.tasks as tasks

    broker = mock.Mock()
    broker.llen.return_value = 3
    monkeypatch.setattr("redis.Redis.from_url", lambda *_args, **_kwargs: broker)

    assert tasks._queue_slots("archive", 4) == (1, 3)

    broker.llen.side_effect = RuntimeError("broker unavailable")
    assert tasks._queue_slots("archive", 4) == (0, None)


def test_archive_claim_includes_each_due_endpoint_before_filling_priority(monkeypatch):
    import marketdata.archive as archive

    monkeypatch.setattr(archive, "archive_capacity", lambda: {"tsetmc": 10, "brs": 10})
    monkeypatch.setattr(archive, "_archive_prereqs_ready", lambda state: True)
    ArchiveFetchState.objects.create(endpoint="stock_history_unadjusted", symbol="price")
    ArchiveFetchState.objects.create(endpoint="stock_transaction_ticks", symbol="ticks")

    states = ArchiveFetchState.objects.filter(pk__in=claim_archive_batch(limit=2))
    assert set(states.values_list("endpoint", flat=True)) == {
        "stock_history_unadjusted", "stock_transaction_ticks",
    }


# ----------------------------------------------------------------------
# test_archive_throughput.py


def test_batch_fills_full_history_first_then_gives_the_rest_to_ticks():
    """Cost ordering: the cheap class drains first, ticks take what is left.

    One History.php request returns ~4,600 rows; one Transaction.php request
    buys a single calendar day. So full history is claimed first and ticks fill
    the remaining slots -- see test_claim_fills_historical_full_before_ticks in
    test_archive_scheduling.py, which pins the same rule with no ticks eligible.

    This used to assert a flat reservation of 2 non-tick slots, which was the
    policy before the claim path was reordered by cost class; the assertion
    outlived the behaviour it described.
    """
    from django.core.cache import cache

    from marketdata import jalali

    cache.clear()
    day = sorted(jalali.recent_days(90))[-1]
    for peer in range(5):
        MarketCandle.objects.create(
            symbol=f"peer-{peer}",
            timeframe=MarketCandle.UNADJUSTED,
            date_time=day,
            close_price=100,
            volume=10,
        )
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
    for row in ticks:
        MarketCandle.objects.create(
            symbol=row.symbol,
            timeframe=MarketCandle.UNADJUSTED,
            date_time=day,
            close_price=1,
            volume=100,
        )

    claimed = claim_archive_batch(limit=12)

    assert len(claimed) == 12
    # All 3 full-history states go first because they are the cheap class...
    assert {row.pk for row in non_ticks}.issubset(claimed)
    # ...and ticks fill the 9 slots that remain, lowest stored_rows first.
    assert len(set(claimed) & {row.pk for row in ticks}) == 9
    assert {row.pk for row in ticks[:9]}.issubset(claimed)


def test_claim_skips_ticks_without_candles():
    from django.core.cache import cache

    from marketdata import jalali

    cache.clear()
    day = sorted(jalali.recent_days(90))[-1]
    for peer in range(5):
        MarketCandle.objects.create(
            symbol=f"peer-{peer}",
            timeframe=MarketCandle.UNADJUSTED,
            date_time=day,
            close_price=100,
            volume=10,
        )
    blocked = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="no-candles",
    )
    ready = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="has-candles",
    )
    MarketCandle.objects.create(
        symbol="has-candles",
        timeframe=MarketCandle.UNADJUSTED,
        date_time=day,
        close_price=1,
        volume=100,
    )

    claimed = claim_archive_batch(limit=12)
    assert ready.pk in claimed
    assert blocked.pk not in claimed
    blocked.refresh_from_db()
    assert blocked.next_attempt_at is not None


def test_completed_state_is_not_claimed_by_normal_batch():
    complete = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="complete",
        verified_complete=True,
    )
    assert complete.pk not in claim_archive_batch(limit=12)


def test_tick_calendar_accepts_a_symbol_with_only_adjusted_candles():
    """35 live symbols have thousands of adjusted bars and zero unadjusted ones.

    Reading only `1d_unadj` left their trading calendar empty, so the tick state
    raised "no trading days known" forever instead of ever fetching a tick.
    """
    from django.core.cache import cache

    from marketdata import jalali

    # The calendar is derived from data inside a trailing window, so anchor the
    # fixture to real recent days rather than hard-coded ones that age out.
    cache.clear()
    days = sorted(jalali.recent_days(90))[-3:]
    # A market-wide calendar needs other symbols trading on the same days.
    for peer in range(5):
        for day in days:
            MarketCandle.objects.create(
                symbol=f"peer-{peer}",
                timeframe=MarketCandle.UNADJUSTED,
                date_time=day,
                close_price=100,
                volume=10,
            )
    for day in days:
        MarketCandle.objects.create(
            symbol="adj-only",
            timeframe=MarketCandle.ADJUSTED,
            date_time=day,
            close_price=100,
            volume=10,
        )

    assert _tick_trading_days("adj-only", window_days=90) == set(days)
    assert _tick_trading_days("never-fetched", window_days=90) == set()


def test_volume_mismatch_is_recorded_so_the_day_can_be_forgiven():
    """A day the provider never serves consistently is a gap, not a retry.

    Nothing recorded the mismatch, so 24 tick states re-fetched the same broken
    day forever and none could reach complete -- which also froze the 90->365 day
    promotion, since that requires every tick state to be complete.
    """
    from marketdata import ingest, validation
    from marketdata.archive import _fetch_and_ingest, _tick_dates_needed, run_archive_state
    from marketdata.models import RejectedRecord

    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS, symbol="MM"
    )
    day = "1405-05-11"
    with patch("marketdata.archive._tick_dates_needed", return_value=[day]), patch(
        "marketdata.archive.fetch_transactions", return_value=[{"volume": 5}]
    ), patch.object(ingest, "screen", return_value=([{"volume": 5}], 0)), patch.object(
        validation, "reconcile_tick_volume", return_value="tick_volume_mismatch:5!=99"
    ), patch("marketdata.archive._symbol_candle_volumes", return_value={day: 99}), patch(
        "marketdata.archive._tick_trading_days", return_value={day}
    ), patch("marketdata.archive._tick_dates_stored", return_value=set()):
        result, expected, stored = _fetch_and_ingest(state)

    assert result == (0, 0)
    assert day in expected
    assert day not in stored
    record = RejectedRecord.objects.get(endpoint="stock_transaction_ticks", symbol="MM")
    assert record.date == day
    assert record.reason == "tick_volume_mismatch"
    assert "5!=99" in record.payload["detail"]
    assert day not in _tick_dates_needed("MM")

    with patch(
        "marketdata.archive._fetch_and_ingest",
        return_value=((0, 0), {day}, set()),
    ):
        state = run_archive_state(state.pk)
    assert state.verified_complete
    assert state.known_gap_rows == 1
    assert state.missing_rows == 0
    assert state.last_error == ""


def test_banking_a_day_counts_as_progress_even_when_the_gap_does_not_shrink():
    """The tick window moves, so `missing` can stay flat while data lands.

    Measuring progress only by a shrinking gap punished symbols for succeeding:
    they banked a day, the window gained a day, `missing` held, and the backoff
    doubled to the 24h cap while they were steadily storing data.
    """
    from marketdata.archive import _reschedule

    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="moving-window",
        stored_rows=4,
        missing_rows=44,
        consecutive_failures=3,
    )

    # One more day banked; the window also gained one, so the gap is unchanged.
    _reschedule(state, stored_count=5, missing_count=44, previous_stored=4,
                previous_missing=44)

    assert state.consecutive_failures == 0


def test_no_progress_at_all_still_backs_off():
    from marketdata.archive import _reschedule

    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="wedged",
        stored_rows=4,
        missing_rows=44,
        consecutive_failures=3,
    )

    _reschedule(state, stored_count=4, missing_count=44, previous_stored=4,
                previous_missing=44)

    assert state.consecutive_failures == 4


def test_archive_fetch_has_one_physical_attempt_by_default():
    with patch("marketdata.fetchers.reserve_request") as reserve, patch(
        "marketdata.fetchers.requests.get", side_effect=requests.Timeout("timeout")
    ):
        with pytest.raises(TransientMarketDataError):
            fetch_json("https://example.test", quota_bucket=ARCHIVE)
    assert reserve.call_count == 1


# ----------------------------------------------------------------------
# test_marketdata_tracking.py
# Quota, provider classification, and DB-verified archive progress.


@pytest.fixture(autouse=True)
def mock_timezone_now(monkeypatch, settings):
    import datetime
    from django.utils import timezone
    # Set to a fixed time (e.g. 09:00:00 UTC) to ensure consistent quota limits
    mocked_dt = datetime.datetime(2026, 8, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(timezone, "now", lambda: mocked_dt)
    settings.BRS_API_KEY = "test-key"


def test_live_floor_survives_an_archive_burst(settings):
    """Archive must never consume the requests reserved for customer-facing prices."""
    from marketdata.quota import TSETMC

    # The TSETMC reserve prices the jobs that key actually enables; with no key
    # configured the simulated plan is empty and nothing gets reserved.
    settings.TSETMC_API_KEY = "test-key"
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 4
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 10
    # The reserve only binds once the provider has told us this plan's ceiling;
    # there is no hardcoded daily limit any more.
    ApiRequestQuota.objects.create(day=quota_day(), plan=TSETMC, limit=10)
    for _ in range(6):
        reserve_request(ARCHIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)
    for _ in range(4):
        reserve_request(LIVE)
    row = ApiRequestQuota.objects.get(plan=TSETMC)
    assert row.used == 10
    assert row.archive_used == 6
    assert row.live_used == 4


def test_bucket_budget_caps_a_single_bucket(settings):
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 2
    reserve_request(LIVE)
    reserve_request(LIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(LIVE)


def test_archive_bucket_has_no_hardcoded_ceiling(settings):
    """The bug: a constant cap stopped the backfill while the account had room.

    ARCHIVE is deliberately uncapped now -- what stops it is the live reserve,
    the rolling window, and ultimately the provider's own refusal.
    """
    from marketdata.quota import bucket_budget

    assert bucket_budget(ARCHIVE) is None
    for _ in range(50):
        reserve_request(ARCHIVE)
    assert ApiRequestQuota.objects.get().archive_used == 50


def test_provider_account_reconciles_local_counter(settings):
    """Provider drift is monotonic and remains represented in bucket totals."""
    reserve_request(ARCHIVE)
    assert reconcile_account({"usage_today": 4021, "request_block": 120}) == 120
    row = ApiRequestQuota.objects.get()
    assert row.used == 4021
    assert row.archive_used + row.live_used + row.other_used == row.used
    assert row.other_used == 4020
    # An out-of-order provider response must not re-open already spent quota.
    reconcile_account({"usage_today": 4000})
    assert ApiRequestQuota.objects.get().used == 4021
    assert reconcile_account(None) == 0
    assert ApiRequestQuota.objects.get().used == 4021


def test_reconcile_records_the_limit_the_provider_reports(settings):
    """The daily ceiling is observed data now, not a constant in settings."""
    from marketdata.quota import TSETMC

    reconcile_account({"usage_today": 12, "limit_today": 10000}, TSETMC)
    row = ApiRequestQuota.objects.get(plan=TSETMC)
    assert (row.limit, row.used) == (10000, 12)


def test_each_provider_plan_keeps_its_own_wallet(settings):
    """The production failure: a spent TSETMC plan refused BRS calls.

    BrsApi meters the two API keys separately (~10,000/day vs ~1,500/day), so
    exhausting one must leave the other completely untouched. This is the single
    most important behaviour in this module -- when it regressed, the USDT quote
    failed 201 times in one day and dollar holdings went stale.
    """
    from marketdata.quota import BRS, TSETMC

    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    ApiRequestQuota.objects.create(day=quota_day(), plan=TSETMC, limit=5)

    for _ in range(5):
        reserve_request(ARCHIVE, TSETMC)
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE, TSETMC)

    # The other wallet is untouched and still spends freely.
    for _ in range(5):
        reserve_request(ARCHIVE, BRS)
    assert ApiRequestQuota.objects.get(plan=BRS).archive_used == 5
    assert ApiRequestQuota.objects.get(plan=TSETMC).used == 5


def test_reconcile_does_not_cross_plans(settings):
    """A BRS `usage_today` must never be merged into the TSETMC counter."""
    from marketdata.quota import BRS, TSETMC

    reconcile_account({"usage_today": 9000}, TSETMC)
    reconcile_account({"usage_today": 300}, BRS)
    assert ApiRequestQuota.objects.get(plan=TSETMC).used == 9000
    assert ApiRequestQuota.objects.get(plan=BRS).used == 300


def test_permanent_http_error_uses_one_call_without_retry(settings):
    # Zero the whole live bucket: the reserve is bounded by floor + headroom, so
    # leaving the headroom set would hold back more than this 10-request day has.
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    with patch("marketdata.fetchers.requests.get") as get:
        get.return_value.status_code = 400
        get.return_value.json.side_effect = ValueError
        with pytest.raises(PermanentMarketDataError):
            fetch_json("https://example.test", retries=2)
    assert get.call_count == 1
    assert ApiRequestQuota.objects.get().used == 1


def test_transient_http_error_does_not_leak_api_key(settings, caplog):
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    secret = "provider-secret"
    from requests.exceptions import RequestException
    with patch(
        "marketdata.fetchers.requests.get",
        side_effect=RequestException(f"failed https://example.test/?key={secret}"),
    ):
        with pytest.raises(TransientMarketDataError) as exc:
            fetch_json("https://example.test", params={"key": secret}, retries=0)
    assert secret not in caplog.text
    assert secret not in str(exc.value)


def test_every_transient_http_attempt_consumes_quota(settings):
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    from requests.exceptions import RequestException

    with (
        patch("marketdata.fetchers.requests.get", side_effect=RequestException("timeout")) as get,
        patch("marketdata.fetchers.time.sleep"),
    ):
        with pytest.raises(TransientMarketDataError):
            fetch_json("https://example.test", retries=2)

    row = ApiRequestQuota.objects.get()
    assert get.call_count == 3
    assert row.used == row.other_used == 3


def test_catalog_accepts_shares_and_rejects_rights_and_funds():
    assert is_ordinary_stock({"isin": "IRO1TEST0001"})
    assert is_ordinary_stock({"isin": "IRO3TEST0001"})
    assert not is_ordinary_stock({"isin": "IRR1TEST0101"})
    assert not is_ordinary_stock({"isin": "IRT1TEST0001"})


def test_catalog_accepts_all_provider_currencies(settings):
    """We choose an integration test because provider catalog sync crosses fetcher payload parsing and DB persistence."""
    settings.TSETMC_API_KEY = "test-key"
    settings.BRS_API_KEY = "test-key"
    with (
        patch("marketdata.catalog.fetch_all_symbols", return_value=[]),
        patch("marketdata.catalog.fetch_gold_currency_free", return_value={
            "currency": [
                {"symbol": "USD", "name": "Dollar"},
                {"symbol": "EUR", "name": "Euro"},
            ],
            "crypto": [{"symbol": "BTC", "name": "Bitcoin"}],
        }),
        patch("marketdata.catalog.fetch_derivatives", return_value=[]),
    ):
        sync_provider_catalog()

    assert MarketInstrument.objects.filter(source="brs", symbol="USD", eligible=True).exists()
    assert MarketInstrument.objects.filter(source="brs", symbol="EUR", eligible=True).exists()
    assert MarketInstrument.objects.filter(source="brs", symbol="BTC", eligible=True).exists()


def test_catalog_classifies_irt_isin_rows_as_etf_not_excluded(settings):
    """IRT-prefixed ISINs (funds) used to fall through to EXCLUDED here, which
    is why MarketInstrument had zero ETF rows. Funds are classified from the
    catalog, not from a Nav.php poll."""
    settings.TSETMC_API_KEY = "test-key"
    settings.BRS_API_KEY = "test-key"
    with (
        patch("marketdata.catalog.fetch_all_symbols", return_value=[
            {"l18": "اهرم", "l30": "صندوق س سهامی کاریزما- اهرمی", "isin": "IRT1TEST0001", "cs": "صندوق سرمایه‌گذاری قابل معامله"},
            {"l18": "فملی", "l30": "ملی صنایع مس ایران", "isin": "IRO1MSMI0001", "cs": "فلزات اساسی"},
        ]),
        patch("marketdata.catalog.fetch_gold_currency_free", return_value={}),
        patch("marketdata.catalog.fetch_derivatives", return_value=[]),
    ):
        sync_provider_catalog()

    etf = MarketInstrument.objects.get(source="tsetmc", symbol="اهرم")
    assert etf.category == MarketInstrument.Category.ETF
    assert etf.eligible is True

    stock = MarketInstrument.objects.get(source="tsetmc", symbol="فملی")
    assert stock.category == MarketInstrument.Category.STOCK


# STOCK_HISTORY_ADJUSTED is History.php?type=1, which returns the Real/Legal
# participant breakdown and no price fields at all. These fixtures use that real
# shape; a price-shaped fixture used to pass while production stored 1.3M
# all-zero rows, because the verifier only compared date sets.
REAL_LEGAL_PAYLOAD = [
    {"date": "1404-01-01", "Buy_CountI": 12, "Buy_I_Volume": 500, "Sell_CountN": 3},
    {"date": "1404-01-02", "Buy_CountI": 15, "Buy_I_Volume": 700, "Sell_CountN": 4},
]


def _seed_price_days(symbol="TEST"):
    for date in ("1404-01-01", "1404-01-02"):
        DailyStockHistory.objects.create(
            symbol=symbol, date=date, pl=100
        )


def test_archive_state_is_complete_only_after_rows_exist(settings):
    settings.TSETMC_API_KEY = "test-key"
    _seed_price_days()
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD):
        state = run_archive_state(state.pk)
    assert state.verified_complete
    assert state.expected_rows == state.stored_rows == 2
    assert state.missing_rows == 0
    assert (
        DailyStockHistory.objects.filter(
            symbol="TEST", buy_count_i__isnull=False
        ).count()
        == 2
    )


def test_real_legal_without_price_rows_never_verifies(settings):
    """No price row at all means nothing to attach the breakdown to.

    Reported as a missing dependency rather than a row-level gap: re-fetching
    *this* endpoint can never produce a price row, so the unadjusted pass has to
    land first. Soft-defer without wedging consecutive_failures.
    """
    settings.TSETMC_API_KEY = "test-key"
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD):
        state = run_archive_state(state.pk)
    assert not state.verified_complete
    assert "no matching daily price rows" in state.last_error
    assert state.consecutive_failures == 0
    assert state.next_attempt_at is not None


def test_real_legal_verifies_despite_individually_rejected_price_days(settings):
    """One price day the validator threw out must not block the whole symbol.

    `stored` used to be the intersection of real/legal and price dates, so a day
    rejected by the *price* validator left a hole this endpoint could never fill
    -- 726 symbols sat 1-2 rows short forever, re-fetching every 60 seconds.
    """
    settings.TSETMC_API_KEY = "test-key"
    # REAL_LEGAL_PAYLOAD carries two days; only the first gets a price row --
    # the second stands in for a day the price validator rejected.
    DailyStockHistory.objects.create(
        symbol="TEST", date=REAL_LEGAL_PAYLOAD[0]["date"], pl=100
    )
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD):
        state = run_archive_state(state.pk)
    assert state.verified_complete
    assert state.missing_rows == 0


def test_archive_state_records_missing_rows_instead_of_claiming_success(settings):
    settings.TSETMC_API_KEY = "test-key"
    _seed_price_days()
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
    )
    with (
        patch("marketdata.archive.fetch_daily_history", return_value=REAL_LEGAL_PAYLOAD),
        patch("marketdata.archive.ingest.ingest_real_legal", return_value=(0, 2)),
    ):
        state = run_archive_state(state.pk)
    assert not state.verified_complete
    assert state.missing_rows == 2


def test_archive_exposes_permanently_rejected_dates_as_known_gaps(settings):
    settings.TSETMC_API_KEY = "test-key"
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
        symbol="KNOWN_GAP",
    )
    payload = {"candle_daily_adjusted": [
        {"date": "1404-01-01", "open": 100, "high": 110, "low": 90,
         "close": 105, "volume": 1000},
        {"date": "1404-01-02", "open": 0, "high": 0, "low": 0,
         "close": 0, "volume": 0},
    ]}
    with patch("marketdata.archive.fetch_candlesticks", return_value=payload):
        state = run_archive_state(state.pk)
    assert state.verified_complete
    assert state.stored_rows == 1
    assert state.known_gap_rows == 1
    assert state.missing_rows == 0


def test_active_asset_must_exist_in_verified_catalog():
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS,
        symbol="IR_GOLD_18K",
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )
    Asset.objects.create(
        key="verified_gold",
        name="Verified Gold",
        asset_class=Asset.AssetClass.GOLD,
        brs_symbol="IR_GOLD_18K",
    )
    with pytest.raises(ValidationError):
        Asset.objects.create(
            key="invented_gold",
            name="Invented Gold",
            asset_class=Asset.AssetClass.GOLD,
            brs_symbol="NOT_REAL",
        )


def test_5m_window_rate_limit(settings, monkeypatch):
    """We choose a unit test because verifying 5-minute rolling window rate limits tests fast, isolated business rules at the base of the test pyramid."""
    from marketdata import quota
    from marketdata.quota import get_quota_status
    monkeypatch.setattr(quota, "get_redis", lambda: None)
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 0
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
    settings.MARKETDATA_REQUIRE_SHARED_WINDOW = False
    # The window is per bucket, and with no Redis in the suite the degraded
    # per-process window applies, so back out both divisors to land on 3.
    settings.MARKETDATA_WINDOW_LIMIT = int(
        3 * quota._DEGRADED_PROCESS_DIVISOR / quota._WINDOW_SHARE[ARCHIVE]
    )
    settings.MARKETDATA_WINDOW_SECONDS = 300
    quota._LOCAL_WINDOWS.clear()

    reserve_request(ARCHIVE)
    reserve_request(ARCHIVE)
    reserve_request(ARCHIVE)

    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)

    status = get_quota_status()
    assert status["window_by_bucket"][ARCHIVE] >= 3


def test_window_quota_uses_one_atomic_redis_operation(settings, monkeypatch):
    from marketdata import quota

    settings.MARKETDATA_WINDOW_LIMIT = 3
    settings.MARKETDATA_WINDOW_SECONDS = 300
    client = mock.Mock()
    client.eval.return_value = 1
    monkeypatch.setattr(quota, "get_redis", lambda: client)

    quota._check_and_record_window()

    client.eval.assert_called_once()


def test_ensure_archive_states_covers_all_endpoints(settings):
    """We choose a unit test because verifying archive state generation across all provider endpoints tests pure data warehouse mapping logic at the base of the test pyramid."""
    from marketdata.archive import ensure_archive_states
    settings.CODAL_ENABLED = True
    ensure_archive_states(stock_symbols=["KAMA"], gold_symbols=["USD"])
    states = ArchiveFetchState.objects.filter(symbol="KAMA")
    endpoints = set(states.values_list("endpoint", flat=True))
    assert ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS in endpoints
    assert ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS in endpoints
    assert ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS in endpoints
    # 7, not 10: market_index_daily, etf_nav_daily and option_contract_daily are
    # live snapshots the provider cannot serve for a past date, so they no longer
    # get per-symbol backfill rows (migration 0008 deleted the existing ones).
    assert len(endpoints) == 7
    assert ArchiveFetchState.Endpoint.MARKET_INDEX_DAILY not in endpoints
    assert ArchiveFetchState.Endpoint.ETF_NAV_DAILY not in endpoints


def test_disabled_codal_is_neither_created_nor_claimed(settings):
    """Dormant means dormant: no new states, and existing ones go unclaimed."""
    from marketdata.archive import claim_archive_batch, ensure_archive_states

    settings.CODAL_ENABLED = False
    ensure_archive_states(stock_symbols=["KAMA"], gold_symbols=["USD"])
    assert not ArchiveFetchState.objects.filter(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS
    ).exists()

    # A state left over from before the flag flipped survives untouched, but is
    # never leased -- it resumes exactly where it was if Codal is switched on.
    stale = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS, symbol="KAMA"
    )
    assert stale.pk not in set(claim_archive_batch(limit=50))
    assert ArchiveFetchState.objects.filter(pk=stale.pk).exists()


def test_disabled_codal_is_not_leased_by_the_maintenance_sweep(settings):
    """The second, easier-to-miss lease path.

    `claim_archive_maintenance` runs from its own unconditional beat entry and
    selects `verified_complete=True` rows -- which is exactly what the 758
    finished Codal states in production are. It bypassed `disabled_endpoints()`
    entirely, so switching Codal off still fetched two of them a day against an
    origin that cannot be reached.
    """
    from marketdata.archive import claim_archive_maintenance

    settings.CODAL_ENABLED = False
    codal = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
        symbol="KAMA", verified_complete=True,
    )
    shareholders = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
        symbol="KAMA", verified_complete=True,
    )
    claimed = set(claim_archive_maintenance(limit=10))
    assert codal.pk not in claimed
    # The other endpoint this sweep exists for is unaffected.
    assert shareholders.pk in claimed


def test_a_disabled_state_already_in_the_broker_is_not_fetched(settings, monkeypatch):
    """Covers the task enqueued before the flag flipped."""
    from marketdata import archive

    settings.CODAL_ENABLED = False
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS, symbol="KAMA",
    )
    monkeypatch.setattr(
        archive, "_fetch_and_ingest",
        lambda *a, **k: pytest.fail("a disabled endpoint must not be fetched"),
    )
    returned = archive.run_archive_state(state.pk)
    # Nothing on the row moved: it resumes exactly where it was.
    state.refresh_from_db()
    assert state.last_attempt_at is None
    # Must return the state like every other exit -- see the next test for why.
    assert returned is not None and returned.pk == state.pk


def test_a_disabled_state_does_not_crash_the_celery_wrapper(settings):
    """The guard must not kill the task it exists to protect.

    `marketdata.tasks.run_archive_state` reads `state.last_error` OUTSIDE its
    try/except, so an early `return` (None) from the archive function raises an
    uncaught AttributeError: the task dies and its WorkflowRun ledger row is
    lost entirely -- not even recorded as failed. Exercising the archive
    function directly cannot catch this, which is how it shipped.

    Not hypothetical: production has 10 suspended Codal states that
    `suspension.claim_probe_batch` re-probes weekly through this exact wrapper.
    """
    from marketdata.models import WorkflowRun
    from marketdata.tasks import run_archive_state as task

    settings.CODAL_ENABLED = False
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
        symbol="KAMA", verified_complete=True,
    )
    task(state.pk)
    assert WorkflowRun.objects.filter(
        workflow="archive_state", symbol="KAMA"
    ).exists(), "the ledger row for this attempt was dropped"


def test_a_suspended_disabled_state_is_not_probed(settings):
    """The third lease path: the weekly recovery probe.

    A subsystem is usually switched off *because* it kept failing, and repeated
    failure is exactly what suspends a state -- so this path is the one most
    likely to be holding a disabled endpoint's rows.
    """
    from marketdata.suspension import claim_probe_batch

    settings.CODAL_ENABLED = False
    codal = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
        symbol="KAMA", suspended_at=timezone.now(), suspension_reason="peer_outlier",
    )
    other = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="KAMA", suspended_at=timezone.now(), suspension_reason="peer_outlier",
    )
    claimed = set(claim_probe_batch(limit=10))
    assert codal.pk not in claimed
    assert other.pk in claimed


def test_archive_state_for_codal_shareholder_and_ticks(settings):
    """We choose an integration test because testing run_archive_state for Codal, Shareholder, and Ticks verifies fetcher response handling and database ingestion boundary logic."""
    settings.CODAL_ENABLED = True
    from marketdata.archive import run_archive_state
    from marketdata.models import CodalAnnouncement, ShareholderRecord, StockTransactionTick

    settings.TSETMC_API_KEY = "test-key"

    codal_state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS,
        symbol="KAMA",
    )
    codal_payload = {
        "announcement": [
            {
                "l18": "KAMA",
                "title": "گزارش مالی",
                "code": "C001",
                "date_publish": "1404-01-01",
                "time_publish": "10:00:00",
            }
        ]
    }
    with patch("marketdata.archive.fetch_codal_announcements", return_value=codal_payload):
        run_archive_state(codal_state.pk)

    assert CodalAnnouncement.objects.filter(symbol="KAMA", code="C001").exists()

    sh_state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
        symbol="KAMA",
    )
    sh_payload = [{"id": 99, "name": "Bank Test", "volume": 1000, "percent": 5.0, "date": "1404-01-01"}]
    with patch("marketdata.archive.fetch_shareholders", return_value=sh_payload):
        run_archive_state(sh_state.pk)

    assert ShareholderRecord.objects.filter(symbol="KAMA", shareholder_id=99).exists()

    tick_state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="KAMA",
    )
    # Ticks cost one request per day, so the worker only asks for days it knows
    # were trading days -- which it learns from already-ingested daily candles.
    # Without a candle there is no calendar and the fetch is refused by design.
    from marketdata import jalali
    from marketdata.models import MarketCandle
    tick_day = jalali.today()
    MarketCandle.objects.create(
        symbol="KAMA", timeframe="1d_unadj", date_time=tick_day, volume=100,
    )
    tick_payload = [{"row": 1, "price": 1500, "volume": 100, "time": "09:30:00", "date": tick_day}]
    with patch("marketdata.archive.fetch_transactions", return_value=tick_payload):
        run_archive_state(tick_state.pk)

    assert StockTransactionTick.objects.filter(symbol="KAMA", row=1).exists()


def test_archive_state_transient_error_reschedules_quickly_then_escalates(settings):
    """First blip still retries in 2 minutes; a persistent one stops hammering.

    The delay used to be a flat 2 minutes with no failure count, so a symbol that
    always timed out consumed a batch slot every 2 minutes indefinitely.
    """
    from marketdata.fetchers import TransientMarketDataError
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST_TRANSIENT",
    )
    with patch("marketdata.archive._fetch_and_ingest", side_effect=TransientMarketDataError("Rate limited", status_code=429)):
        updated_state = run_archive_state(state.pk)

    assert updated_state.consecutive_failures == 1
    assert "Transient rate limit or network error" in updated_state.last_error
    # Unchanged for the first attempt: 2 ** 1 == 2 minutes.
    diff = updated_state.next_attempt_at - updated_state.last_attempt_at
    assert 119 <= diff.total_seconds() <= 121

    with patch("marketdata.archive._fetch_and_ingest", side_effect=TransientMarketDataError("Rate limited", status_code=429)):
        for _ in range(4):
            updated_state = run_archive_state(state.pk)

    assert updated_state.consecutive_failures == 5
    diff = updated_state.next_attempt_at - updated_state.last_attempt_at
    assert diff.total_seconds() == 32 * 60  # 2 ** 5, capped at 60m


def test_archive_stops_only_where_the_live_reserve_begins(settings):
    """With no fixed archive cap, the reserve is the only thing that holds it back."""
    from marketdata.quota import TSETMC

    settings.TSETMC_API_KEY = "test-key"
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 4
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 2
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 2
    ApiRequestQuota.objects.create(day=quota_day(), plan=TSETMC, limit=20)

    # 20 reported - 6 live still spendable = 14 for everything else. The live
    # term is the reserve, not the bare floor: with a whole day left the cadence
    # needs more cycles than the bucket holds, so it saturates at floor+headroom.
    assert remaining_requests(ARCHIVE) == 14
    for _ in range(14):
        reserve_request(ARCHIVE)
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE)

    # The live bucket is still intact -- that is the whole point of the reserve.
    for _ in range(4):
        reserve_request(LIVE)
    assert ApiRequestQuota.objects.get().live_used == 4


def test_quota_error_response_trips_the_breaker_for_that_plan_only(settings):
    """`Stop when the provider says stop` -- the replacement for the hard cap.

    A 500 used to fall straight through to `response.json()` and be returned as
    if it were data, so an exhausted subscription looked like an empty payload.
    """
    from marketdata import quota
    from marketdata.quota import BRS, TSETMC, is_plan_blocked

    with patch("marketdata.fetchers.requests.get") as get:
        get.return_value.status_code = 500
        get.return_value.text = '{"error":"daily request quota exceeded"}'
        get.return_value.json.return_value = {}
        with pytest.raises(QuotaExhausted):
            fetch_json("https://example.test", retries=0, quota_plan=TSETMC)

    assert is_plan_blocked(TSETMC)
    assert not is_plan_blocked(BRS)
    with pytest.raises(QuotaExhausted):
        reserve_request(ARCHIVE, TSETMC)
    reserve_request(ARCHIVE, BRS)  # the other wallet is unaffected


def test_ordinary_server_error_does_not_pause_the_day(settings):
    """A 5xx without a quota message is a bad minute, not an exhausted plan."""
    from marketdata.quota import TSETMC, is_plan_blocked

    with (
        patch("marketdata.fetchers.requests.get") as get,
        patch("marketdata.fetchers.time.sleep"),
    ):
        get.return_value.status_code = 502
        get.return_value.text = "upstream connect error"
        get.return_value.json.return_value = {}
        with pytest.raises(TransientMarketDataError):
            fetch_json("https://example.test", retries=0, quota_plan=TSETMC)

    assert not is_plan_blocked(TSETMC)
    reserve_request(ARCHIVE, TSETMC)


def test_archive_state_permanent_error_exponential_backoff(settings):
    from marketdata.fetchers import PermanentMarketDataError
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST_PERMANENT",
    )
    with patch("marketdata.archive._fetch_and_ingest", side_effect=PermanentMarketDataError("Not found", status_code=404)):
        updated_state = run_archive_state(state.pk)
    
    assert updated_state.consecutive_failures == 1
    assert "PermanentMarketDataError" in updated_state.last_error
    # Should back off exponentially (1 hour for first failure)
    diff = updated_state.next_attempt_at - updated_state.last_attempt_at
    assert 3599 <= diff.total_seconds() <= 3601


# ----------------------------------------------------------------------
# test_rejected_recovery.py


def _row(buy, sell):
    return RejectedRecord.objects.create(
        endpoint="real_legal_history",
        symbol=f"S{buy}{sell}",
        date="1404-01-01",
        reason="buy_sell_volume_mismatch",
        payload={"Buy_I_Volume": buy, "Buy_N_Volume": 0, "Sell_I_Volume": sell, "Sell_N_Volume": 0},
    )


def test_one_percent_reconciliation_boundary_is_recoverable():
    disposition, mismatch, destination = classify(_row(100, 99))
    assert disposition == "recoverable"
    assert Decimal(str(mismatch)) <= Decimal("0.01")
    assert destination == "RealLegalHistory"


def test_material_mismatch_stays_quarantined():
    disposition, _mismatch, destination = classify(_row(100, 98))
    assert disposition == "quarantined"
    assert destination == ""


def test_existing_real_legal_row_is_salvaged_evidence():
    row = _row(100, 99)
    RealLegalHistory.objects.create(symbol=row.symbol, date=row.date)
    assert classify(row)[0] == "salvaged_evidence"


# ----------------------------------------------------------------------
# test_recheck_provider_days.py
# Re-fetch before judging: a third observation arbitrates what two cannot.
# 
# The audit can see that the candle table and the history table disagree, but its
# arbiter is the adjusted candle, and when that matches neither side the row is
# correctly left unrepaired. Asking the provider again supplies the missing vote.


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


# ----------------------------------------------------------------------
# test_resync_symbol.py
# Make a symbol match what the provider serves today.
# 
# کاما's candle table held 8,177 unadjusted rows against 4,936 the provider lists.
# The 3,240 extras all carried a volume of exactly 10,000,000 -- synthetic fill
# that had grown to outnumber the real rows. Arguing row by row is slower and less
# certain than rebuilding the series from the only authority on it.


HISTORY = [
    # `pc` (closing price) is required by validation and is present on every real
    # History.php record; omitting it here rejected the rows as price_not_numeric.
    {"date": "1405-05-11", "pf": 100, "pmax": 110, "pmin": 95, "pl": 105,
     "pc": 105, "tvol": 5000},
    {"date": "1405-05-12", "pf": 105, "pmax": 115, "pmin": 100, "pl": 112,
     "pc": 112, "tvol": 6000},
]
ADJUSTED = {"candle_daily_adjusted": [
    {"date": "1405-05-11", "open": 50, "high": 55, "low": 47, "close": 52, "volume": 5000},
    {"date": "1405-05-12", "open": 52, "high": 57, "low": 50, "close": 56, "volume": 6000},
]}


def _run(**kwargs):
    # apply mode fetches twice and requires agreement, so serve stable data.
    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_daily_history",
        return_value=HISTORY,
    ), patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_candlesticks",
        return_value=ADJUSTED,
    ):
        # The closed-market guard is tested separately; keep these deterministic.
        kwargs.setdefault("force_during_session", True)
        call_command("resync_symbol_from_provider", "کاما", **kwargs)


def test_rows_on_dates_the_provider_never_lists_are_deleted():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-02", close_price=2974, volume=10_000_000,
    )
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-11", close_price=105, volume=5000,
    )

    _run(apply=True)

    days = set(
        MarketCandle.objects.filter(
            symbol="کاما", timeframe=MarketCandle.UNADJUSTED
        ).values_list("date_time", flat=True)
    )
    assert days == {"1405-05-11", "1405-05-12"}


def test_a_wrong_value_on_a_real_date_is_rewritten_not_deleted():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-11", close_price=10.5, volume=5000,  # 10x low
    )

    _run(apply=True)

    row = MarketCandle.objects.get(symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
                                   date_time="1405-05-11")
    assert float(row.close_price) == 105


def test_dry_run_writes_nothing():
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-02", close_price=2974, volume=10_000_000,
    )

    _run()

    assert MarketCandle.objects.filter(date_time="1405-05-02").exists()


def test_a_timeframe_the_provider_does_not_publish_is_left_alone():
    """No adjusted feed must not mean "delete the adjusted series"."""
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time="1399-01-01", close_price=7, volume=100,
    )

    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_daily_history",
        return_value=HISTORY,
    ), patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_candlesticks",
        return_value={},
    ):
        call_command("resync_symbol_from_provider", "کاما", apply=True,
                     force_during_session=True)

    assert MarketCandle.objects.filter(
        timeframe=MarketCandle.ADJUSTED, date_time="1399-01-01"
    ).exists()


def test_daily_history_is_brought_up_to_the_provider():
    _run(apply=True)

    rows = DailyStockHistory.objects.filter(symbol="کاما")
    assert set(rows.values_list("date", flat=True)) == {"1405-05-11", "1405-05-12"}


def test_a_truncated_second_fetch_aborts_instead_of_deleting():
    """A short response is indistinguishable from a shrunken history.

    One flaky call returned 4,506 of 4,509 adjusted rows. Acting on it would have
    deleted three years of real prices, so two fetches must agree first.
    """
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time="1405-05-11", close_price=105, volume=5000,
    )

    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_daily_history",
        side_effect=[HISTORY, HISTORY[:1]],
    ), patch(
        "marketdata.management.commands.resync_symbol_from_provider.fetch_candlesticks",
        return_value=ADJUSTED,
    ):
        with pytest.raises(CommandError, match="truncated"):
            call_command("resync_symbol_from_provider", "کاما", apply=True,
                     force_during_session=True)

    assert MarketCandle.objects.filter(date_time="1405-05-11").exists()


def test_apply_refuses_while_the_session_is_open():
    """The provider serves the day mid-write; consecutive calls disagree."""
    with patch(
        "marketdata.management.commands.resync_symbol_from_provider.market_state."
        "market_state", return_value="open"
    ):
        with pytest.raises(CommandError, match="session is open"):
            call_command("resync_symbol_from_provider", "کاما", apply=True)


# ---------- live is static, leftover is dynamic ------------------------------
#
# The 2026-08-26 production failure: archive spent 10,034 of 10,000 TSETMC
# requests between Tehran midnight and 03:43 with live_used at exactly 0, then
# the breaker tripped and silenced live for the remaining twenty hours. Three
# defects combined, and each gets a test below.


def test_live_reserve_holds_when_provider_has_not_disclosed_a_limit(settings):
    """THE bug: the reserve was gated on `row.limit`, which is 0 on most days.

    `limit` only rides along on provider *error* responses, so on a healthy day
    it is never set -- and `if bucket != LIVE and row.limit:` then skipped the
    live reserve entirely, leaving archive completely unthrottled.
    """
    from marketdata.quota import TSETMC

    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 300
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 0

    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=TSETMC)
    assert row.limit == 0, "precondition: the provider has told us nothing"

    # Open the pacing gate fully so this exercises the reserve gate alone.
    with (
        patch.object(quota, "live_reserve_remaining", return_value=290),
        patch.object(quota, "_day_elapsed_fraction", return_value=1.0),
    ):
        # 300 ceiling - 290 reserved for live = 10 for archive.
        for _ in range(10):
            reserve_request(ARCHIVE, TSETMC)
        with pytest.raises(QuotaExhausted, match="reserved for live"):
            reserve_request(ARCHIVE, TSETMC)


def test_archive_trip_does_not_silence_live(settings):
    """The breaker ran before any bucket distinction, so archive took live down.

    Live has its own reserved headroom; an archive overrun must not spend it.
    A live-triggered trip is different and still stops live.
    """
    from marketdata.quota import (
        ARCHIVE as _A, LIVE as _L, TSETMC, is_plan_blocked, trip_plan_breaker,
    )

    trip_plan_breaker(TSETMC, reason="http_429", bucket=_A)
    assert is_plan_blocked(TSETMC)                      # plan-wide: yes
    assert is_plan_blocked(TSETMC, bucket=_A)           # archive: stopped
    assert not is_plan_blocked(TSETMC, bucket=_L)       # live: still allowed
    reserve_request(_L, TSETMC)                         # and it really can spend
    with pytest.raises(QuotaExhausted):
        reserve_request(_A, TSETMC)

    cache.clear()
    trip_plan_breaker(TSETMC, reason="http_429", bucket=_L)
    assert is_plan_blocked(TSETMC, bucket=_L), "a live 429 must still stop live"


def test_archive_is_paced_across_the_day(settings):
    """Leftover is spread over 24h instead of burned before the market opens."""
    from marketdata.quota import TSETMC, archive_allowance_now, archive_day_ceiling

    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 10_000
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 0
    settings.MARKETDATA_ARCHIVE_BATCH_SIZE = 0
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=TSETMC)
    tehran = ZoneInfo("Asia/Tehran")
    midnight = datetime.datetime(2026, 8, 26, 0, 0, tzinfo=tehran)

    with patch.object(quota, "live_reserve_remaining", return_value=0):
        ceiling = archive_day_ceiling(TSETMC, row, now=midnight)
        assert ceiling == 10_000
        # Pro rata: a quarter of the day buys a quarter of the budget.
        assert archive_allowance_now(TSETMC, row, now=midnight) == 0
        six_am = midnight + datetime.timedelta(hours=6)
        assert archive_allowance_now(TSETMC, row, now=six_am) == 2_500
        six_pm = midnight + datetime.timedelta(hours=18)
        assert archive_allowance_now(TSETMC, row, now=six_pm) == 7_500


def test_live_slice_stays_reserved_after_the_session(settings):
    """The 24h live reservation is static; leftover does not grow after close."""
    from marketdata.quota import TSETMC, archive_day_ceiling

    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 5_000
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 0
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=TSETMC)

    with patch.object(quota, "live_reserve_remaining", return_value=1_200):
        early = archive_day_ceiling(TSETMC, row)
        late = archive_day_ceiling(TSETMC, row)
    assert early == late == 3_800


def test_live_budget_never_exceeds_the_wallet_it_spends(settings):
    """BRS's whole subscription is 1,500; a flat 1,700 live budget cannot bind."""
    from marketdata.quota import BRS, LIVE, TSETMC, bucket_budget

    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 1_200
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 500
    settings.MARKETDATA_PLAN_LIMIT_BRS = 1_500
    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 10_000
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 150

    assert bucket_budget(LIVE, TSETMC) == 1_700      # fits, unchanged
    assert bucket_budget(LIVE, BRS) == 1_350         # clamped to 1500 - 150

def test_live_day_cost_does_not_shrink_in_the_evening(settings):
    """Unit: the 24h live slice is counted from midnight, not from now."""
    from marketdata.quota import TSETMC, live_day_cost, live_reserve_remaining

    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 10_000
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 0
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=TSETMC)
    with (
        patch.object(quota, "_simulate_price_loop", return_value=144),
        patch("marketdata.live_states.full_day_cost", return_value=54),
    ):
        assert live_day_cost(TSETMC, row) == 198
        assert live_reserve_remaining(TSETMC, row) == 198
        row.live_used = 50
        assert live_reserve_remaining(TSETMC, row) == 148

