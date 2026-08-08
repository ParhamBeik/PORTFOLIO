from decimal import Decimal

import pytest

from marketdata.management.commands.recover_rejected_records import classify
from marketdata.models import RealLegalHistory, RejectedRecord

pytestmark = pytest.mark.django_db


def _row(buy, sell):
    return RejectedRecord.objects.create(
        endpoint="real_legal_history",
        symbol=f"S{buy}{sell}",
        date="1404-01-01",
        reason="buy_sell_volume_mismatch",
        payload={"Buy_I_Volume": buy, "Buy_N_Volume": 0, "Sell_I_Volume": sell, "Sell_N_Volume": 0},
    )


def test_one_percent_reconciliation_boundary_is_recoverable():
    disposition, mismatch, destination = classify(_row(100, 99))
    assert disposition == "recoverable"
    assert Decimal(str(mismatch)) <= Decimal("0.01")
    assert destination == "RealLegalHistory"


def test_material_mismatch_stays_quarantined():
    disposition, _mismatch, destination = classify(_row(100, 98))
    assert disposition == "quarantined"
    assert destination == ""


def test_existing_real_legal_row_is_salvaged_evidence():
    row = _row(100, 99)
    RealLegalHistory.objects.create(symbol=row.symbol, date=row.date)
    assert classify(row)[0] == "salvaged_evidence"
