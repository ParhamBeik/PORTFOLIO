"""BrsApi.ir endpoint clients for the market-data warehouse.

Every public function here is the same three steps -- resolve the endpoint from
`endpoints.REGISTRY`, build a query string, spend that endpoint's quota bucket --
so `_call` does all three and the wrappers only name their parameters. The
registry already declares which params the provider rejects the request without
(`required_params`), which must be Jalali (`jalali_params`) and which `type`
values are legal (`valid_types`), so `_call` enforces all three generically
instead of each wrapper hand-rolling its own guard.
"""
import logging
import time

import requests

from . import endpoints, market_state
from .jalali import assert_jalali
from .quota import (
    OTHER,
    TSETMC,
    QuotaExhausted,
    clear_plan_breaker,
    looks_like_quota_error,
    reconcile_account,
    reserve_request,
    trip_plan_breaker,
)

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


class MarketDataFetchError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class PermanentMarketDataError(MarketDataFetchError):
    pass


class TransientMarketDataError(MarketDataFetchError):
    pass


def _safe_params(params):
    return {
        key: "***" if "key" in str(key).lower() else value
        for key, value in (params or {}).items()
    }


def _extract_account(response):
    """Pull the provider's `account` block out of any response, success or error.

    The block rides on every envelope (and on rate-limit error bodies), reporting
    `usage_today` and `request_block`. Swallowing parse failures here keeps a
    malformed body from masking the real HTTP error that the caller will raise.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    account = body.get("account") if isinstance(body, dict) else None
    return account if isinstance(account, dict) else None


def fetch_json(
    url,
    params=None,
    headers=None,
    timeout=20,
    retries=None,
    backoff_factor=1.0,
    quota_bucket=OTHER,
    quota_plan=TSETMC,
):
    """Execute an HTTP GET request to fetch JSON payload with backoff retries.

    Quota is reserved immediately before every HTTP attempt, against
    `quota_plan`'s wallet. A timeout does not prove that the provider failed to
    receive or bill the request, so counting only the logical fetch can
    under-report usage by the full retry multiplier. The provider's `account`
    block (when present on a response) is reconciled to that plan's
    `ApiRequestQuota` row so the local counter self-heals drift from worker
    restarts, dropped responses, and manual probing; `request_block` is honored
    as a real backoff signal on rate-limited responses.

    A quota-exhaustion response trips the plan's circuit breaker, which is what
    stops the day now that no hardcoded daily ceiling exists.
    """
    # Archive failures are rescheduled by ArchiveFetchState. Retrying inline
    # only holds a worker and can spend the provider quota several times for
    # one logical job. Live/other calls retain one short retry.
    retries = (0 if quota_bucket == "archive" else 1) if retries is None else retries
    from .quota import _breaker_is_half_open

    if _breaker_is_half_open(quota_plan, quota_bucket):
        retries = 0
    if timeout is None or timeout == 20:
        from django.conf import settings

        timeout = (
            settings.MARKETDATA_HTTP_CONNECT_TIMEOUT,
            settings.MARKETDATA_HTTP_READ_TIMEOUT,
        )
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}
    attempt = 0
    holding_probe = False
    while attempt <= retries:
        reserve_request(quota_bucket, quota_plan, holding_probe=holding_probe)
        holding_probe = True
        from .workflows import record_http_attempt

        record_http_attempt(quota=True)
        try:
            response = requests.get(url, params=params, headers=req_headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            attempt += 1
            if attempt > retries:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning(
                    "HTTP fetch failed for %s (params=%s): %s status=%s",
                    url, _safe_params(params), type(exc).__name__, status_code,
                )
                raise TransientMarketDataError(
                    f"Provider request failed ({type(exc).__name__}, status={status_code}).",
                    status_code=status_code,
                ) from exc
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
            continue

        # The provider is the source of truth for quota: reconcile our counter to
        # its `usage_today` whenever it exposes one, before deciding to retry.
        block = reconcile_account(_extract_account(response), quota_plan)

        # The provider signals an exhausted subscription with a 5xx carrying a
        # quota message. Checked before the status-class branches below, because
        # that response used to fall straight through to `response.json()` and be
        # returned as if it were data -- a 500 was never handled at all.
        if looks_like_quota_error(response.status_code, response.text):
            trip_plan_breaker(
                quota_plan,
                reason=f"http_{response.status_code}",
                bucket=quota_bucket,
            )
            raise QuotaExhausted(
                f"Provider reports the {quota_plan} plan exhausted "
                f"(HTTP {response.status_code}); paused until reset.",
                reason="plan_blocked",
            )

        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise PermanentMarketDataError(
                f"Provider rejected request with HTTP {response.status_code}.",
                status_code=response.status_code,
            )

        if response.status_code >= 500:
            # Not quota (that returned above) -- an ordinary origin failure. It
            # must still raise: returning the error body as a payload marked
            # backfills converged on zero rows.
            attempt += 1
            if attempt > retries:
                raise TransientMarketDataError(
                    f"Provider returned HTTP {response.status_code}.",
                    status_code=response.status_code,
                )
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
            continue

        if response.status_code == 429:
            # `request_block` (seconds) is the provider's own backoff ask; fall back
            # to exponential backoff when it is absent. Cap so a misreported value
            # can't stall the worker indefinitely.
            wait = min(block or backoff_factor * (2 ** attempt), 120)
            attempt += 1
            if attempt > retries:
                raise TransientMarketDataError(
                    "Provider rate-limited (HTTP 429) after retries.",
                    status_code=429,
                )
            logger.info("http_429 backing_off=%.1fs", wait)
            time.sleep(wait)
            continue

        try:
            payload = response.json()
        except ValueError as exc:
            attempt += 1
            if attempt > retries:
                raise TransientMarketDataError(
                    f"Provider returned non-JSON body (HTTP {response.status_code}).",
                    status_code=response.status_code,
                ) from exc
            time.sleep(backoff_factor * (2 ** (attempt - 1)))
            continue
        clear_plan_breaker(quota_plan, bucket=quota_bucket)
        return payload
    raise TransientMarketDataError("Provider request failed.")


def _call(endpoint_key, api_key, **params):
    """One BrsApi GET, validated against the endpoint's own registry entry.

    Returns None when no API key is configured (the "this deployment does not
    talk to the provider" case). A missing *required* param is a caller bug, not
    a no-op: several endpoints answer HTTP 200 with an empty body when one is
    omitted, which reads as a genuinely quiet day and marks a backfill converged
    on zero rows. Raise instead.
    """
    if not api_key:
        return None
    endpoint = endpoints.get(endpoint_key)
    query = {k: v for k, v in params.items() if v is not None and v != ""}
    for name in endpoint.required_params:
        if name not in query:
            raise ValueError(f"{endpoint_key} requires {name!r}; the provider 200s without it.")
    for name in endpoint.jalali_params:
        if query.get(name):
            assert_jalali(query[name], field=name)
    if endpoint.valid_types and int(query.get("type", endpoint.valid_types[0])) not in endpoint.valid_types:
        raise ValueError(f"{endpoint_key} type must be one of {endpoint.valid_types}, got {query['type']}.")
    return fetch_json(
        endpoint.url,
        params={"key": api_key, **{k: str(v) for k, v in query.items()}},
        quota_bucket=endpoint.bucket,
        quota_plan=endpoint.plan,
    )


# --------------------------------------------------------------------- catalog

def fetch_all_symbols(api_key):
    """The provider's complete TSETMC instrument catalog."""
    return _call("all_symbols", api_key, type=1)


def fetch_symbol_data(api_key, symbol):
    """One symbol's live metrics, fundamentals, order-book depth and notices."""
    return _call("symbol", api_key, l18=symbol)


# ------------------------------------------------------------- stock  history

def fetch_daily_history(api_key, symbol, history_type=0):
    """The complete daily stock history in one request.

    `history_type` 0 is the unadjusted OHLC price series (date, pf/pl/pc,
    pmin/pmax, tvol/tval/tno); 1 is the Real/Legal (حقیقی/حقوقی) participant
    breakdown (date, Buy_CountI/N, Buy_I_Volume, ...). The two payloads are
    disjoint. Adjusted prices are NOT available here -- they come from
    `fetch_candlesticks(type=3)`.
    """
    return _call("stock_history", api_key, l18=symbol, type=history_type)


def fetch_candlesticks(api_key, symbol, candle_type=3):
    """The full daily OHLC candlestick series: type 2 unadjusted, 3 adjusted."""
    return _call("stock_candles", api_key, l18=symbol, type=candle_type)


def fetch_transactions(api_key, symbol, date=None):
    """Intraday tick-by-tick trades for one symbol on one Jalali date."""
    return _call("stock_transaction_ticks", api_key, l18=symbol, date=date)


def fetch_shareholders(api_key, symbol, date=None):
    """Major institutional shareholder holdings for a symbol."""
    return _call("shareholder_records", api_key, l18=symbol, date=date)


# ---------------------------------------------------------------- live market

def fetch_market_index(api_key, index_type=1):
    """The live TSE index snapshot: type 1 overall, type 2 equal-weight.

    Also the only place the provider tells us whether the session is actually
    open (holidays included), so the `state` field is harvested on the way past.
    """
    payload = _call("market_index", api_key, type=index_type)
    market_state.remember_provider_state(payload)
    return payload


def fetch_derivatives(api_key, endpoint_key):
    """A whole live derivative-contract board (options, IME futures, ...)."""
    return _call(endpoint_key, api_key)


# ------------------------------------------------------- gold / currency / crypto

def fetch_gold_currency_free(api_key):
    """Live free gold, fiat currency and cryptocurrency prices."""
    return _call("gold_currency_free", api_key)


def fetch_gold_currency_pro(api_key, section=None):
    """Live pro market data for gold, currency and/or cryptocurrency."""
    return _call("gold_currency_pro", api_key, section=section)


def fetch_gold_currency_pro_history_24h(api_key, symbol):
    """24-hour intraday price history for one gold or currency symbol."""
    return _call("gold_currency_history", api_key, history=1, symbol=symbol)


def fetch_gold_currency_pro_history_daily(api_key, symbol, date_start=None, date_end=None):
    """The full daily history for one gold or currency symbol.

    Omitting both dates returns the entire series (~4,800 rows back to 1390) in
    one request, which is what the archive worker wants. The date params exist
    for narrow re-checks only, and must be Jalali.
    """
    return _call(
        "gold_currency_history",
        api_key,
        history=2,
        symbol="USDT" if symbol == "USDT_IRT" else symbol,
        date_start=date_start,
        date_end=date_end,
    )


# ------------------------------------------------------------------------ codal

def fetch_codal_announcements(
    api_key,
    symbol=None,
    category=None,
    audited=None,
    unaudited=None,
    only_main_company=None,
    only_subsidiaries=None,
    date_start=None,
    date_end=None,
    page=1,
):
    """Codal announcements, with the provider's optional filters."""
    flag = lambda value: None if value is None else ("true" if value else "false")  # noqa: E731
    return _call(
        "codal_announcements",
        api_key,
        page=page,
        l18=symbol,
        category=category,
        audited=flag(audited),
        unaudited=flag(unaudited),
        only_main_company=flag(only_main_company),
        only_subsidiaries=flag(only_subsidiaries),
        date_start=date_start,
        date_end=date_end,
    )
