"""marketdata.codal_pipeline.extract_report: the CodalReport.status it picks
must match the retry policy documented in the module's docstring -- transient
fetch/store problems retried, a document that fails its own validation not
retried forever. Network/storage/parsing are mocked; reserve_request(ARCHIVE)
runs for real against the test DB's quota row."""
import pytest

from marketdata import codal_pipeline
from marketdata.codal_parsers import ParsedDocument
from marketdata.codal_storage import CodalArtifactRejected, CodalBlockedNetwork
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalReport
from marketdata.quota import QuotaExhausted

pytestmark = pytest.mark.django_db


def _announcement(**kwargs):
    defaults = dict(
        symbol="فملی",
        title="گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به ۱۴۰۴/۰۱/۳۱ (مقدار فروش، نرخ فروش)",
        link_excel="https://excel.codal.ir/report.xlsx",
    )
    defaults.update(kwargs)
    return CodalAnnouncement.objects.create(**defaults)


def test_happy_path_downloads_stores_parses_and_marks_parsed(monkeypatch):
    announcement = _announcement()

    monkeypatch.setattr(
        codal_pipeline, "download_artifact",
        lambda url, kind: (url, "application/vnd.ms-excel", b"fake-bytes"),
    )
    monkeypatch.setattr(
        codal_pipeline, "store_artifact",
        lambda content, content_type, kind: ("codal/sha256/ab/deadbeef.xlsx", "deadbeef"),
    )
    parsed = ParsedDocument(tables=[{
        "name": "s1", "sheet_name": "s1",
        "headers": ["محصول", "مقدار فروش"],
        "rows": [["کاما", "100"]],
        "source_coordinates": {},
    }])
    monkeypatch.setattr(codal_pipeline, "parse_artifact", lambda kind, content: parsed)

    report, result = codal_pipeline.extract_report(announcement.pk)

    assert report.status == CodalReport.Status.PARSED
    assert report.quality == CodalReport.Quality.VALIDATED
    assert result["fact_count"] == 1
    artifact = CodalArtifact.objects.get(report=report)
    assert artifact.fetch_status == CodalArtifact.FetchStatus.STORED
    assert artifact.s3_key == "codal/sha256/ab/deadbeef.xlsx"


def test_network_failure_is_retryable_status(monkeypatch):
    announcement = _announcement()

    def _boom(url, kind):
        raise CodalBlockedNetwork("HTTP503@excel.codal.ir")

    monkeypatch.setattr(codal_pipeline, "download_artifact", _boom)

    report, result = codal_pipeline.extract_report(announcement.pk)

    assert report.status == CodalReport.Status.BLOCKED_NETWORK
    assert report.error_code == "no_usable_artifact"


def test_all_artifacts_rejected_is_permanent_not_retryable(monkeypatch):
    announcement = _announcement()

    def _reject(url, kind):
        raise CodalArtifactRejected("invalid_content_signature")

    monkeypatch.setattr(codal_pipeline, "download_artifact", _reject)

    report, result = codal_pipeline.extract_report(announcement.pk)

    assert report.status == CodalReport.Status.UNSUPPORTED
    assert report.error_code == "all_artifacts_rejected"


def test_no_artifact_links_is_permanent_not_retryable():
    announcement = _announcement(link_excel="", link="", link_pdf="", link_attachment="")

    report, result = codal_pipeline.extract_report(announcement.pk)

    assert report.status == CodalReport.Status.UNSUPPORTED
    assert report.error_code == "no_artifact_links"


def test_quota_exhaustion_propagates_and_stops_the_report(monkeypatch):
    announcement = _announcement()

    def _exhausted(url, kind):
        raise AssertionError("download_artifact should not run past reserve_request")

    def _reserve(bucket):
        raise QuotaExhausted("Daily archive request budget exhausted (7900).")

    monkeypatch.setattr(codal_pipeline, "download_artifact", _exhausted)
    monkeypatch.setattr(codal_pipeline, "reserve_request", _reserve)

    with pytest.raises(QuotaExhausted):
        codal_pipeline.extract_report(announcement.pk)

    report = CodalReport.objects.get(announcement=announcement)
    assert report.status == CodalReport.Status.FETCHING  # stuck, not misclassified as a failure


def test_unreconciled_category_is_needs_review_not_parsed(monkeypatch):
    # A monthly-report title (category resolves via classify_announcement's
    # title fallback) but the artifact's table has no matching columns at all.
    announcement = _announcement(title="گزارش فعالیت ماهانه شرکت")
    monkeypatch.setattr(
        codal_pipeline, "download_artifact",
        lambda url, kind: (url, "application/vnd.ms-excel", b"x"),
    )
    monkeypatch.setattr(
        codal_pipeline, "store_artifact", lambda content, content_type, kind: ("k", "c"),
    )
    parsed = ParsedDocument(tables=[{
        "name": "s1", "sheet_name": "s1",
        "headers": ["ستون نامربوط"], "rows": [["x"]], "source_coordinates": {},
    }])
    monkeypatch.setattr(codal_pipeline, "parse_artifact", lambda kind, content: parsed)

    report, result = codal_pipeline.extract_report(announcement.pk)

    assert report.status == CodalReport.Status.NEEDS_REVIEW
    assert report.error_code.startswith("no_typed_facts")


def test_regression_guard_refuses_to_overwrite_good_facts_with_nothing(monkeypatch):
    announcement = _announcement()
    monkeypatch.setattr(
        codal_pipeline, "download_artifact",
        lambda url, kind: (url, "application/vnd.ms-excel", b"x"),
    )
    monkeypatch.setattr(
        codal_pipeline, "store_artifact", lambda content, content_type, kind: ("k", "c"),
    )
    good_parse = ParsedDocument(tables=[{
        "name": "s1", "sheet_name": "s1",
        "headers": ["مقدار فروش"], "rows": [["100"]], "source_coordinates": {},
    }])
    monkeypatch.setattr(codal_pipeline, "parse_artifact", lambda kind, content: good_parse)
    report, _ = codal_pipeline.extract_report(announcement.pk)
    assert report.facts.count() == 1

    # Non-empty (so it passes the "nothing parseable at all" check) but no
    # header matches this category's fact table -- extract_typed_facts yields
    # zero facts, which is exactly the case the regression guard exists for.
    no_match_parse = ParsedDocument(tables=[{
        "name": "s1", "sheet_name": "s1",
        "headers": ["ستون نامربوط"], "rows": [["x"]], "source_coordinates": {},
    }])
    monkeypatch.setattr(codal_pipeline, "parse_artifact", lambda kind, content: no_match_parse)
    with pytest.raises(codal_pipeline.CodalExtractionRegressed):
        codal_pipeline.extract_report(announcement.pk)

    report.refresh_from_db()
    assert report.facts.count() == 1  # untouched by the failed re-parse
