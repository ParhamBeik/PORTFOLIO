from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from marketdata.models import ArchiveFetchState, MarketInstrument
from portfolio.models import Asset
from collections import defaultdict

class Command(BaseCommand):
    help = "Run warehouse database integrity and data quality audit report."

    def handle(self, *args, **options):
        self.stdout.write("=" * 80)
        self.stdout.write("      WAREHOUSE DATABASE INTEGRITY & DATA QUALITY AUDIT REPORT")
        self.stdout.write("=" * 80)

        # 1. Load mappings for asset classes and instrument categories
        instrument_map = {}
        for inst in MarketInstrument.objects.all():
            instrument_map[inst.symbol] = {
                'name': inst.name,
                'category': inst.category,
                'source': inst.source,
                'eligible': inst.eligible
            }

        asset_map = {}
        for asset in Asset.objects.all():
            if asset.tse_symbol:
                asset_map[asset.tse_symbol] = asset.asset_class
            if asset.brs_symbol:
                asset_map[asset.brs_symbol] = asset.asset_class

        def get_asset_info(symbol):
            inst = instrument_map.get(symbol, {})
            category = inst.get('category', 'unknown')
            eligible = inst.get('eligible', False)
            source = inst.get('source', 'unknown')
            name = inst.get('name', '')

            asset_class = asset_map.get(symbol)
            if not asset_class:
                if category == 'stock':
                    asset_class = 'Stock'
                elif category == 'gold':
                    asset_class = 'Gold'
                elif symbol == 'CRYPTO' or category == 'crypto':
                    asset_class = 'Crypto'
                elif symbol == 'COMMODITIES':
                    asset_class = 'Commodity'
                elif symbol == 'TEDPIX':
                    asset_class = 'Index'
                else:
                    asset_class = 'Unknown'
            return name, category, asset_class, eligible, source

        # 2. Endpoint Completeness Stats
        self.stdout.write("\n[1] Endpoint Symbols Status Overview:")
        self.stdout.write(f"{'Endpoint':<30} | {'Total':<6} | {'Complete':<8} | {'Pct %':<6} | {'Zero Data':<9} | {'Unattempted':<11} | {'Failed':<6}")
        self.stdout.write("-" * 90)

        endpoints = ArchiveFetchState.objects.values('endpoint').annotate(
            total=Count('id'),
            complete=Count('id', filter=Q(verified_complete=True)),
            zero_data=Count('id', filter=Q(stored_rows=0)),
            unattempted=Count('id', filter=Q(last_attempt_at__isnull=True)),
            failed=Count('id', filter=Q(consecutive_failures__gt=0))
        ).order_by('endpoint')

        for ep in endpoints:
            pct = (ep['complete'] / ep['total'] * 100) if ep['total'] > 0 else 0
            self.stdout.write(f"{ep['endpoint']:<30} | {ep['total']:<6} | {ep['complete']:<8} | {pct:>5.1f}% | {ep['zero_data']:<9} | {ep['unattempted']:<11} | {ep['failed']:<6}")

        # 3. Analyze symbols with no price/candle data
        self.stdout.write("\n[2] Symbols with 0 price rows in main price history endpoints:")
        self.stdout.write("    (Checking stock_history_unadjusted, stock_candle_unadjusted, and gold_daily)")
        price_endpoints = [
            ArchiveFetchState.Endpoint.STOCK_HISTORY_UNADJUSTED,
            ArchiveFetchState.Endpoint.STOCK_CANDLE_UNADJUSTED,
            ArchiveFetchState.Endpoint.GOLD_DAILY
        ]
        
        price_states = ArchiveFetchState.objects.filter(endpoint__in=price_endpoints)
        symbol_price_stats = defaultdict(lambda: {'total_endpoints': 0, 'stored_rows': 0, 'failures': 0, 'errors': set()})
        
        for s in price_states:
            symbol_price_stats[s.symbol]['total_endpoints'] += 1
            symbol_price_stats[s.symbol]['stored_rows'] += s.stored_rows
            symbol_price_stats[s.symbol]['failures'] += s.consecutive_failures
            if s.last_error:
                symbol_price_stats[s.symbol]['errors'].add(s.last_error)

        useless_price_symbols = []
        low_price_symbols = []
        
        for sym, stats in symbol_price_stats.items():
            name, category, asset_class, eligible, source = get_asset_info(sym)
            stats.update({'name': name, 'category': category, 'asset_class': asset_class, 'eligible': eligible, 'source': source})
            
            if stats['stored_rows'] == 0:
                useless_price_symbols.append((sym, stats))
            elif stats['stored_rows'] < 20:
                low_price_symbols.append((sym, stats))

        self.stdout.write(f"  - Total symbols offering prices: {len(symbol_price_stats)}")
        self.stdout.write(f"  - Useless price symbols (0 rows): {len(useless_price_symbols)}")
        self.stdout.write(f"  - Low efficiency price symbols (< 20 rows): {len(low_price_symbols)}")

        if useless_price_symbols:
            self.stdout.write("\n  * Breakdown of Useless Price Symbols by Asset Class:")
            useless_by_class = defaultdict(list)
            for sym, stats in useless_price_symbols:
                useless_by_class[stats['asset_class']].append(sym)
            for asset_class, syms in sorted(useless_by_class.items()):
                self.stdout.write(f"    - {asset_class}: {len(syms)} symbols ({', '.join(syms[:10])} ...)")

        if low_price_symbols:
            self.stdout.write("\n  * Sample Low Efficiency Price Symbols (< 20 rows):")
            for sym, stats in sorted(low_price_symbols, key=lambda x: x[1]['stored_rows'])[:15]:
                self.stdout.write(f"    - {sym} ({stats['asset_class']}): {stats['stored_rows']} stored rows. Eligible: {stats['eligible']}. Errors: {list(stats['errors'])[:2]}")

        # 4. Analyze other endpoints
        self.stdout.write("\n[3] Analysis of secondary endpoints (Ticks, Shareholders, Codal Announcements):")
        secondary_endpoints = [
            ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
            ArchiveFetchState.Endpoint.SHAREHOLDER_RECORDS,
            ArchiveFetchState.Endpoint.CODAL_ANNOUNCEMENTS
        ]
        
        for ep in secondary_endpoints:
            ep_states = ArchiveFetchState.objects.filter(endpoint=ep)
            total_ep = ep_states.count()
            zero_rows = ep_states.filter(stored_rows=0).count()
            unattempted = ep_states.filter(last_attempt_at__isnull=True).count()
            failed = ep_states.filter(consecutive_failures__gt=0)
            
            self.stdout.write(f"\n  - Endpoint: {ep}")
            self.stdout.write(f"    * Total configured symbols: {total_ep}")
            self.stdout.write(f"    * Symbols with 0 data: {zero_rows} ({zero_rows/total_ep*100:.1f}%)")
            self.stdout.write(f"    * Unattempted symbols: {unattempted} ({unattempted/total_ep*100:.1f}%)")
            self.stdout.write(f"    * Active failure rate (failed fetches): {failed.count()} ({failed.count()/total_ep*100:.1f}%)")
            
            if failed.exists():
                self.stdout.write("    * Sample failures & errors:")
                errors = failed.values('last_error').annotate(count=Count('id')).order_by('-count')[:5]
                for err in errors:
                    err_msg = err['last_error'].split('\n')[0][:80] if err['last_error'] else "None"
                    self.stdout.write(f"      - {err['count']} symbols: {err_msg}")

        self.stdout.write("\n" + "=" * 80)
