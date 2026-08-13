from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from marketdata.models import (
    ArchiveFetchState,
    OperationalMetricSnapshot,
    RejectedRecord,
    SymbolIntegrity,
    SystemLogEvent,
    WorkflowRun,
)
from marketdata.tasks import capture_operational_metrics


pytestmark = pytest.mark.django_db


def test_capture_operational_metrics_is_idempotent_and_retains_90_days(monkeypatch):
    old = OperationalMetricSnapshot.objects.create(
        captured_at=timezone.now() - timedelta(days=91),
        database_counts={"prices": 1},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_db_counts",
        lambda: {"prices": 7},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_table_bytes",
        lambda: {"prices": 1},
    )
    monkeypatch.setattr("marketdata.admin_telemetry.get_database_bytes", lambda: 1)
    monkeypatch.setattr(
        "marketdata.admin_telemetry._workers",
        lambda: {"status": "unknown", "items": {}, "message": ""},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._queues",
        lambda: {"status": "unknown", "depths": {}, "message": ""},
    )

    first = capture_operational_metrics()
    second = capture_operational_metrics()

    assert first == second
    assert not OperationalMetricSnapshot.objects.filter(pk=old.pk).exists()
    snapshot = OperationalMetricSnapshot.objects.get(pk=first)
    assert snapshot.captured_at.minute % 15 == 0
    assert snapshot.captured_at.second == 0
    assert snapshot.database_counts == {"prices": 7}
    assert "table_bytes" in snapshot.__dict__


def test_admin_dashboard_requires_staff(make_user):
    client = Client()
    assert client.get("/admin/").status_code == 302
    client.force_login(make_user())
    assert client.get("/admin/").status_code in (302, 403)


def test_admin_dashboard_renders_operational_history(make_user, monkeypatch):
    cache.clear()
    user = make_user()
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    now = timezone.now()
    OperationalMetricSnapshot.objects.create(
        captured_at=now - timedelta(days=1),
        database_counts={"prices": 10, "stock_history_rows": 20},
    )
    WorkflowRun.objects.create(workflow="archive", outcome="success", rows_accepted=5)
    SystemLogEvent.objects.create(
        level="ERROR", category="test", logger_name="test", message="provider failed"
    )
    RejectedRecord.objects.create(endpoint="history", symbol="TEST", reason="bad_date")
    SymbolIntegrity.objects.create(symbol="TEST", passes_gate=False, reason="coverage")
    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_ADJUSTED,
        symbol="TEST",
        expected_rows=10,
        stored_rows=5,
        missing_rows=5,
        consecutive_failures=5,
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_db_counts",
        lambda: {"prices": 12, "stock_history_rows": 25},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._workers",
        lambda: {"status": "critical", "items": {}, "message": "offline"},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._queues",
        lambda: {"status": "unknown", "depths": {}, "message": "unavailable"},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_quota_status",
        lambda: {
            "limit": 100,
            "used": 25,
            "remaining_daily": 75,
            "quota_history": [{"day": "2026-08-13", "limit": 100, "used": 25}],
        },
    )

    client = Client()
    client.force_login(user)
    response = client.get("/admin/")

    assert response.status_code == 200
    assert b"Operations dashboard" in response.content
    assert b"Archive fill progress" in response.content
    assert b"Database size and 90-day growth" in response.content
    assert b"provider failed" in response.content
    assert b'"prices": 10' in response.content
    assert b"refreshes every 30 seconds while visible" in response.content
    assert b"Codal extraction" in response.content


def test_prune_prices_keeps_latest_and_ledgers(settings, make_user, asset_catalog, write_prices):
    from portfolio.models import Price
    from portfolio.services.maintenance import prune_prices

    settings.PRICE_PRUNE_ENABLED = True
    settings.PRICE_RETENTION_DAYS = 7
    write_prices({"emami_coin": 100})
    old = Price.objects.get(asset=asset_catalog["emami_coin"])
    old.fetched_at = timezone.now() - timedelta(days=20)
    old.save(update_fields=["fetched_at"])
    write_prices({"emami_coin": 200})
    assert Price.objects.filter(asset=asset_catalog["emami_coin"]).count() == 2

    result = prune_prices()
    assert result["enabled"] is True
    assert Price.objects.filter(asset=asset_catalog["emami_coin"]).count() == 1
    assert Price.objects.get(asset=asset_catalog["emami_coin"]).price == 200
    assert WorkflowRun.objects.filter(workflow="prune_prices").exists()
