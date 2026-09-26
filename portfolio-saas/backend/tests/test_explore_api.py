"""Explore must show evidence and quality boundaries without promoting raw facts."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from marketdata import jalali
from marketdata.models import (
    CodalAnnouncement,
    CodalArtifact,
    CodalCandidateFact,
    CodalExtraction,
    CodalReport,
    CodalVerification,
    DailyStockHistory,
    MarketCandle,
    MarketInstrument,
    StockSymbolMetadata,
)

pytestmark = pytest.mark.django_db


def _stock(symbol="کاما", eligible=True, provider_group=""):
    return MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        symbol=symbol, name=f"Company {symbol}", eligible=eligible,
        provider_group=provider_group,
    )


def test_explore_requires_authentication_and_eligible_catalog_symbol(make_user):
    _stock(eligible=False)
    anonymous = APIClient()
    assert anonymous.get("/api/explore/stocks/").status_code == 401

    client = APIClient()
    client.force_authenticate(user=make_user())
    assert client.get("/api/explore/stocks/?q=کاما").data == []
    assert client.get("/api/explore/stocks/کاما/").status_code == 404


def test_company_dossier_exposes_price_conflict_but_no_unverified_metrics(make_user):
    _stock()
    StockSymbolMetadata.objects.create(
        ins_code=123, l18="کاما", l30="Kama", sector="Metals",
    )
    day = jalali.from_gregorian(timezone.localtime(timezone.now(), jalali.TEHRAN).date() - timedelta(days=3))
    DailyStockHistory.objects.create(symbol="کاما", date=day, pl=Decimal("1000"))
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time=day, close_price=Decimal("1100"),
    )
    # Legacy storage can contain a second timestamp on the same day. A clean
    # duplicate must not hide the conflicting observation.
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.UNADJUSTED,
        date_time=f"{day} 00:00:00", close_price=Decimal("1000"),
    )
    CodalAnnouncement.objects.create(
        symbol="کاما", title="Income statement", code="x", date_publish=day,
        category=CodalAnnouncement.Category.AUDITOR_REPORT,
        category_title="Auditor Notes & Opinion",
        doc_type="financial_statements", classified_by="title",
        link="https://www.codal.ir/Reports/Decision.aspx?LetterSerial=123",
    )
    client = APIClient()
    client.force_authenticate(user=make_user())

    search = client.get("/api/explore/stocks/?q=کاما")
    assert search.status_code == 200
    assert search.data[0]["symbol"] == "کاما"

    response = client.get("/api/explore/stocks/کاما/?days=90")
    assert response.status_code == 200, response.data
    data = response.data
    assert data["company"]["sector"] == "Metals"
    assert data["company"]["sector_source"] == "symbol_metadata"
    assert data["disclosures"][0]["category"] == "Financial Statements"
    assert data["disclosures"][0]["category_basis"] == "title"
    assert Decimal(data["price"]["points"][0]["close_rial"]) == Decimal("1000")
    assert data["price"]["unit"] == "Rial per share"
    assert data["price"]["candle_disagreements_over_1pct"] == 1
    assert data["price"]["quality"] == "cross_source_disagreement"
    assert data["financial_metrics"]["status"] == "unavailable_unverified"
    assert data["disclosures"][0]["source_url"].startswith("https://www.codal.ir/")


def test_company_dossier_labels_catalog_sector_when_detailed_metadata_is_missing(make_user):
    instrument = _stock(provider_group="فلزات اساسی")
    client = APIClient()
    client.force_authenticate(user=make_user())

    response = client.get("/api/explore/stocks/کاما/")
    assert response.status_code == 200
    company = response.data["company"]
    assert company["sector"] == "فلزات اساسی"
    assert company["sector_source"] == "instrument_catalog"
    assert company["sector_observed_at"] == instrument.updated_at.isoformat()
    assert company["subsector"] == ""


def test_explore_rejects_unbounded_windows_and_unsafe_disclosure_links(make_user):
    _stock()
    CodalAnnouncement.objects.create(
        symbol="کاما", title="Notice", code="x", date_publish="1405-01-01",
        link="https://example.com/forged",
    )
    client = APIClient()
    client.force_authenticate(user=make_user())
    assert client.get("/api/explore/stocks/کاما/?days=100000").status_code == 400
    assert client.get("/api/explore/stocks/?q=" + "a" * 101).status_code == 400
    response = client.get("/api/explore/stocks/کاما/")
    assert response.data["disclosures"][0]["source_url"] is None


def test_monthly_sales_requires_reconciled_latest_filing(make_user):
    _stock()
    period_end = jalali.from_gregorian(
        timezone.localtime(timezone.now(), jalali.TEHRAN).date() - timedelta(days=60)
    )
    announcement = CodalAnnouncement.objects.create(
        symbol="کاما", title="Monthly sales", code="original",
        date_publish=period_end,
        link="https://www.codal.ir/Reports/Decision.aspx?LetterSerial=123",
    )
    report = CodalReport.objects.create(
        announcement=announcement,
        category=CodalAnnouncement.Category.PRODUCTION_SALES,
        period_end=period_end,
    )
    artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.EXCEL,
        source_url="https://excel.codal.ir/report.xlsx",
        checksum_sha256="a" * 64, fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    extraction = CodalExtraction.objects.create(
        report=report, artifact=artifact,
        checksum_sha256=artifact.checksum_sha256,
        parser_version=settings.CODAL_PARSER_VERSION,
    )
    CodalCandidateFact.objects.create(
        extraction=extraction, fact_code="sales.revenue",
        numeric_value=Decimal("123456"), raw_value="123,456",
        unit="million_rial", currency="IRR",
        period_start=f"{period_end[:8]}01", period_end=period_end,
        dimensions={"row_kind": "total"},
        source_coordinates={"table": 1, "row": 15, "column": 6},
        verification_status=CodalVerification.RECONCILED,
    )
    client = APIClient()
    client.force_authenticate(user=make_user())
    monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
    assert monthly["status"] == "verified"
    assert monthly["points"][0]["value"] == "123456.000000000000"
    assert monthly["points"][0]["source_coordinates"] == {"table": 1, "row": 15, "column": 6}

    corrected = CodalAnnouncement.objects.create(
        symbol="کاما", title="Corrected monthly sales", code="correction",
        date_publish=jalali.from_gregorian(
            timezone.localtime(timezone.now(), jalali.TEHRAN).date() - timedelta(days=30)
        ),
    )
    CodalReport.objects.create(
        announcement=corrected,
        category=CodalAnnouncement.Category.PRODUCTION_SALES,
        period_end=period_end, is_correction=True,
    )
    monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
    assert monthly["status"] == "unavailable_unverified"
    assert monthly["verified_periods"] == 0
    assert monthly["withheld_periods"] == 1
