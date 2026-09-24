"""Unit: live reserve, archive ceiling, and daily-vs-burst 429.

Pyramid: unit — pure quota arithmetic and one HTTP classification branch.
"""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import requests

from marketdata.fetchers import TransientMarketDataError, fetch_json
from marketdata.models import ApiRequestQuota
from marketdata import quota
from marketdata.quota import (
    BRS,
    QuotaExhausted,
    TSETMC,
    archive_day_ceiling,
    is_daily_quota_exhaustion,
    is_plan_blocked,
    live_day_cost,
    live_reserve_remaining,
)

pytestmark = pytest.mark.django_db


def test_billing_product_matches_isolated_provider_counter_matrix():
    from marketdata.endpoints import billing_product

    assert billing_product("market_index", {"type": 1}) == TSETMC
    assert billing_product("codal_announcements", {}) == TSETMC
    assert billing_product("gold_currency_pro", {}) == TSETMC
    assert billing_product("gold_currency_history", {"history": 1}) == TSETMC
    assert billing_product("gold_currency_history", {"history": 2}) == TSETMC


def test_provider_reconciliation_keeps_local_attribution_separate():
    from marketdata.quota import get_quota_status, reconcile_account

    row = ApiRequestQuota.objects.create(
        day=quota.quota_day(), plan=TSETMC, used=4, local_attempts=4,
        successful_requests=3, archive_used=2, live_used=1, other_used=1,
    )
    reconcile_account({"usage_today": 7, "limit": 10_000}, TSETMC)
    row.refresh_from_db()
    assert (row.archive_used, row.live_used, row.other_used) == (2, 1, 1)
    assert (row.local_attempts, row.successful_requests, row.provider_used) == (4, 3, 7)
    assert get_quota_status()["plans"][TSETMC]["provider_variance"] == 3


def test_live_day_cost_uses_simulated_spend_on_a_spending_day(settings):
    settings.MARKETDATA_LIVE_REQUEST_FLOOR = 1_200
    settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 500
    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 10_000
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 150
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=TSETMC)
    with (
        patch("marketdata.quota._simulate_price_loop", return_value=297),
        patch("marketdata.live_states.full_day_cost", return_value=0),
    ):
        assert live_day_cost(TSETMC, row) == 297
        assert live_reserve_remaining(TSETMC, row) == 297


def test_tsetmc_archive_ceiling_uses_provider_request_units(settings):
    from marketdata import quota

    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 10_000
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 150
    settings.MARKETDATA_OTHER_REQUEST_BUDGET = 200
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=TSETMC)
    morning = datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    with patch.object(quota, "live_reserve_remaining", return_value=1_200):
        leftover = 10_000 - 150 - 1_200 - 200
        assert archive_day_ceiling(TSETMC, row, now=morning) == leftover


def test_brs_archive_ceiling_uses_provider_request_units(settings):
    from marketdata import quota

    settings.MARKETDATA_PLAN_LIMIT_BRS = 1_500
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 150
    row = ApiRequestQuota.objects.create(day=quota.quota_day(), plan=BRS)
    with patch.object(quota, "live_reserve_remaining", return_value=1_200):
        assert archive_day_ceiling(BRS, row) == 1_500 - 150 - 1_200


def test_is_daily_quota_exhaustion_distinguishes_burst_from_empty(settings):
    settings.MARKETDATA_PLAN_SAFETY_MARGIN = 150
    settings.MARKETDATA_PLAN_LIMIT_BRS = 1_500
    settings.MARKETDATA_PLAN_LIMIT_TSETMC = 10_000
    assert is_daily_quota_exhaustion({"usage_today": 77, "limit": 1500}, BRS) is False
    assert is_daily_quota_exhaustion({"usage_today": 10058, "limit": 10000}, TSETMC) is True
    assert is_daily_quota_exhaustion(None, BRS) is True


def _quota_http(status, text, payload):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    response.headers["Content-Type"] = "application/json"
    response.json = lambda: payload
    return response


def test_quota_shaped_429_with_budget_left_does_not_trip_breaker(settings):
    payload = {"error": "too many requests limit", "account": {"usage_today": 77, "limit": 1500}}
    body = '{"error":"too many requests limit","account":{"usage_today":77,"limit":1500}}'
    with patch("marketdata.fetchers.requests.get", return_value=_quota_http(429, body, payload)):
        with pytest.raises(TransientMarketDataError):
            fetch_json("https://example.test", retries=0, quota_plan=BRS)
    assert not is_plan_blocked(BRS)


def test_quota_shaped_429_at_the_ceiling_still_trips(settings):
    payload = {"error": "daily request quota exceeded", "account": {"usage_today": 10058, "limit": 10000}}
    body = '{"error":"daily request quota exceeded","account":{"usage_today":10058,"limit":10000}}'
    with patch("marketdata.fetchers.requests.get", return_value=_quota_http(429, body, payload)):
        with pytest.raises(QuotaExhausted):
            fetch_json("https://example.test", retries=0, quota_plan=TSETMC)
    assert is_plan_blocked(TSETMC)
