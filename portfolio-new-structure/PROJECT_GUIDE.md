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

- `src/main.py` — orchestrates the full daily pipeline (run this directly).
- `src/fetcher.py` — talks to market APIs and returns raw market payloads.
- `src/engine.py` — normalizes prices and calculates portfolio snapshots.
- `src/exporter.py` — writes current data into dashboard workbook outputs.
- `src/build_template.py` — builds the `.xlsx` v2 dashboard; also holds asset-class config and history analytics (its only consumers).
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

## Template Manual Edits (2026-07-18)

`template/Portfolio_Tracker_v2.xlsx` was hand-edited against the committed
version. Diff method: `git show HEAD:...Portfolio_Tracker_v2.xlsx` (pre-edit)
vs working copy, dumped per sheet with `openpyxl`. Findings:

**Real cell-reference fixes (Dashboard only):**
- `D10`, `D11`: `=IFERROR(B10/B5,0)` → `=IFERROR(B10/A5,0)`. Weight-% was
  dividing by column B (sibling value) instead of column A (total). Same fix
  on both rows.
- `H22`: `=Live_Data!B14` → `=Live_Data!A14`; `I22`: `=Live_Data!C14` →
  `=Live_Data!B14`; plus new `J22: =Live_Data!C14`. The Latest Prices block was
  pulling from the wrong Live_Data columns; corrected to A/B/C.

**Design retuning (all 7 sheets):** per-column width retune; header rows
standardized to Calibri 11 bold white-on-navy `#1F3864`; data cells
center-aligned; date format canonicalized `yyyy-mm-dd` → `yyyy\-mm\-dd`; ARGB
alpha normalized `00xxxxxx` → `FFxxxxxx` (same visible colors — save artifact,
not a restyle). Dashboard right info panel (Latest Date / USD Rate / Latest
Prices) shifted one column right; new column K added.

**False alarm:** Comparison's 14 flagged "formula changes" are the
serialization artifact `XLOOKUP` → `_xludf.XLOOKUP` with byte-identical
arguments — not real fixes. Settings, History_Data, Live_Data, Allocation, and
Trends had **zero** formula changes (design only).

**✅ Fixes ported to the generator (2026-07-18).** The hand-edits above now
live in `build_template.py`, so every `exporter.py` daily export carries them.
Changes made:
- `/B5` → `/A5` weight-% fix applied at both call sites — the owner-split
  share **and** the asset-class IRT share (same root-cause bug; the hand-edit
  only fixed owner-split, leaving allocation shares rendering 0%).
- Dashboard right info panel shifted one column right: Latest Date `G4`→`H4`,
  USD Rate `J4`→`K4`. USD Rate formula simplified
  `XLOOKUP("Tether"…)→=Live_Data!B4` (display-only; B4 = "US Dollar" IRT).
  Whole Latest Prices block shifted `G/H/I`→`H/I/J` (`price_col` 7→8) so the
  block is column-consistent (the hand-edit had only row 22 shifted).
- Design: data cells center-aligned (`_style_table` + Dashboard pass); date
  format `yyyy-mm-dd`→`yyyy\-mm-dd` (locale-safe) incl. Settings B6; column
  widths retuned to the hand-edit values across all sheets.
- Skipped as invisible save-artifacts (Excel normalizes on open, no code
  needed): ARGB alpha `00→FF`, explicit font name `Calibri`, and Comparison's
  `XLOOKUP→_xludf.XLOOKUP` re-serialization.
- `exporter.py` unchanged — it is a thin caller that delegates to
  `build_template.build_workbook`, so all fixes flow through automatically.

Template and `exports/Portfolio_2026-07-18.xlsx` regenerated from the updated
code.
