from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone
import pytest

from marketdata.admin_telemetry import _codal_history_status, _codal_status
from marketdata.codal_history import RequestBudget, scan_window
from marketdata.fetchers import MarketDataFetchError
from marketdata.models import CodalAnnouncement, CodalHistoryWindow, MarketInstrument
from marketdata.quota import QuotaExhausted


pytestmark = pytest.mark.django_db


def _record(index, *, symbol="فولاد", date="1402-06-15"):
    return {
        "l18": symbol,
        "l30": "فولاد مبارکه",
        "title": "صورت‌های مالی",
        "code": f"ن-{index}",
        "date_publish": date,
        "time_publish": "12:00:00",
        "link": "https://codal.ir/Reports/Decision.aspx",
    }


def test_history_window_verifies_all_pages_and_issuer_keys(monkeypatch):
    rows = [_record(index) for index in range(21)]
    rows[1] = {**rows[1], "l18": "تابعه", "code": rows[0]["code"]}
    pages = []

    def fetch(_key, *, symbol, date_start, date_end, page):
        assert (symbol, date_start, date_end) == ("فولاد", "1402-01-01", "1402-12-29")
        pages.append(page)
        return {"count_announcement": 21, "announcement": rows[(page - 1) * 20:page * 20]}

    monkeypatch.setattr("marketdata.codal_history.fetch_codal_announcements", fetch)
    window = CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-01-01", date_end="1402-12-29"
    )
    budget = RequestBudget(10)
    assert scan_window(window, budget, max_pages=3) == "complete"
    assert (window.expected_rows, window.stored_rows, window.verified_complete) == (21, 21, True)
    assert CodalAnnouncement.objects.count() == 21
    assert scan_window(window, budget, max_pages=3) == "complete"
    assert CodalAnnouncement.objects.count() == 21
    assert pages == [1, 2, 1, 2]
    assert budget.used == 4


def test_oversized_window_splits_without_claiming_coverage(monkeypatch):
    def fetch(_key, *, page, **_kwargs):
        assert page == 1
        return {"count_announcement": 201, "announcement": [_record(i) for i in range(20)]}

    monkeypatch.setattr("marketdata.codal_history.fetch_codal_announcements", fetch)
    parent = CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-01-01", date_end="1402-12-29"
    )
    assert scan_window(parent, RequestBudget(10), max_pages=10) == "split"
    parent.refresh_from_db()
    assert parent.split and not parent.verified_complete
    children = list(CodalHistoryWindow.objects.exclude(pk=parent.pk).order_by("date_start"))
    assert len(children) == 2
    assert children[0].date_start == parent.date_start
    assert children[1].date_end == parent.date_end
    from marketdata.jalali import to_gregorian
    from datetime import timedelta
    assert to_gregorian(children[0].date_end) + timedelta(days=1) == to_gregorian(children[1].date_start)
    assert CodalAnnouncement.objects.count() == 0


def test_out_of_range_provider_response_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "marketdata.codal_history.fetch_codal_announcements",
        lambda *_args, **_kwargs: {
            "count_announcement": 1,
            "announcement": [_record(1, date="1403-01-01")],
        },
    )
    window = CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-01-01", date_end="1402-12-29"
    )
    with pytest.raises(MarketDataFetchError, match="out-of-window"):
        scan_window(window, RequestBudget(10), max_pages=10)
    assert not window.verified_complete
    assert CodalAnnouncement.objects.count() == 0


def test_quota_pause_restarts_window_without_false_completion(monkeypatch):
    rows = [_record(index) for index in range(21)]
    paused = True

    def fetch(_key, *, page, **_kwargs):
        nonlocal paused
        if page == 2 and paused:
            paused = False
            raise QuotaExhausted("Daily quota reached")
        return {"count_announcement": 21, "announcement": rows[(page - 1) * 20:page * 20]}

    monkeypatch.setattr("marketdata.codal_history.fetch_codal_announcements", fetch)
    window = CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-01-01", date_end="1402-12-29"
    )
    budget = RequestBudget(10)
    with pytest.raises(QuotaExhausted):
        scan_window(window, budget, max_pages=3)
    window.refresh_from_db()
    assert not window.verified_complete
    assert CodalAnnouncement.objects.count() == 20
    assert scan_window(window, budget, max_pages=3) == "complete"
    assert CodalAnnouncement.objects.count() == 21
    assert budget.used == 4


def test_command_is_read_only_until_execute(monkeypatch, capsys):
    def fetch(_key, **_kwargs):
        return {"count_announcement": 1, "announcement": [_record(1)]}

    monkeypatch.setattr("marketdata.codal_history.fetch_codal_announcements", fetch)
    args = ["--start-year", "1402", "--end-year", "1402", "--symbols", "فولاد"]
    call_command("backfill_codal_history", *args)
    assert CodalHistoryWindow.objects.count() == 0
    call_command("backfill_codal_history", *args, "--execute", "--max-requests", "10")
    assert CodalHistoryWindow.objects.get().verified_complete
    assert "windows complete: 1" in capsys.readouterr().out


def test_stale_completed_window_can_be_reverified(monkeypatch):
    rows = [_record(1)]
    monkeypatch.setattr(
        "marketdata.codal_history.fetch_codal_announcements",
        lambda *_args, **_kwargs: {"count_announcement": len(rows), "announcement": rows},
    )
    args = ["--start-year", "1402", "--end-year", "1402", "--symbols", "فولاد"]
    call_command("backfill_codal_history", *args, "--execute", "--max-requests", "10")
    window = CodalHistoryWindow.objects.get()
    window.last_success_at = timezone.now() - timedelta(days=400)
    window.save(update_fields=["last_success_at"])
    rows.append(_record(2))
    call_command(
        "backfill_codal_history", *args, "--execute", "--max-requests", "10",
        "--recheck-after-days", "365",
    )
    window.refresh_from_db()
    assert window.verified_complete
    assert (window.expected_rows, window.stored_rows) == (2, 2)


def test_ops_history_counts_only_leaf_windows_as_verifications(settings):
    for symbol in ("فولاد", "فملی"):
        MarketInstrument.objects.create(
            source=MarketInstrument.Source.TSETMC,
            category=MarketInstrument.Category.STOCK,
            symbol=symbol,
            eligible=True,
        )
    CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-01-01", date_end="1402-12-29", split=True
    )
    CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-01-01", date_end="1402-06-31",
        verified_complete=True, last_success_at=timezone.now(),
    )
    CodalHistoryWindow.objects.create(
        symbol="فولاد", date_start="1402-07-01", date_end="1402-12-29",
        consecutive_failures=1,
    )
    CodalHistoryWindow.objects.create(
        symbol="فملی", date_start="1403-01-01", date_end="1403-12-30",
        verified_complete=True, last_success_at=timezone.now() - timedelta(days=400),
    )
    status = _codal_history_status()
    assert status["catalog_stocks"] == status["symbols_started"] == 2
    assert status["leaf_windows"] == 3
    assert status["verified_leaf_windows"] == 2
    assert status["open_leaf_windows"] == status["failed_leaf_windows"] == 1
    assert status["split_parent_windows"] == status["stale_verified_leaf_windows"] == 1
    settings.CODAL_ENABLED = False
    assert _codal_status()["history_discovery"] == status
