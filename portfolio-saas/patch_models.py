import sys

file_path = "backend/marketdata/models.py"
with open(file_path, "r") as f:
    content = f.read()

# For MarketCandle
target1 = """    class Meta:
        ordering = ["-date_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "timeframe", "date_time"],
                name="uniq_market_candle_symbol_tf_dt",
            )
        ]"""
replacement1 = """    class Meta:
        ordering = ["-date_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "timeframe", "date_time"],
                name="uniq_market_candle_symbol_tf_dt",
            )
        ]
        indexes = [
            models.Index(fields=["timeframe", "symbol", "date_time"]),
        ]"""
content = content.replace(target1, replacement1)

# For GoldCurrencyHistory
target2 = """    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date"],
                name="uniq_gold_currency_history_symbol_date",
            )
        ]"""
replacement2 = """    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date"],
                name="uniq_gold_currency_history_symbol_date",
            )
        ]
        indexes = [
            models.Index(fields=["symbol", "date"]),
        ]"""
content = content.replace(target2, replacement2)

with open(file_path, "w") as f:
    f.write(content)

print("Models updated.")
