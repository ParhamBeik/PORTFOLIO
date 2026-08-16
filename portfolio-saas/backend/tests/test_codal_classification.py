"""Codal classification: title/category -> (doc_type, tier).

Unit tests (no DB) for `classify()` and `normalize_title()` -- both are pure
functions of their arguments. The command tests are integration tests: they
exercise the real ORM (bulk_update, uniqueness) through `call_command`, which
is the boundary this task actually needs verified -- the classifier's logic
is already covered by the unit tests above it.
"""
import pytest
from django.core.management import call_command

from marketdata.codal_classification import (
    TIER_1,
    TIER_2,
    TIER_3,
    classify,
    normalize_title,
)
from marketdata.models import CodalAnnouncement

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
