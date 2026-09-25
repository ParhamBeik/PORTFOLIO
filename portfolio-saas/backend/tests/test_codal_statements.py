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
from marketdata.codal_statements import parse_income_statement
from marketdata.models import CodalAnnouncement, CodalArtifact, CodalReport, MarketInstrument


FIXTURES = Path(__file__).parent / "fixtures" / "codal"
INTERIM = (FIXTURES / "foolad_interim_income_v9.html").read_bytes()
ANNUAL = (FIXTURES / "foolad_annual_consolidated_income_v9.html").read_bytes()


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
    CodalReport.objects.create(
        announcement=corrected, category=CodalAnnouncement.Category.AUDITOR_REPORT,
        period_end="1405-03-31", is_audited=False, is_correction=True,
    )
    income = client.get("/api/explore/stocks/فولاد/").data["financial_metrics"]
    assert income["status"] == "unavailable_unverified"
    assert income["verified_periods"] == 0
    assert income["withheld_periods"] == 1


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
