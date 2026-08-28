"""Seed the asset catalog from the canonical price-map keys.

Idempotent: safe to run on every boot. Mirrors the asset keys produced by
portfolio.live.extractor.
"""
from django.core.management.base import BaseCommand
from portfolio.models import Asset
from portfolio.services.catalog import CATALOG_KEY_PREFIXES

ASSETS = [
    # key, name, fa, class, currency, manual, house, tse_symbol, brs_symbol, proxy_key
    # tse_symbol/brs_symbol are the join keys into the marketdata warehouse
    # (blank = no history source; manual + house assets have none).
    # proxy_key names the asset whose price series stands in for risk purposes
    # when this asset has no series of its own. Real estate has no sane proxy.
    ("emami_coin", "Emami Coin", "سکه امامی", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_EMAMI", ""),
    ("half_coin", "Half Coin", "نیم سکه", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_HALF", ""),
    ("quarter_coin", "Quarter Coin", "ربع سکه", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_QUARTER", ""),
    ("quarter_coin_pre86", "Quarter Coin (Pre-86)", "ربع سکه طرح قدیم", Asset.AssetClass.GOLD, Asset.Currency.IRT, True, False, "", "", "quarter_coin"),
    ("one_gram_coin", "1g Coin", "سکه یک گرمی", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_1G", ""),
    ("swiss_gold_bar_1g", "Swiss Gold Bar (1g)", "شمش سوئیس ۱ گرم", Asset.AssetClass.GOLD, Asset.Currency.IRT, True, False, "", "", "gold_18k_gram"),
    ("swiss_gold_bar_2_5g", "Swiss Gold Bar (2.5g)", "شمش سوئیس ۲.۵ گرم", Asset.AssetClass.GOLD, Asset.Currency.IRT, True, False, "", "", "gold_18k_gram"),
    ("gold_18k_gram", "Gold Gram (18K)", "طلای ۱۸ عیار", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_GOLD_18K", ""),
    ("usd_cash", "US Dollar", "دلار", Asset.AssetClass.CASH, Asset.Currency.USD, False, False, "", "USD", ""),
    ("usdt_irt", "Tether", "تتر", Asset.AssetClass.CASH, Asset.Currency.IRT, False, False, "", "USDT_IRT", ""),
    ("euro_cash", "Euro", "یورو", Asset.AssetClass.CASH, Asset.Currency.IRT, False, False, "", "EUR", ""),
    ("kama_stock", "KAMA Stock", "سهام کاما", Asset.AssetClass.STOCK, Asset.Currency.IRT, False, False, "کاما", "", ""),
    ("house_asset", "Real Estate", "", Asset.AssetClass.REAL_ESTATE, Asset.Currency.IRT, False, True, "", "", ""),
]


class Command(BaseCommand):
    help = "Seed the asset catalog."

    def handle(self, *args, **options):
        created = 0
        for key, name, name_fa, cls, currency, manual, house, tse, brs, proxy in ASSETS:
            asset, was_created = Asset.objects.get_or_create(
                key=key,
                defaults={
                    "name": name,
                    "name_fa": name_fa,
                    "asset_class": cls,
                    "currency": currency,
                    "is_manual": manual,
                    "is_house": house,
                    "is_active": True,
                    "tse_symbol": tse,
                    "brs_symbol": brs,
                    "proxy_key": proxy,
                },
            )
            created += int(was_created)
            # Keep warehouse join keys current on existing rows too (idempotent).
            changes = {
                "name": name,
                "name_fa": name_fa,
                "asset_class": cls,
                "currency": currency,
                "is_manual": manual,
                "is_house": house,
                "is_active": True,
                "tse_symbol": tse,
                "brs_symbol": brs,
                "proxy_key": proxy,
            }
            dirty = [field for field, value in changes.items() if getattr(asset, field) != value]
            if not was_created and dirty:
                for field in dirty:
                    setattr(asset, field, changes[field])
                asset.save(update_fields=dirty)
        # Global catalog only, and only rows this command is actually the
        # authority for. This runs on every container start, so an unscoped
        # sweep switches things off on boot: it once wiped every property, and
        # then every stock the user had added from the market list.
        #
        # Which rows are ours is decided by the KEY, not by whether a row has a
        # provider symbol. Keying on the symbol was the earlier repair and it
        # over-corrected -- it kept the picker's tickers, but it also made every
        # seeded asset that carries a symbol (which is nearly all of them)
        # permanently unretirable, so dropping one from ASSETS silently did
        # nothing. `catalog.CATALOG_KEY_PREFIXES` is what the picker stamps on
        # the rows it mints, and is the honest discriminator.
        sweep = Asset.objects.filter(owner__isnull=True, is_house=False).exclude(
            key__in=[row[0] for row in ASSETS]
        )
        for prefix in CATALOG_KEY_PREFIXES:
            sweep = sweep.exclude(key__startswith=prefix)
        # Never retire something somebody owns or has traded, whatever its key
        # looks like. The picker is not the only minting path -- `trades
        # .provision_asset` creates keys like `khgostar_stock` with no prefix at
        # all -- and enumerating minters is a race this command keeps losing.
        # Being referenced is the durable fact; the key shape is a guess about
        # provenance.
        sweep = sweep.exclude(holdings__isnull=False).exclude(
            transactions__isnull=False
        )
        sweep.update(is_active=False)
        self.stdout.write(self.style.SUCCESS(
            f"Asset catalog ready ({created} new, {len(ASSETS)} total)."
        ))
