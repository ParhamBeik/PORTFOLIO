"""Integration tests for serving resilience: concurrency cap, scoped throttling, and ops telemetry."""

from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
import pytest
from rest_framework.settings import api_settings
from rest_framework.test import APIClient

from marketdata.admin_telemetry import _db_connection_metrics, get_ops_overview
from portfolio.models import Account

pytestmark = pytest.mark.django_db


# Integration test: exercises the HTTP request boundary through DRF view dispatch,
# caching layer, and throttle middleware to verify that serving resilience controls
# cooperate without depending on live network infrastructure.
def test_concurrency_cap_rejects_with_429_and_retry_after():
    User = get_user_model()
    user = User.objects.create_user(email="resilience@example.com", password="password123")
    account = Account.objects.create(user=user, name="Resilience Account")

    client = APIClient()
    client.force_authenticate(user=user)

    cache.clear()
    user_key = f"concurrency:analytics:user:{user.id}"
    cache.set(user_key, 2, timeout=60)

    url = reverse("optimization-my-optimal")
    response = client.get(url, {"account": account.id})

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "5"
    assert "Too many concurrent optimization requests" in response.data.get("detail", "")

    cache.clear()


def test_scoped_rate_throttle_enforced_on_analytics():
    User = get_user_model()
    user = User.objects.create_user(email="throttle@example.com", password="password123")
    account = Account.objects.create(user=user, name="Throttle Account")

    client = APIClient()
    client.force_authenticate(user=user)

    cache.clear()
    with patch.dict(api_settings.DEFAULT_THROTTLE_RATES, {"analytics": "1/min"}):
        url = reverse("optimization-my-optimal")
        r1 = client.get(url, {"account": account.id})
        assert r1.status_code in (200, 400)

        r2 = client.get(url, {"account": account.id})
        assert r2.status_code == 429

    cache.clear()


def test_db_connection_metrics_in_ops_telemetry():
    metrics = _db_connection_metrics()
    assert "status" in metrics
    assert "active" in metrics
    assert "max" in metrics
    assert "utilization_pct" in metrics

    overview = get_ops_overview()
    assert "db_connections" in overview
    assert "db_connections" in overview.get("checks", {})
    assert overview["checks"]["db_connections"]["status"] in ("healthy", "warning", "critical", "unknown")


def test_brs_webhook_route_removed():
    client = APIClient()
    response = client.post("/api/marketdata/webhook/brsapi/", {})
    assert response.status_code == 404
