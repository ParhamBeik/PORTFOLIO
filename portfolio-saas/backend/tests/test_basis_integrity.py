"""Valuation-basis integrity: real_toman must actually deflate, and CPI gaps
must fail loud rather than silently clamp or masquerade as nominal data.

Unit tests throughout — pure logic in returns.py/deflator.py/settings.py
around a few seeded warehouse rows, no HTTP/view layer involved.
"""
import datetime as dt

import jdatetime
import pytest

import config.settings as settings_module
from marketdata.models import MarketCandle, RejectedRecord
from portfolio.services.deflator import CpiUnavailable, cpi_for_date, to_basis
from portfolio.services.returns import _price_version_fingerprint, daily_returns_matrix

pytestmark = pytest.mark.django_db


def _seed_warehouse_days(symbol: str, n: int, end: dt.date, price: float = 8000.0):
    """n consecutive daily closes at a CONSTANT price, ending on `end` (Gregorian)."""
    rows = []
    for i in range(n):
        day = end - dt.timedelta(days=n - 1 - i)
        jday = jdatetime.date.fromgregorian(date=day)
        date_str = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
        rows.append(MarketCandle(
            symbol=symbol,
            timeframe="1d_adj",
            date_time=date_str,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1000,
        ))
    MarketCandle.objects.bulk_create(rows, ignore_conflicts=True)


@pytest.fixture(autouse=True)
def _known_cpi_table(monkeypatch):
    """Deterministic CPI table regardless of ambient CPI_BY_JALALI_YEAR_EXTRA."""
    monkeypatch.setattr(settings_module, "CPI_BY_JALALI_YEAR", {
        1398: 100.0,
        1399: 136.4,
        1400: 191.2,
        1401: 278.8,
        1402: 392.3,
        1403: 519.8,
        1404: 680.9,
    })


def test_real_toman_diverges_from_nominal_across_a_cpi_change(asset_catalog):
    """Bug 1 regression guard: a flat nominal price must NOT read flat in real_toman.

    Window is anchored (as_of) inside Jalali 1403, spanning back across the
    1402->1403 CPI change (392.3 -> 519.8), with a constant nominal price. If
    real_toman were silently falling through to the nominal matrix (the bug),
    the two would be numerically identical.
    """
    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    as_of = dt.datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)  # Jalali ~1403-03-12
    _seed_warehouse_days("کاما", 220, end=as_of.date())

    nominal_df, _ = daily_returns_matrix(
        as_of=as_of, history_days=200, universe=["kama_stock"], basis="nominal_toman"
    )
    real_df, _ = daily_returns_matrix(
        as_of=as_of, history_days=200, universe=["kama_stock"], basis="real_toman"
    )

    assert "kama_stock" in nominal_df.columns
    assert "kama_stock" in real_df.columns
    nominal = nominal_df["kama_stock"].dropna()
    real = real_df["kama_stock"].dropna()
    assert len(nominal) > 0 and len(real) > 0

    # Flat nominal price -> ~0 nominal daily return every day.
    assert nominal.abs().max() < 1e-9
    # CPI rises every day (even within a year, via linear interpolation), so
    # the same flat nominal price deflates -> strictly negative real return.
    assert real.abs().max() > 1e-9
    assert (real < 0).all()
    assert not nominal.equals(real)


def test_cpi_for_beyond_table_raises_not_clamps():
    """Bug 2: a year past the table is a loud, typed failure, not a clamp to the last value."""
    with pytest.raises(CpiUnavailable) as exc_info:
        settings_module.cpi_for(1405)
    assert exc_info.value.jalali_year == 1405
    assert exc_info.value.last_verified_year == 1404


def test_cpi_for_date_within_known_year_still_works():
    # 2024-06-01 falls in Jalali 1403 (which starts 2024-03-20), interpolating
    # toward the known 1404 value -- must resolve without raising.
    value = cpi_for_date("2024-06-01")  # Jalali ~1403-03-12
    assert 519.8 < value < 680.9


def test_real_toman_unavailable_when_cpi_unknown_does_not_fall_back_to_nominal(asset_catalog):
    """Bug 2: with no CPI for the current Jalali year, real_toman must fail loud,
    never silently return the nominal numbers under the real_toman label."""
    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])
    _seed_warehouse_days("کاما", 60, end=dt.date.today())

    # No as_of -> "now", whose Jalali year (1405) is deliberately absent from
    # the patched table above.
    with pytest.raises(CpiUnavailable):
        daily_returns_matrix(universe=["kama_stock"], basis="real_toman")

    # And the nominal basis must still work -- only real_toman is unavailable.
    nominal_df, _ = daily_returns_matrix(universe=["kama_stock"], basis="nominal_toman")
    assert "kama_stock" in nominal_df.columns


def test_cpi_override_extends_the_table(monkeypatch):
    """The override mechanism (CPI_BY_JALALI_YEAR_EXTRA merges into this exact
    dict at settings load) is picked up immediately by cpi_for."""
    with pytest.raises(CpiUnavailable):
        settings_module.cpi_for(1405)

    monkeypatch.setitem(settings_module.CPI_BY_JALALI_YEAR, 1405, 950.0)

    assert settings_module.cpi_for(1405) == 950.0


def test_fingerprint_rotates_when_rejected_record_added():
    """Bug 4: a new spike rejection must rotate the returns cache key immediately,
    not wait out the 600s TTL."""
    before = _price_version_fingerprint()
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted", symbol="کاما", date="1403-01-01", reason="price_spike"
    )
    after = _price_version_fingerprint()
    assert before != after
