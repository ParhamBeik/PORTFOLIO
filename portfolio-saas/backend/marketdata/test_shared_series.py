"""The news service can read only curated market observations."""

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@override_settings(NEWS_MARKET_SERVICE_KEY="test-service-key")
def test_shared_series_requires_service_key_and_whitelisted_instrument():
    client = APIClient()
    path = "/api/marketdata/shared-series/"
    assert client.get(path, {"key": "tehran_index"}).status_code == 403
    client.credentials(HTTP_X_NEWS_SERVICE_KEY="test-service-key")
    assert client.get(path, {"key": "portfolio_holdings"}).status_code == 404
    response = client.get(path, {"key": "tehran_index", "days": 7})
    assert response.status_code == 200
    assert response.data["points"] == []
    assert "account" not in str(response.data).lower()


@override_settings(NEWS_MARKET_SERVICE_KEY="test-service-key")
def test_daily_close_is_timestamped_after_the_market_day():
    from datetime import timedelta
    from django.utils import timezone
    from marketdata import jalali
    from marketdata.models import MarketDailyBar

    today = jalali.from_gregorian(timezone.now())
    MarketDailyBar.objects.create(
        asset_class="crypto", symbol="BTC", date=today, close_price=100,
    )
    client = APIClient()
    client.credentials(HTTP_X_NEWS_SERVICE_KEY="test-service-key")
    response = client.get("/api/marketdata/shared-series/", {"key": "bitcoin", "days": 7})
    assert response.status_code == 200
    assert len(response.data["points"]) == 1
    assert response.data["points"][0]["observed_at"] == jalali.to_datetime(today) + timedelta(days=1)
    assert response.data["points"][0]["quality"] == "validated_daily_close"
