"""Coverage report classification helpers."""
import pytest
from django.utils import timezone

from marketdata.coverage_report import classify_archive_state, classify_live_asset
from marketdata.models import ArchiveFetchState
from portfolio.models import Asset, Price


pytestmark = pytest.mark.django_db


def test_classify_archive_state_lifecycle():
    assert classify_archive_state(ArchiveFetchState(verified_complete=True)) == "complete"
    assert classify_archive_state(ArchiveFetchState(last_attempt_at=None)) == "not_tried"
    assert classify_archive_state(ArchiveFetchState(last_attempt_at=timezone.now(), consecutive_failures=2)) == "failed"
    assert classify_archive_state(ArchiveFetchState(last_attempt_at=timezone.now(), stored_rows=3, missing_rows=2)) == "partial"


def test_classify_live_asset(asset_catalog):
    manual = Asset.objects.get(key="emami_coin")
    manual.is_manual = True
    assert classify_live_asset(manual, None, now=timezone.now()) == "manual"

    asset = Asset.objects.get(key="kama_stock")
    asset.is_manual = False
    asset.tse_symbol = asset.tse_symbol or "کاما"
    asset.save(update_fields=["tse_symbol"])
    price = Price.objects.create(asset=asset, price="5000", source="TEST")
    assert classify_live_asset(asset, price, now=timezone.now()) == "fresh"
