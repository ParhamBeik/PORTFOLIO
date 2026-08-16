"""Performance/TWR/XIRR honesty gates.

Integration tests at the service boundary (Account/LedgerEntry rows through
account_performance and the ledger service), because the behaviour under test
is exactly the interaction between ledger state and the performance gate --
not something a pure-function unit test can exercise on its own.
"""
import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from portfolio.models import Account, LedgerEntry
from portfolio.services.ledger import create_ledger_entry, reverse_ledger_entry
from portfolio.services.performance import _position_metrics, account_performance
from portfolio.services.timeline import xirr

pytestmark = pytest.mark.django_db


def _cash_account(make_user, email, *, opened_days_ago):
    account = Account.objects.create(user=make_user(email=email), name="Test")
    opened_at = timezone.now() - dt.timedelta(days=opened_days_ago)
    create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="1000000",
        occurred_at=opened_at,
    )
    account.refresh_from_db()
    return account


def test_short_tracking_window_is_gated_as_insufficient_history(make_user):
    account = _cash_account(make_user, "short@test.test", opened_days_ago=10)

    result = account_performance(account)

    assert result["performance_available"] is False
    assert result["reason"] == "insufficient_history"
    assert "xirr" not in result
    assert result["days_tracked"] == 10


def test_buy_entry_with_null_price_never_reports_numeric_unrealized_pnl(
    make_user, asset_catalog
):
    # Written directly (bypassing create_ledger_entry, which validates a BUY
    # price) to reproduce how the real reconstructed ledger rows landed with
    # price_tomans NULL on a non-opening entry.
    account = Account.objects.create(user=make_user(email="nullprice@test.test"), name="Test")
    LedgerEntry.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("2"),
        price_tomans=None,
        amount_tomans=Decimal("0"),
        timestamp=timezone.now() - dt.timedelta(days=200),
    )

    metrics = _position_metrics(account)

    row = metrics["emami_coin"]
    assert row["cost_basis_known"] is False
    assert row["average_cost_tomans"] is None
    assert row["total_cost_basis_tomans"] is None
    assert row["unrealized_pnl_tomans"] is None


def test_reversed_deposit_is_fully_excluded_from_cashflows(make_user):
    account = _cash_account(make_user, "reversal@test.test", opened_days_ago=100)
    deposit = create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.DEPOSIT,
        amount_tomans="500000",
        occurred_at=timezone.now() - dt.timedelta(days=50),
    )
    reverse_ledger_entry(user=account.user, account_id=account.id, entry_id=deposit.id)

    result = account_performance(account)

    assert result["performance_available"] is True
    # Both the deposit and its reversal must drop out, not just the reversal row.
    assert result["external_flow_count"] == 0
    assert abs(result["twr"]) < 1e-9


def test_xirr_non_convergence_returns_none_not_zero():
    d0 = dt.date(2020, 1, 1)
    # An outflow-only stream has no rate that zeroes the NPV: no root exists.
    cashflows = [(d0, Decimal("-1000")), (d0 + dt.timedelta(days=30), Decimal("-500"))]

    assert xirr(cashflows) is None


def test_usdt_denominated_current_value_matches_opening_denomination(make_user):
    from marketdata.models import GoldCurrencyHistory
    from portfolio.services.returns import to_jalali_str

    account = _cash_account(make_user, "usdt@test.test", opened_days_ago=100)
    GoldCurrencyHistory.objects.create(
        symbol="USDT_IRT",
        date=to_jalali_str(timezone.now() - dt.timedelta(days=101)),
        close_price=Decimal("60000"),
    )

    result = account_performance(account, basis="usdt_denominated")

    assert result["performance_available"] is True
    # Regression guard for the ~10^5 bug: _current_value had no usdt_denominated
    # branch, so it returned raw Toman against a USDT-denominated opening value.
    # A flat cash balance in a consistent basis must show a near-zero TWR.
    assert abs(result["twr"]) < 10
