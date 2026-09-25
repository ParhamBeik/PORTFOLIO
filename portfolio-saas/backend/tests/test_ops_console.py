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
from marketdata.models import GoldCurrencyHistory
from marketdata.models import MarketInstrument
from marketdata.tasks import _retry_code, prune_workflow_runs
from marketdata.tasks import capture_operational_metrics
from marketdata.workflows import WorkflowOutcome
from portfolio.models import Account, Asset, Holding, Liability
from portfolio.views.admin_ops import AdminCleanPricesExecuteView

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
    users = body["users"]
    assert users["total"] >= 1
    assert "with_accounts" in users
    assert users["admins"] >= 1
    assert "staff" not in users
    assert "snapshots_24h" in users
    assert "last_snapshot_at" in users
    inventory = body["admin_model_inventory"]
    assert any(row["admin_path"] == "/admin/portfolio/snapshot/" for row in inventory)
    assert any(row["admin_path"] == "/admin/token_blacklist/blacklistedtoken/" for row in inventory)
    assert all("rows_estimated" in row and "bytes" in row for row in inventory)


def test_ops_overview_says_when_password_reset_mail_cannot_be_sent(staff_user):
    """The one broken journey that is invisible from the outside.

    `PasswordResetRequestView` answers the same 200 whether or not the send
    worked -- on purpose, so the endpoint cannot enumerate accounts -- and logs
    the failure. With no `EMAIL_HOST` the user is told to check an inbox that
    will never receive anything, and only a log line says otherwise. The Ops
    console is where that has to show up.

    Degraded, not critical: every other request is served fine without mail.
    """
    cache.clear()
    client = _auth(APIClient(), staff_user)

    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
    ):
        body = client.get("/api/admin/overview/?refresh=1").json()
    mail = body["outbound_mail"]
    assert mail["ok"] is False
    assert mail["status"] == "unconfigured"
    assert "password-reset" in mail["message"]
    assert body["checks"]["outbound_mail"]["status"] == "unconfigured"
    assert body["overall_status"] == "degraded", (
        "a mail outage must not read as critical -- the API serves everything "
        "else -- but it must not read as healthy either"
    )

    # Django's own default host is "localhost", so an environment that never set
    # the variable is indistinguishable from one pointing a relay at loopback.
    # Production on 2026-09-09 was exactly this, and nothing listened.
    cache.clear()
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="localhost",
        EMAIL_PORT=25,
    ):
        loopback = client.get("/api/admin/overview/?refresh=1").json()
    assert loopback["outbound_mail"]["status"] == "unconfigured"


def test_a_configured_but_unreachable_relay_is_not_reported_as_healthy(monkeypatch):
    """Settings alone cannot answer whether mail leaves the process.

    This is the case that made a socket necessary: production had EMAIL_HOST set
    and every send still failed with ConnectionRefusedError. A settings-only
    check calls that configured, and the console reports healthy while account
    recovery is dead.
    """
    import socket as socket_module

    from marketdata.admin_telemetry import outbound_mail

    cache.clear()
    settings_kwargs = dict(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="smtp.example.com",
        EMAIL_PORT=587,
    )

    def refuse(*args, **kwargs):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(socket_module, "create_connection", refuse)
    with override_settings(**settings_kwargs):
        down = outbound_mail()
    assert down["ok"] is False
    assert down["status"] == "unreachable"
    assert "smtp.example.com:587" in down["message"]

    class Reachable:
        def close(self):
            pass

    cache.clear()
    monkeypatch.setattr(socket_module, "create_connection", lambda *a, **k: Reachable())
    with override_settings(**settings_kwargs):
        up = outbound_mail()
    assert up["status"] == "healthy"
    assert up["host"] == "smtp.example.com"

    # Probing on every dashboard render would dial a relay far too often; the
    # result is cached, so a second call must not open a second socket.
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return Reachable()

    monkeypatch.setattr(socket_module, "create_connection", counting)
    with override_settings(**settings_kwargs):
        outbound_mail()
        outbound_mail()
    assert calls["n"] == 0, "a cached probe result was ignored"


def test_a_local_capture_backend_is_not_reported_as_broken(staff_user):
    """Console and locmem backends are correct in dev and under test."""
    from marketdata.admin_telemetry import outbound_mail

    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", EMAIL_HOST=""
    ):
        assert outbound_mail() == {
            "ok": True,
            "status": "not_delivering",
            "backend": "django.core.mail.backends.locmem.EmailBackend",
            "host": "",
            "message": "Mail is captured locally by this backend, not delivered.",
        }


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
    for key in ("checks", "price_feed", "status", "overall_status", "queue", "quota"):
        assert key in second.json(), f"live health key {key!r} missing from a cached response"


def test_cached_overview_reads_latest_stored_provider_quota(staff_user):
    from marketdata.models import ApiRequestQuota
    from marketdata.quota import AIO, quota_day

    cache.clear()
    row = ApiRequestQuota.objects.create(
        day=quota_day(), plan=AIO, used=7, provider_used=7,
        provider_baseline_used=7,
    )
    client = _auth(APIClient(), staff_user)
    first = client.get("/api/admin/overview/")
    assert first.status_code == 200
    assert first.json()["quota"]["plans"][AIO]["provider_used"] == 7

    row.provider_used = 8
    row.used = 8
    row.save(update_fields=["provider_used", "used"])
    second = client.get("/api/admin/overview/")
    assert second.status_code == 200
    assert second.json()["quota"]["plans"][AIO]["provider_used"] == 8
    assert second.json()["quota"]["plans"][AIO]["provider_variance"] == 1


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
    assert body["disk"]["filesystem_total_bytes"] > 0
    assert body["disk"]["filesystem_used_bytes"] <= body["disk"]["filesystem_total_bytes"]
    assert body["queues"]["depths"]["archive"] == 2
    assert body["codal"]["enabled"] in (True, False)
    assert body["workflow_15m"]["rows_accepted"] >= 12
    dests = {row["destination_table"] for row in body["workflow_15m"]["by_destination"]}
    assert "MarketCandle" in dests
    assert body["workers"]["summary"]["online"] == 1
    assert "coverage" in body
    assert "live" in body["coverage"]
    assert "warehouse" in body["coverage"]
    assert set(body["coverage"]["warehouse"]["counts"].keys()) == {
        "complete", "refresh_due", "partial", "failed", "awaiting_data", "not_tried",
        # A symbol the archive has given up on is neither coverage nor backlog.
        # Without these two the console had no way to answer "how many symbols
        # can we not fetch?", so every unfetchable one was silently counted as
        # something it was not.
        "unfetchable", "suspended",
    }
    assert "refresh_backlog" in body["coverage"]["warehouse"]
    census = body["coverage"]["warehouse"]["symbol_census"]
    assert set(census) >= {
        "symbols_total", "fetched", "never_fetched", "unfetchable",
        "partially_unfetchable", "attempted_never_landed", "fetched_pct",
    }


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
    user.role = User.Role.ADMIN
    user.save(update_fields=["role"])
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
    user.role = User.Role.ADMIN
    user.save(update_fields=["role"])
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


@override_settings(
    ALERT_WEBHOOK_URL="",
    ALERT_TELEGRAM_BOT_TOKEN="bot-token",
    ALERT_TELEGRAM_CHAT_ID="12345",
)
def test_alert_telegram_sends_plain_text_and_does_not_parse_markup():
    from config.observability import notify

    with mock.patch("config.observability.requests.post") as post:
        ok = notify("telegram-alert", {"symbol": "FOOLAD_x"}, dedupe_seconds=60)

    assert ok is True
    assert post.call_args.args[0] == "https://api.telegram.org/botbot-token/sendMessage"
    payload = post.call_args.kwargs["json"]
    assert payload["chat_id"] == "12345"
    assert "parse_mode" not in payload
    assert payload["disable_web_page_preview"] is True
    assert payload["text"].startswith("[telegram-alert]")
    assert "FOOLAD_x" in payload["text"]


@override_settings(
    ALERT_WEBHOOK_URL="https://alerts.test/hook",
    ALERT_TELEGRAM_BOT_TOKEN="bot-token",
    ALERT_TELEGRAM_CHAT_ID="12345",
)
def test_alert_channels_are_independent():
    from config.observability import notify

    def _post(url, **kwargs):
        if "alerts.test" in url:
            raise ConnectionError("webhook down")
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        return resp

    with mock.patch("config.observability.requests.post", side_effect=_post) as post:
        assert notify("split-alert", {"n": 1}, dedupe_seconds=60) is True
    assert post.call_count == 2


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


def test_wedged_alert_ignores_states_the_archive_has_already_parked(settings):
    """A state nobody is claiming is neither coverage nor backlog.

    `classify_archive_state` tests `blacklisted`/`suspended_at` before anything
    else for exactly this reason, and the alert has to mirror it. Without that
    the alert was 100% noise: all 322 states it named on 2026-09-07 were
    suspended, so it fired every 15 minutes with an unchanging payload and
    buried the detections above it that do mean something.
    """
    from marketdata.models import ArchiveFetchState
    from marketdata.tasks import operational_health_check

    settings.ARCHIVE_WEDGED_FAILURE_THRESHOLD = 6
    common = dict(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        consecutive_failures=9,
        last_error="MarketDataFetchError: 1405-05-11: tick_volume_mismatch:5!=9",
    )
    ArchiveFetchState.objects.create(symbol="parked", suspended_at=timezone.now(), **common)
    ArchiveFetchState.objects.create(symbol="gone", blacklisted=True,
                                     suspended_at=timezone.now(), **common)

    with patch("config.observability.notify") as notify:
        operational_health_check()
    assert "wedged-archive-states" not in {c.args[0] for c in notify.call_args_list}

    # An un-parked state at the same failure count still alerts.
    ArchiveFetchState.objects.create(symbol="really-wedged", **common)
    with patch("config.observability.notify") as notify:
        operational_health_check()
    fired = {call.args[0]: call.args[1] for call in notify.call_args_list}
    assert fired["wedged-archive-states"]["count"] == 1


def test_stale_archive_alert_is_silent_when_there_is_no_quota_to_spend(settings):
    """"Quiet" only means something when the archive COULD be spending.

    Under burst allocation the wallet is empty for most of the day by design --
    backfill runs flat out from Tehran midnight and then idles until the reset.
    This alert fired 86 times in the 25h to 2026-09-07, every quarter hour
    outside the 2h38m burst, which is not a stall.
    """
    from marketdata.models import ArchiveFetchState
    from marketdata.quota import BRS, TSETMC
    from marketdata.tasks import operational_health_check

    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="pending", verified_complete=False,
    )

    with (
        patch("marketdata.quota.archive_capacity", return_value={TSETMC: 0, BRS: 0}),
        patch("config.observability.notify") as notify,
    ):
        operational_health_check()
    assert "stale-archive-progress" not in {c.args[0] for c in notify.call_args_list}

    # Same silence, but with quota available, is a real stall.
    with (
        patch("marketdata.quota.archive_capacity", return_value={TSETMC: 4_000, BRS: 0}),
        patch("config.observability.notify") as notify,
    ):
        operational_health_check()
    fired = {call.args[0]: call.args[1] for call in notify.call_args_list}
    assert fired["stale-archive-progress"]["archive_capacity"] == 4_000


def test_stale_archive_alert_ignores_capacity_for_other_provider(settings):
    """BRS room cannot make a TSETMC-only backlog runnable."""
    from marketdata.models import ArchiveFetchState
    from marketdata.quota import BRS, TSETMC
    from marketdata.tasks import operational_health_check

    ArchiveFetchState.objects.create(
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        symbol="tsetmc-only", verified_complete=False,
    )
    with (
        patch("marketdata.quota.archive_capacity", return_value={TSETMC: 0, BRS: 1_500}),
        patch("config.observability.notify") as notify,
    ):
        operational_health_check()
    assert "stale-archive-progress" not in {c.args[0] for c in notify.call_args_list}


def test_prune_keeps_inside_window_and_drops_outside(settings):
    # Straddle the configured window rather than a literal, so changing the
    # retention (30 -> 14 on 2026-09-06) does not silently invert this test.
    window = settings.WORKFLOW_RETENTION_DAYS
    now = timezone.now()
    for age_days, workflow in ((window + 1, "old"), (window - 1, "recent")):
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
        outcome = WorkflowOutcome(
            "archive_state", endpoint="stock_candle_adjusted", symbol="TEST",
            source="brsapi:stock_candle_adjusted",
            destination_table="MarketCandle",
        )
        outcome.finish(
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
    # Where it came from and where it went. Both were recorded on the ledger row
    # and omitted from the line, so `docker logs` -- the artifact anyone
    # actually reads -- could answer neither question.
    assert "source=brsapi:stock_candle_adjusted" in message
    assert "dest=MarketCandle" in message
    # And the thread back to the ledger row and to sibling lines.
    assert f"cid={outcome.correlation_id}" in message


def test_archive_workflow_names_the_table_it_actually_writes():
    """Not the bookkeeping row it updates.

    Every archive endpoint reported `ArchiveFetchState` as its destination --
    the state row the job maintains, never the table the data lands in -- so the
    one column that answers "where did these rows go" said the same thing for
    all eight endpoints.
    """
    from marketdata.archive import destination_for
    from marketdata.models import ArchiveFetchState

    E = ArchiveFetchState.Endpoint
    assert destination_for(E.STOCK_CANDLE_ADJUSTED) == "MarketCandle"
    assert destination_for(E.STOCK_TRANSACTION_TICKS) == "StockTransactionTick"
    assert destination_for(E.GOLD_DAILY) == "GoldCurrencyHistory"
    # The misnamed enum: type=1 is the real/legal breakdown, not adjusted prices.
    assert destination_for(E.STOCK_HISTORY_ADJUSTED) == "RealLegalHistory"
    assert destination_for(E.STOCK_HISTORY_UNADJUSTED) == "DailyStockHistory"
    # No destination invented for an endpoint that writes nothing.
    assert destination_for(E.CRYPTO_DAILY) == ""


def test_archive_source_names_the_provider_path():
    """Enum keys are not registry keys; the path must still be recoverable."""
    from marketdata.archive import source_for
    from marketdata.models import ArchiveFetchState

    E = ArchiveFetchState.Endpoint
    assert source_for(E.STOCK_CANDLE_ADJUSTED) == "brsapi:Tsetmc/Candlestick.php"
    assert source_for(E.STOCK_HISTORY_UNADJUSTED) == "brsapi:Tsetmc/History.php"
    assert source_for(E.GOLD_DAILY) == "brsapi:Market/Gold_Currency_Pro.php"
    assert source_for(E.STOCK_TRANSACTION_TICKS) == "brsapi:Tsetmc/Transaction.php"
    assert source_for(E.CRYPTO_DAILY) == ""


def test_ledgered_names_the_provider_path_from_the_registry():
    """A hand-typed source can only drift from the registry that declares it."""
    from marketdata.tasks import _ledgered

    assert _ledgered("x", endpoint="all_symbols").source == "brsapi:Tsetmc/AllSymbols.php"
    # A scheduler or an aggregation has no provider origin to name.
    assert _ledgered("x", endpoint="archive_scheduler").source == ""
    # An explicit empty source is honoured, not overwritten by the lookup.
    assert _ledgered("x", endpoint="all_symbols", source="").source == ""


# ----------------------------------------------------------------------
# test_reconciliation_features.py


@pytest.fixture
def auth_client(db, make_user):
    user = make_user(email="admin@test.test")
    user.role = "admin"
    user.save()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def test_liability_netting_in_valuation(db, make_user):
    user = make_user(email="user@test.test")
    account = Account.objects.create(name="Test Account", user=user)
    
    asset = Asset.objects.create(
        key="gold_18k_gram", name="Gold 18k", asset_class="Gold"
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


def test_itemised_debts_convert_with_the_total_they_add_up_to(
    db, make_user, asset_catalog, write_prices
):
    """Under a foreign basis, `liabilities[]` must not stay in Toman.

    `_rescale` converted `total_liabilities` but not the rows it is the sum of,
    so a dollar-denominated payload carried an itemised debt list that did not
    add up to the total printed above it -- the "deflated on one basis but not
    the other" trap that walk exists to close, one level deeper than it reached.
    """
    user = make_user(email="debtbasis@test.test")
    account = Account.objects.create(name="Leveraged", user=user)
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1")
    )
    Liability.objects.create(
        account=account, label="Mortgage", amount_tomans=Decimal("420000000")
    )
    Liability.objects.create(
        account=account, label="Car loan", amount_tomans=Decimal("42000000")
    )
    write_prices({"emami_coin": Decimal("1000000000"), "usd_cash": Decimal("420000")})

    client = APIClient()
    client.force_authenticate(user=user)
    payload = client.get("/api/valuation/?basis=usd_denominated").json()

    assert payload["basis"] == "usd_denominated"
    rows = payload["liabilities"]
    assert sorted(round(r["amount_tomans"], 2) for r in rows) == [100.0, 1000.0]
    assert round(sum(r["amount_tomans"] for r in rows), 2) == round(
        payload["total_liabilities"], 2
    )


def test_house_mortgage_is_deducted_exactly_once(db, make_user):
    """A mortgage lives in Liability now, so the house must be valued gross."""
    user = make_user(email="house@test.test")
    account = Account.objects.create(name="Home", user=user)
    house = Asset.objects.create(
        key="house_main", name="House", asset_class="Real Estate",
        is_house=True,
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
        is_house=True,
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


def test_a_house_mortgage_is_owned_by_the_replay_and_a_users_debt_is_not(db, make_user):
    """`derived` must mean "the replay will put this back", nothing looser.

    The reap in `rebuild_projections` deletes on that flag, so the two rows
    below are the whole contract: a mortgage the marks imply is recreated on
    every replay and must carry it, and a debt a person secured on the same
    house is theirs and must not. Classifying on the label's shape instead is
    what migration 0037 had to withdraw -- it caught four seeded rows on gold
    coins that no mark would ever have rebuilt.
    """
    from django.utils import timezone
    from portfolio.models import LedgerEntry
    from portfolio.services.ledger import create_ledger_entry, rebuild_projections

    user = make_user(email="derived-vs-typed@test.test")
    account = Account.objects.create(name="Home", user=user)
    house = Asset.objects.create(
        key="house_three", name="House Three", asset_class="Real Estate",
        is_house=True,
    )
    create_ledger_entry(
        account=account, asset=house, kind=LedgerEntry.Kind.OPENING_POSITION,
        quantity=Decimal("10"), occurred_at=timezone.now(),
        area_sqm=Decimal("90.2"), mortgage_deduction_tomans=Decimal("400000000"),
    )

    mortgage = Liability.objects.get(account=account, asset=house, derived=True)
    assert mortgage.amount_tomans == Decimal("400000000")

    typed = Liability.objects.create(
        account=account, asset=house, label="Loan from my brother",
        kind=Liability.Kind.SECURED_DEBT, amount_tomans=Decimal("50000000"),
    )

    rebuild_projections(account)

    # The derived row is gone and remade -- a new pk, the same figure.
    remade = Liability.objects.get(account=account, asset=house, derived=True)
    assert remade.pk != mortgage.pk
    assert remade.amount_tomans == Decimal("400000000")
    # The typed one is untouched, same row.
    typed.refresh_from_db()
    assert typed.label == "Loan from my brother"
    assert typed.derived is False


def test_usdt_basis_requires_its_own_rate(db):
    from portfolio.services.deflator import to_basis
    
    # Seed historical rate for USD only
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1405-01-01", close_price=Decimal("50000")
    )
    
    import pandas as pd
    series = pd.Series([100000.0], index=[pd.Timestamp("2026-03-21", tz="UTC")])
    
    # A dollar observation does not establish the price of USDT.
    res = to_basis(series, "usdt_denominated")
    assert res.isna().all()


def test_sparse_currency_series_never_carries_a_month_old_rate(db):
    import pandas as pd
    from portfolio.services.deflator import to_basis

    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1405-01-01", close_price=Decimal("50000")
    )
    points = pd.to_datetime(["2026-03-21", "2026-04-21"], utc=True)
    series = pd.Series([100000.0, 100000.0], index=points)

    converted = to_basis(series, "usd_denominated")
    assert converted.iloc[0] == 2.0
    assert pd.isna(converted.iloc[1])


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


def test_pacing_is_not_counted_as_an_error(db):
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


# ----------------------------------------------------------------------
# 100% is reserved for complete.
#
# Rounding 99.96 to "100%" put a complete row fill on the same line as 1,313
# jobs still carrying gaps. Unit tests: one pure function, no DB.


def test_an_incomplete_fill_never_rounds_up_to_complete():
    from marketdata.coverage_report import _pct

    # 9,548,784 of 9,554,746 rows is 99.94%, which used to print as 100%.
    assert _pct(9_548_784, 9_554_746) == 99.9
    assert _pct(9_999, 10_000) == 99.9


def test_a_genuinely_complete_fill_is_still_100():
    from marketdata.coverage_report import _pct

    assert _pct(10, 10) == 100.0
    assert _pct(11, 10) == 100.0
    assert _pct(0, 0) == 0.0
    assert _pct(1, 4) == 25.0


def test_a_skipped_run_that_really_failed_still_reports(db):
    """Excluding the SKIPPED outcome wholesale would silence a dead pipeline.

    `quota_exhausted` (the wallet is genuinely empty) and `origin_unreachable`
    (Codal cannot be reached at all) are both recorded SKIPPED alongside the
    pacer, so the filter has to name the pacing code, not the outcome.
    """
    from marketdata.admin_telemetry import _error_code_breakdown
    from marketdata.models import WorkflowRun

    WorkflowRun.objects.create(
        workflow="archive_tick",
        outcome=WorkflowRun.Outcome.SKIPPED,
        error_code="archive_paced",
    )
    WorkflowRun.objects.create(
        workflow="archive_tick",
        outcome=WorkflowRun.Outcome.SKIPPED,
        error_code="quota_exhausted",
    )
    WorkflowRun.objects.create(
        workflow="codal_extract",
        outcome=WorkflowRun.Outcome.SKIPPED,
        error_code="origin_unreachable",
    )

    codes = {row["error_code"]: row["count"] for row in _error_code_breakdown()}

    assert "archive_paced" not in codes
    assert codes == {"quota_exhausted": 1, "origin_unreachable": 1}


def test_a_property_is_listed_by_its_owners_name_not_its_class(db):
    """An owner-minted asset's catalog `name` is the class, never the property.

    `_mint_property_asset` fills `name` from what the owner typed at creation,
    but the name they go on to see everywhere else is the holding nickname, and
    the console read only the catalog row -- so a house listed as "Real Estate",
    which also cannot tell two properties apart.
    """
    from accounts.models import User
    from marketdata.coverage_report import build_live_coverage
    from portfolio.models import Account, Asset, Holding

    user = User.objects.create_user(email="owner@test.local", password="x")
    account = Account.objects.create(user=user, name="Mine")
    house = Asset.objects.create(
        key="re-abc123", name="Real Estate", asset_class=Asset.AssetClass.REAL_ESTATE,
        is_house=True, is_active=True, owner=user,
    )
    shared = Asset.objects.create(
        key="usd_cash", name="US Dollar", asset_class=Asset.AssetClass.CASH,
        is_active=True,
    )
    Holding.objects.create(account=account, asset=house, quantity=1, display_name="خونه کرج")
    Holding.objects.create(account=account, asset=shared, quantity=5, display_name="")

    names = {row["key"]: row["name"] for row in build_live_coverage()["assets"]}

    assert names["re-abc123"] == "خونه کرج"
    # A shared catalog row has no owner and keeps the catalog's own name.
    assert names["usd_cash"] == "US Dollar"


def test_a_stale_baseline_reports_unknown_rather_than_a_longer_delta(db):
    """Only the upper bound was set, so "24h change" had no lower bound.

    Let snapshot capture stall and the panel went on calling the change since
    whenever-it-last-ran a one-day figure -- sized like several, on the widget
    whose only job is to say whether ingest is moving.
    """
    from datetime import timedelta

    from django.utils import timezone

    from marketdata.admin_telemetry import _fill_rates
    from marketdata.models import OperationalMetricSnapshot

    now = timezone.now()
    # Eight days old: a valid 7-day baseline, far too old to be a 24-hour one.
    OperationalMetricSnapshot.objects.create(
        captured_at=now - timedelta(days=8),
        database_counts={"candles": 1_000_000}, table_bytes={"candles": 10},
    )

    rates = _fill_rates({"candles": 1_500_000}, {"candles": 20})

    assert rates["candles"]["delta_24h"] is None
    assert rates["candles"]["delta_7d"] == 500_000


# The growth charts read this series directly, so its two failure modes are
# unit-testable without a browser: an unmeasured hour must not become a zero,
# and an append-only table must not appear to shrink when the row-count
# ESTIMATOR wobbles (these counts are pg_class reltuples, not COUNT(*)).
def test_growth_history_keeps_gaps_as_gaps_and_never_shrinks_an_append_only_table():
    from marketdata.admin_telemetry import _clean_count_history

    rows = [
        {"counts": {"stock_transaction_ticks": 100, "accounts": 5}},
        {"counts": {"accounts": 5}},                                  # collector skipped
        {"counts": {"stock_transaction_ticks": 90, "accounts": 4}},   # estimator dipped
        {"counts": {"stock_transaction_ticks": 140, "accounts": 4}},
    ]
    cleaned = _clean_count_history(rows, ("stock_transaction_ticks", "accounts"))
    ticks = [r["counts"]["stock_transaction_ticks"] for r in cleaned]

    assert ticks[1] is None, "a missing measurement must stay missing, not become 0"
    assert ticks[2] == 100, "an append-only table cannot shrink; that dip is estimator noise"
    assert ticks == [100, None, 100, 140]

    # A user really can delete a portfolio, so user-owned tables are passed
    # through untouched -- flattening a real deletion would hide it.
    assert [r["counts"]["accounts"] for r in cleaned] == [5, 5, 4, 4]


# ---------------------------------------------------------------------------
# DISK PROJECTION. Both halves of this used to be wrong in the reassuring
# direction. Measured in production on 2026-09-09: the console said "257 days
# until 80% of 250 GB, no alert" while the device was 147.4 GB with 31.6 GB
# free -- 11.6 days from the 80% line. The budget was a hand-typed constant and
# the usage was one database's logical size, on a box that also carries three
# other stacks, the Docker images and this app's own 16 GB of backups.
GB = 1024 ** 3


def _disk_history(now, *, start_gb, end_gb, days, filesystem_gb=None):
    def row(offset_days, own_gb, fs_gb):
        disk = {"database_bytes": int(own_gb * GB), "codal_bytes": 0}
        if fs_gb is not None:
            disk["filesystem_used_bytes"] = int(fs_gb * GB)
        return {"captured_at": now - timedelta(days=offset_days), "disk": disk}

    first_fs, last_fs = filesystem_gb or (None, None)
    return [row(days, start_gb, first_fs), row(0, end_gb, last_fs)]


def test_disk_projection_measures_the_device_not_a_configured_budget(monkeypatch):
    from marketdata import admin_telemetry

    now = timezone.now()
    monkeypatch.setattr(
        admin_telemetry,
        "filesystem_usage",
        lambda path="/": {"total": 147 * GB, "used": 110 * GB, "free": 37 * GB},
    )
    disk = admin_telemetry.project_disk(
        {"database_bytes": 17 * GB, "codal_bytes": 0},
        _disk_history(now, start_gb=10, end_gb=17, days=10),
    )

    # Headroom is 147*0.8 - 110 = 7.6 GB against 0.7 GB/day, so ~11 days.
    # Against the old 250 GB budget and a 17 GB numerator it read ~261 days.
    assert disk["growth_bytes_per_day"] == pytest.approx(0.7 * GB, rel=0.01)
    assert 10 < disk["days_to_80pct"] < 12
    assert disk["alert"] is True
    assert disk["filesystem_total_bytes"] == 147 * GB
    # Our share stays available, but as a breakdown rather than as the headline.
    assert disk["used_bytes"] == 17 * GB


def test_disk_projection_takes_the_faster_of_the_two_growth_series(monkeypatch):
    """The device fills from backups and neighbouring stacks too, not just us."""
    from marketdata import admin_telemetry

    now = timezone.now()
    monkeypatch.setattr(
        admin_telemetry,
        "filesystem_usage",
        lambda path="/": {"total": 100 * GB, "used": 50 * GB, "free": 50 * GB},
    )
    # Our tables grew 1 GB/day; the device grew 4 GB/day. Believing our own
    # series would promise 30 days of headroom where there are 7.5.
    disk = admin_telemetry.project_disk(
        {"database_bytes": 20 * GB, "codal_bytes": 0},
        _disk_history(now, start_gb=10, end_gb=20, days=10, filesystem_gb=(10, 50)),
    )

    assert disk["growth_bytes_per_day"] == pytest.approx(4 * GB, rel=0.01)
    assert disk["days_to_80pct"] == pytest.approx(7.5, rel=0.05)


def test_disk_projection_reports_zero_days_once_past_the_line(monkeypatch):
    from marketdata import admin_telemetry

    monkeypatch.setattr(
        admin_telemetry,
        "filesystem_usage",
        lambda path="/": {"total": 100 * GB, "used": 90 * GB, "free": 10 * GB},
    )
    disk = admin_telemetry.project_disk({"database_bytes": 1 * GB, "codal_bytes": 0}, [])

    assert disk["days_to_80pct"] == 0
    assert disk["alert"] is True


def test_disk_projection_says_unknown_rather_than_healthy_when_unmeasurable(monkeypatch):
    from marketdata import admin_telemetry

    monkeypatch.setattr(admin_telemetry, "filesystem_usage", lambda path="/": None)
    disk = admin_telemetry.project_disk({"database_bytes": 1 * GB, "codal_bytes": 0}, [])

    assert disk["filesystem_total_bytes"] is None
    assert disk["days_to_80pct"] is None
    assert disk["alert"] is False


def test_operational_health_check_alerts_on_a_nearly_full_device(monkeypatch):
    """The alert used to call project_disk() bare, so its numerator was always 0.

    With no argument `database_bytes` defaults to 0, which against the old
    200 GB target meant the alert could only fire if the box grew 6.7 GB/day
    while reporting no usage at all. It had never fired and could not.
    """
    from marketdata import admin_telemetry, tasks

    # Past the 80% line, so this asserts only that the alert is reachable at
    # all -- not how the growth rate is derived, which the projection tests
    # above cover directly.
    monkeypatch.setattr(
        admin_telemetry,
        "filesystem_usage",
        lambda path="/": {"total": 147 * GB, "used": 130 * GB, "free": 17 * GB},
    )
    cache.clear()

    result = tasks.operational_health_check()

    assert "disk-projection" in result["alerts"]
