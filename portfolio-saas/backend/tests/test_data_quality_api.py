import datetime as dt
from decimal import Decimal

import jdatetime
import pytest
from rest_framework.test import APIClient

from marketdata.models import GoldCurrencyHistory, MarketInstrument
from portfolio.models import Account, Holding


pytestmark = pytest.mark.django_db


def test_account_data_quality_is_windowed_and_account_scoped(
    asset_catalog, make_user
):
    owner = make_user("quality-owner@test.test")
    intruder = make_user("quality-intruder@test.test")
    account = Account.objects.create(user=owner, name="Quality")
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "EMAMI"
    asset.save(update_fields=["brs_symbol"])
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("1"))
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS,
        symbol="EMAMI",
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )
    today = dt.date.today()
    jalali = jdatetime.date.fromgregorian(date=today)
    GoldCurrencyHistory.objects.create(
        symbol="EMAMI",
        date=f"{jalali.year:04d}-{jalali.month:02d}-{jalali.day:02d}",
        close_price=100,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(
        f"/api/accounts/{account.id}/data-quality/",
        {"from": (today - dt.timedelta(days=4)).isoformat(), "to": today.isoformat()},
    )
    client.force_authenticate(user=intruder)
    denied = client.get(f"/api/accounts/{account.id}/data-quality/")

    assert response.status_code == 200, response.data
    assert response.data["account_id"] == account.id
    assert response.data["assets"][0]["observed_sessions"] == 1
    assert response.data["assets"][0]["expected_sessions"] == 5
    assert response.data["assets"][0]["reason_codes"] == ["low_coverage"]
    assert denied.status_code == 404
