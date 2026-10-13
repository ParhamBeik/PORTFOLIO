"""Certified statements must survive real filing templates and reject bad evidence."""

import hashlib
import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from marketdata.codal_parsers import ParsedDocument
from marketdata.codal_pipeline import _persist_parsed
from marketdata import codal_pipeline
from marketdata.codal_statements import parse_statement, statement_abstain_reason, statement_sheet_urls
from marketdata.codal_storage import CodalBlockedNetwork
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalCandidateFact, CodalReport, MarketInstrument


FIXTURES = Path(__file__).parent / "fixtures" / "codal"
INTERIM = (FIXTURES / "foolad_interim_income_v9.html").read_bytes()
ANNUAL = (FIXTURES / "foolad_annual_consolidated_income_v9.html").read_bytes()
INTERIM_BALANCE = (FIXTURES / "foolad_interim_balance_v9.html").read_bytes()
ANNUAL_BALANCE = (FIXTURES / "foolad_annual_consolidated_balance_v9.html").read_bytes()
# An audited filing opens on the auditor's opinion: the same tab list, no figures.
OPINION = INTERIM.replace(b"var datasource", b"var opinion")
SOURCE = "https://codal.ir/Reports/Decision.aspx?LetterSerial=yC2oQqrZ3w8Q8FsnnWQQBw%3d%3d&rt=0"


def _parse(content=INTERIM, *, annual=False, title="صورت های مالی"):
    return parse_statement(
        content, symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title=title, period_end="1404-12-29" if annual else "1405-03-31",
        is_consolidated=annual, is_audited=False,
    )


_parse_balance = _parse


def test_real_archived_standalone_and_consolidated_sheets():
    interim = {fact["fact_code"]: fact for fact in _parse()}
    annual = {fact["fact_code"]: fact for fact in _parse(ANNUAL, annual=True)}
    assert len(interim) == len(annual) == 7
    assert interim["income.operating_revenue"]["numeric_value"] == Decimal("834166799")
    assert interim["income.net_profit"]["numeric_value"] == Decimal("120356493")
    assert annual["income.operating_revenue"]["numeric_value"] == Decimal("5243307882")
    assert annual["income.net_profit"]["numeric_value"] == Decimal("1410842891")
    assert interim["income.net_profit"]["source_coordinates"]["address"] == "B21"
    assert annual["income.net_profit"]["source_coordinates"]["address"] == "B24"
    assert all(fact["unit"] == "million_rial" and fact["verification_status"] == "reconciled" for fact in interim.values())


def test_statement_refuses_subsidiary_other_issuer_wrong_scope_and_bad_arithmetic():
    assert _parse(title="صورت های مالی (شرکت فرعی)") == []
    assert _parse(INTERIM.replace("فولاد مبارکه اصفهان".encode(), "شرکت دیگر".encode())) == []
    assert _parse(INTERIM, annual=True) == []
    # Figures that do not add up are kept as evidence, never shown.
    broken = _parse(INTERIM.replace(b"834166799", b"834166798"))
    assert broken and {fact["verification_status"] for fact in broken} == {"extracted"}


def test_observed_balance_sheets_reconcile_and_keep_point_in_time_units():
    interim = {fact["fact_code"]: fact for fact in _parse_balance(INTERIM_BALANCE)}
    annual = {fact["fact_code"]: fact for fact in _parse_balance(ANNUAL_BALANCE, annual=True)}
    assert len(interim) == len(annual) == 11
    assert interim["balance.total_assets"]["numeric_value"] == Decimal("6278290305")
    assert interim["balance.total_liabilities"]["numeric_value"] == Decimal("2896232830")
    assert annual["balance.total_assets"]["numeric_value"] == Decimal("9559001022")
    assert annual["balance.total_equity"]["numeric_value"] == Decimal("5121080404")
    assert interim["balance.total_assets"]["source_coordinates"] == {
        "sheet_code": 0, "table_id": 3223, "label_address": "A22", "address": "B22",
    }
    assert annual["balance.total_assets"]["source_coordinates"]["address"] == "B24"
    assert all(fact["period_start"] == "" and fact["unit"] == "million_rial"
               and fact["currency"] == "IRR" and fact["verification_status"] == "reconciled"
               for fact in interim.values())
    broken = _parse_balance(INTERIM_BALANCE.replace(b"6278290305", b"6278290304"))
    assert {fact["verification_status"] for fact in broken} == {"extracted"}


def test_tab_links_come_from_the_filings_own_tab_list():
    assert statement_sheet_urls(INTERIM, SOURCE, is_consolidated=False) == {
        "income": SOURCE + "&sheetId=1",
        "balance": SOURCE + "&sheetId=0",
    }
    assert statement_sheet_urls(ANNUAL, SOURCE, is_consolidated=True)["balance"].endswith("&sheetId=14")
    assert statement_sheet_urls(INTERIM, SOURCE, is_consolidated=True) == {}
    assert statement_sheet_urls(INTERIM, SOURCE.replace("codal.ir", "example.com"), is_consolidated=False) == {}


def test_abstain_reasons_name_the_bucket():
    assert statement_abstain_reason(b"", title="صورتهای مالی (شرکت فرعی)") == "subsidiary"
    assert statement_abstain_reason(OPINION, title="صورتهای مالی") == "no_datasource"
    assert statement_abstain_reason(INTERIM, title="صورتهای مالی", extracted=True) == "unreconciled"


@pytest.mark.parametrize("venue,scope,symbol,company,revenue,assets", [
    ("otc", "standalone", "بجهرم", "توسعه مولد نیروگاهی جهرم", "22915756", "58426274"),
    ("otc", "consolidated", "کرومیت", "توسعه معادن کرومیت کاوندگان بنا", "2288503", "13382939"),
    ("registered", "standalone", "لکما", "کارخانجات مخابراتی ایران", "614540", "11964352"),
    ("registered", "consolidated", "دحاوی", "الحاوی", "26618031", "32092974"),
])
def test_observed_otc_and_registered_sheets(venue, scope, symbol, company, revenue, assets):
    prefix = f"{venue}_{scope}"
    arguments = {
        "symbol": symbol, "company_name": company, "title": "صورت های مالی",
        "period_end": "1404-12-29", "is_consolidated": scope == "consolidated",
        "is_audited": False,
    }
    income = {f["fact_code"]: f for f in parse_statement((FIXTURES / f"{prefix}_income_v9.html").read_bytes(), **arguments)}
    balance = {f["fact_code"]: f for f in parse_statement((FIXTURES / f"{prefix}_balance_v9.html").read_bytes(), **arguments)}
    assert income["income.operating_revenue"]["numeric_value"] == Decimal(revenue)
    assert balance["balance.total_assets"]["numeric_value"] == Decimal(assets)
    assert {f["verification_status"] for f in [*income.values(), *balance.values()]} == {"reconciled"}


def test_template_name_does_not_gate_reading():
    # Bank, insurance, investment and pre-V9 product templates share the grid.
    renamed = INTERIM.replace(b"FinancialStatement-Listed-Product-V9", b"FinancialStatement-Bank-Listed-V6")
    facts = _parse(renamed)
    assert len(facts) == 7 and facts[0]["dimensions"]["template"] == "FinancialStatement-Bank-Listed-V6"


def test_late_declared_utf8_preserves_issuer_and_exact_source_cells():
    raw = (FIXTURES / "registered_standalone_late_charset_v9.html").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "62711d9962bfaead32d26b6fbd3c1b291cabc3e4c69d862d15af3c80b2b7c1d3"
    assert raw.find(b"charset=UTF-8") > 30_000
    facts = {fact["fact_code"]: fact for fact in parse_statement(
        raw, symbol="کهرام", company_name="تولیدی گرانیت بهسرام",
        title="صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی نشده)",
        period_end="1404-12-29", is_consolidated=False, is_audited=False,
    )}
    assert facts["income.operating_revenue"]["numeric_value"] == Decimal("2089753")
    assert facts["income.net_profit"]["numeric_value"] == Decimal("-635952")
    assert facts["income.net_profit"]["unit"] == "million_rial"


@pytest.mark.django_db
def test_dossier_shows_income_evidence_and_withholds_older_filing_after_correction(make_user):
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC, category=MarketInstrument.Category.STOCK,
        symbol="فولاد", name="Foolad", eligible=True,
    )
    announcement = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="foolad-income-original", date_publish="1405-03-31",
        link="https://www.codal.ir/Reports/Decision.aspx?LetterSerial=123",
    )
    report = CodalReport.objects.create(
        announcement=announcement, category=CodalAnnouncement.Category.STATEMENTS,
        period_end="1405-03-31", is_audited=False,
        parser_version=settings.CODAL_STATEMENT_PARSER_VERSION,
    )
    checksum = hashlib.sha256(INTERIM).hexdigest()
    artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.HTML, source_url=announcement.link,
        checksum_sha256=checksum, s3_key=f"codal/sha256/{checksum[:2]}/{checksum}.html",
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    _persist_parsed(report, artifact, ParsedDocument(facts=_parse()))
    client = APIClient()
    client.force_authenticate(user=make_user())
    income = client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]
    assert income["status"] == "verified"
    assert income["points"][0]["revenue"] == "834166799.000000000000"
    assert income["points"][0]["net_profit"] == "120356493.000000000000"
    assert income["points"][0]["artifact_sha256"] == checksum
    assert income["points"][0]["source_coordinates"]["net_profit"]["address"] == "B21"

    artifact.fetch_status = CodalArtifact.FetchStatus.PENDING
    artifact.save(update_fields=["fetch_status"])
    assert client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]["status"] == "unavailable_unverified"
    artifact.fetch_status = CodalArtifact.FetchStatus.STORED
    artifact.save(update_fields=["fetch_status"])
    profit = CodalCandidateFact.objects.get(extraction__report=report, fact_code="income.net_profit")
    original_coordinates = profit.source_coordinates
    profit.source_coordinates = {"sheet_code": 1, "table_id": 3220, "label_address": "A20", "address": "B21"}
    profit.save(update_fields=["source_coordinates"])
    assert client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]["status"] == "unavailable_unverified"
    profit.source_coordinates = original_coordinates
    profit.save(update_fields=["source_coordinates"])
    assert client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]["status"] == "verified"

    corrected = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="اصلاحیه صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="foolad-income-correction", date_publish="1405-04-01",
        doc_type="financial_statements",
    )
    income = client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]
    assert income["status"] == "unavailable_unverified"
    assert income["withheld_periods"] == 1
    CodalReport.objects.create(
        announcement=corrected, category=CodalAnnouncement.Category.AUDITOR_REPORT,
        period_end="1405-03-31", is_audited=False, is_correction=True,
    )
    income = client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]
    assert income["status"] == "unavailable_unverified"
    assert income["verified_periods"] == 0
    assert income["withheld_periods"] == 1


@pytest.mark.django_db
def test_dossier_shows_balance_evidence_and_withholds_unverified_correction(make_user):
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC, category=MarketInstrument.Category.STOCK,
        symbol="فولاد", name="Foolad", eligible=True,
    )
    announcement = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="foolad-balance-original", date_publish="1405-03-31",
        link="https://codal.ir/Reports/Decision.aspx?LetterSerial=yC2oQqrZ3w8Q8FsnnWQQBw%3d%3d",
    )
    report = CodalReport.objects.create(
        announcement=announcement, category=CodalAnnouncement.Category.STATEMENTS,
        period_end="1405-03-31", is_audited=False,
        parser_version=settings.CODAL_STATEMENT_PARSER_VERSION,
    )
    balance_checksum = hashlib.sha256(INTERIM_BALANCE).hexdigest()
    artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.HTML,
        source_url=announcement.link + "&sheetId=0",
        checksum_sha256=balance_checksum,
        s3_key=f"codal/sha256/{balance_checksum[:2]}/{balance_checksum}.html",
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    _persist_parsed(report, artifact, ParsedDocument(facts=_parse_balance(INTERIM_BALANCE)))
    client = APIClient()
    client.force_authenticate(user=make_user())
    balance = client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]
    assert balance["status"] == "verified"
    assert balance["points"][0]["values"]["total_assets"] == "6278290305.000000000000"
    assert balance["points"][0]["values"]["total_liabilities"] == "2896232830.000000000000"
    assert balance["points"][0]["source_coordinates"]["total_assets"]["address"] == "B22"
    assert balance["points"][0]["source_url"].endswith("&sheetId=0")

    artifact.fetch_status = CodalArtifact.FetchStatus.PENDING
    artifact.save(update_fields=["fetch_status"])
    assert client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]["status"] == "unavailable_unverified"
    artifact.fetch_status = CodalArtifact.FetchStatus.STORED
    artifact.save(update_fields=["fetch_status"])
    assets = CodalCandidateFact.objects.get(extraction__report=report, fact_code="balance.total_assets")
    original_coordinates = assets.source_coordinates
    assets.source_coordinates = {"sheet_code": 0, "table_id": 3223, "label_address": "A22", "address": "A22"}
    assets.save(update_fields=["source_coordinates"])
    assert client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]["status"] == "unavailable_unverified"
    assets.source_coordinates = original_coordinates
    assets.save(update_fields=["source_coordinates"])
    assert client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]["status"] == "verified"

    corrected = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="اصلاحیه صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="foolad-balance-correction", date_publish="1405-04-01",
        doc_type="financial_statements",
    )
    balance = client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]
    assert balance["status"] == "unavailable_unverified"
    assert balance["withheld_periods"] == 1
    CodalReport.objects.create(
        announcement=corrected, category=CodalAnnouncement.Category.AUDITOR_REPORT,
        period_end="1405-03-31", is_audited=False, is_correction=True,
    )
    balance = client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]
    assert balance["status"] == "unavailable_unverified"
    assert balance["verified_periods"] == 0
    assert balance["withheld_periods"] == 1


def _filing(code):
    return CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code=code, date_publish="1405-04-01", link=SOURCE, has_excel=False, has_pdf=False,
    )


def _serve(monkeypatch, pages):
    fetched = []

    def download(url, kind):
        fetched.append(url)
        sheet = re.search(r"sheetId=(\d+)", url)
        page = pages.get(sheet.group(1) if sheet else "")
        if isinstance(page, Exception):
            raise page
        return url, "text/html", page

    monkeypatch.setattr(codal_pipeline, "download_artifact", download)
    monkeypatch.setattr(
        codal_pipeline, "store_artifact",
        lambda content, content_type, kind: ("stored", hashlib.sha256(content).hexdigest()),
    )
    return fetched


@pytest.mark.django_db
def test_extraction_reads_the_statement_tabs_behind_the_opinion_page(monkeypatch, make_user):
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC, category=MarketInstrument.Category.STOCK,
        symbol="فولاد", name="Foolad", eligible=True,
    )
    fetched = _serve(monkeypatch, {"": OPINION, "1": INTERIM, "0": INTERIM_BALANCE})
    monkeypatch.setattr(codal_pipeline, "load_artifact", lambda artifact: {
        "": OPINION, "1": INTERIM, "0": INTERIM_BALANCE,
    }[(re.search(r"sheetId=(\d+)", artifact.source_url) or [None, ""])[1]])
    announcement = _filing("opinion-first")

    report, _ = codal_pipeline.extract_report(announcement.pk)
    assert report.status == CodalReport.Status.PARSED
    assert report.parser_version == settings.CODAL_STATEMENT_PARSER_VERSION
    reconciled = CodalCandidateFact.objects.filter(
        extraction__report=report, verification_status="reconciled",
    )
    assert reconciled.filter(fact_code__startswith="income.").count() == 7
    assert reconciled.filter(fact_code__startswith="balance.").count() == 11
    assert len(fetched) == 3

    client = APIClient()
    client.force_authenticate(user=make_user())
    dossier = client.get("/api/explore/stocks/فولاد/").data
    assert dossier["financial_metrics"]["status"] == "verified"
    assert dossier["balance_sheet"]["status"] == "verified"

    # A re-run reads MinIO: no request reaches the origin.
    codal_pipeline.extract_report(announcement.pk)
    assert len(fetched) == 3


@pytest.mark.django_db
def test_a_page_carrying_its_sheet_skips_that_tab(monkeypatch):
    fetched = _serve(monkeypatch, {"": INTERIM, "0": INTERIM_BALANCE})
    report, _ = codal_pipeline.extract_report(_filing("income-first").pk)
    assert report.status == CodalReport.Status.PARSED
    assert fetched == [SOURCE, SOURCE + "&sheetId=0"]


@pytest.mark.django_db
def test_an_unfetched_tab_is_retried_not_finished(monkeypatch):
    _serve(monkeypatch, {"": OPINION, "1": INTERIM, "0": CodalBlockedNetwork("origin_down")})
    report, _ = codal_pipeline.extract_report(_filing("balance-blocked").pk)
    assert report.status == CodalReport.Status.BLOCKED_NETWORK
    assert report.error_code == "statement_tab_unfetched"
    assert CodalCandidateFact.objects.filter(
        extraction__report=report, fact_code="income.net_profit", verification_status="reconciled",
    ).exists()


@pytest.mark.django_db
def test_an_opinion_without_tabs_names_why(monkeypatch):
    bare = re.sub(rb"<option[^>]*>.*?</option>", b"", OPINION, flags=re.S)
    _serve(monkeypatch, {"": bare})
    monkeypatch.setattr(codal_pipeline, "parse_artifact", lambda kind, content: ParsedDocument(text="opinion"))
    report, _ = codal_pipeline.extract_report(_filing("no-tabs").pk)
    assert report.status == CodalReport.Status.NEEDS_REVIEW
    assert report.error_code == "statement:no_datasource"


def test_a_malformed_datasource_yields_nothing_instead_of_raising():
    page = b'<html><script>var datasource = {"periodEndToDate": "1405/03/31", "period": 3, ' \
           b'"isConsolidated": false, "isAudited": false, "sheets": [null, {"title_Fa": "x", "tables": [null]}]};</script></html>'
    assert _parse(page) == []


def _stored(pages):
    return lambda artifact: pages[(re.search(r"sheetId=(\d+)", artifact.source_url) or [None, ""])[1]]


@pytest.mark.django_db
def test_a_recovered_tab_is_added_on_retry(monkeypatch):
    pages = {"": OPINION, "1": INTERIM, "0": INTERIM_BALANCE}
    monkeypatch.setattr(codal_pipeline, "load_artifact", _stored(pages))
    _serve(monkeypatch, {**pages, "0": CodalBlockedNetwork("origin_down")})
    announcement = _filing("retry-tab")
    report, _ = codal_pipeline.extract_report(announcement.pk)
    assert report.status == CodalReport.Status.BLOCKED_NETWORK

    _serve(monkeypatch, pages)
    CodalArtifact.objects.filter(report=report, fetch_status=CodalArtifact.FetchStatus.BLOCKED_NETWORK).update(
        fetch_status=CodalArtifact.FetchStatus.PENDING,
    )
    report, _ = codal_pipeline.extract_report(announcement.pk)
    assert report.status == CodalReport.Status.PARSED
    assert CodalCandidateFact.objects.filter(
        extraction__report=report, fact_code__startswith="balance.", verification_status="reconciled",
    ).count() == 11


@pytest.mark.django_db
def test_the_statement_page_is_chosen_over_a_generic_excel(monkeypatch):
    announcement = _filing("excel-first")
    announcement.link_excel, announcement.has_excel = "https://excel.codal.ir/r.xlsx", True
    announcement.save()
    fetched = _serve(monkeypatch, {"": INTERIM, "0": INTERIM_BALANCE})
    real_download = codal_pipeline.download_artifact
    monkeypatch.setattr(codal_pipeline, "download_artifact", lambda url, kind: (
        (url, "application/vnd.ms-excel", b"PK workbook") if "excel" in url else real_download(url, kind)
    ))
    monkeypatch.setattr(codal_pipeline, "parse_artifact", lambda kind, content: ParsedDocument(text="generic"))
    report, result = codal_pipeline.extract_report(announcement.pk)
    assert result["parsed_from"] == CodalArtifact.Kind.HTML
    assert report.status == CodalReport.Status.PARSED
    assert fetched


def test_observations_skip_balance_lines_a_template_does_not_report():
    from research.observations import build_observations

    point = {
        "scope": "standalone", "period_end_jalali": "1404-12-29", "audited": True,
        "values": {"total_assets": "10", "total_liabilities": "4", "total_equity": "6"},
    }
    observed = build_observations({"points": []}, balance={"points": [point]})
    assert "balance_standalone_total_assets" in observed
    assert "balance_standalone_cash" not in observed


def test_legacy_balance_tab_and_equity_names_are_read():
    legacy = INTERIM_BALANCE.replace("صورت وضعیت مالی".encode(), "ترازنامه".encode()).replace(
        "جمع حقوق مالکانه".encode(), "جمع حقوق صاحبان سهام".encode())
    facts = {f["fact_code"]: f for f in _parse(legacy)}
    assert facts["balance.total_equity"]["verification_status"] == "reconciled"
    page = INTERIM.replace("صورت وضعیت مالی".encode(), "ترازنامه".encode())
    assert statement_sheet_urls(page, SOURCE, is_consolidated=False)["balance"].endswith("&sheetId=0")


def test_income_without_a_valid_period_is_not_certified():
    facts = _parse(INTERIM.replace(b'"period":3', b'"period":1'))
    assert facts and {f["verification_status"] for f in facts} == {"extracted"}



def test_a_table_in_an_unconvertible_unit_is_not_read():
    # The page note still says million Rial; the table's own unit must win.
    described = "کليه مبالغ به ميليون ريال است".encode()
    assert INTERIM.count(described) == 1
    assert _parse(INTERIM.replace(described, "کليه مبالغ به هزار تومان است".encode())) == []


def test_audit_status_comes_from_the_data_when_the_title_is_silent():
    facts = parse_statement(
        INTERIM, symbol="فولاد", company_name="فولاد مبارکه اصفهان", title="صورت های مالی",
        period_end="1405-03-31", is_consolidated=False, is_audited=None,
    )
    assert facts and {f["dimensions"]["audited"] for f in facts} == {False}


@pytest.mark.django_db
def test_a_title_silent_on_audit_takes_the_status_from_the_data(monkeypatch, make_user):
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC, category=MarketInstrument.Category.STOCK,
        symbol="فولاد", name="Foolad", eligible=True,
    )
    _serve(monkeypatch, {"": INTERIM, "0": INTERIM_BALANCE})
    announcement = _filing("silent-audit")
    announcement.title = "صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱"
    announcement.save()
    report, _ = codal_pipeline.extract_report(announcement.pk)
    assert report.is_audited is False
    client = APIClient()
    client.force_authenticate(user=make_user())
    income = client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]
    assert income["status"] == "verified" and income["points"][0]["audited"] is False


def test_a_literal_plus_in_the_serial_survives_the_tab_link():
    source = "https://codal.ir/Reports/Decision.aspx?LetterSerial=ab+cd%3d%3d&rt=0&sheetId=19"
    assert statement_sheet_urls(INTERIM, source, is_consolidated=False)["balance"] == (
        "https://codal.ir/Reports/Decision.aspx?LetterSerial=ab+cd%3d%3d&rt=0&sheetId=0"
    )


def test_income_off_the_fiscal_year_is_not_certified():
    shifted = INTERIM.replace(b'"yearEndToDate":"1405/12/29"', b'"yearEndToDate":"1405/06/31"')
    assert shifted != INTERIM
    facts = _parse(shifted)
    assert facts and {f["verification_status"] for f in facts} == {"extracted"}


def test_an_unreadable_discontinued_line_is_not_zero():
    cell = re.search(rb'\{[^{}]*"address":"B20"[^{}]*\}', INTERIM).group(0)
    assert b'"value":"0"' in cell
    broken = INTERIM.replace(cell, cell.replace(b'"value":"0"', b'"value":"n/a"'))
    assert {f["verification_status"] for f in _parse(broken)} == {"extracted"}
    blank = INTERIM.replace(cell, cell.replace(b'"value":"0"', b'"value":"-"'))
    assert {f["verification_status"] for f in _parse(blank)} == {"reconciled"}


def test_a_billion_rial_table_is_not_read_as_rial():
    described = "کليه مبالغ به ميليون ريال است".encode()
    assert _parse(INTERIM.replace(described, "کليه مبالغ به ميليارد ريال است".encode())) == []


def test_two_values_at_one_cell_withhold_the_sheet():
    cell = re.search(rb'\{[^{}]*"address":"B4"[^{}]*\}', INTERIM).group(0)
    assert _parse(INTERIM) and _parse(INTERIM.replace(cell, cell + b"," + cell, 1)) == []


@pytest.mark.django_db
def test_an_unverified_page_does_not_stand_in_for_the_advertised_tab(monkeypatch):
    broken = INTERIM.replace(b"834166799", b"834166798")  # sums fail: facts only extracted
    fetched = _serve(monkeypatch, {"": broken, "1": INTERIM, "0": INTERIM_BALANCE})
    report, _ = codal_pipeline.extract_report(_filing("tab-over-unverified").pk)
    assert any(url.endswith("sheetId=1") for url in fetched)
    assert CodalCandidateFact.objects.filter(
        extraction__report=report, fact_code__startswith="income.", verification_status="reconciled",
    ).count() == 7
