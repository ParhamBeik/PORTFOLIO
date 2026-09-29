"""Explore must show evidence and quality boundaries without promoting raw facts."""

from datetime import timedelta
from decimal import Decimal
import hashlib
import io

import pytest
from django.conf import settings
from django.test import override_settings
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
from research.models import ResearchRun
from research.observations import build_observations

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


def test_catalog_pages_every_eligible_listing_and_filters_sector(make_user):
    for number in range(55):
        _stock(symbol=f"STK{number:03d}")
    _stock(symbol="HIDDEN", eligible=False)
    StockSymbolMetadata.objects.create(
        ins_code=4242, l18="STK050", l30="Fifty", sector="Metals",
    )
    client = APIClient()
    client.force_authenticate(user=make_user())

    first = client.get("/api/explore/stocks/?catalog=1&page=1").data
    second = client.get("/api/explore/stocks/?catalog=1&page=2").data
    assert first["count"] == 55
    assert len(first["results"]) == 50
    assert len(second["results"]) == 5
    assert first["next_page"] == 2
    assert second["next_page"] is None
    assert {row["symbol"] for row in first["results"] + second["results"]} == {
        f"STK{number:03d}" for number in range(55)
    }
    filtered = client.get("/api/explore/stocks/?catalog=1&sector=Metals").data
    assert [row["symbol"] for row in filtered["results"]] == ["STK050"]
    assert filtered["results"][0]["financial_status"] == "issuer_filing_certification_pending"


def test_public_dossier_requires_explicit_approval_and_omits_provider_prices():
    _stock()
    _stock(symbol="OTHER")
    anonymous = APIClient()
    url = "/api/public/research/stocks/کاما/"
    assert anonymous.get(url).status_code == 404
    assert anonymous.get("/api/public/research/stocks/").data["results"] == []
    with override_settings(PUBLIC_DOSSIERS_ENABLED=True, PUBLIC_DOSSIER_SYMBOLS=frozenset()):
        assert anonymous.get(url).status_code == 404
    with override_settings(PUBLIC_DOSSIERS_ENABLED=True, PUBLIC_DOSSIER_SYMBOLS=frozenset({"کاما"})):
        response = anonymous.get(url)
    assert response.status_code == 200
    assert response.data["company"]["symbol"] == "کاما"
    assert response.data["price_status"] == "withheld_pending_redistribution_rights"
    assert "price" not in response.data
    assert "portfolio" not in response.data
    with override_settings(PUBLIC_DOSSIERS_ENABLED=True, PUBLIC_DOSSIER_SYMBOLS=frozenset({"کاما"})):
        catalog = anonymous.get("/api/public/research/stocks/").data
    assert [row["symbol"] for row in catalog["results"]] == ["کاما"]


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


def test_monthly_sales_requires_reconciled_latest_filing(make_user, monkeypatch):
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
    archived_bytes = b"<html><body>Filed sales table</body></html>"
    checksum = hashlib.sha256(archived_bytes).hexdigest()
    artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.EXCEL,
        source_url="https://excel.codal.ir/report.xlsx",
        checksum_sha256=checksum, s3_key=f"codal/sha256/{checksum[:2]}/{checksum}.xlsx",
        size_bytes=len(archived_bytes),
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    extraction = CodalExtraction.objects.create(
        report=report, artifact=artifact,
        checksum_sha256=artifact.checksum_sha256,
        parser_version=settings.CODAL_PARSER_VERSION,
    )
    fact = CodalCandidateFact.objects.create(
        extraction=extraction, fact_code="sales.revenue",
        numeric_value=Decimal("123456"), raw_value="123,456",
        unit="million_rial", currency="IRR",
        period_start=f"{period_end[:8]}01", period_end=period_end,
        dimensions={"row_kind": "total"},
        source_coordinates={"table": 1, "row": 15, "column": 6},
        verification_status=CodalVerification.RECONCILED,
    )
    user = make_user()
    client = APIClient()
    client.force_authenticate(user=user)
    monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
    assert monthly["status"] == "verified"
    assert monthly["points"][0]["value"] == "123456.000000000000"
    assert monthly["points"][0]["source_coordinates"] == {"table": 1, "row": 15, "column": 6}

    class ArchiveClient:
        def __init__(self, content):
            self.content = content

        def get_object(self, **_kwargs):
            return {"Body": io.BytesIO(self.content)}

    from marketdata import codal_storage
    archive_client = ArchiveClient(archived_bytes)
    monkeypatch.setattr(codal_storage, "_client", lambda: archive_client)
    evidence_url = f"/api/explore/stocks/کاما/evidence/{extraction.pk}/?days=365"
    assert APIClient().get(evidence_url).status_code == 401
    response = client.get(evidence_url)
    assert response.status_code == 200
    assert response.content == archived_bytes
    assert response["X-Archive-SHA256"] == checksum
    assert response["Content-Disposition"].endswith('.html"')
    assert response["Cache-Control"] == "private, no-store"
    assert client.get(evidence_url.replace("days=365", "days=100000")).status_code == 400
    _stock(symbol="other")
    assert client.get(evidence_url.replace("کاما", "other")).status_code == 404
    archive_client.content = b"<html>tampered</html>"
    assert client.get(evidence_url).status_code == 503
    archive_client.content = archived_bytes

    # A reconciled label alone does not make an unsupported source trace safe.
    original_hash = artifact.checksum_sha256
    for invalid_hash in ("", "g" * 64):
        artifact.checksum_sha256 = invalid_hash
        artifact.save(update_fields=["checksum_sha256"])
        extraction.checksum_sha256 = invalid_hash
        extraction.save(update_fields=["checksum_sha256"])
        monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
        assert monthly["status"] == "unavailable_unverified"
        assert monthly["points"] == []
    artifact.checksum_sha256 = original_hash
    artifact.save(update_fields=["checksum_sha256"])
    extraction.checksum_sha256 = original_hash
    extraction.save(update_fields=["checksum_sha256"])

    artifact.fetch_status = CodalArtifact.FetchStatus.PENDING
    artifact.save(update_fields=["fetch_status"])
    assert client.get("/api/explore/stocks/کاما/").data["monthly_sales"]["points"] == []
    artifact.fetch_status = CodalArtifact.FetchStatus.STORED
    artifact.save(update_fields=["fetch_status"])
    original_key = artifact.s3_key
    artifact.s3_key = ""
    artifact.save(update_fields=["s3_key"])
    assert client.get("/api/explore/stocks/کاما/").data["monthly_sales"]["points"] == []
    artifact.s3_key = original_key
    artifact.save(update_fields=["s3_key"])

    original_coordinates = fact.source_coordinates
    for coordinates in ({}, {"table": 1, "row": 15}, {"row": 15, "column": 6}):
        fact.source_coordinates = coordinates
        fact.save(update_fields=["source_coordinates"])
        monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
        assert monthly["status"] == "unavailable_unverified"
        assert monthly["points"] == []
    fact.source_coordinates = original_coordinates
    fact.save(update_fields=["source_coordinates"])
    monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
    assert monthly["status"] == "verified"
    claim = build_observations(monthly)["latest"]
    saved = ResearchRun.objects.create(
        user=user, symbol="کاما", question="Latest verified sales?",
        status=ResearchRun.Status.ANSWERED, max_cost_usd=Decimal("0.01"),
        selected_observations=[{"id": "latest", **claim}],
        evidence={"scope": "one_tse_company_sales_365_days_statements_3650_days"},
    )
    detail_url = f"/api/research/runs/{saved.pk}/"
    assert client.get(detail_url).data["evidence_state"] == "current"

    corrected = CodalAnnouncement.objects.create(
        symbol="کاما", title=f"اصلاحیه گزارش فعالیت ماهانه دوره 1 ماهه منتهی به {period_end}", code="correction",
        date_publish=jalali.from_gregorian(
            timezone.localtime(timezone.now(), jalali.TEHRAN).date() - timedelta(days=30)
        ),
    )
    monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
    assert monthly["status"] == "unavailable_unverified"
    assert monthly["verified_periods"] == 0
    assert monthly["withheld_periods"] == 1
    assert client.get(evidence_url).status_code == 404
    assert client.get(detail_url).data["evidence_state"] == "changed"

    CodalReport.objects.create(
        announcement=corrected,
        category=CodalAnnouncement.Category.PRODUCTION_SALES,
        period_end=period_end, is_correction=True,
    )
    monthly = client.get("/api/explore/stocks/کاما/").data["monthly_sales"]
    assert monthly["status"] == "unavailable_unverified"
    assert monthly["verified_periods"] == 0
    assert monthly["withheld_periods"] == 1
