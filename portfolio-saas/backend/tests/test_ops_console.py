"""The staff-only Ops surface: admin APIs, dashboard telemetry, the workflow ledger, and alerting.

Merged from 8 files; each section keeps its original banner.
"""

from datetime import timedelta
import datetime as dt
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
import jdatetime
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from marketdata.models import (
    ArchiveFetchState,
    OperationalMetricSnapshot,
    RejectedRecord,
    SymbolIntegrity,
    SystemLogEvent,
    WorkflowRun,
)
from marketdata.models import ArchiveFetchState, SystemLogEvent, WorkflowRun
from marketdata.models import GoldCurrencyHistory, ArchiveFetchState
from marketdata.models import GoldCurrencyHistory, MarketInstrument
from marketdata.models import WorkflowRun
from marketdata.tasks import _retry_code, prune_workflow_runs
from marketdata.tasks import capture_operational_metrics
from marketdata.workflows import WorkflowOutcome
from portfolio.models import Account, Asset, Holding, Liability
from portfolio.models import Account, Holding
from portfolio.views import AdminCleanPricesExecuteView

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_admin_ops_api.py
# Staff-only /api/admin/* operations endpoints.


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


def test_integrity_endpoint_requires_staff(free_user, staff_user):
    client = _auth(APIClient(), free_user)
    assert client.get("/api/integrity/").status_code == 403
    client = _auth(APIClient(), staff_user)
    assert client.get("/api/integrity/").status_code == 200


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


def test_cached_overview_still_reports_live_health(staff_user):
    """A cache hit must not serve a stale "all healthy".

    The overview payload is cached for 15 minutes so the page loads instantly,
    which would otherwise let it insist the price feed is fine a quarter of an
    hour after the feed died -- the one thing an ops console must never do.
    `live_health_overlay` is what keeps the is-it-broken-now signals current on
    every request, and this fails if someone folds it back into the cache.
    """
    from django.core.cache import cache
    from marketdata.admin_telemetry import OVERVIEW_CACHE_KEY

    cache.clear()
    client = _auth(APIClient(), staff_user)

    first = client.get("/api/admin/overview/")
    assert first.status_code == 200
    assert cache.get(OVERVIEW_CACHE_KEY) is not None
    first_generated = first.json()["generated_at"]

    # Served from cache now: the expensive sections must not be rebuilt, but the
    # health block must still be freshly computed.
    second = client.get("/api/admin/overview/")
    assert second.status_code == 200
    assert second.json()["generated_at"] != first_generated, (
        "generated_at came from the cache, so the health block is stale too"
    )
    for key in ("checks", "price_feed", "status", "overall_status", "queue"):
        assert key in second.json(), f"live health key {key!r} missing from a cached response"


def test_hypertable_lookup_failure_is_never_cached(monkeypatch):
    """A transient failure must not pin "there are no hypertables" for 5 minutes.

    While that empty set was cached, every row count read the hypertable
    *parent*, which genuinely holds zero rows -- so a 41.6M-row tick table
    charted as a collapse to zero and back. That is the phantom gap the
    warehouse-growth chart showed on 2026-08-16/17.
    """
    from django.core.cache import cache
    import marketdata.admin_telemetry as telemetry

    cache.clear()

    class Boom:
        def cursor(self):
            raise RuntimeError("timescaledb_information is unavailable")

    monkeypatch.setattr(telemetry, "connection", Boom())
    assert telemetry._hypertables() == set()
    assert cache.get("admin_hypertable_names") is None, (
        "a failed hypertable lookup was cached; row counts will read the "
        "empty parent relation until it expires"
    )


def test_row_counts_never_record_an_unconfirmed_zero(staff_user):
    """A zero estimate must be confirmed by a real count before it is reported.

    Analysing the table while it is empty and inserting afterwards reproduces
    the production shape exactly: a stale statistic that says zero about a
    table that has rows. Trusting it is what charted 41.6M ticks as a gap.
    """
    from django.core.cache import cache
    from django.db import connection
    from marketdata.admin_telemetry import get_cached_db_counts
    from marketdata.models import StockTransactionTick

    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {StockTransactionTick._meta.db_table}")
        cursor.execute(
            "SELECT reltuples FROM pg_class WHERE relname = %s",
            [StockTransactionTick._meta.db_table],
        )
        assert cursor.fetchone()[0] == 0, "expected a zero estimate to test against"

    StockTransactionTick.objects.create(
        symbol="کاما", date="1405-05-28", row=1, time="09:00:00",
        price=Decimal("3000"), volume=10,
    )
    cache.clear()
    counts = get_cached_db_counts()
    assert counts["stock_transaction_ticks"] == 1, (
        "a table with rows reported zero; the estimate was trusted without "
        "falling back to a real count"
    )


def test_repair_metric_zeros_removes_only_bracketed_readings():
    """Integration test: it is the command's ORM read/write boundary under test,
    not a pure function, and the whole point is which rows it leaves alone."""
    from django.core.management import call_command

    base = timezone.now() - timedelta(hours=5)
    rows = [
        {"ticks": 0, "candles": 0},        # before either table had rows
        {"ticks": 100, "candles": 0},
        {"ticks": 0, "candles": 0},        # phantom: bracketed by 100 and 200
        {"ticks": 200, "candles": 0},
    ]
    for offset, counts in enumerate(rows):
        OperationalMetricSnapshot.objects.create(
            captured_at=base + timedelta(hours=offset), database_counts=counts
        )

    call_command("repair_metric_zeros", "--apply")

    stored = list(
        OperationalMetricSnapshot.objects.order_by("captured_at")
        .values_list("database_counts", flat=True)
    )
    assert "ticks" not in stored[2], "the bracketed phantom zero was not removed"
    assert stored[0]["ticks"] == 0, "a leading zero is a table that was genuinely empty"
    assert all(row["candles"] == 0 for row in stored), (
        "a table that is empty throughout was rewritten; only bracketed zeros are false"
    )


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

    monkeypatch.setattr("marketdata.quota.remaining_requests", lambda bucket=None, plan=None: 0)
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


# ----------------------------------------------------------------------
# test_admin_dashboard.py


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
    from portfolio.tasks import prune_prices

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


# ----------------------------------------------------------------------
# test_admin_clean_prices_api.py
# API test for the destructive admin price-cleanup endpoint's confirm-phrase gate.
# 
# Integration test (real HTTP request through DRF, real permission classes): the
# thing under test IS the boundary — whether an unauthenticated/non-staff caller
# or a bare POST without the confirm phrase can trigger irreversible deletes —
# so this has to run through the actual view/URL/permission stack, not the bare
# Python function.


def _staff_client(make_user):
    user = make_user(email="staffadmin@test.test")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_execute_without_confirm_phrase_is_rejected(make_user, asset_catalog):
    client = _staff_client(make_user)
    resp = client.post("/api/admin/clean-prices/execute/", {}, format="json")
    assert resp.status_code == 400


def test_execute_with_wrong_confirm_phrase_is_rejected(make_user, asset_catalog):
    client = _staff_client(make_user)
    resp = client.post(
        "/api/admin/clean-prices/execute/", {"confirm": "delete mispriced data"}, format="json"
    )
    assert resp.status_code == 400


def test_execute_with_correct_confirm_phrase_succeeds(make_user, asset_catalog):
    client = _staff_client(make_user)
    resp = client.post(
        "/api/admin/clean-prices/execute/",
        {"confirm": AdminCleanPricesExecuteView.CONFIRM_PHRASE},
        format="json",
    )
    assert resp.status_code == 200
    assert "purged_snapshots" in resp.data


def test_execute_rejects_non_staff_user(make_user, asset_catalog):
    user = make_user(email="regular@test.test")
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.post(
        "/api/admin/clean-prices/execute/",
        {"confirm": AdminCleanPricesExecuteView.CONFIRM_PHRASE},
        format="json",
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# test_data_quality_api.py


def test_account_data_quality_is_windowed_and_account_scoped(
    asset_catalog, make_user
):
    owner = make_user("quality-owner@test.test")
    intruder = make_user("quality-intruder@test.test")
    account = Account.objects.create(user=owner, name="Quality")
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "EMAMI"
    asset.save(update_fields=["brs_symbol"])
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("1"))
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS,
        symbol="EMAMI",
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )
    today = dt.date.today()
    jalali = jdatetime.date.fromgregorian(date=today)
    GoldCurrencyHistory.objects.create(
        symbol="EMAMI",
        date=f"{jalali.year:04d}-{jalali.month:02d}-{jalali.day:02d}",
        close_price=100,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(
        f"/api/accounts/{account.id}/data-quality/",
        {"from": (today - dt.timedelta(days=4)).isoformat(), "to": today.isoformat()},
    )
    client.force_authenticate(user=intruder)
    denied = client.get(f"/api/accounts/{account.id}/data-quality/")

    assert response.status_code == 200, response.data
    assert response.data["account_id"] == account.id
    # The series starts today, so there are no earlier sessions it is *missing*
    # -- it simply has no history yet. Scoring the four days before its first
    # print as a coverage failure is what dropped 537 clean-but-newly-listed
    # symbols out of the universe. The shortfall is reported as history_start /
    # leading_gap_sessions, and "not enough observations to model" is the
    # returns matrix's call (`insufficient_history`), not the integrity gate's.
    asset_quality = response.data["assets"][0]
    assert asset_quality["observed_sessions"] == 1
    assert asset_quality["expected_sessions"] == 1
    assert asset_quality["leading_gap_sessions"] == 4
    assert asset_quality["history_start"] == today.isoformat()
    assert asset_quality["reason_codes"] == []
    assert denied.status_code == 404


# ----------------------------------------------------------------------
# test_operations.py


def test_request_id_is_accepted_or_generated():
    accepted = APIClient().get(
        "/api/health/", HTTP_X_REQUEST_ID="beta-request-123"
    )
    generated = APIClient().get("/api/health/")

    assert accepted["X-Request-ID"] == "beta-request-123"
    assert generated["X-Request-ID"]
    assert generated["X-Request-ID"] != accepted["X-Request-ID"]


@override_settings(ALERT_WEBHOOK_URL="https://alerts.test/hook")
def test_alert_notifier_redacts_and_deduplicates():
    from config.observability import notify

    with mock.patch("config.observability.requests.post") as post:
        first = notify(
            "test-alert",
            {"password": "secret", "nested": {"authorization": "Bearer token"}},
            dedupe_seconds=60,
        )
        second = notify(
            "test-alert",
            {"password": "secret", "nested": {"authorization": "Bearer token"}},
            dedupe_seconds=60,
        )

    assert first is True
    assert second is False
    payload = post.call_args.kwargs["json"]["details"]
    assert payload["password"] == "[redacted]"
    assert payload["nested"]["authorization"] == "[redacted]"
    assert post.call_args.kwargs["timeout"] == 5


def test_sentry_disabled_does_not_import_or_send():
    from config.observability import init_sentry

    with mock.patch.dict("sys.modules", {"sentry_sdk": None}):
        assert init_sentry("") is False


# ----------------------------------------------------------------------
# test_workflow_ledger.py
# The ledger must say *why* a job retried, and must not grow forever.


@pytest.mark.parametrize(
    "last_error,expected",
    [
        # The three that account for 263 of 773 observed retries.
        ("Transient rate limit or network error: Provider request failed "
         "(ReadTimeout, status=None).", "ReadTimeout"),
        ("Transient rate limit or network error: Provider request failed "
         "(SSLError, status=None).", "SSLError"),
        ("Transient rate limit or network error: Provider request failed "
         "(ConnectionError, status=None).", "ConnectionError"),
        ("MarketDataFetchError: 1405-05-11: tick_volume_mismatch:38214512!=38014512",
         "tick_volume_mismatch"),
        ("MarketDataFetchError: Payload parsed to zero verifiable keys; parser and "
         "payload disagree.", "MarketDataFetchError"),
    ],
)
def test_retry_code_names_the_cause(last_error, expected):
    """A constant `archive_fetch_retry` made 44% of the ledger unqueryable."""
    assert _retry_code(last_error) == expected


def test_retry_code_never_returns_empty():
    """error_code is the grouping key; an empty one silently merges causes."""
    assert _retry_code("") == "archive_fetch_retry"


def test_wedged_states_alert_and_name_their_cause(settings):
    """`stale-archive-progress` only fires when the whole archive goes quiet.

    59 symbols that could never converge sat failing for days inside a busy,
    healthy-looking archive because nothing watched individual states.
    """
    from marketdata.models import ArchiveFetchState
    from marketdata.tasks import operational_health_check

    settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD = 6
    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="wedged", consecutive_failures=7,
        last_error="MarketDataFetchError: 1405-05-11: tick_volume_mismatch:5!=9",
    )
    # Ordinary provider flakiness must stay below the bar.
    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="just-flaky", consecutive_failures=2,
        last_error="Transient rate limit or network error: Provider request "
                   "failed (ReadTimeout, status=None).",
    )

    with patch("config.observability.notify") as notify:
        operational_health_check()

    fired = {call.args[0]: call.args[1] for call in notify.call_args_list}
    assert fired["wedged-archive-states"]["count"] == 1
    assert fired["wedged-archive-states"]["causes"] == {"tick_volume_mismatch": 1}


def test_healthy_archive_raises_no_wedged_alert(settings):
    from marketdata.models import ArchiveFetchState
    from marketdata.tasks import operational_health_check

    settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD = 6
    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="fine", consecutive_failures=1,
    )

    with patch("config.observability.notify") as notify:
        operational_health_check()

    assert "wedged-archive-states" not in {c.args[0] for c in notify.call_args_list}


def test_prune_keeps_inside_window_and_drops_outside():
    now = timezone.now()
    for age_days, workflow in ((31, "old"), (29, "recent")):
        run = WorkflowRun.objects.create(workflow=workflow, outcome="success")
        # created_at is auto_now_add, so age it after the fact.
        WorkflowRun.objects.filter(pk=run.pk).update(
            created_at=now - timedelta(days=age_days)
        )

    assert prune_workflow_runs() == 1
    assert list(WorkflowRun.objects.values_list("workflow", flat=True)) == ["recent"]


# ----------------------------------------------------------------------
# test_workflow_logging.py


def test_workflow_stdout_is_a_compact_human_summary():
    with (
        patch("marketdata.workflows.logger.info") as info,
        patch("marketdata.models.WorkflowRun.objects.create"),
    ):
        WorkflowOutcome(
            "archive_state", endpoint="stock_candle_adjusted", symbol="TEST"
        ).finish(
            "retry",
            rows_received=42,
            rows_accepted=19,
            http_attempts=1,
            quota_attempts=1,
            duration_ms=321,
            error_code="ReadTimeout",
            metadata={"reason": "Provider request failed\n(ReadTimeout)"},
        )

    message = info.call_args.args[0]
    assert message.startswith(
        "workflow=archive_state outcome=retry endpoint=stock_candle_adjusted symbol=TEST"
    )
    assert "rows=42/19" in message
    assert "error=ReadTimeout" in message
    assert "reason=Provider request failed (ReadTimeout)" in message
    assert not message.startswith("{")


# ----------------------------------------------------------------------
# test_reconciliation_features.py


@pytest.fixture
def auth_client(db, make_user):
    user = make_user(email="admin@test.test")
    user.is_staff = True
    user.save()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def test_liability_netting_in_valuation(db, make_user):
    user = make_user(email="user@test.test")
    account = Account.objects.create(name="Test Account", user=user)
    
    asset = Asset.objects.create(
        key="gold_18k_gram", name="Gold 18k", asset_class="Gold", currency="IRT"
    )
    # Create holding
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("10"))
    
    # Valuation without liabilities: 10 * 100_000 = 1,000,000
    prices = {"gold_18k_gram": Decimal("100000")}
    
    from portfolio.services.valuation import value_account
    val = value_account(account, prices)
    assert val["total"] == Decimal("1000000")
    
    # Create liability
    Liability.objects.create(
        account=account,
        label="Test Loan",
        amount_tomans=Decimal("300000"),
    )
    
    # Valuation with liabilities: 1,000,000 - 300,000 = 700,000
    val = value_account(account, prices)
    assert val["total"] == Decimal("700000")
    assert val["total_liabilities"] == 300000.0


def test_house_mortgage_is_deducted_exactly_once(db, make_user):
    """A mortgage lives in Liability now, so the house must be valued gross."""
    user = make_user(email="house@test.test")
    account = Account.objects.create(name="Home", user=user)
    house = Asset.objects.create(
        key="house_main", name="House", asset_class="Real Estate",
        currency="IRT", is_house=True,
    )
    Holding.objects.create(
        account=account, asset=house, quantity=Decimal("100"),
        area_sqm=Decimal("90.2"), mortgage_deduction_tomans=Decimal("0"),
    )
    Liability.objects.create(
        account=account, asset=house, label="Mortgage (House)",
        amount_tomans=Decimal("400000000"),
    )

    from portfolio.services.valuation import value_account
    val = value_account(account, {"house_main": Decimal("100")})

    gross = Decimal("9020000000")  # 100 million Toman/sqm * 90.2 sqm
    assert val["items"][0]["value"] == gross, "house must be valued gross of mortgage"
    assert val["total"] == gross - Decimal("400000000")  # 8,620,000,000


def test_house_opening_position_without_mortgage_invents_none(db, make_user):
    """No mortgage supplied must mean no mortgage — not a 400M phantom."""
    from django.utils import timezone
    from portfolio.models import LedgerEntry
    from portfolio.services.ledger import create_ledger_entry

    user = make_user(email="house2@test.test")
    account = Account.objects.create(name="Home", user=user)
    house = Asset.objects.create(
        key="house_two", name="House Two", asset_class="Real Estate",
        currency="IRT", is_house=True,
    )
    create_ledger_entry(
        account=account, asset=house, kind=LedgerEntry.Kind.OPENING_POSITION,
        quantity=Decimal("10"), occurred_at=timezone.now(),
        area_sqm=Decimal("90.2"),
    )

    holding = Holding.objects.get(account=account, asset=house)
    assert holding.mortgage_deduction_tomans == Decimal("0")
    assert not Liability.objects.filter(account=account, asset=house).exists()

    from portfolio.services.valuation import value_account
    assert value_account(account, {"house_two": Decimal("10")})["total"] == Decimal("902000000")


def test_usdt_basis_conversion_fallback(db):
    from portfolio.services.deflator import to_basis
    
    # Seed historical rate for USD only
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1405-01-01", close_price=Decimal("50000")
    )
    
    import pandas as pd
    series = pd.Series([100000.0], index=[pd.Timestamp("2026-03-21", tz="UTC")])
    
    # Convert using usdt_denominated basis. Since USDT_IRT is missing, it should fallback to USD.
    res = to_basis(series, "usdt_denominated")
    assert not res.isna().all()
    assert res.iloc[0] == 2.0  # 100,000 / 50,000 = 2.0


def test_admin_endpoints(auth_client, db):
    client, user = auth_client
    
    # Test AdminUserListView
    url = reverse("admin-users")
    response = client.get(url)
    assert response.status_code == status.HTTP_200_OK
    assert len(response.data) >= 1
    
    # Test ArchiveFetchStateAdmin custom retry action
    from marketdata.admin import ArchiveFetchStateAdmin
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory
    
    state = ArchiveFetchState.objects.create(
        symbol="FOO",
        endpoint="announcements",
        stored_rows=10,
        expected_rows=20,
        consecutive_failures=3,
        last_error="Temporary network issue."
    )
    
    admin_instance = ArchiveFetchStateAdmin(ArchiveFetchState, AdminSite())
    req = RequestFactory().post("/admin/")
    req.user = user
    
    from unittest.mock import patch
    # settings_test sets CELERY_TASK_ALWAYS_EAGER, so .delay() runs the retry
    # inline; with no provider reachable it fails and re-increments
    # consecutive_failures to 1 before this assertion runs. That is correct
    # behaviour, not a bug -- production dispatches asynchronously and the reset
    # stands. Mock the hand-off so this tests the contract enqueue_archive_retries
    # actually owns: clear the failure count, then enqueue exactly once. The
    # broker ping is mocked for the same reason -- refusing to enqueue when Redis
    # is down is correct production behaviour and is not what this asserts.
    with patch("marketdata.tasks.retry_archive_job_task.delay") as mock_delay, \
         patch("redis.Redis.from_url") as mock_redis, \
         patch("django.contrib.messages.add_message") as mock_add:
        mock_redis.return_value.ping.return_value = True
        admin_instance.retry_selected_jobs(req, ArchiveFetchState.objects.filter(id=state.id))
        mock_add.assert_called_once()
    mock_delay.assert_called_once_with(state.id)

    # Check that failures were reset
    state.refresh_from_db()
    assert state.consecutive_failures == 0


# ----------------------------------------------------------------------
# Quota attribution and the spending-plan preview.


def test_ops_metrics_report_unattributed_quota():
    """Every metered request should be traceable to the workflow that spent it.

    Drift here is the signal that some lane is charging the counter without
    telling the ledger -- the live loop did exactly that for months.
    """
    from marketdata.models import ApiRequestQuota, WorkflowRun
    from marketdata.quota import quota_day
    from marketdata.tasks import _quota_attribution_drift

    ApiRequestQuota.objects.create(day=quota_day(), limit=9800, used=100)
    WorkflowRun.objects.create(
        workflow="archive_state", outcome="success", quota_attempts=90
    )

    drift = _quota_attribution_drift()

    assert drift["quota_charged"] == 100
    assert drift["quota_attributed"] == 90
    assert drift["quota_unattributed"] == 10


def test_quota_plan_command_runs_read_only():
    """The pre-deploy dry run must not lease, fetch, or write anything."""
    from io import StringIO

    from django.core.management import call_command

    from marketdata.models import ArchiveFetchState, LiveFetchState

    LiveFetchState.objects.create(
        endpoint_key="crypto", cadence_seconds=900, session_only=False
    )
    state = ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="PREVIEW",
        missing_rows=42,
    )

    out = StringIO()
    call_command("quota_plan", "--preview", "5", stdout=out)

    text = out.getvalue()
    assert "crypto" in text and "TOTAL" in text
    assert "PREVIEW" in text, "the preview must show what would be claimed next"
    state.refresh_from_db()
    assert state.next_attempt_at is None, "preview must not lease the state"


# ----------------------------------------------------------------------
# One calendar in the "latest data" column.
#
# Half of `database_rows` is keyed on a real timestamp and half on a Jalali date
# string, and passing the string through printed a Jalali year under a Gregorian
# month name -- "04 Jun 1405" sitting next to "28 Aug 2026" in one column, with
# no way to tell which row was fresher.
#
# Unit tests: pure value mapping, no DB.


def test_jalali_latest_becomes_the_gregorian_instant_it_names():
    import datetime as dt

    from marketdata.admin_telemetry import _latest_iso

    # 1405-06-04 is 2026-08-26.
    assert _latest_iso("1405-06-04") == dt.date(2026, 8, 26).isoformat()


def test_gregorian_latest_is_unchanged():
    import datetime as dt

    from marketdata.admin_telemetry import _latest_iso

    when = dt.datetime(2026, 8, 28, 22, 55)
    assert _latest_iso(when) == when.isoformat()
    assert _latest_iso(None) is None


def test_a_non_date_string_is_left_alone_rather_than_dropped():
    from marketdata.admin_telemetry import _latest_iso

    assert _latest_iso("not-a-date") == "not-a-date"


# ----------------------------------------------------------------------
# The error panel counts failures, not reasons.
#
# A SKIPPED run sets `error_code` to say why it stood down, and the archive
# stands down constantly by design (`archive_paced` is the pacer spreading the
# day's budget). Counting those made pacing 99% of the error distribution and
# rounded the real signatures to 0%.
#
# Integration test: the rule is a queryset filter, so it is only true if the ORM
# actually applies it -- a pure-function test would prove nothing here.


def test_paced_skips_are_not_counted_as_errors(db):
    from marketdata.admin_telemetry import _error_code_breakdown
    from marketdata.models import WorkflowRun

    for _ in range(50):
        WorkflowRun.objects.create(
            workflow="archive_tick",
            outcome=WorkflowRun.Outcome.SKIPPED,
            error_code="archive_paced",
        )
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome=WorkflowRun.Outcome.RETRY,
        error_code="MarketDataFetchError",
    )
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome=WorkflowRun.Outcome.FAILED,
        error_code="QuotaExhausted",
    )

    codes = {row["error_code"]: row["count"] for row in _error_code_breakdown()}

    assert "archive_paced" not in codes
    # The two that matter are now the whole list, not 0.9% of it.
    assert codes == {"MarketDataFetchError": 1, "QuotaExhausted": 1}


def test_a_partial_run_still_reports_its_error_code(db):
    from marketdata.admin_telemetry import _error_code_breakdown
    from marketdata.models import WorkflowRun

    # Half-worked is still failed at something, so it keeps its signature.
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome=WorkflowRun.Outcome.PARTIAL,
        error_code="MarketDataFetchError",
    )
    assert _error_code_breakdown() == [{"error_code": "MarketDataFetchError", "count": 1}]
