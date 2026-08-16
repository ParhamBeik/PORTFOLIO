"""Requesting real_toman without CPI coverage must be honest, not a 500.

Integration test: the behaviour under test is the DRF exception handler wired
into the request/response cycle, so it only reproduces through the API layer —
a unit test of `cpi_for()` alone proves the raise, not the response.
"""
import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from config.settings import CpiUnavailable, cpi_for
from portfolio.models import Account

pytestmark = pytest.mark.django_db


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_cpi_for_raises_instead_of_clamping_past_the_table():
    # The old behaviour silently returned the newest known value for ANY future
    # year, which made real_toman identical to nominal_toman for 17 months.
    with pytest.raises(CpiUnavailable) as exc:
        cpi_for(9999)
    assert exc.value.jalali_year == 9999
    assert exc.value.last_verified_year < 9999


def test_cpi_extrapolates_below_the_base_year_on_purpose():
    # Flat before the base year is defined behaviour, not a missing value.
    assert cpi_for(1000) == cpi_for(1398)


@override_settings(CPI_BY_JALALI_YEAR_EXTRA_APPLIED=True)
def test_real_toman_reports_unavailable_rather_than_returning_nominal(
    asset_catalog, make_user
):
    user = make_user(email="cpi-gap@test.test")
    Account.objects.create(user=user, name="Main")

    response = _client(user).get("/api/valuation/?basis=real_toman")

    # Either the CPI covers the period (200) or it honestly refuses (503).
    # What must never happen is a 500, or a 200 carrying nominal numbers
    # mislabelled as real.
    assert response.status_code in (200, 503), response.status_code
    if response.status_code == 503:
        assert response.data["reason"] == "cpi_unavailable"
        assert response.data["basis"] == "real_toman"
        assert "last_verified_jalali_year" in response.data
