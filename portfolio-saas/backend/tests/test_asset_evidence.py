"""Per-asset evidence inspector: named claims without docker/psql."""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from marketdata.models import (
    ArchiveFetchState,
    MarketCandle,
    MarketInstrument,
    RejectedRecord,
    SymbolIntegrity,
    WorkflowRun,
)
from marketdata.workflows import WorkflowOutcome, current_correlation_id
from portfolio.models import Price


pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(db, make_user):
    user = make_user(email="evidence-admin@example.com")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _kama(asset_catalog):
    MarketInstrument.objects.create(
        symbol="کاما",
        name="Kama",
        source=MarketInstrument.Source.TSETMC,
        eligible=True,
    )
    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    ArchiveFetchState.objects.create(
        symbol="کاما",
        endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
        stored_rows=4512,
        expected_rows=4512,
        missing_rows=0,
        verified_complete=True,
    )
    ArchiveFetchState.objects.create(
        symbol="کاما",
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        stored_rows=10,
        expected_rows=55,
        missing_rows=45,
        consecutive_failures=5,
        verified_complete=False,
        last_error="Provider returned zero records where records were expected",
    )
    SymbolIntegrity.objects.create(
        symbol="کاما",
        coverage_ratio=0.883,
        max_gap_days=3,
        rejected_count=12,
        passes_gate=False,
        reason="low_coverage,excessive_rejections",
    )
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-01-15",
        close_price=Decimal("4000"),
        volume=1000,
    )
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted",
        symbol="کاما",
        date="1405-01-01",
        reason="series_spike",
    )
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome="partial",
        endpoint="stock_transaction_ticks",
        symbol="کاما",
        error_code="tick_volume_mismatch",
    )
    Price.objects.create(asset=asset, price=Decimal("4000"), source="API")
    return asset


def test_evidence_requires_staff(asset_catalog, make_user):
    _kama(asset_catalog)
    client = APIClient()
    assert client.get("/api/admin/assets/kama_stock/evidence/").status_code in (401, 403)
    client.force_authenticate(user=make_user(email="free-evidence@example.com"))
    assert client.get("/api/admin/assets/kama_stock/evidence/").status_code == 403


def test_kama_archive_complete_and_gate_fail_disagree(staff_client, asset_catalog):
    _kama(asset_catalog)
    res = staff_client.get("/api/admin/assets/kama_stock/evidence/")
    assert res.status_code == 200
    body = res.json()
    claims = {c["id"]: c["passed"] for c in body["claims"]}
    assert claims["archive_payload_verified"] is True
    assert claims["analytics_gate_179d"] is False
    assert body["identity"]["tse_symbol"] == "کاما"
    assert body["identity"]["key"] == "kama_stock"
    assert "resync_symbol_from_provider" in body["suggested_cli"]
    assert any(s["endpoint"] == "stock_candle_adjusted" and s["verified_complete"] for s in body["archive_states"])
    assert body["integrity"]["passes_gate"] is False
    assert body["rejected"]["count"] >= 1
    assert body["workflows"]


def test_evidence_lookup_by_tse_symbol(staff_client, asset_catalog):
    _kama(asset_catalog)
    res = staff_client.get("/api/admin/assets/کاما/evidence/")
    assert res.status_code == 200
    assert res.json()["identity"]["key"] == "kama_stock"


def test_recompute_integrity_audits(staff_client, asset_catalog):
    _kama(asset_catalog)
    bad = staff_client.post("/api/admin/assets/kama_stock/recompute-integrity/", {}, format="json")
    assert bad.status_code == 400
    ok = staff_client.post(
        "/api/admin/assets/kama_stock/recompute-integrity/",
        {"confirm": True},
        format="json",
    )
    assert ok.status_code == 200
    assert WorkflowRun.objects.filter(workflow="ops_recompute_integrity").exists()


def test_symbol_retry_enqueues(staff_client, asset_catalog, monkeypatch):
    _kama(asset_catalog)
    calls = []

    class DummyTask:
        def delay(self, state_id):
            calls.append(state_id)

    monkeypatch.setattr("marketdata.tasks.retry_archive_job_task", DummyTask())
    monkeypatch.setattr(
        "marketdata.admin_api.get_quota_status",
        lambda: {"limit": 100, "used": 1, "remaining_daily": 99},
    )

    class FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr("redis.Redis.from_url", lambda url: FakeRedis())
    ok = staff_client.post(
        "/api/admin/assets/kama_stock/retry/",
        {"confirm": True},
        format="json",
    )
    assert ok.status_code == 200
    assert ok.json()["queued"]
    assert calls


def test_ingest_stamps_correlation_id():
    from marketdata.ingest import ingest_candles

    outcome = WorkflowOutcome("test_ingest", endpoint="stock_candle_adjusted", symbol="FOO")
    assert current_correlation_id() == outcome.correlation_id
    ingest_candles("FOO", 3, {
        "candle_daily_adjusted": [
            {"date": "1404-02-24", "open": 7380, "high": 7400, "low": 7280, "close": 7340, "volume": 180715348},
        ]
    })
    outcome.finish("success", rows_accepted=1)
    row = MarketCandle.objects.filter(symbol="FOO", timeframe=MarketCandle.ADJUSTED).first()
    assert row is not None
    assert row.last_correlation_id == outcome.correlation_id
    assert row.ingested_at is not None


def test_value_user_flattens_items(asset_catalog, make_user):
    from portfolio.models import Account, Holding
    from portfolio.services.valuation import value_user

    user = make_user(email="flatten@example.com")
    account = Account.objects.create(user=user, name="A")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=1)
    Price.objects.create(asset=asset_catalog["emami_coin"], price=Decimal("10"), source="API")
    payload = value_user(user)
    assert payload["items"]
    assert payload["items"][0]["key"] == "emami_coin"
    assert payload["items"][0]["account_id"] == account.id


def test_workflows_failed_only(staff_client):
    WorkflowRun.objects.create(workflow="archive", outcome="success")
    WorkflowRun.objects.create(workflow="archive", outcome="failed", error_code="boom")
    res = staff_client.get("/api/admin/workflows/?failed_only=true")
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert res.json()["results"][0]["outcome"] == "failed"
