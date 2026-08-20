"""PriceFeedView: the dead-man's switch the watchdog cron and the GitHub
Actions probe both poll. Must not cry wolf every night during OVERNIGHT, when
zero live jobs run by design (see marketdata.market_state.live_job_keys).
"""
from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone

from config.health import PriceFeedView
from portfolio.models import Price

pytestmark = pytest.mark.django_db


def _get(rf):
    return PriceFeedView.as_view()(rf.get("/api/health/prices/"))


def test_stale_price_during_open_hours_is_reported_stale(asset_catalog, write_prices, monkeypatch):
    write_prices({"emami_coin": 500000000})
    Price.objects.update(fetched_at=timezone.now() - timedelta(minutes=30))
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "open")

    response = _get(RequestFactory())
    assert response.status_code == 503
    assert response.data["status"] == "stale"


def test_stale_price_overnight_is_not_reported_stale(asset_catalog, write_prices, monkeypatch):
    """No live job runs OVERNIGHT (marketdata.market_state.live_job_keys), so an
    old price then is the correct current price, not a broken feed -- the
    watchdog must not restart Celery every night for this.
    """
    write_prices({"emami_coin": 500000000})
    Price.objects.update(fetched_at=timezone.now() - timedelta(hours=8))
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")

    response = _get(RequestFactory())
    assert response.status_code == 200
    assert response.data["status"] == "fresh"


def test_fresh_price_is_reported_fresh_regardless_of_state(asset_catalog, write_prices, monkeypatch):
    write_prices({"emami_coin": 500000000})
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    response = _get(RequestFactory())
    assert response.status_code == 200
    assert response.data["status"] == "fresh"
