"""Market-data warehouse: global TSE/gold/currency history, keyed by symbol.

This app is a separate bounded context from `portfolio`:
- No foreign keys to user-land (User, Asset, Account). Rows key on TSE symbol
  strings and source-native Jalali date strings ("1403-10-19").
- Written in daily batches by the backfill/sync tasks, read in range scans by
  the analytics layer and the /api/market/* endpoints.
- Dependency direction: `portfolio` may import from `marketdata` (analytics
  reading history); `marketdata` never imports from `portfolio`.

Idempotency: every append-only table carries a natural-key UniqueConstraint so
re-running a backfill is free (`bulk_create(ignore_conflicts=True)`).
"""
from django.db import models


class ApiRequestQuota(models.Model):
    """Persistent provider-call counter shared by every worker and endpoint."""

    day = models.DateField(unique=True)
    limit = models.PositiveIntegerField(default=9800)
    used = models.PositiveIntegerField(default=0)
    archive_used = models.PositiveIntegerField(default=0)
    live_used = models.PositiveIntegerField(default=0)
    other_used = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-day"]


class MarketInstrument(models.Model):
    """Provider-discovered catalog used to verify supported portfolio assets."""

    class Source(models.TextChoices):
        TSETMC = "tsetmc", "TSETMC"
        BRS = "brs", "BRS"

    class Category(models.TextChoices):
        STOCK = "stock", "Stock"
        GOLD = "gold", "Gold"
        EXCLUDED = "excluded", "Excluded"

    source = models.CharField(max_length=8, choices=Source.choices)
    symbol = models.CharField(max_length=64)
    name = models.CharField(max_length=160, blank=True, default="")
    category = models.CharField(max_length=16, choices=Category.choices)
    provider_group = models.CharField(max_length=64, blank=True, default="")
    isin = models.CharField(max_length=32, blank=True, default="")
    eligible = models.BooleanField(default=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source", "symbol"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "symbol"],
                name="uniq_market_instrument_source_symbol",
            )
        ]


class ArchiveFetchState(models.Model):
    """DB-verified archive progress for one provider endpoint and symbol."""

    class Endpoint(models.TextChoices):
        STOCK_HISTORY_UNADJUSTED = "stock_history_unadjusted", "Stock history unadjusted"
        STOCK_HISTORY_ADJUSTED = "stock_history_adjusted", "Stock history adjusted"
        STOCK_CANDLE_UNADJUSTED = "stock_candle_unadjusted", "Stock candle unadjusted"
        STOCK_CANDLE_ADJUSTED = "stock_candle_adjusted", "Stock candle adjusted"
        GOLD_DAILY = "gold_daily", "Gold daily"

    endpoint = models.CharField(max_length=40, choices=Endpoint.choices)
    symbol = models.CharField(max_length=64)
    expected_rows = models.PositiveIntegerField(default=0)
    stored_rows = models.PositiveIntegerField(default=0)
    missing_rows = models.PositiveIntegerField(default=0)
    first_date = models.CharField(max_length=32, blank=True, default="")
    last_date = models.CharField(max_length=32, blank=True, default="")
    verified_complete = models.BooleanField(default=False, db_index=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["verified_complete", "-missing_rows", "last_attempt_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "symbol"],
                name="uniq_archive_endpoint_symbol",
            )
        ]


class StockSymbolMetadata(models.Model):
    """Detailed metadata and fundamental metrics for a TSE stock symbol."""

    ins_code = models.BigIntegerField(db_index=True, unique=True)
    l18 = models.CharField(max_length=64, db_index=True)
    l30 = models.CharField(max_length=120)
    l30_en = models.CharField(max_length=120, blank=True, default="")
    isin = models.CharField(max_length=32, blank=True, default="")
    code_12 = models.CharField(max_length=32, blank=True, default="")
    code_5 = models.CharField(max_length=16, blank=True, default="")
    code_4 = models.CharField(max_length=16, blank=True, default="")
    market = models.CharField(max_length=64, blank=True, default="")
    market_board = models.CharField(max_length=120, blank=True, default="")
    sector = models.CharField(max_length=120, blank=True, default="")
    sector_sub = models.CharField(max_length=120, blank=True, default="")
    shares_count = models.BigIntegerField(default=0)
    base_volume = models.BigIntegerField(default=0)
    market_cap = models.BigIntegerField(default=0)
    eps = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    pe = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    g_pe = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    ps = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    free_float = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    state = models.CharField(max_length=32, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["l18"]

    def __str__(self) -> str:
        return f"{self.l18} ({self.ins_code})"


class DailyStockHistory(models.Model):
    """Daily price history & Real/Legal (حقیقی/حقوقی) trade participant breakdown."""

    symbol = models.CharField(max_length=64, db_index=True)
    date = models.CharField(max_length=10, db_index=True)  # Jalali format YYYY-MM-DD
    time = models.CharField(max_length=8, blank=True, default="")
    tno = models.IntegerField(default=0)
    tvol = models.BigIntegerField(default=0)
    tval = models.BigIntegerField(default=0)
    pmin = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pmax = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    py = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pf = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pl = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    plc = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    plp = models.FloatField(default=0.0)
    pc = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pcc = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pcp = models.FloatField(default=0.0)
    is_adjusted = models.BooleanField(default=False)

    # Real / Legal participant distribution (حقیقی / حقوقی)
    buy_count_i = models.IntegerField(null=True, blank=True)
    buy_count_n = models.IntegerField(null=True, blank=True)
    sell_count_i = models.IntegerField(null=True, blank=True)
    sell_count_n = models.IntegerField(null=True, blank=True)
    buy_i_volume = models.BigIntegerField(null=True, blank=True)
    buy_n_volume = models.BigIntegerField(null=True, blank=True)
    sell_i_volume = models.BigIntegerField(null=True, blank=True)
    sell_n_volume = models.BigIntegerField(null=True, blank=True)
    buy_i_value = models.BigIntegerField(null=True, blank=True)
    buy_n_value = models.BigIntegerField(null=True, blank=True)
    sell_i_value = models.BigIntegerField(null=True, blank=True)
    sell_n_value = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date", "is_adjusted"],
                name="uniq_stock_history_symbol_date_adj",
            )
        ]


class MarketCandle(models.Model):
    """OHLCV candlestick time series data."""

    symbol = models.CharField(max_length=64, db_index=True)
    timeframe = models.CharField(max_length=16, db_index=True)  # e.g., 1m, 5m, 15m, 30m, 60m, 1d_adj, 1d_unadj
    date_time = models.CharField(max_length=32, db_index=True)
    open_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    high_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    low_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    close_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    volume = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["-date_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "timeframe", "date_time"],
                name="uniq_market_candle_symbol_tf_dt",
            )
        ]


class StockTransactionTick(models.Model):
    """Intraday trade transaction tick records."""

    symbol = models.CharField(max_length=64, db_index=True)
    date = models.CharField(max_length=10, db_index=True)
    time = models.CharField(max_length=8)
    row = models.IntegerField(default=0)
    price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    volume = models.BigIntegerField(default=0)
    canceled = models.BooleanField(default=False)

    class Meta:
        ordering = ["date", "row"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date", "row"],
                name="uniq_stock_tick_symbol_date_row",
            )
        ]


class ShareholderRecord(models.Model):
    """Institutional shareholder ownership records and changes."""

    symbol = models.CharField(max_length=64, db_index=True)
    date = models.CharField(max_length=10, blank=True, default="", db_index=True)
    shareholder_id = models.BigIntegerField()
    name = models.CharField(max_length=255)
    volume = models.BigIntegerField(default=0)
    percent = models.FloatField(default=0.0)
    change = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ["-percent"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date", "shareholder_id"],
                name="uniq_shareholder_symbol_date_id",
            )
        ]


class CodalAnnouncement(models.Model):
    """Codal financial disclosures, quarterly reports, and corporate notices (Categories 1-11)."""

    class Category(models.IntegerChoices):
        GENERAL = 1, "General Disclosures"
        STATEMENTS = 2, "Periodic Financial Statements"
        PRODUCTION_SALES = 3, "Monthly Production & Sales"
        BOARD_REPORT = 4, "Board of Directors Report"
        AUDITOR_REPORT = 5, "Auditor Notes & Opinion"
        ASSEMBLY_DECISION = 6, "General Assembly Decision"
        CAPITAL_INCREASE = 7, "Capital Increase Announcement"
        PORTFOLIO = 8, "Monthly Investment Portfolio"
        GOVERNANCE = 9, "Corporate Governance"
        SUBSIDIARIES = 10, "Subsidiary Financial Statements"
        PROSPECTUS = 11, "IPO & Bond Prospectus"

    symbol = models.CharField(max_length=64, db_index=True)
    company_name = models.CharField(max_length=255, blank=True, default="")
    title = models.TextField()
    code = models.CharField(max_length=64, blank=True, default="")
    category = models.IntegerField(choices=Category.choices, null=True, blank=True, db_index=True)
    category_title = models.CharField(max_length=120, blank=True, default="")
    is_audited = models.BooleanField(null=True, blank=True)
    date_title = models.CharField(max_length=20, blank=True, default="")
    date_send = models.CharField(max_length=10, blank=True, default="")
    time_send = models.CharField(max_length=8, blank=True, default="")
    date_publish = models.CharField(max_length=10, blank=True, default="")
    time_publish = models.CharField(max_length=8, blank=True, default="")
    link = models.URLField(max_length=1024, blank=True, default="")
    link_pdf = models.URLField(max_length=1024, blank=True, default="")
    link_excel = models.URLField(max_length=1024, blank=True, default="")
    link_attachment = models.URLField(max_length=1024, blank=True, default="")

    class Meta:
        ordering = ["-date_publish", "-time_publish"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "code", "date_publish", "time_publish"],
                name="uniq_codal_symbol_code_publish",
            )
        ]


class GoldCurrencyHistory(models.Model):
    """Gold, Fiat Currency, and Crypto daily and 24h price history."""

    symbol = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=120, blank=True, default="")
    unit = models.CharField(max_length=32, blank=True, default="")
    date = models.CharField(max_length=10, db_index=True)
    open_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    high_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    low_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    close_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date"],
                name="uniq_gold_currency_history_symbol_date",
            )
        ]


class MarketIndexData(models.Model):
    """Tehran Stock Exchange overall and equal-weight market index records."""

    date = models.CharField(max_length=10, db_index=True)
    time = models.CharField(max_length=8, blank=True, default="")
    state = models.CharField(max_length=32, blank=True, default="")
    index_overall = models.FloatField(default=0.0)
    index_overall_change = models.FloatField(default=0.0)
    index_equal_weight = models.FloatField(default=0.0)
    index_equal_weight_change = models.FloatField(default=0.0)
    market_value = models.BigIntegerField(default=0)
    trade_number = models.IntegerField(default=0)
    trade_value = models.BigIntegerField(default=0)
    trade_volume = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["-date", "-time"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "time"],
                name="uniq_market_index_date_time",
            )
        ]
