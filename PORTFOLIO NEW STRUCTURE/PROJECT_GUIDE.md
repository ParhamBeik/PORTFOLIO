# Portfolio Project Guide

This project tracks family portfolio value over time.

The source of truth is the modular pipeline in `src/`.

## How The Daily Pipeline Works

1. Read holdings from `data/current_state.json`.
2. Read API settings, manual prices, and constants from `data/settings.json`.
3. Fetch live prices from BRS and TSETMC.
4. Convert raw API data into one standard price map.
5. Calculate each owner's asset values and total portfolio value.
6. Save a timestamped snapshot into `data/history_snapshots.jsonl`.
7. Export today's dashboards into `exports/`.

## Important Files

- `src/run_daily_pipeline.py` — readable alias for running the daily portfolio update.
- `src/main.py` — orchestrates the full daily pipeline.
- `src/fetcher.py` — talks to market APIs and returns raw market payloads.
- `src/engine.py` — normalizes prices and calculates portfolio snapshots.
- `src/analytics.py` — turns snapshot history into daily averages and chart tables.
- `src/exporter.py` — writes current data into dashboard workbook outputs.
- `src/build_template.py` — builds the pure `.xlsx` v2 dashboard workbook.
- `src/asset_classes.py` — defines portfolio classes like Gold, Cash, Stock, and Real Estate.
- `src/utils.py` — small shared helpers for JSON files, logging, and timestamps.
- `src/backfill_kama_history.py` — repairs missing KAMA prices in existing history.

## Data Files

- `data/current_state.json` — what each owner currently holds.
- `data/settings.json` — API endpoints, constants, and display-name mapping.
- `data/latest_prices_cache.json` — last good price map used when APIs fail.
- `data/history_snapshots.jsonl` — append-style ledger of portfolio snapshots, newest first.

## Output Files

- `exports/Portfolio_YYYY-MM-DD.xlsx` — current v2 dashboard.
- `exports/Portfolio_YYYY-MM-DD.xlsm` — legacy macro workbook export.
- `template/Portfolio_Tracker_v2.xlsx` — generated v2 workbook template.
- `template/Portfolio_Tracker_Real_Time.xlsm` — legacy workbook template.

## Mental Model

Think of the project as four layers:

- **Inputs:** holdings, settings, constants, and cached prices.
- **Market fetch:** BRS and TSETMC API responses.
- **Valuation:** standard prices plus owner holdings become one snapshot.
- **Reporting:** history and latest snapshot become Excel dashboards.

When changing the project, start at the layer closest to the problem. For
example, asset quantities belong in `current_state.json`; pricing rules belong
in `engine.py`; workbook layout belongs in `build_template.py`.
