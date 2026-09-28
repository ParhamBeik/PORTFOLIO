"""The Ops census must count only what Explore would currently display."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from marketdata import jalali
from marketdata.models import (
    CodalAnnouncement, CodalArtifact, CodalCandidateFact, CodalExtraction,
    CodalReport, CodalVerification, MarketInstrument, ResearchCoverageSnapshot,
    WorkflowRun,
)
from marketdata.research_coverage import FAMILIES, refresh_research_coverage
from marketdata.tasks import capture_research_coverage


@pytest.mark.django_db
def test_coverage_uses_latest_filing_and_keeps_last_complete_scan(monkeypatch):
    for symbol, category, eligible in (
        ("AAA", MarketInstrument.Category.STOCK, True),
        ("BBB", MarketInstrument.Category.STOCK, True),
        ("ETF", MarketInstrument.Category.ETF, True),
        ("OLD", MarketInstrument.Category.STOCK, False),
    ):
        MarketInstrument.objects.create(
            source=MarketInstrument.Source.TSETMC, symbol=symbol,
            category=category, eligible=eligible,
        )
    today = timezone.localtime(timezone.now(), jalali.TEHRAN).date()
    period_end = jalali.from_gregorian(today - timedelta(days=60))
    original = CodalAnnouncement.objects.create(
        symbol="AAA", title="Monthly sales", code="original",
        date_publish=period_end,
    )
    report = CodalReport.objects.create(
        announcement=original,
        category=CodalAnnouncement.Category.PRODUCTION_SALES,
        period_end=period_end,
    )
    checksum = "a" * 64
    artifact = CodalArtifact.objects.create(
        report=report, kind=CodalArtifact.Kind.EXCEL,
        source_url="https://excel.codal.ir/report.xlsx",
        checksum_sha256=checksum, s3_key=f"codal/sha256/aa/{checksum}.xlsx",
        fetch_status=CodalArtifact.FetchStatus.STORED,
    )
    extraction = CodalExtraction.objects.create(
        report=report, artifact=artifact, checksum_sha256=checksum,
        parser_version=settings.CODAL_PARSER_VERSION,
    )
    CodalCandidateFact.objects.create(
        extraction=extraction, fact_code="sales.revenue",
        numeric_value=Decimal("123"), raw_value="123",
        unit="million_rial", currency="IRR",
        period_start=f"{period_end[:8]}01", period_end=period_end,
        dimensions={"row_kind": "total"},
        source_coordinates={"table": 1, "row": 15, "column": 6},
        verification_status=CodalVerification.RECONCILED,
    )

    first = refresh_research_coverage()
    assert first.universe_size == 2
    assert [row["symbol"] for row in first.symbols] == ["AAA", "BBB"]
    assert first.summary["monthly_sales"]["symbols_with_verified"] == 1
    assert first.summary["monthly_sales"]["symbols_without_filing"] == 1
    assert first.summary["monthly_sales"]["withheld_periods"] == 0

    CodalAnnouncement.objects.create(
        symbol="AAA", code="correction",
        title=f"اصلاحیه گزارش فعالیت ماهانه دوره 1 ماهه منتهی به {period_end}",
        date_publish=jalali.from_gregorian(today - timedelta(days=30)),
    )
    second = refresh_research_coverage()
    assert second.summary["monthly_sales"]["symbols_with_verified"] == 0
    assert second.summary["monthly_sales"]["symbols_with_withheld"] == 1
    assert second.summary["monthly_sales"]["withheld_periods"] == 1
    assert second.symbols[0]["monthly_sales"]["status"] == "unavailable_unverified"

    def fail(*_args):
        raise RuntimeError("scan interrupted")

    monkeypatch.setitem(FAMILIES, "income", fail)
    with pytest.raises(RuntimeError, match="scan interrupted"):
        capture_research_coverage()
    assert ResearchCoverageSnapshot.objects.count() == 2
    assert WorkflowRun.objects.filter(
        workflow="capture_research_coverage", outcome=WorkflowRun.Outcome.FAILED,
    ).exists()

    monkeypatch.setattr("marketdata.admin_telemetry._codal_volume_bytes", lambda: 0)
    from marketdata.admin_telemetry import _codal_status

    with override_settings(CODAL_ENABLED=False):
        status = _codal_status()
    assert status["research_coverage"]["summary"]["monthly_sales"]["withheld_periods"] == 1
    assert status["research_coverage"]["started_at"] == second.started_at.isoformat()
