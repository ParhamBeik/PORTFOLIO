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
