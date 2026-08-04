"""Join db_facts.json + provider_facts.json into comparisons.csv.

Pure local transform -- no database, no network, no credentials. Run after both
extraction scripts:

    python3 build_comparisons.py

Every row states what was compared and why the verdict is what it is. A row is
only MATCH when a provider value actually existed to compare against; absence of
a provider value is NOT VERIFIED, never MATCH.
"""
import csv
import json
import pathlib

HERE = pathlib.Path(__file__).parent
DB = json.loads((HERE / "db_facts.json").read_text(encoding="utf-8"))
PROV = json.loads((HERE / "provider_facts.json").read_text(encoding="utf-8"))
SECONDARY = json.loads((HERE / "secondary_sources.json").read_text(encoding="utf-8"))

# Stored symbol <- provider request symbol. The provider answers a USDT request
# with rows belonging under USDT_IRT.
PROVIDER_TO_STORED = {"USDT": "USDT_IRT"}

# Decimal storage is exact and no rounding is applied on either side, so for a
# settled historical day any non-zero difference is a real difference.
# The band exists only to absorb float division noise in the ratio itself.
TOLERANCE_PCT = 0.0001

# The most recent stored day is still being revised by the provider: the archive
# holds the value captured at ingest time while a re-fetch returns the quote as
# of now. A sub-1% gap on that one day is market drift, not a storage defect --
# but on any older, settled day the same gap would be a real discrepancy.
LATEST_STORED_DAY = "1405-05-12"
SAME_DAY_DRIFT_PCT = 0.01


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def verdict(stored, provider, note, *, date=None, expect_ratio=1.0):
    """Classify one comparison.

    `expect_ratio` is the ratio the ingest pipeline is SUPPOSED to produce
    (1.0 when no conversion applies, 0.1 for a Rial->Toman division). `note`
    explains that expected transformation.
    """
    if stored is None and provider is None:
        return "NOT VERIFIED", "neither side has a value for this date"
    if provider is None:
        return "NOT VERIFIED", "provider returned no row for this date"
    if stored is None:
        return "UNEXPLAINED DIFFERENCE", "provider has a row but nothing is stored"
    if provider == 0:
        return "NOT VERIFIED", "provider value is zero; ratio undefined"

    ratio = stored / provider
    hit = abs(ratio / expect_ratio - 1.0)
    if hit <= TOLERANCE_PCT:
        if expect_ratio == 1.0:
            return "MATCH", note or "stored equals provider"
        return "EXPLAINED DIFFERENCE", note
    if date == LATEST_STORED_DAY and hit <= SAME_DAY_DRIFT_PCT:
        return (
            "EXPLAINED DIFFERENCE",
            f"{(hit * 100):.3f}% gap on the newest stored day: the archived close was "
            "captured at ingest time, the re-fetch returns the current quote "
            f"(within the {SAME_DAY_DRIFT_PCT:.0%} same-day drift band)"
            + (f"; {note}" if note else ""),
        )
    # Landing on 0.1 when 1.0 was expected (or vice versa) is a unit defect.
    if abs(ratio - 0.1) <= TOLERANCE_PCT and expect_ratio == 1.0:
        return (
            "UNEXPLAINED DIFFERENCE",
            "stored is exactly 1/10 of the provider value even though the "
            "provider already quotes this series in Toman — an unwarranted division",
        )
    if abs(ratio - 1.0) <= TOLERANCE_PCT and expect_ratio == 0.1:
        return (
            "UNEXPLAINED DIFFERENCE",
            "provider quotes Rial but the stored value was NOT divided by 10",
        )
    return "UNEXPLAINED DIFFERENCE", "no unit or conversion explains this gap"


rows = []

# ----------------------------------------------------------------- TSE equities
for symbol, spec in PROV["tse"].items():
    for date, prec in spec["dates"].items():
        stored_block = DB["tse"].get(symbol, {}).get(date, {})
        for label, prov_key in (
            ("unadjusted", "candle_unadjusted"),
            ("adjusted", "candle_adjusted"),
        ):
            provider = num((prec.get(prov_key) or {}).get("close"))
            stored = num((stored_block.get(label) or {}).get("close"))
            res, why = verdict(stored, provider, "", date=date)
            rows.append(
                {
                    "instrument": f"{symbol} ({label})",
                    "asset_class": "TSE equity",
                    "date_jalali": date,
                    "stored_value": stored,
                    "provider_value": provider,
                    "secondary_value": SECONDARY.get("tse", {}).get(symbol, {}).get("value"),
                    "currency_unit": "provider declares no unit; see REPORT.md finding F1",
                    "conversion_applied": "none",
                    "abs_diff": (
                        abs(stored - provider)
                        if stored is not None and provider is not None
                        else None
                    ),
                    "pct_diff": (
                        round((stored - provider) / provider * 100, 6)
                        if stored is not None and provider not in (None, 0)
                        else None
                    ),
                    "tolerance_pct": TOLERANCE_PCT * 100,
                    "result": res,
                    "explanation": why,
                }
            )

# ------------------------------------------------- gold / currency / USDT / XAU
for prov_symbol, spec in PROV["brs"].items():
    stored_symbol = PROVIDER_TO_STORED.get(prov_symbol, prov_symbol)
    unit = spec["provider_metadata"]["provider_unit"]
    is_rial = str(unit).strip() in ("ریال", "rial", "IRR", "irr")
    for date, prec in spec["dates"].items():
        provider = num((prec or {}).get("close"))
        block = DB["brs"].get(stored_symbol, {}).get(date, {})
        stored_rows = block.get("rows") or []
        stored = num(stored_rows[0]["close"]) if stored_rows else None
        stored_unit = stored_rows[0]["unit"] if stored_rows else None

        if is_rial:
            note = "provider unit is ریال (Rial); ingest divides by 10 to Toman"
            conversion = "Rial -> Toman (/10)"
        else:
            note = ""
            conversion = "none (provider already quotes Toman)"

        res, why = verdict(
            stored, provider, note, date=date, expect_ratio=0.1 if is_rial else 1.0
        )

        rows.append(
            {
                "instrument": stored_symbol,
                "asset_class": SECONDARY.get("asset_class", {}).get(stored_symbol, "gold/currency"),
                "date_jalali": date,
                "stored_value": stored,
                "provider_value": provider,
                "secondary_value": SECONDARY.get("brs", {}).get(stored_symbol, {}).get("value"),
                "currency_unit": f"provider={unit} / stored={stored_unit}",
                "conversion_applied": conversion,
                "abs_diff": (
                    abs(stored - provider)
                    if stored is not None and provider is not None
                    else None
                ),
                "pct_diff": (
                    round((stored - provider) / provider * 100, 6)
                    if stored is not None and provider not in (None, 0)
                    else None
                ),
                "tolerance_pct": TOLERANCE_PCT * 100,
                "result": res,
                "explanation": why,
            }
        )

FIELDS = [
    "instrument",
    "asset_class",
    "date_jalali",
    "stored_value",
    "provider_value",
    "secondary_value",
    "currency_unit",
    "conversion_applied",
    "abs_diff",
    "pct_diff",
    "tolerance_pct",
    "result",
    "explanation",
]

out = HERE / "comparisons.csv"
with out.open("w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)

tally = {}
for r in rows:
    tally[r["result"]] = tally.get(r["result"], 0) + 1
print(f"wrote {out} ({len(rows)} rows)")
for k in sorted(tally):
    print(f"  {k}: {tally[k]}")
