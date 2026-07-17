"""Asset classes and currency names used by dashboard charts.

Raw holding keys like `emami_coin` and `kama_stock` are grouped into readable
classes like Gold and Stock. Keeping that map here makes analytics and workbook
code agree on the same categories.
"""

# --- Asset class definitions -------------------------------------------------
# This order is also the display order in charts and tables.
GOLD = "Gold"
CASH = "Cash"
STOCK = "Stock"
REAL_ESTATE = "Real Estate"
CRYPTO = "Crypto"

CLASS_ORDER = [GOLD, CASH, STOCK, REAL_ESTATE, CRYPTO]

# Persian labels keep workbook sheets bilingual.
CLASS_LABELS_FA = {
    GOLD: "طلا",
    CASH: "نقد / دلار",
    STOCK: "سهام",
    REAL_ESTATE: "ملک",
    CRYPTO: "رمز ارز",
}

# Stable colors keep the same class recognizable across charts.
CLASS_COLORS = {
    GOLD: "E0A82E",         # gold
    CASH: "4C9A2A",         # green
    STOCK: "2E75B6",        # blue
    REAL_ESTATE: "8B5E3C",  # brown
    CRYPTO: "F2A900",       # bitcoin orange
}

# Keys come from `current_state.json` and `engine.build_snapshot`.
ASSET_KEY_TO_CLASS = {
    "emami_coin": GOLD,
    "half_coin": GOLD,
    "quarter_coin": GOLD,
    "quarter_coin_pre86": GOLD,
    "swiss_gold_bar_1g": GOLD,
    "swiss_gold_bar_2_5g": GOLD,
    "one_gram_coin": GOLD,
    "gold_18k_gram": GOLD,
    "usd_cash": CASH,
    "tether": CASH,
    "usdt_irt": CASH,
    "kama_stock": STOCK,
    "house_asset": REAL_ESTATE,
    "bitcoin_usd": CRYPTO,
    "bitcoin": CRYPTO,
}

# House value is tracked in totals but excluded from allocation percentage charts.
CLASSES_EXCLUDED_FROM_ALLOCATION = {REAL_ESTATE}

# --- Price-key display registry -----------------------------------------------
# Canonical mapping from internal price keys (engine output) to human labels and
# the display order used by the dashboard. Kept here so analytics, the workbook
# builder, and any future consumer agree on one ordering.

PRICE_KEY_LABELS = {
    "bitcoin_usd": "Bitcoin",
    "usdt_irt": "Tether",
    "usd_cash": "US Dollar",
    "emami_coin": "Emami Coin",
    "half_coin": "Half Coin",
    "quarter_coin": "Quarter Coin",
    "quarter_coin_pre86": "Quarter Coin (Pre-86)",
    "swiss_gold_bar_1g": "Swiss Gold Bar (1g)",
    "swiss_gold_bar_2_5g": "Swiss Gold Bar (2.5g)",
    "one_gram_coin": "1g Coin",
    "gold_18k_gram": "Gold Gram (18K)",
    "kama_stock": "KAMA Stock",
    "euro_cash": "Euro",
    "gold_ounce_usd": "Gold Ounce (Global)",
}
PRICE_LABEL_KEYS = {label: key for key, label in PRICE_KEY_LABELS.items()}

# Stable row order so dashboard price tables stay familiar.
LIVE_DATA_ORDER = [
    "Bitcoin",
    "Tether",
    "US Dollar",
    "Emami Coin",
    "Half Coin",
    "Quarter Coin",
    "Quarter Coin (Pre-86)",
    "Swiss Gold Bar (1g)",
    "Swiss Gold Bar (2.5g)",
    "1g Coin",
    "Gold Gram (18K)",
    "KAMA Stock",
    "Euro",
    "Gold Ounce (Global)",
]

# Prices quoted in USD rather than Tomans.
USD_PRICE_KEYS = {"bitcoin_usd", "gold_ounce_usd"}

# --- Currency tracks ---------------------------------------------------------
IRT = "IRT"
USD = "USD"
CURRENCIES = [IRT, USD]

CURRENCY_LABELS = {
    IRT: "Tomans",
    USD: "USDT",
}


def classify(asset_key):
    """Return the class for a raw asset key, or None if unmapped."""
    return ASSET_KEY_TO_CLASS.get(asset_key)


def allocation_classes():
    """Classes shown in the allocation-% charts (house excluded)."""
    return [c for c in CLASS_ORDER if c not in CLASSES_EXCLUDED_FROM_ALLOCATION]


def owners_from_state(current_state):
    """Derive the ordered owner list from current_state.json.

    Keeps insertion order (Python dicts are ordered) so the workbook columns
    line up with how the user wrote the file. Non-dict entries are ignored.
    """
    if not isinstance(current_state, dict):
        return []
    return [owner for owner, assets in current_state.items() if isinstance(assets, dict)]
