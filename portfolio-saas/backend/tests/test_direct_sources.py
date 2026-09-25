"""The migration off BrsApi: TGJU, Wallex, Nobitex.

Every fixture under `tests/fixtures/sources/` is a REAL payload captured from
the production VPS on 2026-08-31, trimmed to the rows under test. Invented
fixtures would have passed against invented parsers; two of the bugs these
tests pin (TGJU's dead slug, Wallex's column-major layout) are only visible in
data the origin actually sent.

The suite is organised around what can actually go wrong here, which is not
"does the parser run". It is:

  * a unit read wrong -- Nobitex quotes Rial, Wallex quotes Toman for the same
    coin, so the same number means two things 10x apart;
  * a dead feed believed -- TGJU serves a retired tether slug with a 2020
    timestamp and no flag saying so;
  * a column-major payload transposed wrong -- misaligning candles silently
    rather than raising;
  * the migration itself changing a price -- the direct row must agree with the
    BrsApi row it replaces, or the swap is not a swap.
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from marketdata.sources import nobitex, tgju, wallex
from marketdata.sources.http import SourceResponseError

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
TEHRAN = ZoneInfo("Asia/Tehran")


def test_paid_usdt_pro_quote_uses_aio_key(monkeypatch):
    from portfolio.live import fetcher

    seen = []
    monkeypatch.setattr(fetcher, "fetch_brsapi", lambda _url, key: {"board_key": key})
    monkeypatch.setattr(
        fetcher, "_usdt_irt_quote", lambda key: seen.append(key) or None
    )
    fetcher._brs_job("https://paid.example", "market-key", "aio-key")
    assert seen == ["aio-key"]

#: The capture instant. Ages are measured against this so the staleness tests
#: assert on the fixture's own clock and do not rot as the calendar moves.
CAPTURED_AT = datetime(2026, 8, 31, 19, 35, 0, tzinfo=TEHRAN)


def load(name):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def tgju_current():
    return load("tgju_live.json")["current"]


@pytest.fixture
def wallex_symbols():
    return load("wallex_markets.json")["result"]["symbols"]


@pytest.fixture
def nobitex_stats():
    return load("nobitex_stats.json")["stats"]


# --------------------------------------------------------------------- TGJU

class TestTgjuUnits:
    """TGJU declares no unit, so the mapping is the safety-critical part."""

    def test_every_mapped_slug_declares_a_unit(self):
        """A slug in the symbol map with no unit would be priced by guesswork.

        `quote()` refuses an unmapped slug rather than inferring from magnitude,
        so a gap here is a silently-dropped asset. Catching it as a test failure
        is considerably cheaper than as a missing price.
        """
        missing = [s for s in tgju.BRS_TO_SLUG.values() if s not in tgju.SLUG_UNITS]
        assert not missing, f"slugs mapped but unit-less: {missing}"

    def test_irr_slugs_are_rial_not_toman(self, tgju_current):
        """The whole migration turns on this being Rial.

        If these were read as Toman, every gold and FX holding would be valued
        at ten times its worth -- and it would look plausible, because the
        numbers are large either way. Pinned against the production comparison:
        BrsApi served emami_coin at 223,510,000 Toman while TGJU serves `sekee`
        at 2,235,100,000. That factor of ten is the assertion.
        """
        price, unit = tgju.quote(tgju_current, "sekee", now=CAPTURED_AT)
        assert unit == "ریال"
        assert price == Decimal("2235100000")

        from marketdata.currency import to_toman
        assert to_toman("IR_COIN_EMAMI", price, unit) == Decimal("223510000")

    def test_dollar_quoted_slugs_are_not_rial(self, tgju_current):
        """`ons` and `crypto-bitcoin` are global USD prices, not local ones."""
        for slug in ("ons", "crypto-bitcoin"):
            _, unit = tgju.quote(tgju_current, slug, now=CAPTURED_AT)
            assert unit == "دلار", f"{slug} must be dollar-quoted"


class TestTgjuTedpixHistory:
    def test_parser_preserves_observed_trading_days(self):
        rows = load("tgju_tedpix_history.json")["data"]
        parsed = tgju.tedpix_history_rows(rows)
        assert [row["jalali"] for row in parsed] == ["1405-06-09", "1405-06-07"]
        assert parsed[0]["close"] == Decimal("6547963.76")

    def test_live_index_adapter_rejects_stale_observations(self):
        current = {
            "bourse": {
                "p": "6,547,963.76",
                "d": "31,779.83",
                "ts": "2026-09-01 12:39:04",
            }
        }
        payload = tgju.live_tedpix_payload(
            current, now=datetime(2026, 9, 1, 12, 40, tzinfo=TEHRAN)
        )
        assert payload["date"] == "1405-06-10"
        assert payload["index"] == Decimal("6547963.76")
        assert payload["time"] == "12:39:04"

        assert tgju.live_tedpix_payload(
            current, now=datetime(2026, 9, 5, 12, 40, tzinfo=TEHRAN)
        ) is None

    def test_gold_history_adapter_preserves_provider_ohlc_and_symbol(self):
        rows = [
            ["2,000", "1,900", "2,100", "2,050", "", "", "2026-08-31", "1405/06/09"],
        ]
        payload = tgju.gold_history_payload("USD", rows)
        assert payload["symbol"] == "USD"
        assert payload["unit"] == "ریال"
        assert payload["history_daily"][0]["date"] == "1405-06-09"
        assert payload["history_daily"][0]["low"] == Decimal("1900")
        assert payload["history_daily"][0]["high"] == Decimal("2100")

    def test_gold_history_adapter_leaves_unmapped_symbols_for_fallback(self):
        assert tgju.gold_history_payload("IR_GOLD_MELTED", []) is None


def test_complete_direct_board_suppresses_paid_brs_fallback(monkeypatch, settings):
    from marketdata.market_state import OPEN
    from portfolio.live import fetcher

    settings.TGJU_ENABLED = True
    settings.WALLEX_ENABLED = True
    settings.NOBITEX_ENABLED = False
    settings.MARKETDATA_IGNORE_MARKET_HOURS = True
    # Both overrides off: this test is about the pure fallback path, where a
    # complete free board means the paid one is not bought at all. Blending and
    # the periodic verification each have their own test below.
    settings.MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS = 0
    settings.MARKETDATA_BLEND_PAID_BOARD = False

    direct = {
        "direct": {"rows": [{"symbol": "USD", "price": "1", "unit": "ریال"}]},
        "direct_complete": True,
    }
    monkeypatch.setattr(fetcher, "_direct_job", lambda: dict(direct))
    brs_calls = []
    monkeypatch.setattr(
        fetcher,
        "_brs_job",
        lambda *_args: brs_calls.append(True) or {"brsapi": {}},
    )
    monkeypatch.setattr(
        "marketdata.market_state.claim_provider_state_probe", lambda _now: False
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: OPEN)

    raw = fetcher.fetch_all_markets(
        {
            "brs_url": "https://paid.example",
            "brs_api_key": "paid-key",
            "tsetmc_url": "",
            "tsetmc_api_key": "",
        }
    )

    assert not brs_calls
    assert raw["direct"]["rows"][0]["symbol"] == "USD"
    assert "direct_complete" not in raw


def test_blended_mode_buys_the_paid_board_every_cycle(monkeypatch, settings):
    """Blending ignores `direct_complete` and the verification throttle entirely.

    The reason for suppressing the paid board was quota, and that reason is gone:
    the Market/* meter is 1,500 per DAY and was measured running at ~42. What is
    left points the other way -- BrsApi declares a unit string on every row, while
    TGJU needs slug mapping and can serve a retired slug that still answers.
    """
    from marketdata.market_state import OPEN
    from portfolio.live import fetcher

    settings.TGJU_ENABLED = True
    settings.WALLEX_ENABLED = True
    settings.NOBITEX_ENABLED = False
    settings.MARKETDATA_IGNORE_MARKET_HOURS = True
    settings.MARKETDATA_BLEND_PAID_BOARD = True
    # Deliberately hostile to a paid fetch: board complete, throttle disabled.
    settings.MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS = 0
    monkeypatch.setattr(fetcher, "_BRS_VERIFY_LOCAL", {"at": None})
    monkeypatch.setattr(
        fetcher,
        "_direct_job",
        lambda: {
            "direct": {"rows": [{"symbol": "USD", "price": "1", "unit": "ریال"}]},
            "direct_complete": True,
        },
    )
    brs_calls = []
    monkeypatch.setattr(
        fetcher, "_brs_job", lambda *_a: brs_calls.append(True) or {"brsapi": {}}
    )
    monkeypatch.setattr(
        "marketdata.market_state.claim_provider_state_probe", lambda _now: False
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: OPEN)

    api = {
        "brs_url": "https://paid.example",
        "brs_api_key": "paid-key",
        "tsetmc_url": "",
        "tsetmc_api_key": "",
    }
    fetcher.fetch_all_markets(api)
    fetcher.fetch_all_markets(api)
    assert len(brs_calls) == 2, "blended mode buys the board on every cycle"


def test_paid_board_is_still_bought_periodically_to_catch_a_stale_free_feed(
    monkeypatch, settings
):
    """A complete free board must not mean the paid one is never bought again.

    TGJU is known to keep answering on slugs that have stopped updating, and a
    stale-but-answering slug satisfies `direct_complete` exactly as well as a
    live one. Suppressing the paid call forever therefore leaves the one board we
    have no second opinion for able to freeze without any signal.

    Also pins the throttle: without Redis this falls back to a per-process clock,
    so the second cycle inside the interval must NOT buy again. Returning True
    unconditionally there would spend ~900 requests/day off a 1,500/day meter for
    as long as Redis was down.
    """
    from marketdata.market_state import OPEN
    from portfolio.live import fetcher

    settings.TGJU_ENABLED = True
    settings.WALLEX_ENABLED = True
    settings.NOBITEX_ENABLED = False
    settings.MARKETDATA_IGNORE_MARKET_HOURS = True
    settings.MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS = 900
    settings.MARKETDATA_BLEND_PAID_BOARD = False

    monkeypatch.setattr(fetcher, "_BRS_VERIFY_LOCAL", {"at": None})
    monkeypatch.setattr("portfolio.live.redis_client.get_redis", lambda: None)
    monkeypatch.setattr(
        fetcher,
        "_direct_job",
        lambda: {
            "direct": {"rows": [{"symbol": "USD", "price": "1", "unit": "ریال"}]},
            "direct_complete": True,
        },
    )
    brs_calls = []
    monkeypatch.setattr(
        fetcher, "_brs_job", lambda *_a: brs_calls.append(True) or {"brsapi": {}}
    )
    monkeypatch.setattr(
        "marketdata.market_state.claim_provider_state_probe", lambda _now: False
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: OPEN)

    api = {
        "brs_url": "https://paid.example",
        "brs_api_key": "paid-key",
        "tsetmc_url": "",
        "tsetmc_api_key": "",
    }
    fetcher.fetch_all_markets(api)
    assert len(brs_calls) == 1, "first cycle should buy the verification board"
    fetcher.fetch_all_markets(api)
    assert len(brs_calls) == 1, "second cycle inside the interval must be throttled"


def test_verification_fallback_fires_on_a_freshly_booted_worker(monkeypatch, settings):
    """The "never claimed" sentinel cannot be 0.0.

    `time.monotonic()` counts from boot, so on a host up for less than the
    interval, `now - 0.0 < interval` is true and the degraded path suppresses
    the verification board for the first fifteen minutes of uptime -- exactly
    when a worker is most likely to have just restarted because something broke.
    Caught for real: this reddened the suite only because the Docker VM had been
    running for under 900 seconds.
    """
    from portfolio.live import fetcher

    settings.MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS = 900
    monkeypatch.setattr(fetcher, "_BRS_VERIFY_LOCAL", {"at": None})
    monkeypatch.setattr("portfolio.live.redis_client.get_redis", lambda: None)
    # A machine that booted twelve seconds ago.
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: 12.0)

    assert fetcher._brs_verification_due() is True
    assert fetcher._brs_verification_due() is False


def test_crypto_snapshot_capture_uses_wallex_without_brs(monkeypatch, settings):
    from marketdata import tasks
    from marketdata.sources import wallex

    settings.WALLEX_ENABLED = True
    monkeypatch.setattr(
        wallex,
        "live_rows",
        lambda: [
            {"base": "BTC", "quote": "TMN", "price": Decimal("200"), "unit": "تومان"},
            {"base": "BTC", "quote": "USDT", "price": Decimal("1"), "unit": "تتر"},
        ],
    )
    paid_calls = []
    monkeypatch.setattr(
        "marketdata.fetchers.fetch_derivatives",
        lambda *_args: paid_calls.append(True) or [],
    )

    rows = tasks._market_snapshot_payload("crypto", "crypto", "paid-key")

    assert rows == [{"symbol": "BTC", "price": Decimal("200"), "unit": "تومان"}]
    assert not paid_calls



class TestTgjuStaleness:
    """The dead-slug trap, which is the one that would have shipped."""

    def test_retired_slug_is_refused(self, tgju_current):
        """`usdt-irr` answers with 273,000 stamped 2020-11-11.

        It is a plausible-looking tether price and it is six years old. Nothing
        in the payload marks it retired, so the age gate is the only defence --
        without it, every USDT holding would be valued at roughly a sixth.
        """
        row = tgju_current["usdt-irr"]
        assert row["ts"].startswith("2020"), "fixture must retain the stale row"

        price, unit = tgju.quote(tgju_current, "usdt-irr", now=CAPTURED_AT)
        assert price is None and unit is None

    def test_live_tether_slug_is_accepted(self, tgju_current):
        """...while the slug that IS live passes and lands on the right number."""
        price, unit = tgju.quote(tgju_current, "crypto-tether-irr", now=CAPTURED_AT)
        assert unit == "ریال"
        from marketdata.currency import to_toman
        assert to_toman("USDT_IRT", price, unit) == Decimal("209643")

    def test_a_quote_without_a_timestamp_is_refused(self):
        """No `ts` means no way to know if it is live. Refuse, do not assume."""
        price, _ = tgju.quote({"sekee": {"p": "2,235,100,000"}}, "sekee",
                              now=CAPTURED_AT)
        assert price is None

    def test_the_gate_fires_at_the_documented_boundary(self, tgju_current):
        """Pin MAX_QUOTE_AGE so widening it is a deliberate, visible edit."""
        fresh = CAPTURED_AT + tgju.MAX_QUOTE_AGE - timedelta(hours=1)
        stale = CAPTURED_AT + tgju.MAX_QUOTE_AGE + timedelta(hours=1)
        assert tgju.quote(tgju_current, "sekee", now=fresh)[0] is not None
        assert tgju.quote(tgju_current, "sekee", now=stale)[0] is None


class TestTgjuParsing:
    def test_display_formatting_is_stripped(self, tgju_current):
        """Prices arrive as '2,092,950' and '4,425.19' -- display strings."""
        price, _ = tgju.quote(tgju_current, "price_dollar_rl", now=CAPTURED_AT)
        assert price == Decimal("2092950")
        ounce, _ = tgju.quote(tgju_current, "ons", now=CAPTURED_AT)
        assert ounce == Decimal("4425.19")

    def test_live_rows_emit_brsapi_symbols(self, tgju_current):
        """Rows must carry BRS symbols, or the extractor cannot find them."""
        rows = tgju.live_rows(tgju_current, now=CAPTURED_AT)
        symbols = {r["symbol"] for r in rows}
        assert {"IR_COIN_EMAMI", "IR_GOLD_18K", "USD", "EUR", "USDT_IRT"} <= symbols

    def test_history_columns_are_open_low_high_close(self):
        """LOW precedes HIGH here, the reverse of every other feed we read.

        Read positionally as OHLC, every row would claim a high below its low.
        Nothing downstream validates that, so it would surface as a quietly
        wrong range rather than an error.
        """
        rows = load("tgju_history.json")["data"]
        parsed = tgju.parse_history_row(rows[0])
        assert parsed["low"] <= parsed["open"] <= parsed["high"]
        assert parsed["low"] <= parsed["close"] <= parsed["high"]
        assert parsed["jalali"] == "1405-06-07"
        assert parsed["gregorian"] == "2026-08-29"

    def test_a_malformed_history_row_is_skipped_not_fatal(self):
        """One bad row must not abort a 3,938-row backfill."""
        assert tgju.parse_history_row(["x", "y", "z"]) is None
        assert tgju.parse_history_row(None) is None


# ------------------------------------------------------------------- Wallex

class TestWallexUnits:
    def test_quote_asset_decides_the_unit(self, wallex_symbols):
        """TMN and USDT books price the same coin ~200,000x apart."""
        rows = {r["symbol"]: r for r in wallex.live_rows(wallex_symbols)}
        assert rows["BTCTMN"]["unit"] == "تومان"
        assert rows["BTCUSDT"]["unit"] == "تتر"

    def test_toman_book_agrees_with_tgju_on_tether(self, wallex_symbols,
                                                   tgju_current):
        """Two independent origins, one truth: USDT/Toman.

        Every dollar-denominated holding is multiplied by this rate, so a
        disagreement here is a whole-portfolio error. Captured in the same
        minute, they must agree closely.
        """
        from marketdata.currency import to_toman
        rows = {r["symbol"]: r for r in wallex.live_rows(wallex_symbols)}
        wallex_usdt = Decimal(rows["USDTTMN"]["price"])

        price, unit = tgju.quote(tgju_current, "crypto-tether-irr", now=CAPTURED_AT)
        tgju_usdt = to_toman("USDT_IRT", price, unit)

        spread = abs(wallex_usdt - tgju_usdt) / max(wallex_usdt, tgju_usdt)
        assert spread < Decimal("0.01"), (
            f"wallex={wallex_usdt} tgju={tgju_usdt} spread={spread:.4%}"
        )

    def test_undeclared_quote_asset_is_skipped(self):
        """No declared unit means no price. Never guess."""
        rows = wallex.live_rows({
            "WEIRD": {"symbol": "WEIRD", "quoteAsset": "???",
                      "stats": {"lastPrice": "123"}},
        })
        assert rows == []


class TestWallexHistory:
    def test_column_major_payload_is_transposed_correctly(self):
        """`{s,t,o,h,l,c,v}` are parallel arrays, not candle objects.

        Zipping them in the wrong order misaligns every candle silently. The
        assertion is the invariant that survives a transposition bug: low is
        the floor, high is the ceiling, on every row.
        """
        rows = wallex.candles(load("wallex_udf.json"))
        assert len(rows) == 6
        for row in rows:
            assert row["low"] <= row["open"] <= row["high"]
            assert row["low"] <= row["close"] <= row["high"]
        assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)

    def test_ragged_columns_refuse_to_transpose(self):
        """A truncated column would mislabel every candle after the cut.

        `zip` stops at the shortest input, so this fails silently by default --
        the candles keep parsing, they just describe the wrong days.
        """
        payload = load("wallex_udf.json")
        payload["c"] = payload["c"][:3]
        with pytest.raises(SourceResponseError, match="disagree in length"):
            wallex.candles(payload)

    def test_no_data_is_empty_not_an_error(self):
        assert wallex.candles({"s": "no_data", "t": []}) == []

    def test_unsupported_resolution_is_rejected_before_the_request(self):
        """`5`, `180`, `W`, `M` look plausible and return error bodies.

        Caught locally, because an error body parsed as candles is an empty
        ingest, and an empty ingest is recorded as a quiet market day.
        """
        with pytest.raises(ValueError, match="rejects resolution"):
            wallex.fetch_ohlc("USDTTMN", resolution="W")


# ------------------------------------------------------------------ Nobitex

class TestNobitexCrossCheck:
    def test_rial_pairs_normalise_to_toman(self, nobitex_stats):
        rows = {r["symbol"]: r for r in nobitex.live_rows(nobitex_stats)}
        assert rows["USDT"]["unit"] == "ریال"
        assert nobitex.to_toman(rows["USDT"]["price"], "ریال") == Decimal("209423")

    def test_closed_pairs_are_skipped(self):
        """A closed market's `latest` is real, and it is not a live price."""
        rows = nobitex.live_rows({
            "btc-rls": {"isClosed": True, "latest": "163570020030"},
        })
        assert rows == []

    def test_the_two_exchanges_agree(self, nobitex_stats, wallex_symbols):
        """Captured minutes apart; BTC/ETH/USDT must land within tolerance."""
        agreements, disagreements = nobitex.cross_check(
            nobitex.live_rows(nobitex_stats), wallex.live_rows(wallex_symbols)
        )
        assert not disagreements, disagreements
        assert {r["coin"] for r in agreements} >= {"BTC", "ETH", "USDT"}

    def test_a_thin_book_does_not_trip_the_check(self):
        """A real spread on an illiquid pair is not a fault.

        Measured in production: eleven liquid coins agreed within 0.96%, while
        PAXG -- tokenised gold, much the thinnest book of the set -- sat at
        3.74%, with spot gold times the USDT rate landing between the two
        quotes. At the original 2% band that warned on every single cycle, and
        a check that cries wolf daily is one nobody reads.
        """
        nob = [{"symbol": "PAXG", "price": "9328900000", "unit": "ریال"}]
        wal = [{"base": "PAXG", "quote": "TMN", "price": "898000000",
                "unit": "تومان"}]
        agreements, disagreements = nobitex.cross_check(nob, wal)
        assert not disagreements
        assert agreements[0]["spread"] < Decimal("0.05")

    def test_the_band_still_catches_an_order_of_magnitude(self):
        """...while staying far below the error it exists to catch.

        A Rial read as Toman reads as |x - 10x| / 10x = 90%, so the band has an
        18x margin. Asserted rather than asserted-in-prose so that widening the
        tolerance far enough to hide a unit error fails here.
        """
        unit_error_spread = Decimal("0.9")
        assert nobitex.DEFAULT_TOLERANCE * 10 < unit_error_spread

        nob = [{"symbol": "USDT", "price": "2094230", "unit": "ریال"}]
        wal = [{"base": "USDT", "quote": "TMN", "price": "209423",
                "unit": "تومان"}]
        # Same price, correctly labelled: agrees.
        assert not nobitex.cross_check(nob, wal)[1]
        # Same payload with the Rial mislabelled as Toman: caught.
        assert nobitex.cross_check(
            [{**nob[0], "unit": "تومان"}], wal
        )[1]

    def test_a_unit_regression_is_caught_as_a_disagreement(self, nobitex_stats,
                                                           wallex_symbols):
        """The check's reason for existing: prove it fires on a 10x error.

        Mislabelling Nobitex's Rial as Toman is the single most likely
        regression in this migration. It must surface as a disagreement, not as
        a plausible price.
        """
        rows = nobitex.live_rows(nobitex_stats)
        for row in rows:
            row["unit"] = "تومان"  # the bug: Rial read as Toman
        _, disagreements = nobitex.cross_check(rows, wallex.live_rows(wallex_symbols))
        assert disagreements, "a 10x unit error must not pass the cross-check"
        assert all(r["spread"] > Decimal("0.5") for r in disagreements)


# ------------------------------------------------------- migration equivalence

class TestMigrationEquivalence:
    """Does swapping the source change what a user sees? It must not."""

    #: Production's live prices at the capture instant, read from
    #: `portfolio_price` on the VPS. The direct sources must reproduce these.
    PRODUCTION_TOMAN = {
        "IR_COIN_EMAMI": Decimal("223510000"),
        "IR_COIN_HALF": Decimal("114000000"),
        "IR_COIN_QUARTER": Decimal("61500000"),
        "IR_COIN_1G": Decimal("32000000"),
        "EUR": Decimal("243980"),
        "USD": Decimal("209300"),
        "IR_GOLD_18K": Decimal("22220100"),
        "USDT_IRT": Decimal("209053"),
    }

    def test_tgju_reproduces_the_prices_brsapi_served(self, tgju_current):
        """Within 0.5%, because BrsApi is reselling this exact feed.

        Five of the eight matched to the rial in the live comparison; the other
        three differed by one refresh interval. A wider tolerance would hide a
        unit error, which is the failure this is really guarding.
        """
        from marketdata.currency import to_toman
        rows = {r["symbol"]: r for r in tgju.live_rows(tgju_current, now=CAPTURED_AT)}

        for symbol, expected in self.PRODUCTION_TOMAN.items():
            row = rows.get(symbol)
            assert row is not None, f"{symbol} vanished in the migration"
            actual = to_toman(symbol, Decimal(row["price"]), row["unit"])
            drift = abs(actual - expected) / expected
            assert drift < Decimal("0.005"), (
                f"{symbol}: brsapi={expected} tgju={actual} drift={drift:.3%}"
            )

    def test_direct_rows_override_brsapi_in_the_extractor(self, tgju_current):
        """The precedence rule that makes the swap a swap.

        BrsApi is indexed first and the direct rows second, so the same symbol
        resolves to the direct price. If this inverts, the migration is inert
        and nothing announces it -- both numbers are plausible.
        """
        from portfolio.live.extractor import _build_lookup

        brs = {"gold": [{"symbol": "USD", "price": "1", "unit": "تومان"}]}
        direct = {"rows": tgju.live_rows(tgju_current, now=CAPTURED_AT)}

        lookup = _build_lookup(brs, direct)
        assert Decimal(lookup["usd"]["price"]) == Decimal("2092950")

    def test_brsapi_still_covers_what_direct_sources_miss(self):
        """Fallback must survive: an unmapped symbol keeps its BrsApi price."""
        from portfolio.live.extractor import _build_lookup

        brs = {"gold": [{"symbol": "IR_GOLD_MELTED", "price": "5", "unit": "تومان"}]}
        lookup = _build_lookup(brs, {"rows": []})
        assert lookup["ir_gold_melted"]["price"] == "5"


# ------------------------------------------------------- crypto history backfill

@pytest.mark.django_db
class TestCryptoHistoryIngest:
    """The backfill that gives ten coins a history they did not have.

    Before it, the whole crypto surface was BTC and USDT_IRT from 1402-08-09.
    A symbol with no history cannot enter the returns matrix, so it cannot be
    optimized over or risk-scored at all -- these tests guard the two ways that
    backfill could quietly go wrong: writing the wrong unit, and rewriting
    history it should only be extending.
    """

    def _candle(self, ts, close, **kw):
        return {
            "ts": ts,
            "open": kw.get("open", close),
            "high": kw.get("high", close),
            "low": kw.get("low", close),
            "close": close,
            "volume": Decimal("1"),
        }

    def test_epoch_dates_land_on_the_right_jalali_day(self):
        """Tehran is UTC+3:30, so the day boundary is 20:30Z.

        Dating a late-session candle by UTC files it a day early. That exact
        off-by-one, in the other direction, is what put 3.7M duplicate candles
        in this warehouse when `ts` was derived wrongly.
        """
        from marketdata.ingest import ingest_direct_crypto_history
        from marketdata.models import GoldCurrencyHistory

        import datetime as dt
        before = int(dt.datetime(2026, 8, 31, 20, 29, tzinfo=dt.timezone.utc).timestamp())
        after = int(dt.datetime(2026, 8, 31, 20, 30, tzinfo=dt.timezone.utc).timestamp())

        ingest_direct_crypto_history("TESTC", "تومان", [
            self._candle(before, Decimal("100")),
            self._candle(after, Decimal("200")),
        ])
        dates = set(
            GoldCurrencyHistory.objects.filter(symbol="TESTC")
            .order_by().values_list("date", flat=True)
        )
        assert dates == {"1405-06-09", "1405-06-10"}

    def test_existing_rows_are_never_rewritten(self):
        """Insert-only, because the two venues are close but not identical.

        Across 1,037 overlapping BTC days Wallex and the incumbent agreed to a
        median 0.43%. Good enough to trust for days we lack; not good enough to
        restate days we have, which would stitch two venues into one series and
        leave a discontinuity at the join in a table the returns matrix reads.
        """
        from marketdata.ingest import ingest_direct_crypto_history
        from marketdata.models import GoldCurrencyHistory

        import datetime as dt
        ts = int(dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.timezone.utc).timestamp())
        GoldCurrencyHistory.objects.create(
            symbol="BTC", unit="تتر", date="1405-06-09",
            close_price=Decimal("78841"),
        )

        created, known = ingest_direct_crypto_history(
            "BTC", "تتر", [self._candle(ts, Decimal("78111"))]
        )
        assert created == 0 and known == 1
        row = GoldCurrencyHistory.objects.get(symbol="BTC", date="1405-06-09")
        assert row.close_price == Decimal("78841"), "incumbent row must survive"

    def test_explicit_precision_repair_only_replaces_zero_same_unit(self):
        from marketdata.ingest import ingest_direct_crypto_history
        from marketdata.models import GoldCurrencyHistory

        import datetime as dt
        ts = int(dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.timezone.utc).timestamp())
        GoldCurrencyHistory.objects.create(
            symbol="SHIB", unit="تتر", date="1405-06-09",
            close_price=Decimal("0"),
        )
        candle = self._candle(ts, Decimal("0.000012345678"))
        created, known = ingest_direct_crypto_history("SHIB", "تتر", [candle])
        assert (created, known) == (0, 1)
        assert GoldCurrencyHistory.objects.get(symbol="SHIB").close_price == 0

        created, known = ingest_direct_crypto_history(
            "SHIB", "تتر", [candle], repair_zero=True
        )
        assert (created, known) == (0, 0)
        assert GoldCurrencyHistory.objects.get(symbol="SHIB").close_price == Decimal("0.000012345678")

        # A different quote book is a different unit, even with the same symbol.
        GoldCurrencyHistory.objects.filter(symbol="SHIB").update(close_price=0, unit="تومان")
        ingest_direct_crypto_history("SHIB", "تتر", [candle], repair_zero=True)
        assert GoldCurrencyHistory.objects.get(symbol="SHIB").close_price == 0

    def test_missing_days_are_filled(self):
        from marketdata.ingest import ingest_direct_crypto_history
        from marketdata.models import GoldCurrencyHistory

        import datetime as dt
        days = [
            int(dt.datetime(2026, 8, d, 10, 0, tzinfo=dt.timezone.utc).timestamp())
            for d in (28, 29, 30)
        ]
        GoldCurrencyHistory.objects.create(
            symbol="ETH", unit="تومان", date="1405-06-08", close_price=Decimal("5")
        )
        created, _ = ingest_direct_crypto_history(
            "ETH", "تومان", [self._candle(t, Decimal("7")) for t in days]
        )
        assert created == 2
        assert GoldCurrencyHistory.objects.filter(symbol="ETH").count() == 3

    def test_a_bad_candle_is_rejected_not_stored(self):
        """A zero or negative close is not a price; storing it poisons returns."""
        from marketdata.ingest import ingest_direct_crypto_history
        from marketdata.models import GoldCurrencyHistory

        import datetime as dt
        ts = int(dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.timezone.utc).timestamp())
        created, skipped = ingest_direct_crypto_history("BADC", "تومان", [
            self._candle(ts, Decimal("0")),
            {"ts": None, "close": Decimal("5")},
        ])
        assert created == 0 and skipped == 2
        assert not GoldCurrencyHistory.objects.filter(symbol="BADC").exists()

    def test_toman_and_tether_series_never_share_a_symbol(self):
        """The same coin is ~200,000x apart in the two books.

        The command maps a TMN book to `{BASE}_IRT` and a USDT book to `{BASE}`,
        matching what the table already does for USDT_IRT and BTC. If those ever
        collapse onto one symbol, one column holds two units and nothing on a
        chart looks wrong.
        """
        from marketdata.management.commands.backfill_crypto_history import QUOTES

        symbols = {q: f"BTC{suffix}" for q, (suffix, _) in QUOTES.items()}
        assert len(set(symbols.values())) == len(symbols)
        assert symbols["TMN"] == "BTC_IRT" and symbols["USDT"] == "BTC"
        assert QUOTES["TMN"][1] == "تومان" and QUOTES["USDT"][1] == "تتر"


class TestIndexStateIsObservedNotAsserted:
    """The market-open signal must never be a constant.

    `market_state_at` is clock-based -- weekday plus session hours -- and the
    ONLY thing that can override it is a cached provider "بسته". So a payload
    that always claims open can never reveal a weekday public holiday, and the
    TSE stock job would run every two minutes for a whole session against a shut
    market, on the wallet that actually binds (TSETMC hit 10,034/10,000 once).
    """

    def _current(self, ts):
        return {"bourse": {"p": "6,547,963.76", "d": "31,779.83", "ts": ts}}

    def test_index_printing_today_reads_as_open(self):
        payload = tgju.live_tedpix_payload(
            self._current("2026-09-01 12:39:04"),
            now=datetime(2026, 9, 1, 12, 40, tzinfo=TEHRAN),
        )
        assert payload["state"] == "باز"

    def test_an_index_that_has_not_printed_today_reads_as_closed(self):
        """A weekday holiday: inside session hours, last tick is yesterday's.

        Fresh enough to pass MAX_QUOTE_AGE, so the old code returned it wearing
        a hard-coded "open". The quote's own date is the evidence that the
        exchange never opened.
        """
        from marketdata.market_state import PROVIDER_CLOSED

        payload = tgju.live_tedpix_payload(
            self._current("2026-09-01 12:39:04"),
            now=datetime(2026, 9, 2, 11, 0, tzinfo=TEHRAN),
        )
        assert payload is not None, "still within MAX_QUOTE_AGE"
        assert payload["state"] == PROVIDER_CLOSED

    def test_that_closed_state_actually_suppresses_the_stock_job(self):
        """End to end: the derived state must reach the job planner.

        Asserting the string alone would pass while the wiring was broken; what
        matters is that `live_job_keys` stops asking for TSE stocks.
        """
        from marketdata.market_state import (
            CLOSED_DAYTIME,
            OPEN,
            live_job_keys,
            market_state_at,
        )

        now = datetime(2026, 9, 2, 11, 0, tzinfo=TEHRAN)
        assert market_state_at(now) == OPEN, "clock alone says the market is open"

        closed = market_state_at(now, provider_closed=True)
        assert closed == CLOSED_DAYTIME
        jobs = live_job_keys(state=closed, now=now, has_brs=True, has_tsetmc=True)
        assert "tsetmc" not in jobs
        assert "gold_currency" in jobs, "gold desks trade when the TSE is shut"


class TestHistoricalIndexRowsMakeNoStateClaim:
    def test_backfilled_rows_carry_an_empty_state(self):
        """`is_closure_day` reads the newest non-empty state for a date.

        A sentinel string on 2,751 historical rows would be read as "not
        closed" for every one of them. Empty is the value that branch already
        excludes, so a historical close stays silent about market state.
        """
        import inspect

        from marketdata import ingest

        source = inspect.getsource(ingest.ingest_tedpix_history)
        assert 'state=""' in source
        assert "tgju_history" not in source


@pytest.mark.django_db
class TestPaidFallbackSkipIsKeyedToWhatTheAppNeeds:
    """Skipping BrsApi is safe only if the free board covers every priced asset.

    Keyed to `tgju.BRS_TO_SLUG` this passed today and would fail the first time
    a user added one of the ~5,000 catalog instruments TGJU does not carry:
    board declared complete, only source that quotes their holding skipped,
    holding frozen with no error anywhere.
    """

    def test_required_symbols_come_from_active_assets(self):
        from portfolio.live.fetcher import _required_symbols
        from portfolio.models import Asset

        Asset.objects.create(key="gbp_cash", name="GBP", brs_symbol="GBP",
                             asset_class=Asset.AssetClass.CASH, is_active=True)
        Asset.objects.create(key="retired", name="Old", brs_symbol="OLD",
                             asset_class=Asset.AssetClass.CASH, is_active=False)

        required = _required_symbols()
        assert "GBP" in required
        assert "OLD" not in required, "an inactive asset needs no live price"

    def test_an_unmapped_holding_keeps_the_paid_call(self):
        from portfolio.live.fetcher import _required_symbols
        from portfolio.models import Asset

        Asset.objects.create(key="gbp_cash", name="GBP", brs_symbol="GBP",
                             asset_class=Asset.AssetClass.CASH, is_active=True)

        # Everything TGJU maps, and nothing else -- the old completeness test.
        direct = {s.upper() for s in tgju.BRS_TO_SLUG}
        assert not _required_symbols() <= direct, (
            "GBP is unpriced by the direct board, so BrsApi must still run"
        )
