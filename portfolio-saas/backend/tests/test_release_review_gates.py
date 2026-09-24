"""Unit checks for the release-review gates: leftover probes and after-send alerts.

These sit at the component boundary (quota + notify), so they are unit tests:
fast, no HTTP, one invariant each. Pyramid: many of these, few e2e.
"""
from unittest import mock

import pytest
from django.test import override_settings

pytestmark = pytest.mark.django_db


def test_probe_claim_is_silent_when_tsetmc_has_no_leftover(settings):
    from marketdata.models import ArchiveFetchState
    from marketdata.quota import BRS, TSETMC
    from marketdata.suspension import PROBE_INTERVAL, claim_probe_batch
    from django.utils import timezone

    now = timezone.now()
    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="parked",
        suspended_at=now - PROBE_INTERVAL,
        last_probe_at=now - PROBE_INTERVAL,
    )
    with mock.patch(
        "marketdata.quota.archive_capacity",
        return_value={TSETMC: 0, BRS: 400},
    ):
        assert claim_probe_batch(now=now) == []
    parked = ArchiveFetchState.objects.get(symbol="parked")
    assert parked.last_probe_at == now - PROBE_INTERVAL


@override_settings(ALERT_WEBHOOK_URL="https://alerts.test/hook")
def test_failed_alert_send_does_not_burn_dedupe_and_logs_undelivered():
    from config.observability import notify

    with (
        mock.patch(
            "config.observability.requests.post",
            side_effect=ConnectionError("down"),
        ) as post,
        mock.patch("config.observability.logger.warning") as warning,
    ):
        first = notify("retry-alert", {"n": 1}, dedupe_seconds=60)
        second = notify("retry-alert", {"n": 1}, dedupe_seconds=60)
    assert first is False
    assert second is False
    assert post.call_count == 2
    undelivered = [
        call.args for call in warning.call_args_list
        if call.args and str(call.args[0]).startswith("alert:")
    ]
    assert undelivered
    assert all(args[2] == 1 for args in undelivered)


def test_burst_probes_take_at_most_two_tsetmc_slots():
    from marketdata.burst_probes import claim_burst_probes
    from marketdata.quota import BRS, TSETMC

    with (
        mock.patch(
            "marketdata.quota.archive_capacity",
            return_value={TSETMC: 80, BRS: 0},
        ),
        mock.patch(
            "marketdata.suspension.claim_probe_batch",
            return_value=[11, 12],
        ) as claim,
        mock.patch("marketdata.tasks.run_archive_state") as run,
    ):
        run.si.return_value.apply_async = mock.Mock()
        assert claim_burst_probes() == [11, 12]
    claim.assert_called_once_with(limit=2)
    assert run.si.call_count == 2


def test_burst_probes_skip_when_tsetmc_is_empty():
    from marketdata.burst_probes import claim_burst_probes
    from marketdata.quota import BRS, TSETMC

    with (
        mock.patch(
            "marketdata.quota.archive_capacity",
            return_value={TSETMC: 0, BRS: 400},
        ),
        mock.patch("marketdata.suspension.claim_probe_batch") as claim,
    ):
        assert claim_burst_probes() == []
    claim.assert_not_called()


def test_live_success_does_not_clear_an_archive_trip():
    from marketdata.quota import (
        ARCHIVE,
        LIVE,
        TSETMC,
        clear_plan_breaker,
        is_plan_blocked,
        trip_plan_breaker,
    )

    trip_plan_breaker(TSETMC, reason="http_429", bucket=ARCHIVE)
    clear_plan_breaker(TSETMC, bucket=LIVE)
    assert is_plan_blocked(TSETMC, bucket=ARCHIVE)
    clear_plan_breaker(TSETMC, bucket=ARCHIVE)
    assert not is_plan_blocked(TSETMC, bucket=ARCHIVE)


def test_probe_holder_can_retry_without_a_second_admit(settings):
    import time
    from unittest.mock import patch

    from marketdata.quota import BRS, LIVE, is_plan_blocked, trip_plan_breaker

    settings.MARKETDATA_BREAKER_RETRY_SECONDS = 1
    t0 = time.time()
    with patch("marketdata.quota.time.time", return_value=t0):
        trip_plan_breaker(BRS, reason="http_500", bucket=LIVE)
    with patch("marketdata.quota.time.time", return_value=t0 + 2):
        assert not is_plan_blocked(BRS, bucket=LIVE, admit=True)
        assert not is_plan_blocked(BRS, bucket=LIVE, holding_probe=True)
        assert is_plan_blocked(BRS, bucket=LIVE, admit=True)


def test_other_reserve_is_tsetmc_only(settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from marketdata.models import ApiRequestQuota
    from marketdata import quota
    from marketdata.quota import BRS, TSETMC, archive_day_ceiling, other_reserve_remaining

    settings.MARKETDATA_PLAN_LIMIT_BRS = 1500
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 0
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 200
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=BRS)
    assert other_reserve_remaining(BRS, row) == 0
    with mock.patch.object(quota, "live_reserve_remaining", return_value=0):
        assert archive_day_ceiling(BRS, row) == 1500
    morning = datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    assert other_reserve_remaining(TSETMC, None, now=morning) == 200


def test_archive_half_open_does_not_advertise_room_to_the_batcher(settings):
    import time
    from marketdata.quota import (
        ARCHIVE, TSETMC, remaining_requests, trip_plan_breaker,
    )

    settings.MARKETDATA_BREAKER_RETRY_SECONDS = 1
    t0 = time.time()
    with mock.patch("marketdata.quota.time.time", return_value=t0):
        trip_plan_breaker(TSETMC, reason="http_429", bucket=ARCHIVE)
    with mock.patch("marketdata.quota.time.time", return_value=t0 + 2):
        assert remaining_requests(ARCHIVE, TSETMC) == 0
