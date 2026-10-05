"""Request latency measurement: middleware, client ingest, flush, report."""
from datetime import datetime, timezone as dt_timezone

import pytest
from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APIClient

from perf import recorder, report
from perf.models import RequestPerfRollup


@pytest.fixture(autouse=True)
def _clean_perf_store():
    recorder.reset_local_store()
    cache.clear()
    yield
    recorder.reset_local_store()


def test_middleware_records_route_pattern_and_server_timing(db):
    client = APIClient()
    response = client.get("/api/auth/me/")
    assert response.status_code == 401
    assert response["Server-Timing"].startswith("app;dur=")
    assert "queries" in response["Server-Timing"]

    rows = recorder.read_bucket(recorder.hour_bucket())
    keys = [k for k in rows if k[0] == "api"]
    assert len(keys) == 1
    source, route, method, _state = keys[0]
    assert route == "api/auth/me/"
    assert method == "GET"
    row = rows[keys[0]]
    assert row["count"] == 1
    assert row["client_errors"] == 1
    assert sum(row["hist"]) == 1


def test_route_label_never_contains_raw_ids(db, django_user_model):
    user = django_user_model.objects.create_user(email="p@x.com", password="pw123456789")
    client = APIClient()
    client.force_authenticate(user)
    client.get("/api/accounts/987654/valuation/")
    routes = {k[1] for k in recorder.read_bucket(recorder.hour_bucket())}
    assert not any("987654" in r for r in routes)


def test_health_and_beacon_requests_are_not_measured(db):
    client = APIClient()
    client.get("/api/health/")
    client.post("/api/perf/client/", {"events": []}, format="json")
    assert not [k for k in recorder.read_bucket(recorder.hour_bucket()) if k[0] == "api"]


def test_flush_is_idempotent(db):
    for ms in (40, 900, 3000):
        recorder.record(source="api", route="api/x/", method="GET", market_state="open", ms=ms,
                        db_queries=5, db_ms=2.5)
    assert report.flush() == 1
    assert report.flush() == 1  # rewrite, not increment
    row = RequestPerfRollup.objects.get()
    assert row.count == 3
    assert row.sum_db_queries == 15
    assert row.max_ms == 3000
    assert sum(row.hist) == 3


def test_client_ingest_whitelists_routes(db):
    client = APIClient()
    response = client.post("/api/perf/client/", {"events": [
        {"kind": "page", "route": "/:summary", "ms": 1200},
        {"kind": "page", "route": "/:made-up-view", "ms": 800},     # view dropped
        {"kind": "page", "route": "/evil/path", "ms": 800},          # rejected
        {"kind": "boot", "ms": 2100},
        {"kind": "api", "route": "/api/accounts/55/performance/?basis=x", "method": "GET",
         "status": 200, "ms": 640},
        {"kind": "api", "route": "/not-api/", "method": "GET", "ms": 10},  # rejected
        {"kind": "api", "route": "/api/assets/", "method": "TRACE", "ms": 10},  # rejected
        {"kind": "page", "route": "/", "ms": -5},                    # rejected
    ]}, format="json")
    assert response.status_code == 202
    assert response.data["accepted"] == 4
    keys = {(k[0], k[1]) for k in recorder.read_bucket(recorder.hour_bucket())}
    assert ("client_page", "/:summary") in keys
    assert ("client_page", "/") in keys
    assert ("client_boot", "boot") in keys
    assert ("client_api", "api/accounts/<int:account_id>/performance/") in keys
    assert len(keys) == 4


def test_client_ingest_rejects_non_list(db):
    response = APIClient().post("/api/perf/client/", {"events": "x"}, format="json")
    assert response.status_code == 400


def test_report_splits_by_market_state_and_day_type(db):
    # 2026-10-05 is a Monday (Jalali weekday 2: trading); 2026-10-09 a Friday.
    monday = datetime(2026, 10, 5, 6, 0, tzinfo=dt_timezone.utc).timestamp()
    friday = datetime(2026, 10, 9, 6, 0, tzinfo=dt_timezone.utc).timestamp()
    recorder.record(source="api", route="api/valuation/", method="GET", market_state="open",
                    ms=2000, ts=monday)
    recorder.record(source="api", route="api/valuation/", method="GET", market_state="closed_daytime",
                    ms=100, ts=friday)
    report.flush(now_ts=monday, lookback_hours=1)
    report.flush(now_ts=friday, lookback_hours=1)

    data = report.summarize(since=datetime(2026, 10, 1, tzinfo=dt_timezone.utc),
                            until=datetime(2026, 10, 10, tzinfo=dt_timezone.utc))
    assert data["by_market_state"]["open"]["avg_ms"] == 2000
    assert data["by_market_state"]["closed_daytime"]["avg_ms"] == 100
    assert data["by_day_type"]["trading_day"]["count"] == 1
    assert data["by_day_type"]["weekend"]["count"] == 1
    route = data["routes"][0]
    assert route["route"] == "api/valuation/"
    assert route["count"] == 2
    # 06:00 UTC is 09:30 Tehran.
    assert "9" in data["by_tehran_hour"]


def test_report_endpoint_is_staff_only(db, django_user_model):
    client = APIClient()
    assert client.get("/api/perf/report/").status_code in (401, 403)
    user = django_user_model.objects.create_user(email="u@x.com", password="pw123456789")
    client.force_authenticate(user)
    assert client.get("/api/perf/report/").status_code == 403
    admin = django_user_model.objects.create_user(
        email="a@x.com", password="pw123456789", is_staff=True, is_superuser=True,
    )
    client.force_authenticate(admin)
    response = client.get("/api/perf/report/?days=1&source=api")
    assert response.status_code == 200
    assert "routes" in response.data


def test_percentile_from_hist():
    hist = [0] * recorder.HIST_LEN
    hist[recorder.histogram_index(40)] = 90
    hist[recorder.histogram_index(3000)] = 10
    assert recorder.percentile_from_hist(hist, 0.5) == 50
    assert recorder.percentile_from_hist(hist, 0.95) == 4000
    assert recorder.percentile_from_hist([0] * recorder.HIST_LEN, 0.5) is None


def test_perf_report_command_runs(db, capsys):
    recorder.record(source="api", route="api/x/", method="GET", market_state="open", ms=12)
    call_command("perf_report", "--flush", "--days", "1")
    out = capsys.readouterr().out
    assert "api/x/" in out
