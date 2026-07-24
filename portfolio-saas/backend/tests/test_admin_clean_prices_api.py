"""API test for the destructive admin price-cleanup endpoint's confirm-phrase gate.

Integration test (real HTTP request through DRF, real permission classes): the
thing under test IS the boundary — whether an unauthenticated/non-staff caller
or a bare POST without the confirm phrase can trigger irreversible deletes —
so this has to run through the actual view/URL/permission stack, not the bare
Python function.
"""
from rest_framework.test import APIClient

from portfolio.views import AdminCleanPricesExecuteView


def _staff_client(make_user):
    user = make_user(email="staffadmin@test.test")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_execute_without_confirm_phrase_is_rejected(make_user, asset_catalog):
    client = _staff_client(make_user)
    resp = client.post("/api/admin/clean-prices/execute/", {}, format="json")
    assert resp.status_code == 400


def test_execute_with_wrong_confirm_phrase_is_rejected(make_user, asset_catalog):
    client = _staff_client(make_user)
    resp = client.post(
        "/api/admin/clean-prices/execute/", {"confirm": "delete mispriced data"}, format="json"
    )
    assert resp.status_code == 400


def test_execute_with_correct_confirm_phrase_succeeds(make_user, asset_catalog):
    client = _staff_client(make_user)
    resp = client.post(
        "/api/admin/clean-prices/execute/",
        {"confirm": AdminCleanPricesExecuteView.CONFIRM_PHRASE},
        format="json",
    )
    assert resp.status_code == 200
    assert "purged_snapshots" in resp.data


def test_execute_rejects_non_staff_user(make_user, asset_catalog):
    user = make_user(email="regular@test.test")
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.post(
        "/api/admin/clean-prices/execute/",
        {"confirm": AdminCleanPricesExecuteView.CONFIRM_PHRASE},
        format="json",
    )
    assert resp.status_code == 403
