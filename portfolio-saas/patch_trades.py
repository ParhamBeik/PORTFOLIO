import re
file_path = "backend/portfolio/services/trades.py"
with open(file_path, "r") as f:
    content = f.read()

# Remove StaleTradeUndo
content = re.sub(r'class StaleTradeUndo\(TradeError\):\n    """Raised when undoing a trade would rewrite later history for that asset."""\n\n\n', '', content)

# Replace the undo_trade function body parts
old_check = """    latest_id = (
        Transaction.objects.filter(account=trade.account, asset=trade.asset)
        .order_by("-timestamp", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    if latest_id != trade.pk:
        raise StaleTradeUndo("Only the latest trade for this asset can be undone.")"""
        
content = content.replace(old_check, "")

with open(file_path, "w") as f:
    f.write(content)
