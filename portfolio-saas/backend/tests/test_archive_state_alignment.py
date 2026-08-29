import pytest
from marketdata.archive import ensure_archive_states
from marketdata.models import ArchiveFetchState, MarketInstrument
from marketdata.tasks import tracked_brs_symbols
from portfolio.models import Asset


@pytest.mark.django_db
def test_tracked_brs_symbols_excludes_crypto_and_commodities(db):
    """tracked_brs_symbols should only return gold and currency instruments, ignoring crypto/commodities."""
    # Create instruments in various categories
    MarketInstrument.objects.create(
        source="brs",
        symbol="IR_COIN_EMAMI",
        name="سکه امامی",
        category="gold",
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="USD",
        name="دلار",
        category="currency",
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="AI Analysis Token",
        name="ای‌آی آنالیز توکن",
        category="crypto",
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="GOLD_OUNCE_GLOBAL",
        name="انس جهانی",
        category="commodity",
        eligible=True,
    )

    symbols = tracked_brs_symbols()
    assert "IR_COIN_EMAMI" in symbols
    assert "USD" in symbols
    assert "AI Analysis Token" not in symbols
    assert "GOLD_OUNCE_GLOBAL" not in symbols


@pytest.mark.django_db
def test_ensure_archive_states_does_not_assign_crypto_to_gold_daily(db):
    """ensure_archive_states should not create gold_daily fetch states for crypto tokens."""
    MarketInstrument.objects.create(
        source="brs",
        symbol="IR_COIN_HALF",
        name="نیم سکه",
        category="gold",
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="ARCS",
        name="ارکس",
        category="crypto",
        eligible=True,
    )

    # Calling ensure_archive_states() with defaults
    ensure_archive_states()

    # Verify that IR_COIN_HALF has a gold_daily state
    assert ArchiveFetchState.objects.filter(
        endpoint=ArchiveFetchState.Endpoint.GOLD_DAILY, symbol="IR_COIN_HALF"
    ).exists()

    # Verify that ARCS does NOT have a gold_daily state
    assert not ArchiveFetchState.objects.filter(
        endpoint=ArchiveFetchState.Endpoint.GOLD_DAILY, symbol="ARCS"
    ).exists()
