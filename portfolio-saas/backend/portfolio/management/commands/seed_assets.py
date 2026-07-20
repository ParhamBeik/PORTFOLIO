"""Seed the asset catalog from the canonical price-map keys.

Idempotent: safe to run on every boot. Mirrors the asset keys produced by
portfolio.live.extractor and the classes in PORTFOLIO NEW STRUCTURE/src/asset_classes.py.
"""
from django.core.management.base import BaseCommand
from portfolio.models import Asset

ASSETS = [
    # key, name, fa, class, currency, manual, house, tse_symbol, brs_symbol
    # tse_symbol/brs_symbol are the join keys into the marketdata warehouse
    # (blank = no history source; manual + house assets have none).
    ("emami_coin", "Emami Coin", "سکه امامی", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_EMAMI"),
    ("half_coin", "Half Coin", "نیم سکه", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_HALF"),
    ("quarter_coin", "Quarter Coin", "ربع سکه", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_QUARTER"),
    ("quarter_coin_pre86", "Quarter Coin (Pre-86)", "ربع سکه قبل ۸۶", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", ""),
    ("one_gram_coin", "1g Coin", "سکه یک گرمی", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_COIN_1G"),
    ("swiss_gold_bar_1g", "Swiss Gold Bar (1g)", "شمش طلا ۱ گرمی", Asset.AssetClass.GOLD, Asset.Currency.IRT, True, False, "", ""),
    ("swiss_gold_bar_2_5g", "Swiss Gold Bar (2.5g)", "شمش طلا ۲.۵ گرمی", Asset.AssetClass.GOLD, Asset.Currency.IRT, True, False, "", ""),
    ("gold_18k_gram", "Gold Gram (18K)", "طلای ۱۸ عیار", Asset.AssetClass.GOLD, Asset.Currency.IRT, False, False, "", "IR_GOLD_18K"),
    ("usd_cash", "US Dollar", "دلار", Asset.AssetClass.CASH, Asset.Currency.USD, False, False, "", "USD"),
    ("usdt_irt", "Tether", "تتر", Asset.AssetClass.CASH, Asset.Currency.IRT, False, False, "", "USDT_IRT"),
    ("euro_cash", "Euro", "یورو", Asset.AssetClass.CASH, Asset.Currency.IRT, False, False, "", "EUR"),
    ("kama_stock", "KAMA Stock", "سهام کما", Asset.AssetClass.STOCK, Asset.Currency.IRT, False, False, "کاما", ""),
    ("bitcoin_usd", "Bitcoin", "بیت‌کوین", Asset.AssetClass.CRYPTO, Asset.Currency.USD, False, False, "", "BTC"),
    ("house_asset", "Real Estate", "ملک", Asset.AssetClass.REAL_ESTATE, Asset.Currency.IRT, False, True, "", ""),
]


class Command(BaseCommand):
    help = "Seed the asset catalog."

    def handle(self, *args, **options):
        created = 0
        for key, name, name_fa, cls, currency, manual, house, tse, brs in ASSETS:
            asset, was_created = Asset.objects.get_or_create(
                key=key,
                defaults={
                    "name": name,
                    "name_fa": name_fa,
                    "asset_class": cls,
                    "currency": currency,
                    "is_manual": manual,
                    "is_house": house,
                    "tse_symbol": tse,
                    "brs_symbol": brs,
                },
            )
            created += int(was_created)
            # Keep warehouse join keys current on existing rows too (idempotent).
            if not was_created and (asset.tse_symbol != tse or asset.brs_symbol != brs):
                asset.tse_symbol = tse
                asset.brs_symbol = brs
                asset.save(update_fields=["tse_symbol", "brs_symbol"])
        self.stdout.write(self.style.SUCCESS(
            f"Asset catalog ready ({created} new, {len(ASSETS)} total)."
        ))
