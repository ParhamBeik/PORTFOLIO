"""Certified income must survive real filing templates and reject bad evidence."""

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from rest_framework.test import APIClient

from marketdata.codal_parsers import ParsedDocument
from marketdata.codal_pipeline import _persist_parsed
from marketdata import codal_pipeline
from marketdata.codal_statements import (
    balance_sheet_url, parse_balance_sheet, parse_income_statement,
)
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalReport, MarketInstrument


FIXTURES = Path(__file__).parent / "fixtures" / "codal"
INTERIM = (FIXTURES / "foolad_interim_income_v9.html").read_bytes()
ANNUAL = (FIXTURES / "foolad_annual_consolidated_income_v9.html").read_bytes()
INTERIM_BALANCE = (FIXTURES / "foolad_interim_balance_v9.html").read_bytes()
ANNUAL_BALANCE = (FIXTURES / "foolad_annual_consolidated_balance_v9.html").read_bytes()


def _parse(content=INTERIM, *, annual=False, title="صورت های مالی"):
    return parse_income_statement(
        content, symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title=title, period_end="1404-12-29" if annual else "1405-03-31",
        is_consolidated=annual, is_audited=False,
    )


def test_real_archived_standalone_and_consolidated_sheets():
    interim = {fact["fact_code"]: fact for fact in _parse()}
    annual = {fact["fact_code"]: fact for fact in _parse(ANNUAL, annual=True)}
    assert len(interim) == len(annual) == 6
    assert interim["income.operating_revenue"]["numeric_value"] == Decimal("834166799")
    assert interim["income.net_profit"]["numeric_value"] == Decimal("120356493")
    assert annual["income.operating_revenue"]["numeric_value"] == Decimal("5243307882")
    assert annual["income.net_profit"]["numeric_value"] == Decimal("1410842891")
    assert interim["income.net_profit"]["source_coordinates"]["address"] == "B21"
    assert annual["income.net_profit"]["source_coordinates"]["address"] == "B24"
    assert all(fact["unit"] == "million_rial" and fact["verification_status"] == "reconciled" for fact in interim.values())


def test_statement_refuses_subsidiary_wrong_unit_wrong_scope_and_bad_arithmetic():
    assert _parse(title="صورت های مالی (شرکت فرعی)") == []
    assert _parse(INTERIM.replace("فولاد مبارکه اصفهان".encode(), "شرکت دیگر".encode())) == []
    assert _parse(INTERIM.replace("میلیون ریال".encode(), "هزار ریال".encode())) == []
    assert _parse(INTERIM, annual=True) == []
    assert _parse(INTERIM.replace(b"834166799", b"834166798")) == []


def _parse_balance(content=INTERIM_BALANCE, *, annual=False, title="صورت های مالی"):
    return parse_balance_sheet(
        content, symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title=title, period_end="1404-12-29" if annual else "1405-03-31",
        is_consolidated=annual, is_audited=False,
    )


def test_observed_balance_sheets_reconcile_and_keep_point_in_time_units():
    interim = {fact["fact_code"]: fact for fact in _parse_balance()}
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
               and fact["currency"] == "IRR" for fact in interim.values())


def test_balance_sheet_abstains_on_wrong_identity_unit_scope_or_equation():
    assert _parse_balance(title="صورت های مالی (شرکت فرعی)") == []
    assert _parse_balance(INTERIM_BALANCE.replace("فولاد مبارکه اصفهان".encode(), "شرکت دیگر".encode())) == []
    assert _parse_balance(INTERIM_BALANCE.replace("میلیون ریال".encode(), "هزار ریال".encode())) == []
    assert _parse_balance(INTERIM_BALANCE, annual=True) == []
    assert _parse_balance(INTERIM_BALANCE.replace(b"6278290305", b"6278290304")) == []


def test_balance_sheet_link_requires_advertised_sheet_and_codal_source():
    source = "https://codal.ir/Reports/Decision.aspx?LetterSerial=yC2oQqrZ3w8Q8FsnnWQQBw%3d%3d&rt=0"
    assert balance_sheet_url(INTERIM, source, is_consolidated=False).endswith("&sheetId=0")
    assert balance_sheet_url(ANNUAL, source, is_consolidated=True).endswith("&sheetId=14")
    assert balance_sheet_url(INTERIM, source, is_consolidated=True) is None
    assert balance_sheet_url(INTERIM, source.replace("codal.ir", "example.com"), is_consolidated=False) is None


@pytest.mark.parametrize("venue,scope,symbol,company,serial,revenue,assets", [
    ("otc", "standalone", "بجهرم", "توسعه مولد نیروگاهی جهرم", "mnKmuErJuqPJH4lQQQaQQQvCwnOg%3d%3d", "22915756", "58426274"),
    ("otc", "consolidated", "کرومیت", "توسعه معادن کرومیت کاوندگان بنا", "r9mC89jB39KVhOadOOObOOOKQIXg%3d%3d", "2288503", "13382939"),
    ("registered", "standalone", "لکما", "کارخانجات مخابراتی ایران", "NXh0zzf8btuBRc7NnlYrdg%3d%3d", "614540", "11964352"),
    ("registered", "consolidated", "دحاوی", "الحاوی", "i8COOObOOOXrPKfcWIiu70yhDpMA%3d%3d", "26618031", "32092974"),
])
def test_observed_otc_and_registered_v9_sheets(venue, scope, symbol, company, serial, revenue, assets):
    prefix = f"{venue}_{scope}"
    income_html = (FIXTURES / f"{prefix}_income_v9.html").read_bytes()
    balance_html = (FIXTURES / f"{prefix}_balance_v9.html").read_bytes()
    arguments = {
        "symbol": symbol, "company_name": company, "title": "صورت های مالی",
        "period_end": "1404-12-29", "is_consolidated": scope == "consolidated",
        "is_audited": False,
    }
    income = {fact["fact_code"]: fact for fact in parse_income_statement(income_html, **arguments)}
    balance = {fact["fact_code"]: fact for fact in parse_balance_sheet(balance_html, **arguments)}
    assert len(income) == 6
    assert len(balance) == 11
    assert income["income.operating_revenue"]["numeric_value"] == Decimal(revenue)
    assert balance["balance.total_assets"]["numeric_value"] == Decimal(assets)
    assert balance["balance.total_assets"]["period_start"] == ""
    expected_venue = "OTC" if venue == "otc" else "Registered"
    assert expected_venue in income["income.operating_revenue"]["dimensions"]["template"]
    assert balance_sheet_url(
        income_html,
        f"https://codal.ir/Reports/Decision.aspx?LetterSerial={serial}&rt=0&let=6&ct=0&ft=-1",
        is_consolidated=scope == "consolidated",
    ).endswith(f"sheetId={14 if scope == 'consolidated' else 0}")


def test_unobserved_v9_venue_is_withheld():
    raw = (FIXTURES / "otc_standalone_income_v9.html").read_bytes()
    assert parse_income_statement(
        raw.replace(b"FinancialStatement-OTC-Product-V9", b"FinancialStatement-Unknown-Product-V9"),
        symbol="بجهرم", company_name="توسعه مولد نیروگاهی جهرم", title="صورت های مالی",
        period_end="1404-12-29", is_consolidated=False, is_audited=False,
    ) == []
    balance = (FIXTURES / "otc_standalone_balance_v9.html").read_bytes()
    assert parse_balance_sheet(
        balance.replace(b"FinancialStatement-OTC-Product-V9", b"FinancialStatement-Unknown-Product-V9"),
        symbol="بجهرم", company_name="توسعه مولد نیروگاهی جهرم", title="صورت های مالی",
        period_end="1404-12-29", is_consolidated=False, is_audited=False,
    ) == []


@pytest.mark.django_db
def test_balance_backfill_archives_source_and_is_idempotent(monkeypatch, capsys):
    from marketdata.management.commands import backfill_balance_sheets as command

    source_url = (
        "https://codal.ir/Reports/Decision.aspx?"
        "LetterSerial=yC2oQqrZ3w8Q8FsnnWQQBw%3d%3d&rt=0"
    )
    announcement = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="balance-backfill", date_publish="1405-04-01", link=source_url,
    )
    report = CodalReport.objects.create(
        announcement=announcement, category=CodalAnnouncement.Category.STATEMENTS,
    )
    source_artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.HTML, source_url=source_url,
        checksum_sha256=hashlib.sha256(INTERIM).hexdigest(),
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    fetched = []
    monkeypatch.setattr(command, "load_artifact", lambda artifact: INTERIM)
    monkeypatch.setattr(
        command, "download_artifact",
        lambda url, kind: (fetched.append((url, kind)) or url, "text/html", INTERIM_BALANCE),
    )
    monkeypatch.setattr(
        command, "store_artifact",
        lambda content, content_type, kind: ("balance-key", hashlib.sha256(content).hexdigest()),
    )
    call_command("backfill_balance_sheets", symbol="فولاد", limit=10, fetch=True, dry_run=True)
    assert "certified=1" in capsys.readouterr().out
    assert report.artifacts.count() == 1
    assert report.extractions.count() == 0

    call_command("backfill_balance_sheets", symbol="فولاد", limit=10, fetch=True)
    balance = report.artifacts.exclude(pk=source_artifact.pk).get()
    assert balance.source_url.endswith("&sheetId=0")
    assert balance.checksum_sha256 == hashlib.sha256(INTERIM_BALANCE).hexdigest()
    assert balance.extractions.get().candidates.filter(verification_status="reconciled").count() == 11
    assert len(fetched) == 2

    monkeypatch.setattr(command, "load_artifact", lambda artifact: INTERIM)
    call_command("backfill_balance_sheets", symbol="فولاد", limit=10, fetch=True)
    assert "skipped=2" in capsys.readouterr().out
    assert len(fetched) == 2


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
        checksum_sha256=checksum, fetch_status=CodalArtifact.FetchStatus.STORED,
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
    artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.HTML,
        source_url=announcement.link + "&sheetId=0",
        checksum_sha256=hashlib.sha256(INTERIM_BALANCE).hexdigest(),
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    _persist_parsed(report, artifact, ParsedDocument(facts=_parse_balance()))
    client = APIClient()
    client.force_authenticate(user=make_user())
    balance = client.get("/api/explore/stocks/فولاد/").data["balance_sheet"]
    assert balance["status"] == "verified"
    assert balance["points"][0]["values"]["total_assets"] == "6278290305.000000000000"
    assert balance["points"][0]["values"]["total_liabilities"] == "2896232830.000000000000"
    assert balance["points"][0]["source_coordinates"]["total_assets"]["address"] == "B22"
    assert balance["points"][0]["source_url"].endswith("&sheetId=0")

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


@pytest.mark.django_db
def test_live_extraction_prefers_certifiable_html_over_generic_excel(monkeypatch):
    announcement = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="let174-foolad-income", date_publish="1405-04-01",
        link_excel="https://excel.codal.ir/Report.xlsx",
        link="https://www.codal.ir/Reports/Decision.aspx?LetterSerial=123",
    )
    monkeypatch.setattr(
        codal_pipeline, "download_artifact",
        lambda url, kind: (url, "text/html", INTERIM if kind == CodalArtifact.Kind.HTML else b"generic"),
    )
    monkeypatch.setattr(
        codal_pipeline, "store_artifact",
        lambda content, content_type, kind: ("stored", hashlib.sha256(content).hexdigest()),
    )
    monkeypatch.setattr(
        codal_pipeline, "parse_artifact",
        lambda kind, content: ParsedDocument(text="generic parsed content"),
    )
    report, result = codal_pipeline.extract_report(announcement.pk)
    assert result["parsed_from"] == CodalArtifact.Kind.HTML
    assert report.parser_version == settings.CODAL_STATEMENT_PARSER_VERSION
    assert report.extractions.get().candidates.filter(verification_status="reconciled").count() == 6


@pytest.mark.django_db
def test_offline_statement_reparse_is_idempotent(monkeypatch, capsys):
    announcement = CodalAnnouncement.objects.create(
        symbol="فولاد", company_name="فولاد مبارکه اصفهان",
        title="صورت های مالی دوره منتهی به ۱۴۰۵/۳/۳۱ (حسابرسی نشده)",
        code="let174-foolad-backfill", date_publish="1405-04-01",
    )
    report = CodalReport.objects.create(announcement=announcement, category=CodalAnnouncement.Category.STATEMENTS)
    CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.HTML,
        source_url="https://www.codal.ir/Reports/Decision.aspx?LetterSerial=123",
        checksum_sha256=hashlib.sha256(INTERIM).hexdigest(),
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    monkeypatch.setattr(
        "marketdata.management.commands.reparse_income_statements.load_artifact",
        lambda artifact: INTERIM,
    )
    call_command("reparse_income_statements", symbol="فولاد", limit=10, dry_run=True)
    assert "certified=1" in capsys.readouterr().out
    assert report.extractions.count() == 0
    call_command("reparse_income_statements", symbol="فولاد", limit=10)
    assert report.extractions.get().candidates.filter(verification_status="reconciled").count() == 6
    call_command("reparse_income_statements", symbol="فولاد", limit=10)
    assert "skipped=1" in capsys.readouterr().out
    assert report.extractions.count() == 1
