"""Deterministic, source-backed observations available to the AI router."""

from decimal import Decimal, ROUND_HALF_UP


def _period_index(point):
    year, month, _ = map(int, point["period_end_jalali"].split("-"))
    return year * 12 + month


def _money(value):
    return f"{Decimal(value):,.0f} million Rial"


def _change(current, previous):
    older = Decimal(previous["value"])
    if older == 0:
        return None
    newer = Decimal(current["value"])
    percent = ((newer / older) - 1) * 100
    return percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_observations(monthly_sales, income=None):
    points = monthly_sales["points"]
    result = {}
    if points:
        result.update(_sales_observations(monthly_sales))
    for scope in ("standalone", "consolidated"):
        scope_points = [point for point in (income or {}).get("points", []) if point["scope"] == scope]
        if not scope_points:
            continue
        latest = max(scope_points, key=lambda point: point["period_end_jalali"])
        period = f"{latest['period_start_jalali']}–{latest['period_end_jalali']}"
        qualifier = f"{scope}, {'audited' if latest['audited'] else 'unaudited'}"
        for key, label, statement in (
            ("revenue", "operating revenue", f"{_money(latest['revenue'])}"),
            ("net_profit", "net profit", f"{_money(latest['net_profit'])}"),
            ("net_margin", "net profit margin", f"{latest['net_margin_pct']}% (net profit / revenue)"),
        ):
            result[f"income_{scope}_{key}"] = {
                "description": f"Latest verified {scope} income statement {label} for this company, with filing period and audit status",
                "statement": f"Latest verified {qualifier} income statement for {period}: {label} {statement}.",
                "sources": [latest],
            }
    return result


def _sales_observations(monthly_sales):
    points = monthly_sales["points"]
    latest = points[-1]
    highest = max(points, key=lambda point: (Decimal(point["value"]), point["period_end_jalali"]))
    lowest = min(points, key=lambda point: (Decimal(point["value"]), point["period_end_jalali"]))
    by_index = {_period_index(point): point for point in points}
    result = {
        "latest": {
            "description": "Latest verified monthly sales in the selected company and 1-year window",
            "statement": f"Latest verified month {latest['period_end_jalali']}: {_money(latest['value'])}.",
            "sources": [latest],
        },
        "highest": {
            "description": "Highest monthly sales among verified months in the 1-year window",
            "statement": f"Highest among verified months: {highest['period_end_jalali']} at {_money(highest['value'])}.",
            "sources": [highest],
        },
        "lowest": {
            "description": "Lowest monthly sales among verified months in the 1-year window",
            "statement": f"Lowest among verified months: {lowest['period_end_jalali']} at {_money(lowest['value'])}.",
            "sources": [lowest],
        },
        "coverage": {
            "description": "How many latest monthly filings have verified sales and how many are withheld",
            "statement": (
                f"{monthly_sales['verified_periods']} of {monthly_sales['latest_filing_periods']} "
                f"latest monthly filings have source-reconciled sales; "
                f"{monthly_sales['withheld_periods']} are withheld."
            ),
            "sources": [],
        },
    }
    prior = by_index.get(_period_index(latest) - 1)
    if prior:
        change = _change(latest, prior)
        if change is not None:
            result["month_change"] = {
                "description": "Latest verified month versus the immediately previous verified calendar month",
                "statement": f"Sales changed {change:+,.2f}% from {prior['period_end_jalali']} to {latest['period_end_jalali']}.",
                "sources": [prior, latest],
            }
    year_ago = by_index.get(_period_index(latest) - 12)
    if year_ago:
        change = _change(latest, year_ago)
        if change is not None:
            result["year_change"] = {
                "description": "Latest verified month versus the same Jalali month one year earlier",
                "statement": f"Sales changed {change:+,.2f}% from {year_ago['period_end_jalali']} to {latest['period_end_jalali']}.",
                "sources": [year_ago, latest],
            }
    last_six = [by_index.get(_period_index(latest) - offset) for offset in range(6)]
    if all(last_six):
        total = sum(Decimal(point["value"]) for point in last_six)
        result["six_month_sum"] = {
            "description": "Sum of six consecutive verified monthly sales filings ending at the latest verified month",
            "statement": (
                f"Six consecutive reported months ending {latest['period_end_jalali']} "
                f"sum to {_money(total)}. This is sales, not profit."
            ),
            "sources": list(reversed(last_six)),
        }
    return result
