"""Peer-relative outlier suspension for ArchiveFetchState.

Detection (`_outlier_candidates`) is pure -- no DB -- so those cases are plain
unit tests. Recovery/blacklist/probe-selection need real rows since they are
queryset filters, so those are thin integration tests against Postgres.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from marketdata.models import ArchiveFetchState
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

Endpoint = ArchiveFetchState.Endpoint


def _state(symbol, endpoint=Endpoint.STOCK_HISTORY_ADJUSTED, **kwargs):
    return ArchiveFetchState.objects.create(symbol=symbol, endpoint=endpoint, **kwargs)


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
