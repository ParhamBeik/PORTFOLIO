"""extractor.extract_standard_prices must match the legacy engine exactly.

This is the contract that makes the SaaS a faithful port: identical raw payloads
must yield identical price maps, key for key. These are pure functions — no
database is needed.
"""
from decimal import Decimal

from portfolio.live.extractor import extract_standard_prices

# Mirror settings.MANUAL_PRICES plus the two derived-coin factors, in the shape
# the legacy engine's `constants` dict expects.
LEGACY_CONSTANTS = {
    "swiss_gold_bar_1g_price": 25900000,
    "swiss_gold_bar_2_5g_price": 61610000,
    "quarter_pre86_factor": 0.8694109297,
    "quarter_to_1g_ratio": 0.493733384,
}

PARITY_KEYS = [
    "emami_coin", "half_coin", "quarter_coin", "quarter_coin_pre86",
    "one_gram_coin", "swiss_gold_bar_1g", "swiss_gold_bar_2_5g",
    "gold_18k_gram", "usd_cash", "usdt_irt", "euro_cash",
    "gold_ounce_usd", "bitcoin_usd", "kama_stock",
]


def test_extract_matches_legacy_for_every_key(raw_market_sample, legacy_engine):
    saas = extract_standard_prices(raw_market_sample)
    legacy = legacy_engine.extract_standard_prices(raw_market_sample, LEGACY_CONSTANTS)

    assert set(saas) == set(legacy), (
        f"key sets differ: saas_only={set(saas) - set(legacy)} legacy_only={set(legacy) - set(saas)}"
    )

    mismatches = {
        k: (float(saas[k]), float(legacy[k]))
        for k in PARITY_KEYS
        if float(saas[k]) != float(legacy[k])
    }
    assert not mismatches, f"price map diverged from legacy engine: {mismatches}"


def test_usdt_low_quote_is_converted_to_tomans(raw_market_sample):
    """H1 regression: a sub-10 USD tether quote becomes its Toman equivalent."""
    prices = extract_standard_prices(raw_market_sample)
    # USDT quoted at 1.0 USD, USD at 63200 Toman -> 63200 Toman (whole number).
    assert prices["usdt_irt"] == Decimal("63200")
    assert prices["usd_cash"] == Decimal("63200")


def test_btc_high_quote_is_left_in_usd(raw_market_sample):
    """Above the conversion threshold the USD quote is passed through unchanged."""
    prices = extract_standard_prices(raw_market_sample)
    assert prices["bitcoin_usd"] == Decimal("64500")


def test_kama_extracted_from_tsetmc(raw_market_sample):
    prices = extract_standard_prices(raw_market_sample)
    assert prices["kama_stock"] == Decimal("5230")


def test_kama_falls_back_to_last_price_when_missing():
    """When TSETMC returns nothing, KAMA reuses the last-known price."""
    raw = {"brsapi": {"items": [{"symbol": "USD", "price": 63200}]}, "tsetmc": []}
    prices = extract_standard_prices(raw, last_prices={"kama_stock": 9999})
    assert prices["kama_stock"] == Decimal("9999")


def test_derived_coins_match_legacy_arithmetic(raw_market_sample, legacy_engine):
    """quarter_pre86 and one_gram_coin are derived from the quarter coin identically."""
    saas = extract_standard_prices(raw_market_sample)
    legacy = legacy_engine.extract_standard_prices(raw_market_sample, LEGACY_CONSTANTS)
    assert saas["quarter_coin_pre86"] == Decimal("108676366")
    assert saas["one_gram_coin"] == Decimal("61716673")
    assert saas["quarter_coin_pre86"] == Decimal(legacy["quarter_coin_pre86"])
    assert saas["one_gram_coin"] == Decimal(legacy["one_gram_coin"])
