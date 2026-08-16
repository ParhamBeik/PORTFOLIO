"""Staff-only /api/admin/* operations endpoints."""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from marketdata.models import ArchiveFetchState, SystemLogEvent, WorkflowRun


pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email="ops-admin@example.com",
        password="x",
        is_staff=True,
        is_superuser=True,
    )


@pytest.fixture
def free_user(db):
    return User.objects.create_user(email="ops-user@example.com", password="x")


def _auth(client, user):
    client.force_authenticate(user=user)
    return client


def test_admin_overview_requires_staff(free_user, staff_user):
    client = APIClient()
    assert client.get("/api/admin/overview/").status_code in (401, 403)
    _auth(client, free_user)
    assert client.get("/api/admin/overview/").status_code == 403
    _auth(client, staff_user)
    res = client.get("/api/admin/overview/")
    assert res.status_code == 200
    body = res.json()
    assert "generated_at" in body
    assert body["database_counts"]["approximate"] is True
    assert "checks" in body


def test_admin_overview_refresh_bypasses_cache(staff_user, monkeypatch):
    from django.core.cache import cache
    from marketdata.admin_telemetry import OVERVIEW_CACHE_KEY

    cache.clear()
    calls = {"n": 0}
    # Count the EXPENSIVE build, not the wrapper. AdminOverviewView calls
    # get_ops_overview() on every request by design -- the caching lives inside
    # it -- so patching the wrapper counts requests and can never show a cache
    # hit. get_admin_telemetry_context is what a cache miss actually runs.
    import marketdata.admin_telemetry as telemetry

    real = telemetry.get_admin_telemetry_context

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(telemetry, "get_admin_telemetry_context", counting)
    client = _auth(APIClient(), staff_user)
    res1 = client.get("/api/admin/overview/")
    assert res1.status_code == 200
    assert calls["n"] == 1
    assert cache.get(OVERVIEW_CACHE_KEY) is not None
    res2 = client.get("/api/admin/overview/")
    assert res2.status_code == 200
    assert calls["n"] == 1
    res3 = client.get("/api/admin/overview/?refresh=1")
    assert res3.status_code == 200
    assert calls["n"] == 2


def test_admin_workflows_filter_and_pagination(staff_user):
    WorkflowRun.objects.create(workflow="archive", outcome="success", endpoint="stock_candle_adjusted")
    WorkflowRun.objects.create(workflow="archive", outcome="failed", endpoint="stock_transaction_ticks")
    client = _auth(APIClient(), staff_user)
    res = client.get("/api/admin/workflows/?outcome=failed")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["results"][0]["outcome"] == "failed"


def test_admin_archive_retry_requires_confirm_and_audits(staff_user, monkeypatch):
    state = ArchiveFetchState.objects.create(
        symbol="کاما",
        endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
        consecutive_failures=3,
        stored_rows=0,
        expected_rows=10,
        missing_rows=10,
    )
    calls = []

    class DummyTask:
        def delay(self, state_id):
            calls.append(state_id)

    monkeypatch.setattr("marketdata.tasks.retry_archive_job_task", DummyTask())
    monkeypatch.setattr(
        "marketdata.admin_api.get_quota_status",
        lambda: {"limit": 100, "used": 1, "remaining": 99},
    )

    class FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr("redis.Redis.from_url", lambda url: FakeRedis())

    client = _auth(APIClient(), staff_user)
    bad = client.post("/api/admin/archive-states/retry/", {"ids": [state.id], "confirm": False}, format="json")
    assert bad.status_code == 400
    ok = client.post("/api/admin/archive-states/retry/", {"ids": [state.id], "confirm": True}, format="json")
    assert ok.status_code == 200
    assert state.id in ok.json()["queued"]
    assert calls == [state.id]
    assert SystemLogEvent.objects.filter(category="ops_retry").exists()
    assert WorkflowRun.objects.filter(workflow="ops_retry").exists()


def test_admin_overview_includes_fill_completeness_disk(staff_user, monkeypatch):
    from django.core.cache import cache
    from datetime import timedelta
    from django.utils import timezone
    from marketdata.models import OperationalMetricSnapshot

    cache.clear()
    now = timezone.now()
    OperationalMetricSnapshot.objects.create(
        captured_at=now - timedelta(hours=25),
        database_counts={"prices": 10, "candles": 100},
        table_bytes={"prices": 1000, "candles": 5000},
        disk={"database_bytes": 1_000_000, "codal_bytes": 0},
    )
    ArchiveFetchState.objects.create(
        symbol="A",
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
        verified_complete=True,
        stored_rows=10,
        expected_rows=10,
    )
    ArchiveFetchState.objects.create(
        symbol="B",
        endpoint=ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
        verified_complete=False,
        stored_rows=1,
        expected_rows=10,
        missing_rows=9,
    )
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome="success",
        destination_table="MarketCandle",
        rows_accepted=12,
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_db_counts",
        lambda: {"prices": 40, "candles": 130},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_table_bytes",
        lambda: {"prices": 4000, "candles": 8000},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_database_bytes",
        lambda: 2_000_000,
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._workers",
        lambda: {"status": "healthy", "items": {"live@x": {"status": "online"}}, "message": ""},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._queues",
        lambda: {"status": "healthy", "depths": {"live": 0, "archive": 2, "codal": 0}, "message": ""},
    )

    client = _auth(APIClient(), staff_user)
    res = client.get("/api/admin/overview/")
    assert res.status_code == 200
    body = res.json()
    assert body["archive"]["progress_pct"] == 50.0
    hist = next(c for c in body["archive"]["categories"] if c["endpoint"] == "stock_history_unadjusted")
    assert hist["complete"] == 1 and hist["total"] == 2
    assert body["fill_rates"]["prices"]["delta_24h"] == 30
    assert body["disk"]["budget_gb"] == 250
    assert body["queues"]["depths"]["archive"] == 2
    assert body["codal"]["enabled"] in (True, False)
    assert body["workflow_15m"]["rows_accepted"] >= 12
    dests = {row["destination_table"] for row in body["workflow_15m"]["by_destination"]}
    assert "MarketCandle" in dests
    assert body["workers"]["summary"]["online"] == 1
    assert "coverage" in body
    assert "live" in body["coverage"]
    assert "warehouse" in body["coverage"]
    assert set(body["coverage"]["warehouse"]["counts"].keys()) == {"complete", "partial", "failed", "not_tried"}


def test_pipelines_write_workflow_runs(settings, monkeypatch):
    from marketdata.tasks import archive_tick, capture_operational_metrics

    monkeypatch.setattr("marketdata.admin_telemetry.get_cached_db_counts", lambda: {"prices": 1})
    monkeypatch.setattr("marketdata.admin_telemetry.get_cached_table_bytes", lambda: {"prices": 1})
    monkeypatch.setattr("marketdata.admin_telemetry.get_database_bytes", lambda: 1)
    monkeypatch.setattr(
        "marketdata.admin_telemetry._workers",
        lambda: {"status": "unknown", "items": {}, "message": ""},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._queues",
        lambda: {"status": "unknown", "depths": {}, "message": ""},
    )
    capture_operational_metrics()
    assert WorkflowRun.objects.filter(workflow="capture_operational_metrics").exists()

    monkeypatch.setattr("marketdata.quota.remaining_requests", lambda bucket: 0)
    archive_tick()
    assert WorkflowRun.objects.filter(workflow="archive_tick", outcome="skipped").exists()


def test_fake_ingest_visible_on_overview_without_logs(staff_user, monkeypatch):
    """Ops contract: an accepted batch shows up on overview JSON, not in docker logs."""
    from django.core.cache import cache

    cache.clear()
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome="success",
        endpoint="stock_candle_adjusted",
        symbol="کاما",
        destination_table="MarketCandle",
        rows_accepted=42,
        rows_received=42,
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_db_counts",
        lambda: {"candles": 100},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry.get_cached_table_bytes",
        lambda: {"candles": 1000},
    )
    monkeypatch.setattr("marketdata.admin_telemetry.get_database_bytes", lambda: 1000)
    monkeypatch.setattr(
        "marketdata.admin_telemetry._workers",
        lambda: {"status": "healthy", "items": {"w": {"status": "online"}}, "message": ""},
    )
    monkeypatch.setattr(
        "marketdata.admin_telemetry._queues",
        lambda: {"status": "healthy", "depths": {"live": 0, "archive": 0, "codal": 0}, "message": ""},
    )

    client = _auth(APIClient(), staff_user)
    body = client.get("/api/admin/overview/").json()
    assert body["workflow_15m"]["rows_accepted"] >= 42
    assert any(
        row["destination_table"] == "MarketCandle" and row["accepted"] >= 42
        for row in body["workflow_15m"]["by_destination"]
    )
    assert body["last_success"].get("archive_state")


def test_admin_assets_list_filter_and_search(staff_user):
    from portfolio.models import Asset

    Asset.objects.create(key="kama_stock", name="Kama", name_fa="کاما", asset_class="Stock", tse_symbol="کاما", is_manual=True)
    Asset.objects.create(key="emami_coin", name="Emami", asset_class="Gold", brs_symbol="IR_COIN_EMAMI", is_manual=True)
    client = _auth(APIClient(), staff_user)
    res = client.get("/api/admin/assets/?asset_class=Stock&search=کاما")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["results"][0]["key"] == "kama_stock"
    assert body["results"][0]["tse_symbol"] == "کاما"
    assert "asset_classes" in body
