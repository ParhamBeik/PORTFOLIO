"""Cross-symbol detection: one bad provider day, not eight market moves.

Every other unit check compares a symbol against its own history, so a payload
that corrupts many symbols at once slipped through. On 1405-04-31 six currencies
were written at ~96,000 Toman; the per-symbol test reported four and missed SEK
and CNY entirely, because those two moved only 4.9x and 3.4x.
"""
import pytest

from marketdata.management.commands.audit_warehouse import Command
from marketdata.models import GoldCurrencyHistory

pytestmark = pytest.mark.django_db


def _history(symbol, values, start_day=10):
    for offset, value in enumerate(values):
        GoldCurrencyHistory.objects.create(
            symbol=symbol,
            date=f"1405-04-{start_day + offset:02d}",
            close_price=value,
            unit="تومان",
        )


def test_one_contaminated_day_across_symbols_is_caught():
    bad_day_index = 2
    for symbol, normal in (("AFN", 2874), ("AMD", 458), ("SEK", 19710)):
        values = [normal] * 5
        values[bad_day_index] = 96100
        _history(symbol, values)

    findings = Command().check_same_day_collisions()

    assert {f["symbol"] for f in findings} == {"AFN", "AMD", "SEK"}
    assert {f["date"] for f in findings} == {"1405-04-12"}
    # SEK is the point of the check: a 4.9x step no per-symbol test would flag.
    sek = next(f for f in findings if f["symbol"] == "SEK")
    assert "2 unrelated symbols" in sek["evidence"]


def test_a_single_symbol_spiking_alone_is_not_a_collision():
    """One currency moving is a market event; the check must not claim otherwise."""
    _history("AFN", [2874, 2874, 96100, 2874, 2874])
    _history("AMD", [458] * 5)
    _history("SEK", [19710] * 5)

    assert Command().check_same_day_collisions() == []


def test_steady_inflation_is_not_flagged():
    """A lifetime median made every old row an outlier and produced 326 false
    positives where SAR, QAR and MYR all legitimately traded near 900."""
    for symbol, base in (("SAR", 900), ("QAR", 890), ("MYR", 910)):
        _history(symbol, [base + step * 10 for step in range(5)])

    assert Command().check_same_day_collisions() == []
