import pytest
from marketdata.archive import ensure_archive_states
from marketdata.models import ArchiveFetchState, MarketInstrument
from marketdata.tasks import tracked_brs_symbols


@pytest.mark.django_db
def test_tracked_brs_symbols_excludes_crypto_and_commodities(db):
    """tracked_brs_symbols should only return gold and currency instruments, ignoring crypto/commodities.

    Currency is spelled the way `sync_provider_catalog` actually spells it: the
    GOLD category with a "currency" provider_group. Asserting on a category of
    "currency" passes while testing nothing -- no such category exists, so the
    row it builds could never come out of the real catalog, and the filter it is
    meant to prove could drop every USD/EUR row and still be green.
    """
    # Create instruments in various categories
    MarketInstrument.objects.create(
        source="brs",
        symbol="IR_COIN_EMAMI",
        name="سکه امامی",
        category=MarketInstrument.Category.GOLD,
        provider_group="gold",
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="USD",
        name="دلار",
        category=MarketInstrument.Category.GOLD,
        provider_group="currency",
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="AI Analysis Token",
        name="ای‌آی آنالیز توکن",
        category=MarketInstrument.Category.CRYPTO,
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="GOLD_OUNCE_GLOBAL",
        name="انس جهانی",
        category=MarketInstrument.Category.COMMODITY,
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
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )
    MarketInstrument.objects.create(
        source="brs",
        symbol="ARCS",
        name="ارکس",
        category=MarketInstrument.Category.CRYPTO,
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
