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


# ----------------------------------------------------------------------
# Direct discovery from codal.ir search (phase 1, shadow mode).
# docs/CODAL-DIRECT-MIGRATION.md; fixtures captured from production 2026-10-05.

import json
from pathlib import Path

from django.core.cache import cache

from marketdata import codal_discovery
from marketdata.models import CodalDiscoveryDay, CodalLetter
from marketdata.sources import codal_search
from marketdata.sources.http import SourceResponseError

_FIXTURES = Path(__file__).parent / "fixtures" / "codal_direct"


def _page():
    return json.loads((_FIXTURES / "v2_q_day_page1_trimmed.json").read_text())


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_normalize_letter_folds_digits_and_decodes_the_serial():
    row = codal_search.normalize_letter(_page()["Letters"][0])
    assert row["tracing_no"] == 1409625
    assert row["letter_code"] == "ن-30"
    assert (row["date_publish"], row["time_publish"]) == ("1404-07-02", "21:12:23")
    # %3d decoded with unquote: the same serial a BrsApi link carries.
    assert row["letter_serial"] == "UdN7Z4ZTiFs2puG8H2sIiw=="
    assert row["letter_type"] == 58
    assert row["raw"]["TracingNo"] == 1409625


def test_a_literal_plus_in_a_serial_survives_decoding():
    """parse_qsl turns "+" into a space; the serial would then match nothing."""
    letter = {**_page()["Letters"][0], "Url": "/Reports/Decision.aspx?LetterSerial=ab+cd%2Bef%3d&let=6"}
    assert codal_search.normalize_letter(letter)["letter_serial"] == "ab+cd+ef="


def test_a_429_parks_the_origin_and_halves_the_budget(monkeypatch, settings):
    settings.CODAL_SEARCH_START_PER_HOUR = 12

    def refuse(*args, **kwargs):
        raise SourceResponseError("429", origin="codal_search", status_code=429)

    monkeypatch.setattr(codal_search, "fetch", refuse)
    assert codal_search.can_send()
    with pytest.raises(codal_search.CodalSearchThrottled):
        codal_search.fetch_letters("1404-07-02", 1)
    assert not codal_search.can_send()
    assert cache.get(codal_search._RATE_KEY) == 6


def test_the_hourly_budget_is_never_exceeded(settings):
    settings.CODAL_SEARCH_START_PER_HOUR = 3
    now = 1_000_000.0
    for offset in range(3):
        assert codal_search.can_send(now + offset)
        codal_search._record_sent(now + offset)
    assert not codal_search.can_send(now + 10)
    assert codal_search.can_send(now + 3601)


def test_is_attacker_counts_as_a_refusal(monkeypatch):
    monkeypatch.setattr(codal_search, "fetch", lambda *a, **k: {**_page(), "IsAttacker": True})
    with pytest.raises(codal_search.CodalSearchThrottled):
        codal_search.fetch_letters("1404-07-02", 1)
    assert not codal_search.can_send()


def test_an_unexpected_shape_raises_instead_of_reading_as_a_quiet_day(monkeypatch):
    monkeypatch.setattr(codal_search, "fetch", lambda *a, **k: {"Message": "nope"})
    with pytest.raises(SourceResponseError):
        codal_search.fetch_letters("1404-07-02", 1)


def _serve(monkeypatch, letters, total, pages):
    rows = [codal_search.normalize_letter(letter) for letter in letters]
    monkeypatch.setattr(
        codal_search, "fetch_letters",
        lambda day, page: {"total": total, "pages": pages, "letters": rows},
    )


def test_a_day_completes_when_stored_letters_reach_the_total(monkeypatch):
    _serve(monkeypatch, _page()["Letters"], total=4, pages=1)
    state = CodalDiscoveryDay.objects.create(date="1404-07-02")
    result = codal_discovery.step(state)
    state.refresh_from_db()
    assert result["complete"] and state.verified_complete
    assert CodalLetter.objects.filter(date_publish="1404-07-02").count() == 4
    assert state.next_check_at is not None


def test_reaching_the_last_page_short_restarts_with_backoff(monkeypatch):
    _serve(monkeypatch, _page()["Letters"], total=290, pages=1)
    state = CodalDiscoveryDay.objects.create(date="1404-07-02")
    codal_discovery.step(state)
    state.refresh_from_db()
    assert not state.verified_complete
    assert state.next_page == 1 and state.consecutive_failures == 1
    assert state.last_error == "short: stored 4 of 290"
    codal_discovery.step(state)
    state.refresh_from_db()
    assert state.consecutive_failures == 2


def test_refetching_a_letter_updates_it_in_place(monkeypatch):
    letters = _page()["Letters"]
    _serve(monkeypatch, letters, total=4, pages=1)
    codal_discovery.step(CodalDiscoveryDay.objects.create(date="1404-07-02"))
    _serve(monkeypatch, [{**letters[0], "Title": "اصلاحیه"}], total=4, pages=1)
    codal_discovery.step(CodalDiscoveryDay.objects.get(date="1404-07-02"))
    assert CodalLetter.objects.count() == 4
    assert CodalLetter.objects.get(tracing_no=1409625).title == "اصلاحیه"


def test_pick_day_serves_the_live_window_first_then_walks_back(monkeypatch, settings):
    settings.CODAL_DISCOVERY_LIVE_DAYS = 1
    settings.CODAL_DISCOVERY_OLDEST_DAY = "1404-06-29"
    monkeypatch.setattr(codal_discovery, "tehran_today", lambda: "1404-07-02")
    now = timezone.now()
    assert codal_discovery.pick_day(now).date == "1404-07-02"
    for day in ("1404-07-02", "1404-07-01"):
        CodalDiscoveryDay.objects.update_or_create(date=day, defaults={
            "verified_complete": True, "last_success_at": now,
            "next_check_at": now + timedelta(days=7),
        })
    assert codal_discovery.pick_day(now).date == "1404-06-31"
    CodalDiscoveryDay.objects.filter(date="1404-06-31").update(verified_complete=True)
    assert codal_discovery.pick_day(now).date == "1404-06-30"
    CodalDiscoveryDay.objects.filter(date="1404-06-30").update(verified_complete=True)
    assert codal_discovery.pick_day(now).date == "1404-06-29"
    CodalDiscoveryDay.objects.filter(date="1404-06-29").update(verified_complete=True)
    assert codal_discovery.pick_day(now) is None
    # A stale live day is reopened ahead of everything else.
    assert codal_discovery.pick_day(now + timedelta(minutes=16)).date == "1404-07-02"


def test_disabled_discovery_spends_nothing(monkeypatch, settings):
    from marketdata import tasks

    settings.CODAL_DISCOVERY_ENABLED = False
    monkeypatch.setattr(codal_search, "fetch_letters", lambda *a: pytest.fail("fetched"))
    assert tasks.codal_discovery_tick() == "idle"
    assert not CodalDiscoveryDay.objects.exists()


def test_crossref_matches_by_serial_then_by_key_and_counts_the_rest(monkeypatch):
    from marketdata.management.commands.codal_crossref import crossref_day

    letters = _page()["Letters"]
    _serve(monkeypatch, letters, total=4, pages=1)
    codal_discovery.step(CodalDiscoveryDay.objects.create(date="1404-07-02"))
    first, second = (codal_search.normalize_letter(letter) for letter in letters[:2])
    CodalAnnouncement.objects.create(
        symbol=first["symbol"], code=first["letter_code"], title="t",
        date_publish="1404-07-02", time_publish=first["time_publish"],
        link="https://codal.ir/Reports/Decision.aspx?LetterSerial="
             + first["letter_serial"].replace("=", "%3d"),
    )
    CodalAnnouncement.objects.create(
        symbol=second["symbol"], code=second["letter_code"], title="t",
        date_publish="1404-07-02", time_publish=second["time_publish"], link="",
    )
    CodalAnnouncement.objects.create(
        symbol="X", code="ن-1", title="t", date_publish="1404-07-02", time_publish="09:00:00",
    )
    stats, disagree = crossref_day("1404-07-02", catalog={first["symbol"]})
    assert stats["matched_serial"] == 1 and stats["matched_key"] == 1
    assert stats["new_catalog"] + stats["new_other"] == 2
    assert stats["ours_only"] == 1 and not disagree
