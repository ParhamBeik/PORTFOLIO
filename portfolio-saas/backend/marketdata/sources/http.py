"""HTTP transport for the free, unmetered origins (TGJU, Nobitex, Wallex, TSETMC).

Deliberately NOT `marketdata.fetchers.fetch_json`. That function's whole
contract is BrsApi's: it reserves quota against a plan wallet before every
attempt, reconciles the provider's `account` block, and trips a per-plan circuit
breaker on an exhaustion body. None of that applies here -- these origins are
free and publish no usage envelope -- and routing them through it would spend
the BrsApi budget on requests BrsApi never sees. That is the exact failure this
whole migration exists to end, so the two transports stay separate.

What replaces the quota machinery:

  * a per-origin minimum interval, because an unmetered origin that starts
    refusing us is worse than a metered one that bills us. Wallex served 60
    requests in 17.1s without complaint, which is precisely why we pace.
  * a per-origin reachability breaker, so a blocked host (TSETMC without an
    Iranian egress) costs one probe per cooldown instead of a connect timeout
    on every scheduled fetch. Codal without this burned ~583 doomed connects
    and ~20,000 no-op workflow runs in a single day.
  * an optional egress proxy, for origins that drop this host (TSETMC, even
    from the Iranian production VPS).
"""
import logging
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# A browser-shaped UA. TSETMC's CDN in particular answers differently to an
# obvious script client, and the others do not care -- so this costs nothing and
# removes one variable from any future debugging session.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
}


class SourceError(RuntimeError):
    """Base for every direct-source failure."""

    def __init__(self, message, *, origin=None, status_code=None):
        super().__init__(message)
        self.origin = origin
        self.status_code = status_code


class SourceUnreachable(SourceError):
    """Connect-level failure: DNS, TCP, TLS, or the breaker being open.

    Distinct from `SourceResponseError` on purpose. Unreachable means "the
    network path is wrong", which is an operator problem and must not be retried
    in a tight loop; a bad response means "the path works, the request didn't",
    which is a code or argument problem worth surfacing loudly.
    """


class SourceResponseError(SourceError):
    """The origin answered, but not with usable JSON."""


# ------------------------------------------------------------------ pacing

_pace_lock = threading.Lock()
_last_call_at: dict[str, float] = {}


def _pace(origin):
    """Block until this origin's minimum interval has elapsed.

    Process-local by design. A distributed token bucket in Redis would be
    stricter, but the live loop runs these fetches from one worker and the
    archive from another, so per-process pacing already keeps each well under
    any published limit. Escalate only if a real 429 ever appears.
    """
    interval = float(getattr(settings, "DIRECT_SOURCE_MIN_INTERVAL", 0.25))
    if interval <= 0:
        return
    with _pace_lock:
        wait = interval - (time.monotonic() - _last_call_at.get(origin, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call_at[origin] = time.monotonic()


# -------------------------------------------------------------- breaker

def _breaker_key(origin):
    return f"marketdata:source_breaker:{origin}"


def _failure_key(origin):
    return f"marketdata:source_failures:{origin}"


def origin_is_parked(origin):
    return bool(cache.get(_breaker_key(origin)))


def _note_failure(origin):
    """Count a connect-level failure and park the origin once it repeats.

    `cache.incr` needs the key to exist, and a `get`-then-`set` would lose
    counts across the live and archive workers, so the miss path seeds it. The
    race is benign: worst case two workers each seed 1 and we take one extra
    attempt before parking.
    """
    threshold = int(getattr(settings, "DIRECT_SOURCE_FAILURE_THRESHOLD", 8))
    cooldown = int(getattr(settings, "DIRECT_SOURCE_COOLDOWN_SECONDS", 600))
    key = _failure_key(origin)
    try:
        failures = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=cooldown * 4)
        failures = 1
    if failures >= threshold:
        cache.set(_breaker_key(origin), True, timeout=cooldown)
        cache.delete(key)
        logger.warning(
            "source_breaker_open origin=%s after %s consecutive connect failures; "
            "parked for %ss",
            origin, failures, cooldown,
        )


def _note_success(origin):
    cache.delete(_failure_key(origin))
    cache.delete(_breaker_key(origin))


# ------------------------------------------------------------------ fetch

def fetch(url, *, origin, params=None, headers=None, proxy=None, timeout=None,
          retries=1, expect_json=True):
    """One GET against a free origin, paced and breaker-guarded.

    `origin` is the pacing/breaker identity ("tgju", "wallex", ...), not the
    hostname: TGJU serves live quotes and history from two different hosts that
    share one operator, and pacing them independently would double the load we
    actually place on it.

    Returns the decoded JSON (or the raw text when `expect_json` is False).
    Raises rather than returning None, because a silent None here becomes an
    empty ingest, and an empty ingest is recorded as a genuinely quiet market
    day -- the failure mode that has bitten this warehouse repeatedly.
    """
    if origin_is_parked(origin):
        raise SourceUnreachable(
            f"{origin} is parked by the reachability breaker.", origin=origin
        )

    if timeout is None:
        timeout = (
            settings.MARKETDATA_HTTP_CONNECT_TIMEOUT,
            settings.MARKETDATA_HTTP_READ_TIMEOUT,
        )
    proxies = {"http": proxy, "https": proxy} if proxy else None
    request_headers = {**DEFAULT_HEADERS, **(headers or {})}

    attempt = 0
    while True:
        _pace(origin)
        try:
            response = requests.get(
                url, params=params, headers=request_headers,
                timeout=timeout, proxies=proxies,
            )
        except requests.exceptions.RequestException as exc:
            attempt += 1
            if attempt > retries:
                _note_failure(origin)
                raise SourceUnreachable(
                    f"{origin} unreachable at {url} ({type(exc).__name__}).",
                    origin=origin,
                ) from exc
            time.sleep(0.5 * (2 ** (attempt - 1)))
            continue

        # The path works, so the breaker must reset even if the RESPONSE is bad.
        # Counting a 404 as a reachability failure would park a live origin
        # because one symbol was misspelled.
        _note_success(origin)

        if response.status_code >= 500 or response.status_code == 429:
            attempt += 1
            if attempt > retries:
                raise SourceResponseError(
                    f"{origin} returned HTTP {response.status_code}.",
                    origin=origin, status_code=response.status_code,
                )
            time.sleep(1.0 * (2 ** (attempt - 1)))
            continue

        if response.status_code >= 400:
            raise SourceResponseError(
                f"{origin} rejected the request with HTTP {response.status_code}.",
                origin=origin, status_code=response.status_code,
            )

        if not expect_json:
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise SourceResponseError(
                f"{origin} returned a non-JSON body (HTTP {response.status_code}).",
                origin=origin, status_code=response.status_code,
            ) from exc


def iran_egress_proxy():
    """The proxy for origins that geo-block foreign source addresses.

    Empty string means "connect directly", which is correct on a host inside
    Iran and simply fails fast (into the breaker) on one outside it.
    """
    return getattr(settings, "IRAN_EGRESS_PROXY", "") or None
