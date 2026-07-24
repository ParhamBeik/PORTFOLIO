import pytest
from rest_framework.test import APIClient


pytestmark = pytest.mark.django_db


def test_price_history_rejects_invalid_limit(make_user):
    client = APIClient()
    client.force_authenticate(user=make_user())

    response = client.get("/api/prices/history/?asset=emami_coin&limit=abc")

    assert response.status_code == 400
