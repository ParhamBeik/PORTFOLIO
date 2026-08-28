"""Search the provider catalog and mint a shared Asset when the user picks one.

The seeded Asset table is a handful of well-known instruments. The market
catalog (MarketInstrument) is the real universe. The add-holding wizard searches
here, then `ensure_asset` creates a global Asset so trades/prices can join.
"""
import hashlib
import re

from django.db.models import Q
from rest_framework.exceptions import ValidationError

from marketdata.currency import canonical_symbol
from marketdata.models import MarketInstrument
from portfolio.models import Asset

SEARCH_LIMIT = 40
#: Most of a page that already-owned assets may take, leaving the rest for the
#: market catalog. Without a reservation, owning SEARCH_LIMIT assets in a class
#: made every new instrument in it permanently unreachable from the picker.
ASSET_SHARE = SEARCH_LIMIT // 2

_CLASS_FILTER = {
    Asset.AssetClass.STOCK: Q(
        category=MarketInstrument.Category.STOCK,
        source=MarketInstrument.Source.TSETMC,
        eligible=True,
    ),
    Asset.AssetClass.CRYPTO: Q(
        category=MarketInstrument.Category.CRYPTO,
        source=MarketInstrument.Source.BRS,
        eligible=True,
    ),
    Asset.AssetClass.GOLD: Q(
        category=MarketInstrument.Category.GOLD,
        source=MarketInstrument.Source.BRS,
        eligible=True,
    )
    & ~Q(provider_group="currency"),
    Asset.AssetClass.CASH: Q(
        source=MarketInstrument.Source.BRS,
        eligible=True,
    )
    & (Q(provider_group="currency") | Q(symbol__in=("USDT", "USDT_IRT", "USD", "EUR"))),
}


def _class_for(inst: MarketInstrument) -> str:
    if inst.category == MarketInstrument.Category.STOCK:
        return Asset.AssetClass.STOCK
    if inst.category == MarketInstrument.Category.CRYPTO:
        return Asset.AssetClass.CRYPTO
    if inst.provider_group == "currency" or inst.symbol in ("USDT", "USDT_IRT", "USD", "EUR"):
        return Asset.AssetClass.CASH
    if inst.category == MarketInstrument.Category.GOLD:
        return Asset.AssetClass.GOLD
    raise ValidationError("This instrument cannot be added to a portfolio.")


def _key_for(inst: MarketInstrument) -> str:
    isin = (inst.isin or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{5,32}", isin):
        prefix = "tse" if inst.source == MarketInstrument.Source.TSETMC else "brs"
        return f"{prefix}-{isin.lower()}"[:64]
    if re.fullmatch(r"[A-Za-z0-9_]+", inst.symbol or ""):
        prefix = "tse" if inst.source == MarketInstrument.Source.TSETMC else "brs"
        return f"{prefix}-{inst.symbol.lower()}"[:64]
    digest = hashlib.sha1(f"{inst.source}:{inst.symbol}".encode("utf-8")).hexdigest()[:16]
    prefix = "tse" if inst.source == MarketInstrument.Source.TSETMC else "brs"
    return f"{prefix}-{digest}"


def _row_from_asset(asset: Asset) -> dict:
    return {
        "key": asset.key,
        "source": (
            MarketInstrument.Source.TSETMC
            if asset.tse_symbol
            else MarketInstrument.Source.BRS if asset.brs_symbol else ""
        ),
        "symbol": asset.tse_symbol or asset.brs_symbol or "",
        # The unit discriminator, not just a join key: non-empty means Rial.
        "tse_symbol": asset.tse_symbol,
        "name": asset.name,
        "name_fa": asset.name_fa,
        "asset_class": asset.asset_class,
        "currency": asset.currency,
        "is_manual": asset.is_manual,
        "is_house": asset.is_house,
        "is_active": asset.is_active,
    }


def _row_from_instrument(inst: MarketInstrument, asset: Asset | None) -> dict:
    if asset is not None:
        return _row_from_asset(asset)
    return {
        "key": None,
        "source": inst.source,
        "symbol": inst.symbol,
        "tse_symbol": (
            inst.symbol if inst.source == MarketInstrument.Source.TSETMC else ""
        ),
        "name": inst.name or inst.symbol,
        "name_fa": inst.name or inst.symbol,
        "asset_class": _class_for(inst),
        "currency": Asset.Currency.IRT,
        "is_manual": False,
        "is_house": False,
        "is_active": True,
    }


def _matching_assets(asset_class: str, q: str, user):
    qs = Asset.objects.filter(is_active=True, asset_class=asset_class).filter(
        Q(owner__isnull=True) | Q(owner=user)
    )
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(name_fa__icontains=q)
            | Q(key__icontains=q)
            | Q(tse_symbol__icontains=q)
            | Q(brs_symbol__icontains=q)
        )
    return list(qs.order_by("name")[:SEARCH_LIMIT])


def search_catalog(*, asset_class: str, q: str = "", user) -> list[dict]:
    """Return up to SEARCH_LIMIT rows: existing assets first, then catalog hits.

    Existing assets are capped at ASSET_SHARE rather than SEARCH_LIMIT so the
    market catalog always has room. Filling the whole page with owned assets
    first meant that once a class held SEARCH_LIMIT of them, the loop below hit
    its break before emitting a single instrument and no new ticker could ever
    be found again.
    """
    q = (q or "").strip()
    if asset_class not in _CLASS_FILTER:
        return []

    assets = _matching_assets(asset_class, q, user)[:ASSET_SHARE]
    seen_symbols = {
        (a.tse_symbol or a.brs_symbol)
        for a in assets
        if a.tse_symbol or a.brs_symbol
    }
    seen_keys = {a.key for a in assets}
    rows = [_row_from_asset(a) for a in assets]

    inst_qs = MarketInstrument.objects.filter(_CLASS_FILTER[asset_class])
    if q:
        inst_qs = inst_qs.filter(Q(symbol__icontains=q) | Q(name__icontains=q))
    instruments = list(inst_qs.order_by("symbol")[:SEARCH_LIMIT])

    symbols = [i.symbol for i in instruments]
    existing_by_tse = {
        a.tse_symbol: a
        for a in Asset.objects.filter(tse_symbol__in=symbols, is_active=True)
    }
    existing_by_brs = {
        a.brs_symbol: a
        for a in Asset.objects.filter(brs_symbol__in=symbols, is_active=True)
    }

    for inst in instruments:
        if inst.symbol in seen_symbols:
            continue
        asset = (
            existing_by_tse.get(inst.symbol)
            if inst.source == MarketInstrument.Source.TSETMC
            else existing_by_brs.get(inst.symbol)
        )
        if asset is not None and asset.key in seen_keys:
            continue
        rows.append(_row_from_instrument(inst, asset))
        seen_symbols.add(inst.symbol)
        if asset is not None:
            seen_keys.add(asset.key)
        if len(rows) >= SEARCH_LIMIT:
            break
    return rows[:SEARCH_LIMIT]


def ensure_asset(*, source: str, symbol: str) -> Asset:
    """Return the shared Asset for an eligible catalog row, creating it if needed."""
    symbol = (symbol or "").strip()
    source = (source or "").strip()
    if not symbol or source not in MarketInstrument.Source.values:
        raise ValidationError("Choose an instrument from the catalog.")
    inst = MarketInstrument.objects.filter(
        source=source, symbol=symbol, eligible=True
    ).first()
    if inst is None:
        raise ValidationError("That instrument is not in the eligible catalog.")
    # Store the symbol the warehouse actually WRITES under. Ingest canonicalizes
    # on write (USDT -> USDT_IRT), but the gold/currency branch of the catalog
    # sync leaves the raw form eligible, so both appear as pickable rows. Keying
    # the asset on the raw one means every history read misses -- the seeded
    # `usdt_irt` is not found, a duplicate `brs-usdt` is minted, it has no
    # GoldCurrencyHistory rows, and the spike guard values it at 1 Toman.
    if inst.source == MarketInstrument.Source.TSETMC:
        stored_symbol = inst.symbol
        existing = Asset.objects.filter(tse_symbol=stored_symbol).first()
    else:
        stored_symbol = canonical_symbol(inst.symbol)
        existing = Asset.objects.filter(brs_symbol=stored_symbol).first()
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=["is_active"])
        return existing

    asset_class = _class_for(inst)
    display = (inst.name or inst.symbol)[:120]
    fields = {
        "name": inst.symbol[:120] if asset_class == Asset.AssetClass.STOCK else display,
        "name_fa": display,
        "asset_class": asset_class,
        "currency": Asset.Currency.IRT,
        "is_active": True,
        "owner": None,
    }
    if inst.source == MarketInstrument.Source.TSETMC:
        fields["tse_symbol"] = stored_symbol
    else:
        fields["brs_symbol"] = stored_symbol
    # get_or_create, not create: the check above is a separate statement from
    # the write, and the picker fires this straight from a click. Two clicks --
    # or two users adding the same ticker -- both saw `existing is None` and the
    # second died on the unique `key` as a 500 on the add-holding screen.
    asset, _created = Asset.objects.get_or_create(key=_key_for(inst), defaults=fields)
    return asset
