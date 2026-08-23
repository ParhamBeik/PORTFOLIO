"""The Codal disclosure pipeline: parsing, classification, artifact storage, and the end-to-end extraction path.

Merged from 4 files; each section keeps its original banner.
"""

from decimal import Decimal

from django.core.management import call_command
import pytest

from marketdata import codal_parsers
from marketdata import codal_pipeline
from marketdata import codal_storage
from marketdata.codal_classification import (
    TIER_1,
    TIER_2,
    TIER_3,
    classify,
    normalize_title,
)
from marketdata.codal_parsers import ParsedDocument
from marketdata.codal_storage import CodalArtifactRejected, CodalBlockedNetwork
from marketdata.models import CodalAnnouncement
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalReport

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_codal_parsers.py
# Unit tests for marketdata/codal_parsers.py: Persian number normalization,
# the longest-keyword-match fix (see _match_fact_code docstring), and that
# parse_pdf degrades to an empty ParsedDocument rather than crashing or OCR'ing
# when there is no text layer.


def test_parse_number_handles_persian_digits_and_thousands_separators():
    assert codal_parsers.parse_number("۱٬۲۳۴٫۵") == Decimal("1234.5")


def test_parse_number_handles_parenthesized_negatives():
    assert codal_parsers.parse_number("(500)") == Decimal("-500")


def test_parse_number_returns_none_for_non_numeric():
    assert codal_parsers.parse_number("سود") is None


def test_longest_keyword_match_does_not_collapse_rate_and_revenue_into_quantity():
    fields = codal_parsers.CATEGORY_FIELDS[3]
    assert codal_parsers._match_fact_code("نرخ فروش", fields) == "sales.rate"
    assert codal_parsers._match_fact_code("مبلغ فروش", fields) == "sales.revenue"
    assert codal_parsers._match_fact_code("مقدار فروش", fields) == "sales.quantity"


def test_extract_typed_facts_from_a_table():
    parsed = codal_parsers.ParsedDocument(tables=[{
        "name": "sheet1", "sheet_name": "sheet1",
        "headers": ["محصول", "مقدار فروش", "نرخ فروش"],
        "rows": [["کاما", "۱۰۰", "۲۰۰۰"]],
        "source_coordinates": {},
    }])
    result = codal_parsers.extract_typed_facts(parsed, category=3)
    codes = {fact["fact_code"] for fact in result.facts}
    assert codes == {"sales.quantity", "sales.rate"}
    assert codal_parsers.category_reconciles(result, category=3) is True


def test_category_reconciles_is_false_without_facts():
    parsed = codal_parsers.ParsedDocument()
    assert codal_parsers.category_reconciles(parsed, category=3) is False


def test_parse_pdf_with_no_text_layer_yields_empty_document_not_a_crash():
    from io import BytesIO

    # pypdf ships in requirements.txt (the Codal worker needs it) but is not
    # required to run the rest of the suite on a bare local venv.
    PdfWriter = pytest.importorskip("pypdf").PdfWriter

    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)

    parsed = codal_parsers.parse_pdf(buffer.getvalue())
    assert parsed.text == ""
    assert parsed.used_ocr is False
    assert parsed.confidence == 1.0


# ----------------------------------------------------------------------
# test_codal_pipeline.py
# marketdata.codal_pipeline.extract_report: the CodalReport.status it picks
# must match the retry policy documented in the module's docstring -- transient
# fetch/store problems retried, a document that fails its own validation not
# retried forever. Network/storage/parsing are mocked. Note that this path spends
# no provider quota at all: codal.ir is not the metered API.


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


def test_artifact_download_spends_no_provider_quota(monkeypatch):
    """Documents come from codal.ir, not from the metered provider.

    Reserving ARCHIVE quota per artifact charged ~1,000 requests/day of BrsApi's
    allowance to a host BrsApi has nothing to do with, and coupled document
    extraction to a budget running out for unrelated reasons.
    """
    from marketdata import quota
    from marketdata.models import ApiRequestQuota

    announcement = _announcement()
    monkeypatch.setattr(
        codal_pipeline, "download_artifact",
        lambda url, kind: (url, "application/vnd.ms-excel", b"x"),
    )
    monkeypatch.setattr(
        codal_pipeline, "store_artifact", lambda content, content_type, kind: ("k", "c"),
    )

    codal_pipeline.extract_report(announcement.pk)

    assert not ApiRequestQuota.objects.filter(day=quota.quota_day()).exists()


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


# ----------------------------------------------------------------------
# test_codal_storage.py
# Unit tests (pure functions, no network/S3): SSRF/content validation is the
# security boundary of marketdata/codal_storage.py, and store_artifact's key
# must stay a pure function of the bytes for its dedupe promise to hold.


def test_disallowed_host_is_rejected_before_any_network_call():
    with pytest.raises(codal_storage.CodalArtifactRejected, match="disallowed_url"):
        codal_storage.download_artifact("https://evil.example.com/x.pdf", "pdf")


def test_http_scheme_is_rejected():
    with pytest.raises(codal_storage.CodalArtifactRejected, match="disallowed_url"):
        codal_storage.download_artifact("http://codal.ir/x.pdf", "pdf")


@pytest.mark.parametrize("kind,content,expected", [
    ("pdf", b"%PDF-1.4 ...", True),
    ("pdf", b"not a pdf", False),
    ("excel", b"PK\x03\x04rest", True),
    ("excel", b"nope", False),
    ("html", b"<!doctype html><body>x</body>", True),
    ("html", b"plain text", False),
])
def test_magic_byte_check(kind, content, expected):
    assert codal_storage._valid_magic(kind, content) is expected


def test_store_artifact_key_is_a_pure_function_of_content(monkeypatch):
    calls = {"head": 0, "put": 0}

    class _FakeClient:
        def head_object(self, **kwargs):
            calls["head"] += 1
            raise Exception("NoSuchKey")

        def put_object(self, **kwargs):
            calls["put"] += 1

    monkeypatch.setattr(codal_storage, "_client", lambda: _FakeClient())
    monkeypatch.setattr(codal_storage, "_ensure_bucket", lambda: None)

    key1, checksum1 = codal_storage.store_artifact(b"same bytes", "application/pdf", "pdf")
    key2, checksum2 = codal_storage.store_artifact(b"same bytes", "application/pdf", "pdf")

    assert key1 == key2
    assert checksum1 == checksum2
    assert key1.startswith("codal/sha256/")
    assert key1.endswith(".pdf")
    assert calls["put"] == 2  # fake head_object always "misses" -- key derivation is what's under test


def test_store_artifact_wraps_backend_errors_as_blocked_storage(monkeypatch):
    class _FailingClient:
        def head_object(self, **kwargs):
            raise Exception("miss")

    def _boom():
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(codal_storage, "_client", lambda: _FailingClient())
    monkeypatch.setattr(codal_storage, "_ensure_bucket", _boom)

    with pytest.raises(codal_storage.CodalBlockedStorage):
        codal_storage.store_artifact(b"x", "application/pdf", "pdf")


# ----------------------------------------------------------------------
# test_codal_classification.py
# Codal classification: title/category -> (doc_type, tier).
# 
# Unit tests (no DB) for `classify()` and `normalize_title()` -- both are pure
# functions of their arguments. The command tests are integration tests: they
# exercise the real ORM (bulk_update, uniqueness) through `call_command`, which
# is the boundary this task actually needs verified -- the classifier's logic
# is already covered by the unit tests above it.


# --- unit tests: classify() is pure, no django_db marker needed -----------


@pytest.mark.parametrize("title, expected_doc_type, expected_tier", [
    # Tier 1 -- financial statements, audited and unaudited
    ("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی نشده)", "financial_statements", TIER_1),
    ("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی شده)", "financial_statements", TIER_1),
    # Tier 1 -- interim/quarterly report
    ("اطلاعات و صورت‌های مالی میاندوره‌ای دوره ۹ ماهه منتهی به ۱۴۰۳/۰۹/۳۰ (حسابرسی نشده)", "interim_financials", TIER_1),
    # Tier 1 -- monthly production & sales
    ("گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به ۱۴۰۴/۰۹/۳۰", "production_sales", TIER_1),
    # Tier 2 -- AGM decisions
    ("تصمیمات مجمع عمومی عادی سالیانه دوره ۱۲ ماهه", "agm_decision", TIER_2),
    # Tier 2 -- clarification of a rumour
    ("شفاف سازی در خصوص شایعه، خبر یا گزارش منتشر شده", "clarification", TIER_2),
    # Tier 2 -- explanation of published financials
    ("توضیحات در خصوص اطلاعات و صورت های مالی منتشر شده", "clarification", TIER_2),
    # Tier 3 -- AGM invitation notice
    ("آگهی دعوت به مجمع عمومی عادی سالیانه نوبت دوم", "meeting_notice", TIER_3),
    # Tier 3 -- address change
    ("تغییر نشانی", "address_change", TIER_3),
    # Tier 3 -- compliance grace period
    ("اعطای فرصت به ناشر جهت رعایت دستورالعمل پذیرش اوراق بهادار", "compliance_notice", TIER_3),
])
def test_classify_matches_owner_confirmed_titles(title, expected_doc_type, expected_tier):
    result = classify(title)
    assert result.doc_type == expected_doc_type
    assert result.tier == expected_tier
    assert result.classified_by == "title"


def test_persian_letter_and_zwnj_normalization():
    # Arabic Yeh/Kaf (U+064A, U+0643) must read the same as Persian Yeh/Keheh
    # (U+06CC, U+06A9); built via translate() on explicit code points so the
    # test does not depend on which glyph an editor happened to save.
    persian_title = "صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی نشده)"
    to_arabic = str.maketrans({"ی": "ي", "ک": "ك"})
    arabic_variant = persian_title.translate(to_arabic)
    assert classify(persian_title).doc_type == "financial_statements"
    assert classify(arabic_variant).doc_type == "financial_statements"
    # ZWNJ inside "صورت‌های" must not break the "صورتهای مالی" keyword match --
    # already exercised above since persian_title itself carries the ZWNJ.
    assert "‌" in persian_title


def test_persian_indic_digit_folding():
    assert normalize_title("۱۲۳٤٥") == "12345"  # Persian-Indic then Arabic-Indic
    assert normalize_title("سال ۱۴۰۴") == "سال 1404"


def test_provider_category_wins_over_title_guess():
    # A title with no recognizable keyword, but the provider says category 5
    # (Auditor Notes & Opinion) -- category must win, not fall through to "other".
    result = classify("چیزی که هیچ کلیدواژه‌ای ندارد", category=5, category_title="Auditor Notes & Opinion")
    assert result == ("auditor_opinion", TIER_2, "category")


def test_provider_category_overrides_a_contradicting_title():
    # Title reads like a financial statement, but category 6 (Assembly Decision)
    # is the provider's own ground truth and must still win.
    result = classify("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹", category=6)
    assert result.doc_type == "agm_decision"
    assert result.tier == TIER_2
    assert result.classified_by == "category"


@pytest.mark.parametrize("title", [
    "یک عنوان کاملا ناشناخته و بی‌ربط",
    "",
    None,
    "1234567890",
    "!@#$%^&*()",
])
def test_unknown_or_degenerate_titles_land_in_tier_3_without_crashing(title):
    result = classify(title)
    assert result.tier == TIER_3
    assert result.doc_type in {"other"} or result.classified_by == "title"


# --- integration tests: the management command touches the real ORM -------


def _make(symbol, code, title, category=None, date_publish="1404-01-01"):
    return CodalAnnouncement.objects.create(
        symbol=symbol, code=code, title=title, category=category,
        date_publish=date_publish,
    )


@pytest.mark.django_db
def test_classify_command_labels_rows_and_is_idempotent():
    _make("TEST1", "C1", "صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی شده)")
    _make("TEST1", "C2", "آگهی دعوت به مجمع عمومی عادی سالیانه نوبت دوم")
    _make("TEST1", "C3", "چیز عجیب و غریب", category=None)
    _make("TEST1", "C4", "irrelevant title", category=5)  # provider category wins

    call_command("classify_codal_announcements")

    rows = {r.code: r for r in CodalAnnouncement.objects.filter(symbol="TEST1")}
    assert rows["C1"].doc_type == "financial_statements"
    assert rows["C1"].tier == TIER_1
    assert rows["C2"].doc_type == "meeting_notice"
    assert rows["C2"].tier == TIER_3
    assert rows["C3"].doc_type == "other"
    assert rows["C3"].tier == TIER_3
    assert rows["C3"].classified_by == "default"
    assert rows["C4"].doc_type == "auditor_opinion"
    assert rows["C4"].classified_by == "category"

    # Re-running must be a no-op: same rows, same values, no duplicates created.
    before = list(
        CodalAnnouncement.objects.filter(symbol="TEST1")
        .order_by("code")
        .values("code", "doc_type", "tier", "classified_by")
    )
    call_command("classify_codal_announcements")
    after = list(
        CodalAnnouncement.objects.filter(symbol="TEST1")
        .order_by("code")
        .values("code", "doc_type", "tier", "classified_by")
    )
    assert before == after
    assert CodalAnnouncement.objects.filter(symbol="TEST1").count() == 4


@pytest.mark.django_db
def test_classify_command_dry_run_writes_nothing():
    _make("TEST2", "C1", "تغییر نشانی")
    call_command("classify_codal_announcements", "--dry-run")
    row = CodalAnnouncement.objects.get(symbol="TEST2", code="C1")
    assert row.tier is None
    assert row.doc_type == ""


@pytest.mark.django_db
def test_coverage_report_runs_read_only_and_reports_json(capsys):
    _make("TEST3", "C1", "صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی شده)", date_publish="1404-06-01")
    _make("TEST3", "C2", "تغییر نشانی", date_publish="1404-06-02")
    _make("TEST4", "C1", "تغییر نشانی", date_publish="1404-06-02")  # no Tier-1 doc at all
    call_command("classify_codal_announcements")
    capsys.readouterr()  # discard the classify command's own stdout

    call_command("codal_coverage_report", "--json")
    captured = capsys.readouterr()
    import json
    payload = json.loads(captured.out)
    assert payload["tiers"]["1"]["symbols"] == 1
    assert "TEST4" in payload["symbols_without_tier1"]["symbols"]
    assert "TEST3" not in payload["symbols_without_tier1"]["symbols"]

    # No rows must have been mutated by a read-only report.
    row = CodalAnnouncement.objects.get(symbol="TEST3", code="C1")
    assert row.tier == TIER_1


def test_material_disclosure_is_tier_2_by_owner_decision():
    """Pinned: ~6% of all announcements, so its tier drives fetch priority.

    Confirmed with the owner on 2026-08-14. It is NOT covered by the original
    three-tier brief, so without this test it would drift back to the Tier-3
    default the first time the keyword table is reordered.
    """
    result = classify("افشای اطلاعات بااهمیت - گروه الف", None, "")

    assert result.doc_type == "material_disclosure"
    assert result.tier == 2


def test_the_three_types_left_at_tier_3_stay_there():
    """Also an owner decision: dividend schedule, board report, board changes."""
    for title in (
        "زمانبندی پرداخت سود دوره ۱۲ ماهه",
        "گزارش فعالیت هیئت مدیره دوره ۱۲ ماهه",
        "تغییرات در ترکیب اعضای هیئت مدیره",
    ):
        assert classify(title, None, "").tier == 3, title


# ----------------------------------------------------------------------
# Event-driven extraction and the codal.ir reachability breaker.
#
# The extractor used to be a 200-row batch on a six-hourly cron against a backlog
# of ~74,000 announcements, and every attempt reserved BrsApi quota it had no
# business spending. Meanwhile codal.ir is unreachable from the production host
# (TCP 443 times out), so ~986 of those attempts a day were guaranteed to fail at
# connect. Work now starts at ingest time, and a dead origin parks itself.


def test_ingesting_a_new_announcement_queues_its_extraction(monkeypatch):
    """"As soon as a downloadable document appears" -- no waiting for a cron."""
    from marketdata import ingest

    queued = []
    monkeypatch.setattr(
        "marketdata.tasks.extract_codal_report.delay", lambda pk: queued.append(pk)
    )
    monkeypatch.setattr("marketdata.tasks._queue_slots", lambda queue, limit: (limit, 0))
    payload = {"announcement": [{
        "l18": "فملی", "l30": "ملی مس", "title": "گزارش فعالیت ماهانه",
        "code": "1", "category": 1, "category_title": "ماهانه",
        "date_title": "1404-01-31", "date_send": "1404-02-01", "time_send": "10:00:00",
        "date_publish": "1404-02-01", "time_publish": "10:00:00",
        "link_excel": "https://excel.codal.ir/r.xlsx",
    }]}

    created, _ = ingest.ingest_codal(payload)

    assert created == 1
    assert queued == list(
        CodalAnnouncement.objects.values_list("id", flat=True)
    )


def test_event_enqueue_matches_symbol_and_code_as_a_pair(monkeypatch):
    from marketdata import ingest

    exact = _announcement(symbol="A", code="1")
    paired = _announcement(symbol="B", code="2")
    cross = _announcement(symbol="A", code="2")
    queued = []
    monkeypatch.setattr(
        "marketdata.tasks.extract_codal_report.delay", lambda pk: queued.append(pk)
    )
    monkeypatch.setattr("marketdata.tasks._queue_slots", lambda queue, limit: (limit, 0))

    assert ingest._enqueue_codal_extractions([
        CodalAnnouncement(symbol="A", code="1"),
        CodalAnnouncement(symbol="B", code="2"),
    ]) == 2
    assert queued == [exact.pk, paired.pk]
    assert cross.pk not in queued


def test_reingesting_the_same_announcement_queues_nothing_new(monkeypatch):
    """Idempotence comes from report__isnull, not from the bulk_create count."""
    from marketdata import ingest

    queued = []
    monkeypatch.setattr(
        "marketdata.tasks.extract_codal_report.delay", lambda pk: queued.append(pk)
    )
    announcement = _announcement(code="7", date_publish="1404-02-01", time_publish="10:00:00")
    CodalReport.objects.create(announcement=announcement)

    assert ingest._enqueue_codal_extractions([announcement]) == 0
    assert queued == []


def test_an_unreachable_origin_stops_enqueueing_instead_of_retrying(monkeypatch):
    """A network path that is down is not 74,000 individual document failures."""
    from marketdata import tasks
    from marketdata.models import WorkflowRun

    monkeypatch.setattr(
        "marketdata.codal_storage.origin_unreachable", lambda: True
    )
    monkeypatch.setattr(
        "marketdata.tasks.extract_codal_report.delay",
        lambda pk: pytest.fail("must not enqueue against a dead origin"),
    )

    assert tasks.queue_codal_extractions() == 0
    run = WorkflowRun.objects.get(workflow="queue_codal_extractions")
    assert run.outcome == WorkflowRun.Outcome.SKIPPED
    assert run.error_code == "origin_unreachable"


def test_a_rejected_document_does_not_trip_the_origin_breaker(monkeypatch):
    """The origin answered; this file is just unusable. Only connect-level
    failures are evidence about the network."""
    tripped = []
    monkeypatch.setattr(codal_storage, "_record_origin_failure", lambda: tripped.append(1))
    monkeypatch.setattr(codal_storage, "_record_origin_success", lambda: None)

    class _Response:
        is_redirect = is_permanent_redirect = False
        status_code = 200
        headers = {"Content-Type": "text/plain"}

        def raise_for_status(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        codal_storage.requests.Session, "get", lambda *a, **kw: _Response()
    )
    with pytest.raises(CodalArtifactRejected):
        codal_storage.download_artifact("https://codal.ir/x.pdf", "pdf")
    assert tripped == []


def test_recovery_probe_is_single_flight(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {codal_storage._BREAKER_PROBE_PENDING_KEY: "1"}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, *, ex, nx):
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    client = FakeRedis()
    monkeypatch.setattr(codal_storage, "_breaker_client", lambda: client)

    assert codal_storage.origin_unreachable(probe=True) is False
    assert codal_storage.origin_unreachable(probe=True) is True
