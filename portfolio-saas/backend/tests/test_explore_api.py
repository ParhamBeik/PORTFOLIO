"""Explore must show evidence and quality boundaries without promoting raw facts."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from marketdata import jalali
from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    MarketCandle,
    MarketInstrument,
    StockSymbolMetadata,
)

pytestmark = pytest.mark.django_db


def _stock(symbol="کاما", eligible=True):
    return MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        symbol=symbol, name=f"Company {symbol}", eligible=eligible,
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
    assert Decimal(data["price"]["points"][0]["close_rial"]) == Decimal("1000")
    assert data["price"]["unit"] == "Rial per share"
    assert data["price"]["candle_disagreements_over_1pct"] == 1
    assert data["price"]["quality"] == "cross_source_disagreement"
    assert data["financial_metrics"]["status"] == "unavailable_unverified"
    assert data["disclosures"][0]["source_url"].startswith("https://www.codal.ir/")


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
