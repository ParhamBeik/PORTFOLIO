"""Panel readings are evidence, not a substitute for local attribution."""
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

from marketdata.models import ApiRequestQuota
from marketdata.endpoints import REGISTRY, billing_product
from marketdata.provider_meter import (
    ProviderMeterUnavailable,
    parse_panel_metrics,
    read_panel_metrics,
)
from marketdata.quota import AIO, MARKET_CGCC, quota_day, reconcile_panel_metrics
from marketdata.tasks import reconcile_quota_meters


PANEL = {
    "successful": True,
    "metrics": [
        {"Name_En": "AIO", "Usage_To_Limit": "9,465/10,000"},
        {"Name_En": "Market_CGCC", "Usage_To_Limit": "627/1500"},
    ],
}


def test_every_registered_path_matches_the_vps_counter_matrix():
    for key, endpoint in REGISTRY.items():
        expected = (
            MARKET_CGCC
            if endpoint.path in {
                "Market/Gold_Currency.php",
                "Market/Cryptocurrency.php",
                "Market/Commodity.php",
            }
            else AIO
        )
        assert billing_product(key, {"history": 2}) == expected


def test_parse_panel_metrics_requires_complete_valid_snapshot():
    assert parse_panel_metrics(PANEL) == {
        AIO: {"used": 9465, "limit": 10000},
        MARKET_CGCC: {"used": 627, "limit": 1500},
    }
    with pytest.raises(ProviderMeterUnavailable):
        parse_panel_metrics({"successful": True, "metrics": PANEL["metrics"][:1]})
    with pytest.raises(ProviderMeterUnavailable):
        parse_panel_metrics({"successful": False, "metrics": PANEL["metrics"]})
    with pytest.raises(ProviderMeterUnavailable):
        parse_panel_metrics({"successful": True, "metrics": PANEL["metrics"] * 2})


def test_panel_read_does_not_leak_credentials_on_request_error(settings):
    settings.BRSAPI_ACCOUNT_PHONE = "private-phone"
    settings.TSETMC_API_KEY = "private-key"
    with patch("marketdata.provider_meter.requests.get", side_effect=requests.RequestException("private-key")):
        with pytest.raises(ProviderMeterUnavailable) as error:
            read_panel_metrics()
    assert "private-key" not in str(error.value)
    assert error.value.__cause__ is None


def test_panel_read_parses_both_counters(settings):
    settings.BRSAPI_ACCOUNT_PHONE = "private-phone"
    settings.TSETMC_API_KEY = "private-key"
    response = Mock(status_code=200)
    response.json.return_value = PANEL
    with patch("marketdata.provider_meter.requests.get", return_value=response) as get:
        assert read_panel_metrics()[MARKET_CGCC]["used"] == 627
    assert get.call_args.kwargs["params"] == {"Phone": "private-phone", "Key": "private-key"}


@pytest.mark.django_db
def test_panel_reconciliation_preserves_local_counts_and_observation_time():
    row = ApiRequestQuota.objects.create(
        day=quota_day(), plan=AIO, used=3, local_attempts=3,
        successful_requests=2, live_used=1, archive_used=2,
    )
    observed_at = datetime.now(ZoneInfo("Asia/Tehran"))
    result = reconcile_panel_metrics(parse_panel_metrics(PANEL), day=quota_day(), observed_at=observed_at)
    row.refresh_from_db()
    assert (row.provider_used, row.used, row.limit) == (9465, 9465, 10000)
    assert (row.local_attempts, row.successful_requests, row.live_used, row.archive_used) == (3, 2, 1, 2)
    assert (row.provider_observed_at, row.provider_observation_source) == (observed_at, "panel")
    assert result[AIO]["variance"] == 9462
    assert ApiRequestQuota.objects.get(day=quota_day(), plan=MARKET_CGCC).provider_used == 627


@pytest.mark.django_db
def test_panel_task_is_opt_in_and_does_not_probe_when_disabled(settings):
    settings.MARKETDATA_PANEL_METER_ENABLED = False
    with patch("marketdata.provider_meter.read_panel_metrics") as read:
        assert reconcile_quota_meters() == {"status": "disabled"}
    read.assert_not_called()

    settings.MARKETDATA_PANEL_METER_ENABLED = True
    with patch("marketdata.provider_meter.read_panel_metrics", return_value=parse_panel_metrics(PANEL)):
        result = reconcile_quota_meters()
    assert result[AIO]["provider_used"] == 9465
