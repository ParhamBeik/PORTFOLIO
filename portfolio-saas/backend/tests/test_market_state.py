from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from marketdata.market_state import claim_provider_state_probe


def test_open_session_reprobes_after_cached_closed_state():
    now = datetime(2026, 8, 22, 9, 30, tzinfo=ZoneInfo("Asia/Tehran"))
    with patch("marketdata.market_state._provider_says_closed", return_value=True):
        with patch("marketdata.market_state.get_redis", return_value=None):
            assert claim_provider_state_probe(now) is True
