"""Registry of every BrsApi endpoint, classified by response nature and request cost.

This is the one place that answers "is this endpoint live or historical, and what
does one request buy?". Before this module the answer was spread across
`archive.py`'s STOCK_ENDPOINTS tuple, per-fetcher `quota_bucket=` arguments, and
the beat schedule, and the three disagreed.

The classification below was established by probing the provider directly, not by
reading the code. Notably: `Gold_Currency_Pro.php?history=2` returns ~15 years in
one request, and `Transaction.php` is strictly one request per calendar day.

`Tsetmc/Nav.php` is retired from the live poll: the payload never produced a
usable series, `EtfNav.php` 404s, and production stored zero NAV snapshots.
Warehouse `etf_nav` bars remain readable if any exist.
"""
from dataclasses import dataclass, field

from .quota import ARCHIVE, BRS, LIVE, OTHER, TSETMC

BASE_URL = "https://Api.BrsApi.ir"


class Nature:
    """How an endpoint's response relates to time."""

    # Snapshot of right now. Cannot be asked for a past date, so it never gets an
    # ArchiveFetchState row -- there is no "backfill" to converge on.
    LIVE = "live"

    # Entire history in ONE request. The cheapest rows-per-quota-unit available.
    HISTORICAL_FULL = "historical_full"

    # Date-window capable and paged. Cost scales with the window requested.
    HISTORICAL_RANGE = "historical_range"

    # One request buys exactly one calendar day. The only expensive class; every
    # member needs an explicit bound or it will eat the whole daily quota.
    HISTORICAL_PER_DAY = "historical_per_day"


@dataclass(frozen=True)
class Endpoint:
    key: str
    path: str
    nature: str
    bucket: str
    # Which provider subscription bills this call. BrsApi issues one key per
    # product -- `Tsetmc/*` and `Codal/*` use the paid daily meter, while the
    # current `Market/*` product is unmetered. The path, not the API-key string,
    # selects the provider product.
    plan: str = TSETMC
    # Params the provider rejects the request without (beyond `key`).
    required_params: tuple = ()
    # Params that must be Jalali YYYY-MM-DD; Gregorian returns HTTP 400.
    jalali_params: tuple = ()
    # Approximate rows a single successful request returns, for cost ordering.
    rows_per_request: int = 1
    notes: str = ""
    valid_types: tuple = field(default=())

    @property
    def url(self):
        return f"{BASE_URL}/{self.path}"


REGISTRY = {
    endpoint.key: endpoint
    for endpoint in (
        # ------------------------------------------------------------------ LIVE
        Endpoint(
            key="gold_currency_free",
            plan=BRS,
            path="Market/Gold_Currency.php",
            nature=Nature.LIVE,
            bucket=LIVE,
            rows_per_request=60,
        ),
        Endpoint(
            key="gold_currency_pro",
            plan=BRS,
            path="Market/Gold_Currency_Pro.php",
            nature=Nature.LIVE,
            bucket=LIVE,
            rows_per_request=120,
            notes="Per-symbol time_unix shows feeds refresh at different cadences.",
        ),
        Endpoint(
            key="market_index",
            path="Tsetmc/Index.php",
            nature=Nature.LIVE,
            bucket=LIVE,
            rows_per_request=1,
            valid_types=(1, 2),
            notes=(
                "Returns a `state` field with the Persian market status; this is "
                "the only market-open signal the provider gives us. The `date` "
                "param is accepted but ignored -- there is no index history here."
            ),
        ),
        Endpoint(
            key="crypto",
            plan=BRS,
            path="Market/Cryptocurrency.php",
            nature=Nature.LIVE,
            bucket=LIVE,
            rows_per_request=774,
            notes="NOT Market/Crypto.php -- that path returns 404.",
        ),
        Endpoint(
            key="commodity",
            plan=BRS,
            path="Market/Commodity.php",
            nature=Nature.LIVE,
            bucket=LIVE,
            rows_per_request=40,
        ),
        Endpoint(
            key="option_contracts",
            path="Tsetmc/Option.php",
            nature=Nature.LIVE,
            bucket=LIVE,
            rows_per_request=200,
        ),
        # IME/Futures.php and IME/Option.php (Iran Mercantile Exchange) were
        # removed on 2026-09-06. They billed the TSETMC plan on every live tick
        # and every row they returned failed validation on arrival -- 135
        # rejected, 0 kept, per pass, for the whole time they ran. Nothing in
        # the product ever read an `ime_future`/`ime_option` row. Do not
        # re-register them without a fetcher that produces rows the validator
        # accepts; the endpoints answer, which is what made this look healthy.
        Endpoint(
            key="symbol",
            path="Tsetmc/Symbol.php",
            nature=Nature.LIVE,
            bucket=OTHER,
            required_params=("l18",),
            notes="Metadata refresh, not a customer-facing price path.",
        ),
        Endpoint(
            key="all_symbols",
            path="Tsetmc/AllSymbols.php",
            nature=Nature.LIVE,
            bucket=OTHER,
            rows_per_request=1301,
            notes="Catalog sync. `cs` carries the sector; ETFs have IRT ISINs.",
        ),
        # ------------------------------------------------------- HISTORICAL_FULL
        Endpoint(
            key="stock_history",
            path="Tsetmc/History.php",
            nature=Nature.HISTORICAL_FULL,
            bucket=ARCHIVE,
            required_params=("l18", "type"),
            rows_per_request=4616,
            valid_types=(0, 1),
            notes=(
                "type 0 is unadjusted OHLC/price history; type 1 is the "
                "real/legal participant breakdown. Adjusted prices come from "
                "Candlestick.php type 3. No date param."
            ),
        ),
        Endpoint(
            key="stock_candles",
            path="Tsetmc/Candlestick.php",
            nature=Nature.HISTORICAL_FULL,
            bucket=ARCHIVE,
            required_params=("l18", "type"),
            rows_per_request=4256,
            valid_types=(2, 3),
            notes=(
                "type 2 unadjusted, 3 adjusted. type 0 is rejected 400 and type 1 "
                "returns status=no_data -- despite what the old docstring claimed."
            ),
        ),
        Endpoint(
            key="gold_currency_history",
            plan=BRS,
            path="Market/Gold_Currency_Pro.php",
            nature=Nature.HISTORICAL_FULL,
            bucket=ARCHIVE,
            required_params=("symbol", "history"),
            jalali_params=("date_start", "date_end"),
            rows_per_request=4792,
            notes=(
                "history=2 with NO date params returns the entire series back to "
                "1390 in one request. Date-walking this endpoint is pure waste."
            ),
        ),
        # ------------------------------------------------------ HISTORICAL_RANGE
        Endpoint(
            key="codal_announcements",
            path="Codal/Announcement.php",
            nature=Nature.HISTORICAL_RANGE,
            bucket=ARCHIVE,
            jalali_params=("date_start", "date_end"),
            rows_per_request=30,
            notes=(
                "609,380 announcements across 30,469 pages. Must be scoped by "
                "recency; exhausting it is not a goal."
            ),
        ),
        # ---------------------------------------------------- HISTORICAL_PER_DAY
        Endpoint(
            key="stock_transaction_ticks",
            path="Tsetmc/Transaction.php",
            nature=Nature.HISTORICAL_PER_DAY,
            bucket=ARCHIVE,
            required_params=("l18", "date"),
            jalali_params=("date",),
            rows_per_request=500,
            notes=(
                "Omitting `date` returns [] with HTTP 200 -- a silent empty "
                "success, not an error. Always pass an explicit Jalali date."
            ),
        ),
        Endpoint(
            key="shareholder_records",
            path="Tsetmc/Shareholder.php",
            nature=Nature.HISTORICAL_PER_DAY,
            bucket=ARCHIVE,
            required_params=("l18",),
            jalali_params=("date",),
            rows_per_request=20,
        ),
    )
}


def get(key):
    return REGISTRY[key]


def bucket_for(key):
    return REGISTRY[key].bucket


def source_for(key, default=""):
    """A short "where did this row come from" label, e.g. `brsapi:Tsetmc/Symbol.php`.

    Derived from the registry rather than typed at each call site: the registry
    is already the single declaration of which provider path and which billing
    plan an endpoint uses, so a hand-written source string can only ever drift
    from it. Unknown keys fall back to `default` -- not every workflow is a
    provider fetch, and a scheduler or an aggregation has no origin to name.
    """
    endpoint = REGISTRY.get(key)
    if endpoint is None:
        return default
    return f"brsapi:{endpoint.path}"


def keys_by_nature(nature):
    return tuple(k for k, e in REGISTRY.items() if e.nature == nature)


LIVE_KEYS = keys_by_nature(Nature.LIVE)
FULL_HISTORY_KEYS = keys_by_nature(Nature.HISTORICAL_FULL)
RANGE_HISTORY_KEYS = keys_by_nature(Nature.HISTORICAL_RANGE)
PER_DAY_KEYS = keys_by_nature(Nature.HISTORICAL_PER_DAY)
