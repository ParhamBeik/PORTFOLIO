"""Per-account scoping of the analytics endpoint.

The top-bar portfolio selector drives `?account=<id>` on every analytics call.
This guards the view->service boundary: that scoping resolves to the owned
account and that `current_weights` / `total_value_tomans` reflect ONLY that
account's holdings, while an absent param aggregates across all of the user's
accounts. `current_weights` and `total_value_tomans` derive from the live
valuation (not the returns matrix), so no price history is required.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from marketdata.models import AssetMetricSnapshot
from portfolio.models import Account, Holding, Snapshot
from portfolio.services.insights import net_worth_trend

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def test_analytics_scoped_to_one_account(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "kama_stock": Decimal("5230")})
    pro = make_user(email="scope@t.t")

    # Two portfolios with disjoint holdings: A is all gold, B is all stock.
    a = Account.objects.create(user=pro, name="Retirement")
    b = Account.objects.create(user=pro, name="Trading")
    Holding.objects.create(account=a, asset=asset_catalog["emami_coin"], quantity=Decimal("2"))  # 960M
    Holding.objects.create(account=b, asset=asset_catalog["kama_stock"], quantity=Decimal("100"))  # 523,000

    client = _client(pro)

    # Account A: weights are 100% emami_coin, total is A's liquid value only.
    resp_a = client.get(f"/api/analytics/?account={a.id}")
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert set(body_a["current_weights"]) == {"emami_coin"}
    assert body_a["current_weights"]["emami_coin"] == pytest.approx(1.0)
    assert Decimal(body_a["total_value_tomans"]) == Decimal("960000000")

    # Account B: disjoint — kama_stock only, different total. Proves A did not leak in.
    resp_b = client.get(f"/api/analytics/?account={b.id}")
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert set(body_b["current_weights"]) == {"kama_stock"}
    assert Decimal(body_b["total_value_tomans"]) == Decimal("523000")

    # No param: aggregate across both accounts.
    resp_all = client.get("/api/analytics/")
    assert resp_all.status_code == 200
    body_all = resp_all.json()
    assert set(body_all["current_weights"]) == {"emami_coin", "kama_stock"}
    assert Decimal(body_all["total_value_tomans"]) == Decimal("960523000")


def test_analytics_rejects_account_owned_by_another_user(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "kama_stock": Decimal("5230")})
    pro = make_user(email="owner@t.t")
    other = make_user(email="other@t.t")

    mine = Account.objects.create(user=pro, name="Retirement")
    Holding.objects.create(account=mine, asset=asset_catalog["emami_coin"], quantity=Decimal("2"))

    theirs = Account.objects.create(user=other, name="Secret")
    Holding.objects.create(account=theirs, asset=asset_catalog["kama_stock"], quantity=Decimal("100"))

    resp = _client(pro).get(f"/api/analytics/?account={theirs.id}")
    assert resp.status_code == 404


def test_analytics_rejects_invalid_account_id(make_user):
    pro = make_user(email="invalid-scope@t.t")
    assert _client(pro).get("/api/analytics/?account=abc").status_code == 400


def test_aggregate_trend_ignores_account_snapshots(make_user):
    user = make_user(email="trend@t.t")
    account = Account.objects.create(user=user, name="Trading")
    Snapshot.objects.create(user=user, account=None, total_value_tomans=100)
    Snapshot.objects.create(user=user, account=account, total_value_tomans=1000)
    Snapshot.objects.create(user=user, account=None, total_value_tomans=110)
    assert net_worth_trend(user)["delta_pct"] == 10


def test_asset_ranking_is_scoped_to_owned_account(asset_catalog, make_user):
    pro = make_user(email="ranking@t.t")
    account = Account.objects.create(user=pro, name="Mine")
    other = Account.objects.create(user=pro, name="Other")
    asset_catalog["emami_coin"].brs_symbol = "IR_COIN_EMAMI"
    asset_catalog["emami_coin"].save(update_fields=["brs_symbol"])
    asset_catalog["kama_stock"].tse_symbol = "KAMA"
    asset_catalog["kama_stock"].save(update_fields=["tse_symbol"])
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=1
    )
    Holding.objects.create(
        account=other, asset=asset_catalog["kama_stock"], quantity=1
    )
    AssetMetricSnapshot.objects.create(
        symbol="IR_COIN_EMAMI", as_of="1405-05-10", sharpe=2
    )
    AssetMetricSnapshot.objects.create(
        symbol="KAMA", as_of="1405-05-10", sharpe=9
    )

    response = _client(pro).get(
        f"/api/analytics/asset-ranking/?account={account.id}"
    )

    assert response.status_code == 200
    assert [row["symbol"] for row in response.json()] == ["IR_COIN_EMAMI"]
