"""Detected TSE capital increases, offered to the holder as ledger entries.

The warehouse finds a free-share event as a step in the adjusted/unadjusted
price ratio confirmed by a Codal announcement (`marketdata.CorporateAction`).
For a bonus issue or a split that step IS the share multiplier: 1,000 shares
through a factor of 1.29 become 1,290. Nothing is booked on the user's behalf:
the factor can also carry a same-day dividend, and a paid rights issue is not
free shares at all, so the user confirms, and may correct, the quantity.

Suggestions are computed on read, never stored. The warehouse forgets actions
its candles stop showing, and a stored suggestion would outlive that.
"""
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from marketdata import jalali
from marketdata.models import CorporateAction

from ..models import CorporateActionDismissal, LedgerEntry
from .ledger import LedgerError, active_entries, create_ledger_entry
from .timeline import holdings_as_of

FREE_SHARE_KINDS = (CorporateAction.Kind.CAPITAL_INCREASE, CorporateAction.Kind.SPLIT)
# Registering the new shares takes weeks, so a hand-entered bonus can land well
# after the ex-date. One inside this window means the user already booked it.
MANUAL_ENTRY_WINDOW = (timedelta(days=7), timedelta(days=90))


def external_id(symbol: str, date: str) -> str:
    return f"ca:{symbol}:{date}"


def _ex_datetime(date: str):
    return jalali.to_datetime(date)


def pending_suggestions(user) -> list[dict]:
    rows = []
    for account in user.accounts.all():
        holdings = [
            h for h in account.holdings.select_related("asset")
            if h.asset.tse_symbol and not h.asset.is_house
        ]
        if not holdings:
            continue
        symbols = {h.asset.tse_symbol for h in holdings}
        actions = CorporateAction.objects.filter(
            symbol__in=symbols, kind__in=FREE_SHARE_KINDS, factor__gt=1,
        ).order_by("date")
        if not actions:
            continue
        dismissed = set(
            account.corporate_action_dismissals.values_list("symbol", "date")
        )
        booked = set(
            LedgerEntry.all_objects.filter(account=account).exclude(external_id="").values_list("external_id", flat=True)
        )
        manual_issues = [
            e for e in active_entries(account, kinds=[LedgerEntry.Kind.RIGHTS_ISSUE])
        ]
        by_symbol = {h.asset.tse_symbol: h.asset for h in holdings}
        for action in actions:
            if (action.symbol, action.date) in dismissed:
                continue
            if external_id(action.symbol, action.date) in booked:
                continue
            ex = _ex_datetime(action.date)
            if ex is None:
                continue
            asset = by_symbol[action.symbol]
            before, after = MANUAL_ENTRY_WINDOW
            if any(
                e.asset_id == asset.id and ex - before <= e.timestamp <= ex + after
                for e in manual_issues
            ):
                continue
            held = holdings_as_of(user, account, ex - timedelta(microseconds=1)).get(
                asset.key, Decimal("0")
            )
            if held <= 0:
                continue
            added = (held * (action.factor - 1)).quantize(Decimal("1"), rounding=ROUND_DOWN)
            if added <= 0:
                continue
            rows.append({
                "account_id": account.id,
                "account_name": account.name,
                "asset_key": asset.key,
                "symbol": action.symbol,
                "date": action.date,
                "factor": str(action.factor),
                "held_quantity": str(held),
                "suggested_quantity": str(added),
            })
    return rows


def _pending(user, account, symbol: str, date: str) -> dict:
    for row in pending_suggestions(user):
        if row["account_id"] == account.id and row["symbol"] == symbol and row["date"] == date:
            return row
    raise LedgerError("No pending capital increase for that symbol and date.")


def accept_suggestion(*, user, account, symbol: str, date: str, quantity=None) -> LedgerEntry:
    row = _pending(user, account, symbol, date)
    asset = account.holdings.select_related("asset").get(asset__key=row["asset_key"]).asset
    return create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.RIGHTS_ISSUE,
        asset=asset,
        quantity=quantity if quantity is not None else row["suggested_quantity"],
        occurred_at=_ex_datetime(date),
        source="system",
        external_id=external_id(symbol, date),
        note=f"Capital increase {symbol} {date} (factor {row['factor']})",
    )


def dismiss_suggestion(*, user, account, symbol: str, date: str) -> None:
    _pending(user, account, symbol, date)
    CorporateActionDismissal.objects.get_or_create(account=account, symbol=symbol, date=date)
