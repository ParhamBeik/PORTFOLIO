"""Daily portfolio pipeline.

This is the current entrypoint for the modular project. It connects the small
parts together: load inputs, fetch prices, value holdings, save history, and
export dashboards.
"""

import os
from rich.console import Console
from rich.table import Table
from rich import box

from utils import load_json, save_json, append_jsonl, get_current_timestamp, setup_rich_logging, log_step
from fetcher import fetch_all_markets
from engine import extract_standard_prices, build_snapshot
from exporter import export_daily_xlsx
from backfill_kama_history import backfill as backfill_kama_history

# --- Paths -------------------------------------------------------------------
# All runtime data lives beside this src/ folder inside PORTFOLIO NEW STRUCTURE.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

CURRENT_STATE_PATH = os.path.join(DATA_DIR, "current_state.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
CACHE_PATH = os.path.join(DATA_DIR, "latest_prices_cache.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history_snapshots.jsonl")

logger = setup_rich_logging(__name__)
console = Console()

# --- Fresh-install defaults --------------------------------------------------
# These are only used if the data files do not exist yet.
DEFAULT_CURRENT_STATE = {}

DEFAULT_SETTINGS = {
    "api_settings": {
        "brs_url": "https://Api.BrsApi.ir/Market/Gold_Currency.php",
        # Keys are read from env vars (BRS_API_KEY / TSETMC_API_KEY) first, then
        # from data/settings.json. Never committed in source; see .gitignore.
        "brs_api_key": "",
        "tsetmc_url": "https://Api.BrsApi.ir/Tsetmc/AllSymbols.php",
        "tsetmc_symbol_url": "https://Api.BrsApi.ir/Tsetmc/Symbol.php",
        "tsetmc_history_url": "https://Api.BrsApi.ir/Tsetmc/History.php",
        "tsetmc_api_key": "",
    },
    "api_urls": {
        "brsapi": "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency_v2.json",
        "tsetmc_base": "http://www.tsetmc.com/tsev2/data/instinfodata.aspx",
    },
    "constants": {
        "quarter_pre86_factor": 0.8694109297,
        "quarter_to_1g_ratio": 0.493733384,
        "house_area_sqm": 90.2,
        "house_mortgage_deduction": 400000000,
        # Swiss bars: set by hand in settings and used directly by engine.
        "swiss_gold_bar_1g_price": 25900000,
        "swiss_gold_bar_2_5g_price": 61610000,
    },
}


def _print_data_summary(current_state, settings, prices, used_cache):
    """Show small tables so the user can see what ran and why."""
    table = Table(title="Loaded Inputs", box=box.SIMPLE)
    table.add_column("Item", justify="left", style="bold")
    table.add_column("Status", justify="left")

    table.add_row("Owners in portfolio", str(len(current_state)))
    table.add_row("Settings found", "yes" if settings else "no")
    table.add_row("Price source", "cache (fallback)" if used_cache else "live fetch")
    table.add_row("Known assets priced", f"{len(prices)}")
    console.print(table)


def run_pipeline():
    """Run the daily update from inputs to Excel exports."""
    log_step("Starting portfolio pipeline", "info")

    # 1) Load owner holdings and project settings.
    current_state = load_json(CURRENT_STATE_PATH)
    if not current_state:
        log_step("current_state.json not found - writing a default empty state.", "warning")
        current_state = dict(DEFAULT_CURRENT_STATE)
        save_json(CURRENT_STATE_PATH, current_state)

    settings = load_json(SETTINGS_PATH)
    if not settings:
        log_step("settings.json not found - writing defaults with swiss manual gold constants.", "warning")
        settings = DEFAULT_SETTINGS
        save_json(SETTINGS_PATH, settings)

    # 2) Fetch live market payloads, then convert them into our standard keys.
    # Keys: env vars win, then settings.json. Lets ops rotate without touching data.
    api_settings = settings.get("api_settings") or settings.get("api_urls") or {}
    api_settings = {
        **api_settings,
        "brs_api_key": os.environ.get("BRS_API_KEY") or api_settings.get("brs_api_key", ""),
        "tsetmc_api_key": os.environ.get("TSETMC_API_KEY") or api_settings.get("tsetmc_api_key", ""),
    }
    log_step("Fetching market data from configured sources...", "info")
    raw_data = fetch_all_markets(api_settings)

    if raw_data and (raw_data.get("brsapi") or raw_data.get("tsetmc")):
        log_step("Live market data received. Building prices from live APIs.", "success")
        current_prices = extract_standard_prices(raw_data, settings.get("constants", {}))
        save_json(CACHE_PATH, current_prices)
        log_step("Updated cache with freshly extracted prices.", "success")
    else:
        log_step("No live market payload available. Trying cache fallback.", "warning")
        current_prices = load_json(CACHE_PATH)
        if current_prices:
            log_step("Loaded prices from latest_prices_cache.json.", "success")
        else:
            log_step("Cache missing or empty. Continuing with empty prices map.", "error")
            current_prices = {}

    # 3) Build one timestamped portfolio snapshot and save it to history.
    snapshot_data = build_snapshot(current_state, settings, current_prices)
    log_step("Portfolio snapshot created from state + selected prices.", "success")

    final_record = {
        "timestamp": get_current_timestamp(),
        "snapshot": snapshot_data,
    }
    append_jsonl(HISTORY_PATH, final_record)
    log_step(f"Snapshot saved into {HISTORY_PATH}.", "success")

    # 4) Repair old KAMA gaps when needed; never block the daily run on this.
    try:
        backfill_kama_history(dry_run=False, skip_if_complete=True)
    except Exception as exc:
        log_step(f"KAMA history backfill skipped after error: {exc}", "warning")

    # 5) Print quick human-readable totals for the terminal.
    totals = final_record["snapshot"].get("total_values_tomans", {})
    if not totals:
        log_step("No owners in current_state; snapshot total section is empty.", "warning")

    usd_rate = current_prices.get("usd_cash", 0)
    if not usd_rate:
        log_step("No USD price available; dollar values can't be calculated.", "warning")

    for owner, val in totals.items():
        usd_val = val / usd_rate if usd_rate else 0
        log_step(f"{owner}: {val:,.0f} Tomans  (~${usd_val:,.0f})", "info")
    grand_total = sum(totals.values())
    grand_total_usd = grand_total / usd_rate if usd_rate else 0
    log_step(
        f"Total (all portfolios): {grand_total:,.0f} Tomans  (~${grand_total_usd:,.0f})",
        "success",
    )

    used_cache = not (raw_data and (raw_data.get("brsapi") or raw_data.get("tsetmc")))
    _print_data_summary(current_state, settings, current_prices, used_cache)

    # 6) Export the v2 .xlsx dashboard (regenerated from history_snapshots.jsonl).
    export_daily_xlsx(snapshot_data, history_path=HISTORY_PATH)
    log_step("Pipeline finished successfully.", "success")


if __name__ == "__main__":
    run_pipeline()
