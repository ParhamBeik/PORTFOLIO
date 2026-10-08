"""Valuation engine: holdings x latest prices -> portfolio value.

SaaS port of the tracker engine build_snapshot pricing rules.
Two scale levers live here:
  1. latest prices are read once (DISTINCT ON) and cached, not per-asset;
  2. valuation is pure arithmetic over a preloaded holding set.
"""
import logging
import time
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from marketdata.currency import (
    holding_value_to_toman,
    is_tse_priced,
    to_toman,
)

from portfolio.models import (
    Account,
    Asset,
    HOUSE_AREA_SQM,
    HOUSE_PRICE_SCALE,
    Holding,
    LedgerEntry,
    Liability,
    Price,
    USD_QUOTED_KEYS,
    positive_price_q,
)
from .timeline import cash_as_of, cash_on_days, holdings_as_of, house_state_as_of, load_house_marks
from .visibility import hidden_asset_ids, hidden_keys
from marketdata.calendars import candle_close_qs, market_closure_days
from marketdata.market_state import (
    CLOSED_DAYTIME,
    DAYTIME_START,
    OPEN,
    SESSION_START,
    TEHRAN,
    TRADING_WEEKDAYS,
)
from marketdata.models import GoldCurrencyHistory
from marketdata.provenance import (
    BRS_SERIES_ENDPOINTS,
    PRICE_SERIES_ENDPOINTS,
    STOCK_SERIES_ENDPOINTS,
    daily_bar_price,
    rejected_pairs,
    toman_rate_kwargs,
    toman_rate_tables,
)

logger = logging.getLogger(__name__)

_LATEST_PRICES_CACHE_KEY = "prices:latest:verified-toman-v2"
_LATEST_PRICES_STATE_KEY = "prices:latest:verified-toman-v2:market-state"
_LATEST_PRICES_LOCK_KEY = "prices:latest:verified-toman-v2:rebuild-lock"
_LATEST_PRICES_LOCK_SECONDS = 30
_LATEST_PRICES_WAIT_SECONDS = 5.0
_ARCHIVE_DROP_FLOOR = Decimal("0.50")
_ARCHIVE_SPIKE_CEILING = Decimal("2.00")

# One definition, shared with the Ops console rather than restated. Two literal
# 300s for "how old is too old" is how the console and the valuation drift into
# disagreeing about the same price.
from marketdata.evidence import LIVE_PRICE_FRESH_SECONDS as _FRESH_SECONDS
# The live loop only fetches TSE prices while the market session is OPEN, and
# gold/currency/crypto prices during OPEN or CLOSED_DAYTIME, pausing only
# OVERNIGHT (marketdata.market_state.live_job_keys). Outside those windows the
# last price IS the current price -- flagging it "stale" by a flat clock
# threshold marked every held stock stale every evening and every Thursday/
# Friday (the Iranian weekend), for data that was never wrong.
#
# ponytail: these are calendar-day grace bounds, not a real trading-session
# calendar (see MAX_FORWARD_FILL_SESSIONS below for that). Good enough to
# cover the Thu/Fri weekend without a session lookup on every valuation call;
# upgrade to `calendars.sessions_between` if a genuine multi-day outage needs
# finer detection than "older than the grace window".
_CLOSED_TSE_GRACE_SECONDS = 4 * 24 * 3600
_CLOSED_BRS_GRACE_SECONDS = 24 * 3600

# Shared by every price-resolution path in this module (guard_price_map,
# compute_dynamic_net_worth_series, value_as_of): forward-fill past this many
# sessions invents a price rather than reports one. Sessions are always counted
# with `marketdata.calendars.sessions_between` -- never in calendar days, and
# never off a single symbol's own rows. See that helper for why both fail.
MAX_FORWARD_FILL_SESSIONS = 5

# How far back the session calendar used to age archive closes is built. Only
# needs to comfortably exceed MAX_FORWARD_FILL_SESSIONS in calendar days --
# wide enough that Nowruz (~2 weeks shut) still leaves the bound measurable,
# narrow enough that the lookup never walks the whole candle history.
_STALENESS_WINDOW_DAYS = 60


def current_market_state() -> str:
    from marketdata.market_state import market_state

    return market_state()


def _asset_market_is_open(asset, state: str) -> bool:
    """Return whether this asset's live feed should be authoritative now."""

    if asset.asset_class == Asset.AssetClass.CRYPTO:
        return True
    if asset.asset_class == Asset.AssetClass.STOCK or asset.tse_symbol:
        return state == OPEN
    if (
        asset.asset_class in (Asset.AssetClass.GOLD, Asset.AssetClass.CASH)
        or asset.brs_symbol
    ):
        return state in (OPEN, CLOSED_DAYTIME)
    return False


def _q(value) -> Decimal:
    """Coerce to Decimal, treating anything non-numeric as zero."""
    try:
        return Decimal(value)
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def get_latest_prices() -> dict:
    """Return cached portfolio prices keyed by asset.

    TSE stock values are Rial and valuation divides their value by ten; other
    admitted prices are Toman. Old foreign-seed ticks without a verified unit
    become unavailable until a declared archive close or new live tick exists.

    Uses Postgres DISTINCT ON to fetch the newest price for every asset in a
    single query, so this is O(1) regardless of how many assets or users exist.

    The archive cross-check below is deliberately on the READ path as well as the
    write path (`guard_price_map`). Guarding only writes assumes every row in the
    table arrived through the fetch loop, and a single bad row that got in by any
    other route -- a direct edit, a row predating the guard, a future writer that
    forgets to call it -- is otherwise trusted forever by every valuation. The
    blast radius is total rather than partial: one `price=1` row values the whole
    holding at one Toman. The extra queries are amortised by the 120s cache.

    The live fetch task refreshes this cache right after it writes
    (`refresh_prices_cache`), so a reader normally hits a warm entry. A miss is
    rebuilt by one request at a time: the rebuild is a market-wide DISTINCT ON
    plus the archive cross-check, and before the single-flight lock every
    request that arrived during a rebuild ran its own copy of it.
    """
    current_state = current_market_state()
    cached = _cached_latest_prices(current_state)
    if cached is not None:
        return cached

    lock_acquired = cache.add(_LATEST_PRICES_LOCK_KEY, 1, timeout=_LATEST_PRICES_LOCK_SECONDS)
    if not lock_acquired:
        deadline = time.monotonic() + _LATEST_PRICES_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.05)
            cached = _cached_latest_prices(current_state)
            if cached is not None:
                return cached
        # The holder is slow or died: compute rather than keep a request waiting.
    try:
        return _store_latest_prices(_compute_latest_prices(current_state), current_state)
    finally:
        if lock_acquired:
            cache.delete(_LATEST_PRICES_LOCK_KEY)


def _cached_latest_prices(current_state):
    cached = cache.get(_LATEST_PRICES_CACHE_KEY)
    if cached is not None and cache.get(_LATEST_PRICES_STATE_KEY) == current_state:
        return cached
    return None


def _store_latest_prices(prices, current_state):
    cache.set(_LATEST_PRICES_CACHE_KEY, prices, timeout=120)
    cache.set(_LATEST_PRICES_STATE_KEY, current_state, timeout=120)
    return prices


def refresh_prices_cache() -> dict:
    """Recompute the latest-price map and overwrite the cache in place.

    For writers (the live fetch task). Deleting the entry instead left every
    reader that arrived before the next rebuild to recompute it themselves --
    every 20 seconds while the market is open.
    """
    current_state = current_market_state()
    return _store_latest_prices(_compute_latest_prices(current_state), current_state)


def _compute_latest_prices(current_state) -> dict:
    latest = (
        Price.objects.select_related("asset")
        .filter(positive_price_q(), asset__is_active=True)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    latest = list(latest)
    prices = {
        row.asset.key: (
            _q(row.price)
            if row.asset.key not in USD_QUOTED_KEYS
            or (row.price_unit == Price.Unit.IRT and row.price_unit_verified)
            else Decimal("0")
        )
        for row in latest
    }
    fetched_at = {
        row.asset.key: row.fetched_at
        for row in latest
        if row.asset.key not in USD_QUOTED_KEYS
        or (row.price_unit == Price.Unit.IRT and row.price_unit_verified)
    }
    # Replace only what is already priced. Filling assets that have no Price row
    # at all is the write path's job; doing it here would turn "unpriced" into a
    # silent archive value and hide the gap the valuation layer reports.
    prices.update({
        key: value
        for key, value in _archive_replacements(
            prices,
            live_fetched_at=fetched_at,
            market_state=current_state,
        ).items()
        if key in prices
    })
    return prices


def guard_price_map(prices: dict, *, fill_missing=True, archive_replacements=None) -> dict:
    """Replace missing or broken live prices with previous prices or archive closes."""
    supplied_keys = set(prices)
    guarded = {key: _q(value) for key, value in prices.items()}

    # Read once, used twice: the forward-fill below needs the newest stored row
    # per asset, and so does the archive comparison -- a close only outranks a
    # live price if it is not from an older session than the one already held.
    latest_db_rows = list(
        Price.objects.select_related("asset")
        .filter(positive_price_q(), asset__is_active=True)
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    prev_prices = {
        row.asset.key: (_q(row.price), row.fetched_at)
        for row in latest_db_rows
        if row.asset.key not in USD_QUOTED_KEYS
        or (row.price_unit == Price.Unit.IRT and row.price_unit_verified)
    }

    # 1. The warehouse close is more authoritative than an older live row.
    replacements = (
        archive_replacements
        if archive_replacements is not None
        else _archive_replacements(
            guarded,
            live_fetched_at={key: when for key, (_price, when) in prev_prices.items()},
        )
    )
    guarded.update(
        replacements if fill_missing else {
            key: value for key, value in replacements.items() if key in supplied_keys
        }
    )

    # 2. Forward-fill only when neither live nor archive data is available, and
    # only within MAX_FORWARD_FILL_SESSIONS days. The forward-filled value is
    # never persisted as a new Price row (see _persistable_prices), so without
    # this bound a delisted/broken asset's `fetched_at` never advances and this
    # keeps re-forward-filling the same ancient price forever -- the same
    # invariant value_as_of enforces via `calendars.sessions_between`.
    now = timezone.now()
    max_age = timedelta(days=MAX_FORWARD_FILL_SESSIONS)

    # Large positive moves remain observable; archive corroboration above
    # rejects only catastrophic deviations.
    for key, live_price in list(guarded.items()):
        prev = prev_prices.get(key)
        if not prev or live_price > 0:
            continue
        prev_price, prev_fetched_at = prev
        if prev_price <= 0:
            continue
        if now - prev_fetched_at > max_age:
            logger.warning(
                "Key='%s' missing or zero live price and last price is older than "
                "%s days (fetched_at=%s). Not forward-filling.",
                key, MAX_FORWARD_FILL_SESSIONS, prev_fetched_at,
            )
            continue
        logger.info(
            "Key='%s' missing or zero live price. Forward-filling previous price %s.",
            key, prev_price
        )
        guarded[key] = prev_price

    return guarded


def stored_price_sessions(keys) -> dict:
    """When each key's newest stored price was recorded.

    The session a price belongs to is what says whether an archive close is
    newer information or older; see `_archive_replacements`.
    """
    return {
        row.asset.key: row.fetched_at
        for row in Price.objects.select_related("asset")
        .filter(positive_price_q(), asset__key__in=list(keys))
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
        if row.asset.key not in USD_QUOTED_KEYS
        or (row.price_unit == Price.Unit.IRT and row.price_unit_verified)
    }


def fetch_sessions(live_prices: dict, *, fetched_at) -> dict:
    """Session per key for one fetch cycle.

    A key quoted this cycle is from `fetched_at`; a key that went unquoted --
    which is every TSE symbol the moment the session closes -- keeps the
    session of whatever is already stored. Claiming everything was fetched
    this instant asserts the archive is behind a price that does not exist.
    """
    stored = stored_price_sessions(live_prices.keys())
    sessions = {}
    for key, value in live_prices.items():
        when = fetched_at if _q(value) > 0 else stored.get(key)
        if when is not None:
            sessions[key] = when
    return sessions


def _daily_bar_as_of(asset, jalali_str) -> tuple[Decimal, int]:
    """(Toman close, sessions stale) from the live-only daily-bar series.

    Returns (0, 0) when there is no usable bar, so the caller's existing
    `missing_price` branch handles it. The staleness count is produced here
    rather than skipped: a bar is a price like any other and the 5-session
    forward-fill bound applies to it too.
    """
    from marketdata.calendars import market_for_asset, sessions_between

    rows = daily_bar_price([asset], as_of=jalali_str, latest_only=True)
    if not rows:
        return Decimal("0"), 0
    _symbol, date, price = max(rows, key=lambda row: row[1])
    return _q(price), sessions_between(
        date, jalali_str, market=market_for_asset(asset)
    )


def _newest_close_per_symbol(
    base_qs, symbols, rejections, *, date_field, include_unit=False,
) -> dict:
    """Newest non-rejected close per symbol, optionally retaining its unit.

    Postgres `DISTINCT ON` answers "newest row per symbol" in one index-ordered
    pass, which is the whole job in the overwhelming case. What it cannot
    express is "newest row that is not in this rejection set", so the rare
    symbol whose top row IS rejected falls through to a second scan scoped to
    just those symbols -- same answer, paid for only where it is needed.

    The shape this replaces streamed EVERY adjusted candle and EVERY gold row
    that the held symbols had ever printed, then kept the first of each via
    `setdefault`: tens of thousands of rows fetched per call to use one each,
    on a path the live valuation hits on every cache miss.
    """
    if not symbols:
        return {}
    ordering = ("symbol", f"-{date_field}")
    resolved: dict = {}
    contested: list[str] = []
    fields = ("symbol", date_field, "close_price", "unit") if include_unit else (
        "symbol", date_field, "close_price"
    )
    top = (
        base_qs.order_by(*ordering)
        .distinct("symbol")
        .values(*fields)
    )
    for row in top:
        day = str(row[date_field]).split()[0]
        if (row["symbol"], day) in rejections:
            contested.append(row["symbol"])
        else:
            resolved[row["symbol"]] = (
                (day, row["close_price"], row["unit"])
                if include_unit else (day, row["close_price"])
            )
    if contested:
        rest = (
            base_qs.filter(symbol__in=contested)
            .order_by(*ordering)
            .values(*fields)
        )
        for row in rest:
            if row["symbol"] in resolved:
                continue
            day = str(row[date_field]).split()[0]
            if (row["symbol"], day) not in rejections:
                resolved[row["symbol"]] = (
                    (day, row["close_price"], row["unit"])
                    if include_unit else (day, row["close_price"])
                )
    return resolved


def _foreign_brs_symbols(assets) -> set[str]:
    """Symbols for which an unlabelled close cannot safely mean Toman."""
    from marketdata.models import MarketInstrument

    assets = list(assets)
    symbols = {asset.brs_symbol for asset in assets if asset.brs_symbol}
    foreign = {
        asset.brs_symbol for asset in assets
        if asset.brs_symbol and (
            asset.key in USD_QUOTED_KEYS
            or asset.asset_class == Asset.AssetClass.CRYPTO
        )
    }
    foreign.update(
        MarketInstrument.objects.filter(
            source=MarketInstrument.Source.BRS,
            symbol__in=symbols,
            category__in=(
                MarketInstrument.Category.CRYPTO,
                MarketInstrument.Category.COMMODITY,
            ),
        ).values_list("symbol", flat=True)
    )
    return foreign


def _brs_close_toman(symbol, day, close, unit, foreign_symbols, cash, tether):
    if not unit and symbol in foreign_symbols:
        return Decimal("0")
    return to_toman(
        symbol, close, unit,
        **toman_rate_kwargs(unit, day, cash, tether),
    )


def _latest_archive_closes(assets) -> tuple[dict, dict]:
    """Newest usable warehouse close per asset key, as (prices, Jalali dates).

    Three tables answer this depending on the feed -- adjusted candles for TSE
    symbols, gold/currency history for BRS symbols, and the distilled daily bar
    for the live-only classes that have no provider history endpoint at all.
    BRS and daily-bar foreign quotes are converted at their own dated rate;
    TSE candles remain Rial for the portfolio share convention. Rejected rows
    are excluded from all three.
    """

    stock_symbols = {
        asset.tse_symbol: asset.key
        for asset in assets
        if asset.tse_symbol
    }
    brs_symbols = {
        asset.brs_symbol: asset.key
        for asset in assets
        if asset.brs_symbol
    }

    all_symbols = list(stock_symbols.keys()) + list(brs_symbols.keys())
    rejections = rejected_pairs(all_symbols, PRICE_SERIES_ENDPOINTS)

    archive_prices = {}
    archive_dates = {}
    # Adjusted closes live in MarketCandle.ADJUSTED. History.php?type=1 (once
    # tagged is_adjusted=True on DailyStockHistory, since removed -- see
    # marketdata.ingest.ingest_real_legal) was never adjusted prices at all,
    # it is the Real/Legal breakdown, so that fallback used to silently match
    # nothing.
    stock_newest = _newest_close_per_symbol(
        candle_close_qs(stock_symbols), stock_symbols, rejections,
        date_field="date_time",
    )
    for symbol, (day, close) in stock_newest.items():
        key = stock_symbols[symbol]
        archive_prices.setdefault(key, _q(close))
        archive_dates.setdefault(key, day)

    brs_newest = _newest_close_per_symbol(
        GoldCurrencyHistory.objects.filter(symbol__in=brs_symbols, close_price__gt=0),
        brs_symbols, rejections, date_field="date", include_unit=True,
    )
    cash_rates, tether_rates = toman_rate_tables(
        [unit for _day, _close, unit in brs_newest.values()],
        [day for day, _close, _unit in brs_newest.values()],
    )
    foreign_symbols = _foreign_brs_symbols(assets)
    for symbol, (day, close, unit) in brs_newest.items():
        key = brs_symbols[symbol]
        price = _brs_close_toman(
            symbol, day, close, unit, foreign_symbols, cash_rates, tether_rates,
        )
        if price > 0:
            archive_prices.setdefault(key, price)
            archive_dates.setdefault(key, day)

    # Live-only feeds converge into MarketDailyBar, which stores the provider's
    # number in whatever currency it was quoted and carries no unit column.
    # `provenance.daily_bar_price` is the one reader that resolves all of that
    # -- class guard, rejected rows, and the unit converted at each row's own
    # date. Four callers used to re-derive it and reached four different answers.
    bar_newest = {}
    for symbol, date, price in daily_bar_price(assets, latest_only=True):
        if date > bar_newest.get(symbol, ("",))[0]:
            bar_newest[symbol] = (date, price)
    for symbol, key in {**stock_symbols, **brs_symbols}.items():
        if key in archive_prices or symbol not in bar_newest:
            continue
        archive_dates[key], archive_prices[key] = bar_newest[symbol]

    return archive_prices, archive_dates


def _archive_replacements(
    prices: dict,
    *,
    live_fetched_at: dict | None = None,
    market_state: str | None = None,
    verified_close_keys: set[str] | None = None,
) -> dict:
    """Return archive-backed replacements for missing, broken, or outrun live prices.

    `live_fetched_at`, when given, also catches a live price that passes the
    magnitude sanity band below but is simply behind: the live loop can miss
    an entire trading session outright (an outage), while the warehouse's own
    archive backfill -- a separate pipeline, unaffected by a live-loop outage
    -- keeps converging on real closes. A live price from session N-1 sitting
    next to an archive close already at session N is not a spike, it is stale
    data that happens to still be in a plausible range.
    """
    from marketdata.calendars import market_for_asset, session_calendar, sessions_between
    from portfolio.services.returns import to_jalali_str

    assets = list(Asset.objects.filter(is_active=True).exclude(is_house=True))
    assets_by_key = {asset.key: asset for asset in assets}
    archive_prices, archive_dates = _latest_archive_closes(assets)
    if live_fetched_at is None:
        # Default to the sessions we already hold, so a caller that supplies
        # nothing still cannot have a lagging close substituted for a newer
        # price. Opting IN to that protection is how the closed-market
        # regression reached three separate branches.
        live_fetched_at = stored_price_sessions(assets_by_key.keys())

    # One calendar per market, built once for the whole batch rather than per
    # asset. The window is deliberately a fixed recent slice and NOT "back to
    # the oldest close in play": MarketCandle is a 7.5M-row table, and a single
    # delisted asset whose last close is a year old would otherwise make every
    # price fetch scan a year of candles across every symbol just to count
    # distinct dates.
    #
    # A bounded window still answers the only question asked here -- "is this
    # close more than MAX_FORWARD_FILL_SESSIONS sessions old?" -- because a date
    # falling before the window start bisects to 0 and yields the full session
    # count, which is by construction over the bound. It also keeps the closure
    # semantics right: if the market barely traded in the window, few sessions
    # elapsed and the price is correctly NOT stale.
    now = timezone.now()
    now_jalali = to_jalali_str(now)
    window_start = to_jalali_str(now - timedelta(days=_STALENESS_WINDOW_DAYS))
    session_calendars = {
        market: session_calendar(market, start=window_start, end=now_jalali)
        for market in ("tse", "gold_currency")
    }

    replacements = {}
    for key, archive_price in archive_prices.items():
        live_price = _q(prices.get(key))
        asset = assets_by_key.get(key)
        archive_date = archive_dates[key]
        live_session = (
            to_jalali_str(live_fetched_at[key])
            if live_fetched_at and key in live_fetched_at
            else None
        )
        # Past the forward-fill bound this close is not a current price, it is
        # the last thing a delisted/halted asset ever printed. It may no longer
        # stand in FOR a price -- the valuation layer should report the gap
        # rather than dress an ancient number up as today's -- but it is still
        # a valid magnitude reference, so the spike guard below keeps using it.
        too_stale = asset is not None and sessions_between(
            archive_date, now_jalali,
            calendar=session_calendars[market_for_asset(asset)],
        ) > MAX_FORWARD_FILL_SESSIONS
        if too_stale:
            logger.warning(
                "Key='%s' archive close %s is from %s, beyond the %d-session "
                "forward-fill bound -- it may still veto a corrupt quote but "
                "will not stand in as a current price.",
                key, archive_price, archive_date, MAX_FORWARD_FILL_SESSIONS,
            )
        # The archive ingests a session's close well after that session ends,
        # so between the bell and the backfill the newest live row IS the
        # close. Substituting an older archive row for a price we already hold
        # from a later session walks the value backwards -- it replaced today's
        # real closing price with yesterday's the moment the market shut.
        archive_is_behind = live_session is not None and archive_date < live_session
        if (
            asset
            and market_state
            and not _asset_market_is_open(asset, market_state)
            and not archive_is_behind
            and not too_stale
        ):
            logger.info("Key='%s' market is closed; using archive close %s.", key, archive_price)
            replacements[key] = archive_price
            if verified_close_keys is not None:
                verified_close_keys.add(key)
        elif live_price <= 0 and not archive_is_behind and not too_stale:
            # No quote this cycle. The archive stands in only if it is not
            # older than what we already have: once the session closes the
            # live loop stops quoting, and an unguarded substitution here
            # persists yesterday's close as today's newest observation.
            logger.warning("Key='%s' Live=0. Using archive price %s", key, archive_price)
            replacements[key] = archive_price
        elif live_price > 0 and (
            live_price < archive_price * _ARCHIVE_DROP_FLOOR
            or live_price > archive_price * _ARCHIVE_SPIKE_CEILING
        ):
            # Deliberately NOT gated on the archive being current: a live quote
            # this far out of band is corrupt, and an older good close beats a
            # fresh bad one. `live_price > 0` keeps "no quote at all" out of
            # here -- zero is below every floor and would match every time.
            logger.warning(
                "Key='%s' Live=%s Archive=%s outside range [%s, %s]. Using archive price.",
                key, live_price, archive_price, archive_price * _ARCHIVE_DROP_FLOOR, archive_price * _ARCHIVE_SPIKE_CEILING
            )
            replacements[key] = archive_price
        elif live_price > 0 and live_session is not None and archive_date > live_session:
            logger.warning(
                "Key='%s' live price is from session %s but the archive already "
                "has session %s (%s) -- the live loop missed a session, using "
                "the newer archive close.",
                key, live_session, archive_date, archive_price,
            )
            replacements[key] = archive_price
    return replacements


def _is_tse_asset(asset) -> bool:
    """Whether this asset's LIVE LANE is the TSETMC subscription.

    Deliberately the same test as `currency.is_tse_priced`, and it must stay
    that way. It briefly also matched `asset_class == STOCK`, which is the
    predicate `is_tse_priced` exists to reject -- two names for one concept,
    disagreeing on manual stocks, in the same file. The unit boundary is the
    consequence that matters: route a divisor through a wider predicate and a
    manual stock silently loses 90% of its value.
    """
    return is_tse_priced(asset)


def _live_plan_blocked(asset) -> bool:
    """Whether this asset's live provider lane cannot currently fetch.

    Two distinct ways for that to be true, and asking only the first is why this
    badge could not fire in the incident it was written for:

      * the breaker is open ON THE LIVE BUCKET -- `is_plan_blocked(..., LIVE)`
        deliberately answers False for an archive-tripped breaker, since archive
        exhausting itself must never silence live;
      * live has simply run out of its own slice, which is what actually happened
        when archive drained a whole wallet before dawn with `live_used=0`.
        There the breaker was never live-tripped at all, so the first check alone
        falls through to "Stale" and the operator gets no quota signal.
    """
    from marketdata.quota import BRS, LIVE, TSETMC, is_plan_blocked, remaining_requests

    plan = TSETMC if _is_tse_asset(asset) else BRS
    if is_plan_blocked(plan, bucket=LIVE):
        return True
    try:
        # `remaining_requests` is the same accounting `reserve_request` uses, so
        # the badge cannot disagree with what the fetcher is actually allowed.
        return remaining_requests(LIVE, plan) <= 0
    except Exception:  # noqa: BLE001 -- a badge must never break valuation
        return False


def _clock_sessions_elapsed(asset, fetched_at, now) -> int:
    """Sessions that have OPENED since this quote, by the wall clock.

    Warehouse calendars cannot answer this when the same quota block that
    starved the live loop also starved today's candles -- `sessions_between`
    would report 0 and keep the badge on Live. So the weekly rhythm is counted
    from the clock, but known past closures still come from the calendar: only
    the current day can be missing from it for the reason above, and without
    that a two-week Nowruz shutdown flips every holding to Stale on day one.

    A session counts when it opened strictly after the quote and at or before
    `now` -- which is the whole point. Starting the walk at quote_date + 1 meant
    a 07:00 print, taken before the 08:30 open, was never measured against its
    own day's session and stayed green through and after the session it missed.
    """
    from datetime import datetime, time as dtime, timedelta

    import jdatetime


    now_local = now.astimezone(TEHRAN)
    quote_local = fetched_at.astimezone(TEHRAN)
    end = now_local.date()
    tse = _is_tse_asset(asset)
    open_at = dtime(*(SESSION_START if tse else DAYTIME_START))
    closed = _known_closure_days(tse, quote_local.date(), end)

    elapsed = 0
    day = quote_local.date()
    while day <= end:
        jalali_day = jdatetime.date.fromgregorian(date=day)
        session_day = (not tse) or jalali_day.weekday() in TRADING_WEEKDAYS
        if session_day and jalali_day.strftime("%Y-%m-%d") not in closed:
            opened = datetime.combine(day, open_at, tzinfo=TEHRAN)
            if quote_local < opened <= now_local:
                elapsed += 1
        day += timedelta(days=1)
    return elapsed


def _known_closure_days(tse: bool, start, end) -> frozenset:
    """Jalali days the exchange was shut, for days the warehouse can vouch for.

    TSE only -- `market_closure_days` reads whole-market volume, which the
    gold/FX desk has no equivalent of, and that desk publishes on almost every
    calendar day anyway.

    Excludes today: a missing candle for the current day is exactly the
    quota-starvation case this whole function exists to see through, so it must
    never be read as "the market was closed".
    """
    from datetime import timedelta

    import jdatetime


    if not tse or start >= end:
        return frozenset()
    try:
        return frozenset(
            market_closure_days(
                start=jdatetime.date.fromgregorian(date=start).strftime("%Y-%m-%d"),
                end=jdatetime.date.fromgregorian(
                    date=end - timedelta(days=1)
                ).strftime("%Y-%m-%d"),
            )
        )
    except Exception:  # noqa: BLE001 -- fall back to the pure clock
        return frozenset()


def _live_quality_status(
    asset, age_seconds: int, state: str, *, fetched_at=None, now=None
) -> str:
    """live / stale / quota for a stored Price row.

    A quote older than `_FRESH_SECONDS` is only still "live" if no newer one
    should have arrived -- that asset's market is shut AND this print is from
    the latest session. A quota-blocked live lane, or a trading session that
    has started since the quote, means the number on screen is last-known,
    not current. Closed-market grace must not launder a missed session into
    a green Live badge.
    """
    if age_seconds <= _FRESH_SECONDS:
        return "live"
    degraded = "quota" if _live_plan_blocked(asset) else "stale"
    if _asset_market_is_open(asset, state):
        return degraded
    if fetched_at is not None:
        as_of = now or timezone.now()
        if _clock_sessions_elapsed(asset, fetched_at, as_of) > 0:
            return degraded
    grace = (
        _CLOSED_TSE_GRACE_SECONDS if _is_tse_asset(asset) else _CLOSED_BRS_GRACE_SECONDS
    )
    return "live" if age_seconds <= grace else "stale"


def invalidate_prices_cache() -> None:
    """Called after a fresh fetch so reads immediately see new prices."""
    cache.delete(_LATEST_PRICES_CACHE_KEY)
    cache.delete(_LATEST_PRICES_STATE_KEY)


def _house_value(price_per_sqm_million: Decimal, area_sqm: Decimal = HOUSE_AREA_SQM) -> Decimal:
    """Gross real-estate value: area * price/sqm.

    `price_per_sqm_million` is user-entered as "million Tomans per sqm", matching
    the Toman scale every other asset_value() branch produces (Price is
    Toman-denominated).

    The mortgage is deliberately NOT subtracted here. Since migration 0017 a
    mortgage is a `Liability` row, and every caller already nets liabilities off
    the account total — deducting it again here would double-count the debt.
    """
    sqm_price = _q(price_per_sqm_million) * HOUSE_PRICE_SCALE
    area = _q(area_sqm) if area_sqm is not None else HOUSE_AREA_SQM
    return sqm_price * area


# Every money field a liability row carries, named once so `views._common`
# converts all of them under a foreign basis. A row that is half Toman and half
# dollars is worse than one that is entirely the wrong currency, because the
# error is only visible in the arithmetic between two of its own columns.
LIABILITY_MONEY_FIELDS = (
    "amount_tomans",
    "declared_amount_tomans",
    "principal_tomans",
    "monthly_installment_tomans",
)


def _liability_row(liability, as_of=None) -> dict:
    """One debt, as the valuation payload reports it.

    `amount_tomans` is what was actually NETTED off the total -- the outstanding
    balance for `as_of`, not the column of the same name. The rows have to add
    up to `total_liabilities` printed above them, and for a loan being repaid
    the stored column stopped being that number the month after it was typed.
    The stored figure is still reported, under `declared_amount_tomans`, so the
    two can be compared where they are supposed to agree.
    """
    outstanding = liability.outstanding_tomans(as_of)
    return {
        "id": liability.id,
        "label": liability.label,
        "kind": liability.kind,
        "lender": liability.lender,
        "amount_tomans": float(outstanding),
        "declared_amount_tomans": float(liability.amount_tomans),
        "balance_basis": liability.balance_basis,
        "principal_tomans": (
            float(liability.principal_tomans)
            if liability.principal_tomans is not None
            else None
        ),
        "monthly_installment_tomans": (
            float(liability.monthly_installment_tomans)
            if liability.monthly_installment_tomans is not None
            else None
        ),
        "asset_key": liability.asset.key if liability.asset else None,
    }


def asset_value(holding: Holding, price: Decimal) -> Decimal:
    """Quantity x unit price in Toman, or the house formula for real estate."""
    if holding.asset.is_house:
        return _house_value(holding.quantity, area_sqm=getattr(holding, "area_sqm", HOUSE_AREA_SQM))
    return holding_value_to_toman(
        holding.asset, _q(holding.quantity) * _q(price)
    )



def _quality_rollup(items, excluded, total_assets, priced_assets):
    if total_assets and priced_assets == 0:
        return "unavailable"
    statuses = {item["quality_status"] for item in items}
    if excluded or statuses & {"unavailable", "stale", "fallback", "quota"}:
        return "partial"
    if statuses <= {"live"}:
        return "complete"
    return "manual"


def value_account(
    account: Account, prices: dict | None = None, *, include_hidden: bool = False
) -> dict:
    """Compute one account's per-asset values and total.

    Returns: {'total': Decimal, 'items': [{'asset','key','class','quantity',
    'unit_price','value'}], 'hidden_items': [...]}

    Holdings the user has switched off come back in `hidden_items` -- valued, so
    the UI can show what is being left out, but absent from `items` and from
    every derived figure. Keeping them out of `items` rather than tagging them
    inside it is deliberate: `items` is what the allocation donut, the risk
    weights (`views._holding_weights`), the diagnostics and the optimizer all
    read, and the split makes every one of them honour the tick with no code of
    its own.

    `include_hidden=True` restores the everything-you-own total. Only the
    snapshot writers pass it, so the recorded history keeps one meaning and
    switching an asset off never puts a step in it.
    """
    prices = prices if prices is not None else get_latest_prices()
    items, hidden_items, excluded, total = [], [], [], Decimal("0")
    hidden_ids = set() if include_hidden else hidden_asset_ids([account] if account.pk else [])
    liabilities_qs = (
        account.liabilities.select_related("asset") if account.pk else Liability.objects.none()
    )
    # A hidden house takes its mortgage with it. Subtracting the debt of an asset
    # we are not counting would drop net worth by the loan alone.
    liabilities_qs = [l for l in liabilities_qs if l.asset_id not in hidden_ids]
    # `outstanding_tomans()`, never `amount_tomans`: a loan being repaid owes
    # less every month, and the stored column is only the figure last written
    # down. See `Liability.outstanding_tomans` for the three rules.
    total_liabilities = sum(
        (l.outstanding_tomans() for l in liabilities_qs), Decimal("0")
    )
    holdings = (
        account.holdings.select_related("asset")
        if account.pk
        else Holding.objects.none()
    )
    holdings = list(holdings)
    latest_rows = {
        row.asset_id: row
        for row in Price.objects.filter(asset_id__in=[h.asset_id for h in holdings])
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    }
    now = timezone.now()
    priced_assets = 0
    from marketdata.currency import TSE_PRICE_UNIT, tse_unit_verified
    from marketdata.market_state import market_state as _current_market_state

    market_state_now = _current_market_state()
    for holding in holdings:
        # None when the asset has no price yet — distinguishable from a real 0 (M2).
        unit_price = prices.get(holding.asset.key)
        row = latest_rows.get(holding.asset_id)
        archive_record = None
        is_hidden = holding.asset_id in hidden_ids
        if holding.asset.is_house or (
            holding.asset.is_manual and unit_price is not None and _q(unit_price) > 0
        ):
            value = asset_value(holding, unit_price)
            source = "manual_valuation"
            priced_at = holding.updated_at
            age_seconds = max(0, int((now - holding.updated_at).total_seconds()))
            # Manual marks never poll; stop calling them live after 90 days.
            quality_status = "manual" if age_seconds <= 90 * 86400 else "stale"
        elif unit_price is None or _q(unit_price) <= 0:
            value = None
            source = None
            priced_at = None
            age_seconds = None
            quality_status = "unavailable"
            if not is_hidden:
                # A switched-off holding is not "excluded from the valuation for
                # want of a price" -- it is excluded because the user said so.
                excluded.append({
                    "asset_key": holding.asset.key,
                    "reason": "missing_price",
                })
        else:
            value = asset_value(holding, unit_price)
            row_unit_usable = (
                row is not None and (
                    holding.asset.key not in USD_QUOTED_KEYS
                    or (row.price_unit == Price.Unit.IRT and row.price_unit_verified)
                )
            )
            if row_unit_usable and _q(row.price) == _q(unit_price):
                source = row.source
                priced_at = row.fetched_at
                age_seconds = max(0, int((now - row.fetched_at).total_seconds()))
                quality_status = _live_quality_status(
                    holding.asset,
                    age_seconds,
                    market_state_now,
                    fetched_at=row.fetched_at,
                    now=now,
                )
            else:
                source = "archive"
                priced_at = None
                age_seconds = None
                quality_status = "fallback"
                from marketdata.provenance import latest_archive_close

                archive_record = latest_archive_close(holding.asset)
        if value is not None and not is_hidden:
            total += value
            priced_assets += 1
        price_unit_status = "ok"
        if holding.asset.tse_symbol and not tse_unit_verified():
            price_unit_status = "unverified"
        item = {
            "asset": holding.asset.name,
            "name_fa": holding.asset.name_fa or "",
            # `label` is what to show; `display_name` is the raw nickname, blank
            # when there isn't one. An edit box needs the second, not the first,
            # or clearing the field looks like a rename to the catalog name.
            "label": holding.label,
            "display_name": holding.display_name,
            # The ticker, so the UI can offer it as the rename placeholder and
            # keep `name_fa` (the registered company name) for the tooltip.
            "symbol": holding.asset.tse_symbol or "",
            "key": holding.asset.key,
            "class": holding.asset.asset_class,
            "is_manual": holding.asset.is_manual,
            "is_house": holding.asset.is_house,
            "is_hidden": is_hidden,
            "quantity": holding.quantity,
            # What one of this asset is. Declared by the server for the same
            # reason `unit_price_currency` is: the client cannot infer it from
            # the asset class (one Gold row is grams, the rest are coins) and
            # guessing produced a quantity editor that offered fractions of a
            # share. See `Asset.quantity_step`.
            "quantity_step": holding.asset.quantity_step,
            "unit_price": unit_price,
            "value": value,
            "source": source,
            "priced_at": priced_at.isoformat() if priced_at else None,
            "age_seconds": age_seconds,
            "quality_status": quality_status,
            "price_unit_status": price_unit_status,
            # Which currency `unit_price` is quoted in. `value` is ALWAYS Toman --
            # `holding_value_to_toman` divides the product, never the price, because
            # a TSE quote is shown in Rial on purpose. The client cannot infer this
            # from the asset class (a manual stock has no TSE feed and is priced in
            # Toman), and inferring it from magnitude is how unit bugs start, so the
            # server declares it. Without it the UI suffixed every price "T" and a
            # reader multiplying price x quantity got ten times the value shown.
            "unit_price_currency": "rial" if is_tse_priced(holding.asset) else "toman",
        }
        if holding.asset.is_house:
            # The two numbers a property is actually described by. Sent from here
            # so no client re-derives the millions-per-sqm convention.
            item["area_sqm"] = holding.area_sqm
            item["price_per_sqm_tomans"] = holding.price_per_sqm_tomans
        if archive_record:
            item["archive_record"] = archive_record
        if is_hidden:
            hidden_items.append(item)
            continue
        items.append(item)
    total_assets = len(items)
    quality_status = _quality_rollup(items, excluded, total_assets, priced_assets)
    # Cash is net worth. It lives on the account (rebuilt from the ledger), not
    # in `items`, because items feed risk weights and per-asset performance,
    # which have no meaning for a toman balance. Performance used to add it on
    # its own while Home and the snapshot history did not, so the two disagreed
    # by exactly the cash balance.
    cash = (account.cash_balance_tomans or Decimal("0")) if account.pk else Decimal("0")
    total += cash
    total -= total_liabilities
    return {
        "total": total,
        "cash_tomans": cash,
        "items": items,
        "hidden_items": hidden_items,
        "priced_assets": priced_assets,
        "total_assets": total_assets,
        "quality_status": quality_status,
        "tse_unit_policy": TSE_PRICE_UNIT,
        "markets": market_clocks(items),
        "excluded": excluded,
        "liabilities": [_liability_row(l) for l in liabilities_qs],
        "total_liabilities": float(total_liabilities),
    }


#: Home's per-market clocks, in display order: (group, label, member classes).
MARKET_GROUPS = (
    ("stocks", "Stocks", {Asset.AssetClass.STOCK}),
    ("gold_fx", "Gold & FX", {Asset.AssetClass.GOLD, Asset.AssetClass.CASH}),
    ("crypto", "Crypto", {Asset.AssetClass.CRYPTO}),
)


def market_clocks(items) -> list[dict]:
    """One clock per market the user actually holds.

    The total mixes markets that keep different hours: at 20:00 the stock part
    is Wednesday's close while dollars and coins moved an hour ago. Without
    saying so per market, a flat stock line next to a live total reads as a
    frozen feed. `open` uses the same rule as `_asset_market_is_open`, and
    `last_priced_at` is the newest price behind that market's holdings.
    """
    state = current_market_state()
    is_open = {
        "stocks": state == OPEN,
        "gold_fx": state in (OPEN, CLOSED_DAYTIME),
        "crypto": True,
    }
    def group_of(item):
        if item.get("symbol") or item.get("class") == Asset.AssetClass.STOCK:
            return "stocks"
        for group, _label, classes in MARKET_GROUPS[1:]:
            if item.get("class") in classes:
                return group
        return None  # houses and manual assets keep no market hours

    clocks = []
    for group, label, _classes in MARKET_GROUPS:
        members = [item for item in items if not item.get("is_house") and group_of(item) == group]
        if not members:
            continue
        stamps = [item["priced_at"] for item in members if item.get("priced_at")]
        clocks.append({
            "market": group,
            "label": label,
            "open": is_open[group],
            "last_priced_at": max(stamps) if stamps else None,
            "holdings": len(members),
        })
    return clocks


def value_user(user, *, include_hidden: bool = False) -> dict:
    """Aggregate valuation across all of a user's accounts."""
    prices = get_latest_prices()
    accounts, total = [], Decimal("0")
    priced_assets = total_assets = 0
    excluded = []
    total_liabilities = Decimal("0")
    all_liabilities = []
    for account in user.accounts.all():
        valuation = value_account(account, prices, include_hidden=include_hidden)
        total += valuation["total"]
        priced_assets += valuation["priced_assets"]
        total_assets += valuation["total_assets"]
        excluded.extend(
            {"account_id": account.id, **item} for item in valuation["excluded"]
        )
        accounts.append({
            "id": account.id,
            "name": account.name,
            "broker": account.broker,
            "total": valuation["total"],
            "cash_tomans": valuation["cash_tomans"],
            "items": valuation["items"],
            "hidden_items": valuation["hidden_items"],
            "liabilities": valuation.get("liabilities", []),
            "total_liabilities": valuation.get("total_liabilities", 0.0),
        })
        total_liabilities += Decimal(str(valuation.get("total_liabilities", 0.0)))
        all_liabilities.extend([
            {"account_id": account.id, "account_name": account.name, **l}
            for l in valuation.get("liabilities", [])
        ])
    items = [
        {"account_id": acc["id"], "account_name": acc["name"], **item}
        for acc in accounts
        for item in acc["items"]
    ]
    hidden_items = [
        {"account_id": acc["id"], "account_name": acc["name"], **item}
        for acc in accounts
        for item in acc["hidden_items"]
    ]
    quality_status = _quality_rollup(items, excluded, total_assets, priced_assets)
    return {
        "total": total,
        "cash_tomans": sum((a["cash_tomans"] for a in accounts), Decimal("0")),
        "accounts": accounts,
        "items": items,
        "hidden_items": hidden_items,
        "prices": prices,
        "priced_assets": priced_assets,
        "total_assets": total_assets,
        "quality_status": quality_status,
        "markets": market_clocks(items),
        "excluded": excluded,
        "liabilities": all_liabilities,
        "total_liabilities": float(total_liabilities),
    }


SYNTHETIC_HISTORY_MAX_DAYS = 90

# How far back the hidden-holding adjustment is recomputed day by day.
#
# Separate from SYNTHETIC_HISTORY_MAX_DAYS: that bounds a fabricated series we
# would rather not show at all, while this bounds real arithmetic we do want,
# just not without a ceiling. `?days=all` on a long-lived account would otherwise
# size the loop from the oldest snapshot -- and if a hidden asset has trade
# history, every one of those days costs a full per-account ledger replay
# (`holdings_as_of`), so a single chart request could run thousands of them.
# Points older than this reuse the oldest computed adjustment and are flagged
# `approximated`; see `_subtract_hidden_holdings`.
HIDDEN_ADJUSTMENT_MAX_DAYS = 1095


def _traded_outside(accounts, excluded_asset_ids) -> bool:
    """Whether any account traded an asset other than `excluded_asset_ids`.

    Reversals net out: a trade entered and then reversed was never made.
    """
    from .ledger import active_entries

    trades = active_entries(accounts, kinds=[
        LedgerEntry.Kind.BUY,
        LedgerEntry.Kind.SELL,
        LedgerEntry.Kind.RIGHTS_ISSUE,
    ])
    return any(entry.asset_id not in excluded_asset_ids for entry in trades)


def _has_cash_history(account) -> bool:
    """Whether the account ever moved cash, reversals netted out."""
    from .ledger import CASH_KINDS, active_entries

    return bool(active_entries(account, kinds=CASH_KINDS))


def _accounts_have_buy_sell(accounts, asset_ids=None) -> bool:

    qs = LedgerEntry.objects.filter(
        account__in=list(accounts),
        kind__in=[
            LedgerEntry.Kind.BUY,
            LedgerEntry.Kind.SELL,
            LedgerEntry.Kind.RIGHTS_ISSUE,
        ],
    )
    if asset_ids is not None:
        qs = qs.filter(asset_id__in=asset_ids)
    return qs.exists()


def _walked_quantities(accounts, day_ends) -> dict:
    """Non-house quantities on each of `day_ends`, from one ledger read.

    The same backwards walk `holdings_as_of` does -- qty(t) = qty_now - buys
    after t + sells after t -- but run once across the whole window instead of
    re-reading the ledger for every day it is asked about. That per-day re-read
    is why the net-worth replay carried a 90-day ceiling: the cost was two
    queries per account per day, so a year-long window meant thousands of them,
    and the comparison page's 1Y and 6M buttons were capped down to 90 days
    rather than pay it. Walking the days newest-first makes the whole window one
    pass over one query.

    Houses are excluded exactly as they are there: a mark REPLACES the previous
    one, so unwinding it additively would drive the price per square metre to
    zero. The caller resolves them from `house_state_as_of` instead.
    """
    from portfolio.models import Holding, LedgerEntry

    quantities: dict[str, Decimal] = {}
    for holding in Holding.objects.filter(account__in=accounts).select_related("asset"):
        if not holding.asset.is_house:
            key = holding.asset.key
            quantities[key] = quantities.get(key, Decimal("0")) + _q(holding.quantity)

    from .ledger import active_entries

    moves = sorted(
        (
            entry for entry in active_entries(accounts)
            if entry.timestamp > min(day_ends)
            and entry.asset_id is not None
            and not entry.asset.is_house
            and entry.kind in {
                LedgerEntry.Kind.OPENING_POSITION,
                LedgerEntry.Kind.BUY,
                LedgerEntry.Kind.SELL,
                LedgerEntry.Kind.RIGHTS_ISSUE,
            }
        ),
        key=lambda entry: (entry.timestamp, entry.pk),
        reverse=True,
    )

    walked, cursor = {}, 0
    for day_end in sorted(day_ends, reverse=True):
        while cursor < len(moves) and moves[cursor].timestamp > day_end:
            entry = moves[cursor]
            key = entry.asset.key
            adds = entry.kind in {
                LedgerEntry.Kind.OPENING_POSITION, LedgerEntry.Kind.BUY,
                LedgerEntry.Kind.RIGHTS_ISSUE,
            }
            step = _q(entry.quantity)
            quantities[key] = quantities.get(key, Decimal("0")) + (
                -step if adds else step
            )
            cursor += 1
        walked[day_end] = {k: v for k, v in quantities.items() if v > Decimal("0")}
    return walked


def compute_dynamic_net_worth_series(
    user, account=None, days: int = 30, *, only_hidden: bool = False,
    max_days: int | None = None,
) -> list[dict]:
    """Compute an instant on-the-fly historical net worth series for a portfolio.

    Multiplies holdings against historical asset price time-series in
    MarketCandle and GoldCurrencyHistory for past `days` (capped at 90).

    When the account(s) have no BUY/SELL ledger rows (opening/quantity-only),
    current quantities are held constant across the window. Otherwise quantities
    are walked backward via `holdings_as_of`.

    `only_hidden` inverts the visibility filter and values *just* the switched-off
    holdings. That is what `SnapshotListView` subtracts from the recorded totals,
    so the same price resolution -- warehouse closes, the 5-session forward-fill
    bound, house marks in force -- decides both sides of the subtraction. Two
    implementations of "what was this worth that day" would disagree, and the
    difference would land in the user's net worth.

    Each point carries `approximated`: True when some asset that day had no real
    close to read and its live price stood in. On the hidden series that is the
    signal the chart uses to say the adjustment is an estimate.
    """
    from datetime import datetime as dt, time as dtime, timedelta
    import jdatetime
    from django.utils import timezone
    from marketdata.calendars import (
        candle_close_qs,
        market_for_asset,
        session_calendar,
        sessions_between,
    )
    from portfolio.models import Holding, Liability

    days = max(1, min(int(days), max_days or SYNTHETIC_HISTORY_MAX_DAYS))
    now = timezone.now()

    if account is not None:
        holdings = list(account.holdings.select_related("asset").all())
        accounts = [account]
    else:
        holdings = list(Holding.objects.filter(account__user=user).select_related("asset").all())
        accounts = list(user.accounts.all())

    # Switched-off holdings leave the history too, along with their mortgages --
    # the user asked for them to be absent from every figure, not just today's.
    hidden_ids = hidden_asset_ids(accounts)
    holdings = [
        h for h in holdings if (h.asset_id in hidden_ids) == only_hidden
    ]

    # Nothing to value -- unless there is cash, which is net worth on its own:
    # a book whose every holding is switched off still has its cash, and Home
    # still shows it. Cash since withdrawn still has a past, so a ledger with
    # cash movements counts at a zero balance too -- but not one that ever
    # traded: only current holdings are valued, so a position bought and sold
    # off since would be missing from the line and its gain from the return.
    if not holdings and (
        only_hidden or not any(
            acc.cash_balance_tomans or _has_cash_history(acc) for acc in accounts
        ) or _traded_outside(accounts, hidden_ids)
    ):
        return []

    latest_quantities = {h.asset.key: _q(h.quantity) for h in holdings}
    assets = {h.asset.key: h.asset for h in holdings}
    # Scoped to the assets actually being valued. A book with one traded stock and
    # one untouched property should not walk the property's quantity backwards
    # through a per-day ledger replay it can never change.
    constant_holdings = not _accounts_have_buy_sell(
        accounts, asset_ids=[h.asset_id for h in holdings]
    )
    house_keys = {h.asset.key for h in holdings if h.asset.is_house}
    house_histories = {
        acc.pk: load_house_marks(acc) for acc in accounts
    } if house_keys else {}

    stock_symbols = {a.tse_symbol: a.key for a in assets.values() if a.tse_symbol}
    brs_symbols = {a.brs_symbol: a.key for a in assets.values() if a.brs_symbol}

    window_start_jalali = jdatetime.date.fromgregorian(
        date=(now - timedelta(days=days - 1)).date()
    ).strftime("%Y-%m-%d")

    # These two maps answer two different questions, which is why each table is
    # read twice rather than once without a bound.
    #
    #   1. What did this asset close at on each day INSIDE the window? Bounded
    #      at `window_start_jalali`.
    #   2. What was the last real close at or BEFORE the window opened? That is
    #      the priming pass below, and it needs exactly one row per symbol --
    #      the newest one -- however far back it sits.
    #
    # One unbounded query used to serve both, which meant a 30-day chart fetched
    # every adjusted candle and every gold row the held symbols had ever printed
    # and discarded all but the tail. Clipping that query to the window alone
    # would NOT be equivalent: an asset last priced before the window would lose
    # its `last_priced_date` entry, and an entry is what the staleness guard
    # measures. Without one a delisted holding stops being dropped and starts
    # being carried at its live price for the whole series. Hence a bounded bulk
    # read plus a one-row-per-symbol DISTINCT ON priming read.
    # A close the warehouse has already quarantined is not a price. `value_as_of`
    # and `_latest_archive_closes` have always skipped these; this path did not,
    # so a `series_spike` the validator caught still landed on the net-worth
    # chart at full size -- the one screen where a fake 30x day is most visible.
    # No date bound: the primer below deliberately reaches past the window, so a
    # bounded rejection set could miss the verdict on the row it primes from.
    stock_rejections = rejected_pairs(
        stock_symbols, STOCK_SERIES_ENDPOINTS
    ) if stock_symbols else set()
    brs_rejections = rejected_pairs(
        brs_symbols, BRS_SERIES_ENDPOINTS
    ) if brs_symbols else set()

    # The primer goes through `_newest_close_per_symbol` rather than a bare
    # DISTINCT ON, because "newest row" and "newest USABLE row" differ exactly
    # when the top row is quarantined -- and priming from nothing is what sends
    # a holding to its live price for the whole series.
    stock_closes = {}
    if stock_symbols:
        s_rows = candle_close_qs(
            list(stock_symbols), since=window_start_jalali
        ).values("symbol", "date_time", "close_price")
        # `as_of` is the helper's own end-of-day bound, which knows that a bare
        # "1405-05-09" and "1405-05-09 00:00:00" are the same session.
        primed = _newest_close_per_symbol(
            candle_close_qs(list(stock_symbols), as_of=window_start_jalali),
            stock_symbols, stock_rejections, date_field="date_time",
        )
        for symbol, (day, close) in primed.items():
            # Portfolio TSE quotes follow warehouse Rial under the legacy
            # one-tenth-share convention.
            stock_closes.setdefault(day, {})[stock_symbols[symbol]] = _q(close)
        for r in s_rows:
            if (r["symbol"], r["date_time"].split()[0]) in stock_rejections:
                continue
            key = stock_symbols[r["symbol"]]
            stock_closes.setdefault(r["date_time"], {})[key] = _q(r["close_price"])

    gold_closes = {}
    if brs_symbols:
        brs_base = GoldCurrencyHistory.objects.filter(
            symbol__in=list(brs_symbols.keys()), close_price__gt=0
        )
        g_rows = list(brs_base.filter(date__gte=window_start_jalali).values(
            "symbol", "date", "close_price", "unit"
        ))
        primed = _newest_close_per_symbol(
            brs_base.filter(date__lte=window_start_jalali),
            brs_symbols, brs_rejections, date_field="date", include_unit=True,
        )
        cash_rates, tether_rates = toman_rate_tables(
            [row["unit"] for row in g_rows]
            + [unit for _day, _close, unit in primed.values()],
            [row["date"] for row in g_rows]
            + [day for day, _close, _unit in primed.values()],
        )
        foreign_symbols = _foreign_brs_symbols(assets.values())
        for symbol, (day, close, unit) in primed.items():
            price = _brs_close_toman(
                symbol, day, close, unit, foreign_symbols, cash_rates, tether_rates,
            )
            if price > 0:
                gold_closes.setdefault(day, {})[brs_symbols[symbol]] = price
        for r in g_rows:
            if (r["symbol"], r["date"]) in brs_rejections:
                continue
            key = brs_symbols[r["symbol"]]
            price = _brs_close_toman(
                r["symbol"], r["date"], r["close_price"], r["unit"],
                foreign_symbols, cash_rates, tether_rates,
            )
            if price > 0:
                gold_closes.setdefault(r["date"], {})[key] = price

    # Crypto and the other live-only classes have no provider history endpoint,
    # so neither query above can see them and they used to be pinned at today's
    # live price for the whole window -- a flat line, and every point flagged
    # approximated. Their distilled daily bars go into the same map, which is
    # what gives them the same priming, staleness bound and forward-fill as
    # every other asset. Third of the three price-resolution paths that must
    # agree; `value_as_of` reads the same bars via `_daily_bar_as_of`.
    #
    # Bounded a month before the window so the priming pass below still finds a
    # close at-or-before it. Reaching further back is pointless: past
    # MAX_FORWARD_FILL_SESSIONS the staleness guard drops the asset anyway.
    bars_since = jdatetime.date.fromgregorian(
        date=(now - timedelta(days=days + 30)).date()
    ).strftime("%Y-%m-%d")
    keys_by_symbol = {
        (a.tse_symbol or a.brs_symbol): a.key for a in assets.values()
    }
    for symbol, date, price in daily_bar_price(assets.values(), since=bars_since):
        key = keys_by_symbol.get(symbol)
        if key is not None:
            gold_closes.setdefault(date, {}).setdefault(key, price)

    latest_prices = get_latest_prices()
    usd_rate = Decimal(latest_prices.get("usd_cash", 0) or 0)

    # Track last known price for each asset to seamlessly fill non-trading days.
    #
    # Primed from each asset's last real close at or before the window opens, so
    # a window that starts on a Thursday values a stock at the Wednesday close
    # rather than at today's price. Assets with no warehouse history at all
    # (crypto, the Swiss bars -- the provider has no series for them) still fall
    # back to the live price: it is the only number that exists, and the whole
    # series is flagged `is_estimated`.
    last_known_prices = {}
    # Jalali date of the last REAL close used per asset. Once an asset stops
    # printing, last_known_prices must not be trusted past
    # MAX_FORWARD_FILL_SESSIONS *sessions* -- counted on the market's own
    # calendar, never in calendar days, or the Thu/Fri weekend and every public
    # holiday age a price faster than the market does. Same invariant, and now
    # the same helper, that value_as_of enforces. A live-price fallback gets no
    # entry here: there is no real close to measure staleness from.
    last_priced_date = {}
    for closes in (stock_closes, gold_closes):
        for date, by_key in closes.items():
            if date > window_start_jalali:
                continue
            for key, price in by_key.items():
                if date >= last_priced_date.get(key, ""):
                    last_priced_date[key] = date
                    last_known_prices[key] = price
    for key in assets:
        if key not in last_known_prices:
            last_known_prices[key] = _q(latest_prices.get(key, 0))

    # Fetched once for the whole window rather than per asset per day: the loop
    # below asks the same staleness question up to `days` x len(assets) times.
    # It has to reach back to the oldest primed close, not just to the window
    # start -- an asset last priced before the window opened is measured from
    # that older date, and a calendar starting later would undercount the gap
    # and keep forward-filling a price that should already have been dropped.
    today_jalali = jdatetime.date.fromgregorian(date=now.date()).strftime("%Y-%m-%d")
    calendar_start = min([window_start_jalali, *last_priced_date.values()])
    calendars = {
        market: session_calendar(market, start=calendar_start, end=today_jalali)
        for market in {market_for_asset(asset) for asset in assets.values()}
    }

    # The hidden series carries the hidden assets' own debts, so subtracting it
    # removes a mortgaged property whole -- value and mortgage together.
    liabilities = Liability.objects.filter(account__in=accounts)
    liabilities = (
        liabilities.filter(asset_id__in=hidden_ids)
        if only_hidden
        else liabilities.exclude(asset_id__in=hidden_ids)
    )
    liabilities = list(liabilities)
    # A debt secured on an asset rides with that asset, exactly as the hidden-row
    # rule above already has it: a mortgaged property leaves value and mortgage
    # together or not at all. An unsecured loan is attached to nothing, so it is
    # simply always there. Both are needed by the matched pair below -- see
    # `paired_liabilities`.
    asset_key_by_id = {asset.id: key for key, asset in assets.items()}

    def liabilities_on(as_of):
        """The day's debts: total, per secured asset, and unattached.

        Recomputed per day rather than hoisted, because a loan on a repayment
        schedule owes less every month. Held at one figure for the whole window
        the chart drew every installment paid over the last year as portfolio
        appreciation on the day the balance was last edited, and none of it
        anywhere else.
        """
        by_key: dict[str, Decimal] = {}
        unattached = Decimal("0")
        total = Decimal("0")
        for l in liabilities:
            owed = l.outstanding_tomans(as_of)
            total += owed
            key = asset_key_by_id.get(l.asset_id) if l.asset_id else None
            if key is None:
                unattached += owed
            else:
                by_key[key] = by_key.get(key, Decimal("0")) + owed
        return total, by_key, unattached

    # Marks are "in force that calendar day", not at today's clock on that date.
    # A purchase at 17:40 was missing from the 16:30 reading of Aug 9.
    day_ends = [
        timezone.make_aware(
            dt.combine((now - timedelta(days=i)).date(), dtime.max),
            timezone.get_current_timezone(),
        )
        for i in range(days - 1, -1, -1)
    ]
    # Cash is part of net worth -- `value_account` and the nightly snapshot both
    # count it -- so the rebuilt days count it too. Left out, the chart's last
    # point sat below the total directly above it by exactly the cash balance.
    # The switched-off series is subtracted from the recorded totals and holds
    # no cash, so it gets none.
    cash_by_day = [Decimal("0")] * len(day_ends)
    fees_by_day = [Decimal("0")] * len(day_ends)
    inflows_by_day = [Decimal("0")] * len(day_ends)
    if not only_hidden:
        for acc in accounts:
            found: dict = {}
            for n, amount in enumerate(cash_on_days(user, acc, day_ends, totals=found)):
                cash_by_day[n] += amount
                fees_by_day[n] += found["fees"][n]
                inflows_by_day[n] += found["inflows"][n]
    # A cash-only book is drawn only while it held cash inside the window; a
    # line of zeros is no history, and a benchmark against it would compare
    # a portfolio that held nothing.
    if not holdings and not any(cash_by_day):
        return []
    prev_cash: Decimal | None = None
    prev_fees: Decimal | None = None
    prev_inflows: Decimal | None = None
    walked_quantities = (
        {} if constant_holdings else _walked_quantities(accounts, day_ends)
    )

    series = []
    # Yesterday's quantities, carried so each day can also be valued as if the
    # book had not changed. See `total_ex_flows` below.
    prev_day_holdings: dict | None = None
    prev_day_prices: dict = {}
    prev_house_values: dict = {}
    for i in range(days - 1, -1, -1):
        target_date = now - timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        jalali_str = jdatetime.date.fromgregorian(date=target_date.date()).strftime("%Y-%m-%d")

        total = Decimal("0")
        day_prices: dict = {}
        house_values: dict = {}
        total_ex_flows_at_prior_prices = Decimal("0")
        # Which assets made it onto BOTH sides of the day's pair. Their debts go
        # on both sides too; a debt whose asset sat the day out sits it out with
        # the asset it is secured on.
        paired_keys: set[str] = set()
        # Yesterday's WHOLE book at yesterday's prices, pair or no pair. The
        # denominator for spreading an unsecured loan, which is charged against
        # all of it rather than against the part that happened to price twice.
        held_base = Decimal("0")
        # The same day priced with YESTERDAY's quantities. `total` moves for two
        # unrelated reasons -- prices moved, or the book changed -- and only the
        # first is performance. Recording a position you already owned is a
        # bookkeeping entry, yet it lands in `total` as if the money appeared:
        # the family account's openings on 2026-08-09 doubled the series in one
        # day and the benchmark comparison read that as a +108% gain. Chaining
        # `total_ex_flows[t] / total[t-1]` neutralises every quantity change --
        # openings, buys and sells alike -- which is what time-weighted return
        # means and what "both lines start at 100" already claims to show.
        total_ex_flows = Decimal("0")
        # True once some asset this day had no real close anywhere and its live
        # price had to stand in. A property is never approximate: its worth on a
        # date is the mark that was in force, not a market print.
        approximated = False

        day_end = day_ends[days - 1 - i]
        house_areas = {}
        if constant_holdings:
            # Today's house qty is the current mark, not history. Painting it
            # onto every past day is the cliff-on-add-day bug.
            day_holdings = {
                k: v for k, v in latest_quantities.items() if k not in house_keys
            }
            for acc in accounts:
                qty_map, area_map = house_state_as_of(
                    house_histories.get(acc.pk, []), day_end
                )
                for k, v in qty_map.items():
                    day_holdings[k] = day_holdings.get(k, Decimal("0")) + v
                house_areas.update(area_map)
        else:
            # Precomputed for the whole window (see `_walked_quantities`), and
            # houses resolved from the preloaded marks rather than from a second
            # per-day query inside `holdings_as_of` -- this branch was already
            # calling `house_state_as_of` for the areas, so the marks were being
            # read twice a day to produce the same answer.
            day_holdings = dict(walked_quantities[day_end])
            for acc in accounts:
                qty_map, area_map = house_state_as_of(
                    house_histories.get(acc.pk, []), day_end
                )
                for k, v in qty_map.items():
                    day_holdings[k] = day_holdings.get(k, Decimal("0")) + v
                house_areas.update(area_map)

        for key, asset in assets.items():
            qty = day_holdings.get(key, Decimal("0"))
            prev_qty = (
                qty if prev_day_holdings is None
                else prev_day_holdings.get(key, Decimal("0"))
            )
            if asset.is_house:
                holding = next((h for h in holdings if h.asset_id == asset.id), None)
                area = house_areas.get(key)
                if area is None:
                    area = holding.area_sqm if holding else HOUSE_AREA_SQM
                house_value = _house_value(qty, area_sqm=area)
                total += house_value
                # A property's "quantity" IS its price per square meter, so a new
                # mark is a revaluation -- performance, and it must stay in the
                # return. Only the day the property first appears is a flow. Both
                # sides of the ratio carry it, so a mark that drops to zero reads
                # as the loss it is instead of the position vanishing.
                prev_house_value = prev_house_values.get(key)
                if prev_qty > 0 and prev_house_value is not None:
                    total_ex_flows += house_value
                    total_ex_flows_at_prior_prices += prev_house_value
                    paired_keys.add(key)
                    held_base += prev_house_value
                house_values[key] = house_value
            else:
                # Yesterday's value of this holding, counted whether or not today
                # prices out. The pair may drop it; the unsecured loan is still
                # charged against it, so it belongs in that denominator.
                prev_p = prev_day_prices.get(key)
                if prev_qty > 0 and prev_p is not None:
                    held_base += holding_value_to_toman(asset, prev_qty * prev_p)
                p = stock_closes.get(jalali_str, {}).get(key)
                if p is None:
                    p = gold_closes.get(jalali_str, {}).get(key)
                if p is not None:
                    last_known_prices[key] = p
                    last_priced_date[key] = jalali_str
                else:
                    last_real_date = last_priced_date.get(key)
                    if last_real_date is not None:
                        stale = sessions_between(
                            last_real_date,
                            jalali_str,
                            calendar=calendars[market_for_asset(asset)],
                        )
                        if stale > MAX_FORWARD_FILL_SESSIONS:
                            continue  # gap exceeded: don't invent a price
                    else:
                        # No real close anywhere means an asset the provider has
                        # no history for; the live price is the only figure
                        # available, and only for a position we actually hold.
                        approximated = approximated or qty > 0
                    p = last_known_prices[key]
                total += holding_value_to_toman(asset, qty * p)
                # Both sides of the day's ratio, over the SAME asset at the SAME
                # quantity -- only the price differs. An asset priced today but
                # not yesterday (or the reverse) enters neither, so a holding
                # dropped by the forward-fill guard cannot book a one-day loss
                # equal to its whole weight and, the index being a running
                # product, never recover from it.
                if prev_qty > 0 and prev_p is not None:
                    total_ex_flows += holding_value_to_toman(asset, prev_qty * p)
                    total_ex_flows_at_prior_prices += holding_value_to_toman(
                        asset, prev_qty * prev_p
                    )
                    paired_keys.add(key)
                day_prices[key] = p

        # The day's debts, not the window's: a loan on a schedule owes less each
        # month, and that has to reach the net-worth line. It cannot reach the
        # RATIO -- repaying a loan is a cash flow, not performance -- and it
        # does not, because the pair below charges one and the same day's figure
        # to both of its sides.
        # Cash earns nothing, so in the pair it is yesterday's balance on both
        # sides: it dilutes the day's move exactly as much as it should, and a
        # deposit -- a flow, like an opening -- never reads as a gain. A
        # dividend is treated the same way, as a flow: adjusted closes already
        # carry it. A fee is not a flow, it is a cost, so the day's fees come
        # off today's side -- otherwise the line drops by the fee while the
        # return index, and every benchmark comparison built on it, does not.
        # Charged as a share of the money it was paid out of: yesterday's book
        # plus the day's deposits. A commission on a trade funded the same day
        # would otherwise land whole on a small prior book -- 0.3% of the trade
        # read as -30% of yesterday -- and the index never gives that back.
        cash_today = cash_by_day[days - 1 - i]
        cash_before = cash_today if prev_cash is None else prev_cash
        fees_today = fees_by_day[days - 1 - i]
        inflows_today = inflows_by_day[days - 1 - i]
        fees_paid = Decimal("0") if prev_fees is None else fees_today - prev_fees
        deposited = Decimal("0") if prev_inflows is None else inflows_today - prev_inflows
        total += cash_today
        total_ex_flows_at_prior_prices += cash_before
        held_base += cash_before
        base = total_ex_flows_at_prior_prices
        fee_charge = (
            min(base, fees_paid * base / (base + deposited))
            if fees_paid > 0 and base > 0 else Decimal("0")
        )
        total_ex_flows += cash_before - fee_charge
        prev_cash = cash_today
        prev_fees = fees_today
        prev_inflows = inflows_today

        total_liabilities, liability_by_key, unattached_liabilities = liabilities_on(
            target_date
        )
        total -= total_liabilities
        # The pair has to net out debt for the same reason `total` does: the
        # chart is the return on what the family OWNS. Assets 100 against a
        # mortgage of 40 is 60 of net worth, and a 10% rise in the assets is a
        # 16.7% gain to them -- reporting 10% understates every leveraged day.
        # Both sides carry the SAME day's figure, so a repayment cannot show up
        # as a flow in the ratio however much the balance moves day to day.
        #
        # A secured debt rides with its own asset, so it is in the pair exactly
        # when that asset is. An UNSECURED loan is against the whole book, and
        # the pair is only ever part of it -- an asset bought today, or dropped
        # by the forward-fill guard, is in neither side. Charging the whole loan
        # to that smaller base levers the day up: 1,500 + 500 against a loan of
        # 800 reads +1.25% on a 1% move, but +2.14% on a day the 500 has no
        # price, and the index is a running product that never gives it back.
        # Pro-rated to the share of yesterday's book that the pair represents.
        # This does not claim to be the return on a book we cannot fully price;
        # it keeps the leverage proportionate to the part we can.
        secured = sum(
            (liability_by_key[key] for key in paired_keys if key in liability_by_key),
            Decimal("0"),
        )
        unsecured = unattached_liabilities
        if unsecured > 0 and held_base > 0:
            unsecured = unsecured * (total_ex_flows_at_prior_prices / held_base)
        elif unsecured > 0:
            unsecured = Decimal("0")
        paired_liabilities = secured + unsecured
        total_ex_flows -= paired_liabilities
        total_ex_flows_at_prior_prices -= paired_liabilities
        val_usd = str(round(total / usd_rate, 2)) if usd_rate > 0 else None
        series.append({
            "timestamp": target_date.isoformat(),
            "date": date_str,
            "total": str(round(total, 4)),
            # The day's price move on an unchanged book, as a matched pair: the
            # ratio between them is the return, and it needs no reference to any
            # other day's total.
            "total_ex_flows": str(round(total_ex_flows, 4)),
            "total_ex_flows_base": str(round(total_ex_flows_at_prior_prices, 4)),
            "total_usd": val_usd,
            "is_estimated": True,
            "approximated": approximated,
        })
        prev_day_holdings = day_holdings
        prev_day_prices = day_prices
        prev_house_values = house_values

    return series


def resolve_asset_point_in_time_price(
    asset, as_of_jalali: str, *, max_sessions: int = MAX_FORWARD_FILL_SESSIONS
) -> tuple[Decimal | None, int, str | None]:
    """Single point-in-time price resolver for an asset.

    Owns the canonical precedence:
    1. Adjusted stock candles (TSETMC)
    2. Gold / FX daily history (BRS)
    3. Distilled daily bars (crypto, ETF NAV, commodities)

    Enforces:
    - Rejection filtering via RejectedRecord
    - 5-session forward-fill bound via calendars.sessions_between()
    - Unit safety (read-time only: Rial for TSE, Toman for BRS/bars)

    Returns (price, stale_sessions, source) or (None, stale_sessions, rejection_reason).
    """
    from marketdata.calendars import (
        candle_close_qs,
        sessions_between,
    )
    from portfolio.services.returns import USD_QUOTED_KEYS

    price = Decimal("0")
    stale_sessions = 0
    source = None

    if asset.tse_symbol:
        rejections = [
            day
            for _sym, day in rejected_pairs([asset.tse_symbol], STOCK_SERIES_ENDPOINTS)
        ]
        rejections_set = set(rejections) | {f"{d} 00:00:00" for d in rejections}
        candles = candle_close_qs(asset.tse_symbol, as_of=as_of_jalali).exclude(
            date_time__in=rejections_set
        )
        candle = candles.order_by("-date_time").first()
        if candle:
            price = _q(candle.close_price)
            source = "tse_candle_adjusted"
            stale_sessions = sessions_between(
                candle.date_time[:10], as_of_jalali, market="tse"
            )
    elif asset.brs_symbol:
        rejections = [
            day
            for _sym, day in rejected_pairs([asset.brs_symbol], BRS_SERIES_ENDPOINTS)
        ]
        hist = (
            GoldCurrencyHistory.objects.filter(
                symbol=asset.brs_symbol, date__lte=as_of_jalali, close_price__gt=0
            )
            .exclude(date__in=rejections)
            .order_by("-date")
            .first()
        )
        if hist:
            raw_price = Decimal(str(hist.close_price))
            if hist.unit or asset.key not in USD_QUOTED_KEYS:
                cash_rates, tether_rates = toman_rate_tables([hist.unit], [hist.date])
                price = to_toman(
                    hist.symbol, raw_price, hist.unit,
                    **toman_rate_kwargs(
                        hist.unit, hist.date, cash_rates, tether_rates,
                    ),
                )
            source = "gold_currency_history"
            stale_sessions = sessions_between(
                hist.date, as_of_jalali, market="gold_currency"
            )

    if price <= 0:
        price, stale_sessions = _daily_bar_as_of(asset, as_of_jalali)
        if price > 0:
            source = "market_daily_bar"

    if price <= 0:
        return None, 0, "missing_price"

    if stale_sessions > max_sessions:
        return None, stale_sessions, "price_gap_exceeded"

    return price, stale_sessions, source


def conversion_rate_as_of(basis: str, as_of) -> Decimal | None:
    """Use the requested currency's own accepted rate within five calendar days."""
    from .returns import to_jalali_str
    import jdatetime

    symbol = {
        "usd_denominated": "USD",
        "usdt_denominated": "USDT_IRT",
    }.get(basis)
    if symbol is None:
        return None
    jalali = to_jalali_str(as_of)
    rejected = rejected_pairs([symbol], BRS_SERIES_ENDPOINTS)
    row = (
        GoldCurrencyHistory.objects.filter(
            symbol=symbol, date__lte=jalali, close_price__gt=0
        )
        .exclude(date__in=[day for sym, day in rejected if sym == symbol])
        .order_by("-date")
        .first()
    )
    if row is None:
        return None
    try:
        year, month, day = (int(part) for part in row.date.split("-"))
        observed = jdatetime.date(year, month, day).togregorian()
    except (TypeError, ValueError):
        return None
    requested = as_of.date() if hasattr(as_of, "date") else as_of
    if (requested - observed).days > 5:
        return None
    return Decimal(str(row.close_price))


def value_as_of(user, account=None, as_of=None, basis="nominal", *, include_hidden=False) -> dict:
    """Compute valuation of portfolio assets as of a specific date and basis.

    `include_hidden` counts switched-off holdings too, the way a stored
    `Snapshot` does (the snapshot reader subtracts them on the way out).
    """
    from django.utils import timezone
    from portfolio.services.deflator import cpi_for_date, normalize_basis
    from portfolio.services.returns import normalize_as_of, to_jalali_str
    from portfolio.models import Asset

    as_of_dt = normalize_as_of(as_of)
    if as_of_dt is None:
        as_of_dt = timezone.now()

    jalali_str = to_jalali_str(as_of_dt)

    accounts = [account] if account else user.accounts.all()
    items = []
    total = Decimal("0")

    basis = normalize_basis(basis)

    usd_rate = Decimal("1")
    conversion_source = None
    if basis in ("usd_denominated", "usdt_denominated"):
        rate = conversion_rate_as_of(basis, as_of_dt)
        if rate is None:
            return {
                "total": 0.0,
                "items": [],
                "as_of": as_of_dt.isoformat(),
                "basis": basis,
                "quality_status": "unavailable",
                "excluded": [{"reason": "missing_conversion_rate"}],
            }
        usd_rate = rate
        conversion_source = "USDT" if basis == "usdt_denominated" else "USD"
    cpi = Decimal(str(cpi_for_date(as_of_dt))) if basis == "real_toman" else None

    excluded = []

    # Holdings are resolved per account before the price loop so the assets they
    # name can be fetched in one query. The lookup used to sit inside the inner
    # loop, costing one query per holding per account -- and `value_as_of` is
    # called once per TWR cash-flow boundary, so a performance request paid that
    # repeatedly.
    per_account = [(acc, holdings_as_of(user, acc, as_of_dt)) for acc in accounts]
    asset_by_key = {
        a.key: a
        for a in Asset.objects.filter(
            key__in={key for _, holdings in per_account for key in holdings}
        )
    }

    # Resolve close price for each asset
    for acc, acc_holdings in per_account:
        # Per account, not per user: the same asset may be counted in one
        # portfolio and switched off in another.
        acc_hidden = set() if include_hidden else hidden_keys(user, account=acc)
        for key, qty in acc_holdings.items():
            if qty <= 0 or key in acc_hidden:
                continue
            asset = asset_by_key.get(key)
            if not asset:
                continue

            # Find historical unit price
            price = Decimal("0")
            if asset.is_house:
                holding = Holding.objects.filter(account=acc, asset=asset).first()
                # The area belongs to the mark in force on this date, not to the
                # holding's current value: `qty` above is already the historical
                # price-per-sqm, so pairing it with today's area would mix two
                # different points in time.
                from portfolio.services.timeline import house_area_as_of

                area = house_area_as_of(acc, as_of_dt).get(key)
                if area is None:
                    if holding is None:
                        excluded.append(
                            {"asset_key": key, "reason": "missing_house_terms"}
                        )
                        continue
                    area = holding.area_sqm
                val = _house_value(qty, area_sqm=area)
                price = val / qty if qty else Decimal("0")
            else:
                price, stale_sessions, reason = resolve_asset_point_in_time_price(
                    asset, jalali_str
                )
                if price is None:
                    if reason == "price_gap_exceeded":
                        excluded.append({
                            "asset_key": key,
                            "reason": "price_gap_exceeded",
                            "stale_sessions": stale_sessions,
                            "max_forward_fill_sessions": MAX_FORWARD_FILL_SESSIONS,
                        })
                    else:
                        excluded.append({"asset_key": key, "reason": reason or "missing_price"})
                    continue

            # Apply basis
            if not asset.is_house:
                val = holding_value_to_toman(asset, qty * price)
            if basis in ("usd_denominated", "usdt_denominated") and usd_rate > 0:
                val = val / usd_rate
                toman_price = price / Decimal("10") if asset.tse_symbol else price
                price = toman_price / usd_rate
            elif basis == "real_toman":
                val = val / cpi * Decimal("100")
                price = price / cpi * Decimal("100")

            total += val
            items.append({
                "asset": asset.name,
                "key": asset.key,
                "class": asset.asset_class,
                "quantity": float(qty),
                "unit_price": float(price),
                "value": float(val),
            })

        cash = cash_as_of(user, acc, as_of_dt)
        if basis in ("usd_denominated", "usdt_denominated"):
            cash /= usd_rate
        elif basis == "real_toman":
            cash = cash / cpi * Decimal("100")
        total += cash

    liabilities = Liability.objects.filter(account__in=accounts)
    if not include_hidden:
        liabilities = liabilities.exclude(asset_id__in=hidden_asset_ids(list(accounts)))
    # As of the date being valued, not as of today: this feeds the TWR cash-flow
    # boundaries, and charging a two-year-old boundary with today's smaller
    # balance books the whole repayment as investment performance.
    total_liabilities = sum(
        (l.outstanding_tomans(as_of_dt) for l in liabilities), Decimal("0")
    )

    scaled_liabilities = total_liabilities
    if basis in ("usd_denominated", "usdt_denominated") and usd_rate > 0:
        scaled_liabilities /= usd_rate
    elif basis == "real_toman":
        scaled_liabilities = scaled_liabilities / cpi * Decimal("100")
        
    total -= scaled_liabilities

    return {
        "total": float(total),
        "items": items,
        "as_of": as_of_dt.isoformat(),
        "basis": basis,
        "quality_status": "partial" if excluded else "complete",
        "excluded": excluded,
        "total_liabilities": float(scaled_liabilities),
        "conversion_source": conversion_source,
        "liabilities": [_liability_row(l, as_of_dt) for l in liabilities],
    }
