"""Pro-tier financial insights.

These run over a user's live valuation. They are intentionally simple and
explainable (no opaque ML) — the point of v1 is trustworthy, auditable
suggestions. Each insight returns a severity, a message, and the numbers behind
it so the frontend can render it consistently.
"""
from decimal import Decimal

from portfolio.models import Snapshot
from portfolio.services import value_account, value_user

# A healthy portfolio keeps any single liquid asset below this share.
CONCENTRATION_THRESHOLD = Decimal("0.40")
# Suggested gold-class band (free allocation heuristic).
GOLD_BAND = (Decimal("0.20"), Decimal("0.50"))


def _liquid_items(valuation: dict) -> list:
    # value_user() returns {'accounts': [...]}; value_account() returns {'items': [...]}
    # with no 'accounts' key. Tolerate both so the same helpers serve per-account
    # and whole-user scopes.
    accounts = valuation.get("accounts") or [{"items": valuation.get("items", [])}]
    items = [i for acct in accounts for i in acct["items"]]
    return [i for i in items if i["class"] != "Real Estate"]


def _total(items: list) -> Decimal:
    return sum((i["value"] for i in items), Decimal("0"))


def allocation_breakdown(valuation: dict) -> dict:
    """Share of liquid net worth by asset class."""
    items = _liquid_items(valuation)
    total = _total(items) or Decimal("1")
    by_class: dict[str, Decimal] = {}
    for item in items:
        by_class[item["class"]] = by_class.get(item["class"], Decimal("0")) + item["value"]
    return {cls: round(float(v / total) * 100, 1) for cls, v in by_class.items()}


def concentration_risk(valuation: dict) -> dict:
    """Flag when one asset dominates the liquid portfolio."""
    items = _liquid_items(valuation)
    total = _total(items) or Decimal("1")
    if not items:
        return {"severity": "info", "share": 0, "message": "No holdings to assess."}
    top = max(items, key=lambda i: i["value"])
    share = float(top["value"] / total)
    severity = (
        "high" if share >= float(CONCENTRATION_THRESHOLD) + 0.2
        else "warning" if share >= float(CONCENTRATION_THRESHOLD)
        else "ok"
    )
    return {
        "severity": severity,
        "asset": top["asset"],
        "share": round(share * 100, 1),
        "message": (
            f"{top['asset']} is {round(share*100,1)}% of your liquid portfolio."
            + (" Consider rebalancing." if severity != "ok" else "")
        ),
    }


def gold_band_suggestion(valuation: dict) -> dict:
    """Suggest adding/reducing gold exposure toward the target band."""
    allocation = allocation_breakdown(valuation)
    gold = Decimal(str(allocation.get("Gold", 0)))
    low, high = GOLD_BAND
    if gold < low * 100:
        return {"severity": "info", "message": (
            f"Gold is {gold}% of your portfolio; target is {int(low*100)}-{int(high*100)}%. "
            "Consider adding gold to hedge currency risk."
        )}
    if gold > high * 100:
        return {"severity": "warning", "message": (
            f"Gold is {gold}% of your portfolio; above the {int(high*100)}% band. "
            "Consider diversifying into other asset classes."
        )}
    return {"severity": "ok", "message": f"Gold allocation ({gold}%) is within target band."}


def net_worth_trend(user, account=None, days: int = 7) -> dict:
    """Net worth change over the last N snapshot days.

    `account=None` reads the user-total series (account=None rows); passing an
    account reads that account's per-account snapshot series.
    """
    snaps = user.snapshots
    if account is not None:
        snaps = snaps.filter(account=account)
    else:
        snaps = snaps.filter(account=None)
    recent = list(snaps.order_by("-timestamp")[: days * 4])  # up to a few per day
    if len(recent) < 2:
        return {"severity": "info", "message": "Not enough history yet.", "delta_pct": 0}
    newest, oldest = recent[0], recent[-1]
    old = oldest.total_value_tomans or Decimal("0")
    new = newest.total_value_tomans or Decimal("0")
    if old <= 0:
        return {"severity": "info", "message": "Baseline still being built.", "delta_pct": 0}
    delta_pct = round(float((new - old) / old) * 100, 2)
    return {
        "severity": "ok" if delta_pct >= 0 else "warning",
        "delta_pct": delta_pct,
        "message": f"Net worth {'up' if delta_pct >= 0 else 'down'} {abs(delta_pct)}% over recent history.",
    }


from portfolio.services.valuation import get_latest_prices


def build_insights(user, account=None) -> dict:
    """Run all insights for a user. Only callable by PRO users (IsPro gate).

    `account=None` analyzes the whole-user portfolio; passing an account scopes
    every insight to that single portfolio.
    """
    valuation = value_account(account) if account is not None else value_user(user)
    prices = valuation.get("prices") if isinstance(valuation.get("prices"), dict) else get_latest_prices()
    usd_rate = Decimal(str(prices.get("usd_cash", 0) or 0))
    total_usd = (valuation["total"] / usd_rate) if usd_rate > 0 else Decimal("0")
    return {
        "valuation": {
            "total": valuation["total"],
            "total_usd": total_usd,
        },
        "allocation": allocation_breakdown(valuation),
        "concentration": concentration_risk(valuation),
        "gold_band": gold_band_suggestion(valuation),
        "net_worth_trend": net_worth_trend(user, account),
    }

