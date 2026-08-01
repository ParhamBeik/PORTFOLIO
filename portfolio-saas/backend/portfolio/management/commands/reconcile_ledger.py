import sys
from decimal import Decimal
from django.core.management.base import BaseCommand
from portfolio.models import Account, Holding, Transaction

def _q(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")

class Command(BaseCommand):
    help = "Asserts that Holding.quantity matches the sum of ledger transactions per account."

    def handle(self, *args, **options):
        has_drift = False
        accounts = Account.objects.all()

        for account in accounts:
            holdings = {h.asset.id: h for h in account.holdings.select_related("asset")}
            
            transactions = Transaction.objects.filter(account=account).select_related("asset")
            ledger_qty = {}
            for txn in transactions:
                asset_id = txn.asset.id
                ledger_qty.setdefault(asset_id, Decimal("0"))
                if txn.side == Transaction.Side.BUY:
                    ledger_qty[asset_id] += _q(txn.quantity)
                else:
                    ledger_qty[asset_id] -= _q(txn.quantity)
                    
            # Check for drift
            all_asset_ids = set(holdings.keys()).union(set(ledger_qty.keys()))
            
            for asset_id in all_asset_ids:
                h_qty = _q(holdings[asset_id].quantity) if asset_id in holdings else Decimal("0")
                l_qty = ledger_qty.get(asset_id, Decimal("0"))
                
                # Account for float precision by rounding to 6 places as per model max_digits=20, decimal_places=6
                if round(h_qty, 6) != round(l_qty, 6):
                    asset_name = holdings[asset_id].asset.name if asset_id in holdings else transactions.filter(asset_id=asset_id).first().asset.name
                    self.stderr.write(
                        f"Drift detected in Account {account.id} ({account.name}) for Asset {asset_name}: "
                        f"Holding={h_qty}, Ledger={l_qty}"
                    )
                    has_drift = True

        if has_drift:
            self.stderr.write("Reconciliation failed due to ledger drift.")
            sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS("All holdings match ledger perfectly."))
