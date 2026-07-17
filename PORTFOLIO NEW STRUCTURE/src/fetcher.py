"""Market API fetch layer.

Functions here only fetch raw market data. They do not calculate portfolio
values; `engine.py` turns these payloads into standard project prices.
"""

import requests
import logging
from urllib.parse import urlencode

from utils import log_step

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Request setup -----------------------------------------------------------
# Send browser-like headers because some market endpoints reject bare clients.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
}

DEFAULT_TSETMC_SYMBOL_URL = "https://BrsApi.ir/Api/Tsetmc/Symbol.php"
DEFAULT_TSETMC_HISTORY_URL = "https://BrsApi.ir/Api/Tsetmc/History.php"
KAMA_SYMBOL = "کاما"


# --- Small parsing helpers ---------------------------------------------------
def _make_api_url(base_url, params):
    """Build a query URL without changing the existing endpoint structure."""
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode(params)}"


def extract_price(record):
    """Return a positive TSETMC price from one record, preferring last price.

    Shared by the engine (pricing) and the KAMA backfill (history repair) so the
    two never drift on what `pl`/`pc` mean.
    """
    if not isinstance(record, dict):
        return 0
    for field_name in ("pl", "pc"):
        try:
            price = float(record.get(field_name) or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return 0


def find_tsetmc_symbol(tsetmc_payload, name):
    """Find one stock row by Persian symbol name.

    Canonical matcher shared by the fetch layer (KAMA fallback decision) and the
    engine (pricing). Searches by l18 (symbol) first, then falls back to l30
    (company name) and partial/lowercase matching. Logs each match path so API
    shape changes stay visible during a run.
    """
    if not isinstance(tsetmc_payload, list):
        log_step(f"TSETMC payload is not a list (type: {type(tsetmc_payload).__name__}). Cannot search for '{name}'.", "warning")
        return None

    if not tsetmc_payload:
        log_step("TSETMC payload is an empty list. No stock data available.", "warning")
        return None

    log_step(f"TSETMC payload: {len(tsetmc_payload)} records. Searching for '{name}'...", "info")
    if isinstance(tsetmc_payload[0], dict):
        log_step(f"Sample record keys: {list(tsetmc_payload[0].keys())[:10]}", "info")

    # 1) Exact match on l18 (symbol name)
    for record in tsetmc_payload:
        if isinstance(record, dict) and record.get("l18") == name:
            log_step(f"Found '{name}' by exact l18 match.", "success")
            return record

    # 2) Exact match on l30 (company name)
    for record in tsetmc_payload:
        if isinstance(record, dict) and record.get("l30") == name:
            log_step(f"Found '{name}' by exact l30 (company name) match.", "success")
            return record

    # 3) Partial/case-insensitive match on l18 / l30
    name_lower = name.strip().casefold()
    for record in tsetmc_payload:
        if not isinstance(record, dict):
            continue
        l18 = str(record.get("l18", "")).strip().casefold()
        l30 = str(record.get("l30", "")).strip().casefold()
        if (l18 and name_lower in l18) or (l30 and name_lower in l30):
            log_step(f"Found '{name}' by partial match.", "success")
            return record

    log_step(f"Symbol '{name}' NOT found in TSETMC payload (searched l18 and l30, exact and partial).", "warning")
    return None


# --- Individual API calls ----------------------------------------------------
def fetch_brsapi(brs_url, brs_api_key):
    """Get BRS API gold/currency data."""
    try:
        log_step("Trying BRS API (gold/currency).", "info")
        response = requests.get(
            f"{brs_url}?key={brs_api_key}", headers=HEADERS, timeout=20
        )
        response.raise_for_status()
        log_step("BRS API returned valid data.", "success")
        return response.json()
    except requests.exceptions.RequestException as exc:
        logger.error(f"Error fetching BRSAPI data: {exc}")
        log_step("Could not fetch BRS API. Will use cached values if available.", "warning")
        return None


def fetch_tsetmc(tsetmc_url, tsetmc_api_key):
    """Get TSETMC stock list JSON."""
    try:
        log_step("Trying TSETMC API (stock data).", "info")
        response = requests.get(
            f"{tsetmc_url}?key={tsetmc_api_key}&type=1",
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        log_step("TSETMC API returned valid data.", "success")
        return response.json()
    except requests.exceptions.RequestException as exc:
        logger.error(f"Error fetching TSETMC data: {exc}")
        log_step("Could not fetch TSETMC API. Will use cached values if available.", "warning")
        return None


def fetch_tsetmc_symbol(tsetmc_symbol_url, tsetmc_api_key, symbol):
    """Get one TSETMC symbol JSON."""
    try:
        log_step(f"Trying TSETMC Symbol API for '{symbol}'.", "info")
        response = requests.get(
            _make_api_url(tsetmc_symbol_url, {"key": tsetmc_api_key, "l18": symbol}),
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        log_step(f"TSETMC Symbol API returned data for '{symbol}'.", "success")
        return response.json()
    except requests.exceptions.RequestException as exc:
        logger.error(f"Error fetching TSETMC symbol data for {symbol}: {exc}")
        log_step(f"Could not fetch TSETMC Symbol API for '{symbol}'.", "warning")
        return None


def fetch_tsetmc_history(tsetmc_history_url, tsetmc_api_key, symbol):
    """Get historical TSETMC stock JSON for one symbol."""
    try:
        log_step(f"Trying TSETMC History API for '{symbol}'.", "info")
        response = requests.get(
            _make_api_url(
                tsetmc_history_url,
                {"key": tsetmc_api_key, "type": 0, "l18": symbol},
            ),
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        log_step(f"TSETMC History API returned data for '{symbol}'.", "success")
        return response.json()
    except requests.exceptions.RequestException as exc:
        logger.error(f"Error fetching TSETMC history data for {symbol}: {exc}")
        log_step(f"Could not fetch TSETMC History API for '{symbol}'.", "warning")
    return None


# --- Combined fetch used by the daily pipeline -------------------------------
def fetch_all_markets(api_settings):
    """Return all raw market payloads needed by `engine.extract_standard_prices`."""
    raw_data = {}

    # BRS API: gold, currency and crypto prices.
    brs_url = api_settings.get("brs_url")
    brs_key = api_settings.get("brs_api_key")
    if brs_url and brs_key:
        log_step("API source is configured: BRS URL + key present.", "info")
        raw_data["brsapi"] = fetch_brsapi(brs_url, brs_key)
    else:
        log_step("BRS API skipped: URL or key missing in settings.", "skip")

    # TSETMC API: Tehran stock exchange prices.
    tsetmc_url = api_settings.get("tsetmc_url")
    tsetmc_key = api_settings.get("tsetmc_api_key")
    if tsetmc_url and tsetmc_key:
        log_step("API source is configured: TSETMC URL + key present.", "info")
        raw_data["tsetmc"] = fetch_tsetmc(tsetmc_url, tsetmc_key)
        kama_record = find_tsetmc_symbol(raw_data.get("tsetmc"), KAMA_SYMBOL)
        if extract_price(kama_record) <= 0:
            log_step("KAMA not priced from AllSymbols. Trying Symbol.php fallback.", "warning")
            raw_data["tsetmc_symbol_kama"] = fetch_tsetmc_symbol(
                api_settings.get("tsetmc_symbol_url", DEFAULT_TSETMC_SYMBOL_URL),
                tsetmc_key,
                KAMA_SYMBOL,
            )
        else:
            log_step("KAMA found in AllSymbols payload.", "success")
    else:
        log_step("TSETMC API skipped: URL or key missing in settings.", "skip")

    if raw_data.get("brsapi"):
        log_step("BRS data stage: captured in memory.", "success")
    else:
        log_step("BRS data stage: no data (fallback branch will be used).", "warning")

    if raw_data.get("tsetmc"):
        log_step("TSETMC data stage: captured in memory.", "success")
    else:
        log_step("TSETMC data stage: no data (fallback branch will be used).", "warning")
    return raw_data
