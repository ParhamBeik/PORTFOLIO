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


class JalaliDerivedDateTime(models.DateTimeField):
    """timestamptz derived from a sibling Jalali column at write time.

    TimescaleDB cannot range-partition a varchar, so every hypertable here needs
    a real timestamp beside its Jalali domain key. Making that column NOT NULL
    means every writer has to fill it, and there are many: the ingest paths, the
    nightly aggregators, the repair commands, and every test that seeds a row.
    Patching each one leaves the next writer to rediscover the constraint.

    `pre_save` is the single point Django routes ALL ORM writes through --
    `save()`, `bulk_create()`, `update_or_create()` alike -- so deriving the
    value here means no caller ever has to know this column exists. An explicit
    value is always respected; this only fills a blank.
    """

    def __init__(self, *args, date_field="date", time_field=None, **kwargs):
        self.date_field = date_field
        self.time_field = time_field
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["date_field"] = self.date_field
        if self.time_field is not None:
            kwargs["time_field"] = self.time_field
        return name, path, args, kwargs

    def pre_save(self, model_instance, add):
        existing = getattr(model_instance, self.attname, None)
        if existing is not None:
            return existing
        from . import jalali

        raw_date = getattr(model_instance, self.date_field, "") or ""
        # MarketCandle.date_time has historically carried "<date> <time>"; keep
        # only the date portion, matching what the ingest path stores.
        date_value = str(raw_date).split()[0] if raw_date else ""
        time_value = (
            getattr(model_instance, self.time_field, "") or ""
            if self.time_field
            else ""
        )
        derived = jalali.to_datetime(date_value, time_value)
        setattr(model_instance, self.attname, derived)
        return derived

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


class InstrumentListingHistory(models.Model):
    symbol = models.CharField(max_length=64, unique=True)
    first_seen = models.CharField(max_length=10)
    last_seen = models.CharField(max_length=10)
    eligible_from = models.CharField(max_length=10, null=True, blank=True)
    eligible_to = models.CharField(max_length=10, null=True, blank=True)

    class Meta:
        ordering = ["symbol"]


class ArchiveFetchState(models.Model):
    """DB-verified archive progress for one provider endpoint and symbol."""

    class Endpoint(models.TextChoices):
        STOCK_HISTORY_UNADJUSTED = "stock_history_unadjusted", "Stock history unadjusted"
        STOCK_HISTORY_ADJUSTED = "stock_history_adjusted", "Stock history adjusted"
        STOCK_CANDLE_UNADJUSTED = "stock_candle_unadjusted", "Stock candle unadjusted"
        STOCK_CANDLE_ADJUSTED = "stock_candle_adjusted", "Stock candle adjusted"
        GOLD_DAILY = "gold_daily", "Gold & currency daily"
        CRYPTO_DAILY = "crypto_daily", "Cryptocurrency daily"
        COMMODITY_DAILY = "commodity_daily", "Commodities daily"
        MARKET_INDEX_DAILY = "market_index_daily", "TSE market index daily"
        ETF_NAV_DAILY = "etf_nav_daily", "ETF funds NAV daily"
        OPTION_CONTRACT_DAILY = "option_contract_daily", "Options contract daily"
        CODAL_ANNOUNCEMENTS = "codal_announcements", "Codal financial disclosures"
        SHAREHOLDER_RECORDS = "shareholder_records", "Shareholder roster data"
        STOCK_TRANSACTION_TICKS = "stock_transaction_ticks", "Intraday trade ledgers"

    endpoint = models.CharField(max_length=40, choices=Endpoint.choices)
    symbol = models.CharField(max_length=64)
    expected_rows = models.PositiveIntegerField(default=0)
    stored_rows = models.PositiveIntegerField(default=0)
    missing_rows = models.PositiveIntegerField(default=0)
    known_gap_rows = models.PositiveIntegerField(default=0)
    first_date = models.CharField(max_length=32, blank=True, default="")
    last_date = models.CharField(max_length=32, blank=True, default="")
    verified_complete = models.BooleanField(default=False, db_index=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    target_window_days = models.PositiveSmallIntegerField(default=90)
    archive_cursor = models.PositiveIntegerField(default=1)

    # Suspension (see marketdata/suspension.py): a symbol whose failures are
    # a peer-relative outlier on its endpoint is pulled out of normal claiming
    # without ever deleting the row. `blacklisted` is a separate operator flag
    # ("never coming back") from `suspended_at` ("currently parked") so a probe
    # can clear the latter while the former stays sticky until someone unsets it.
    suspended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    suspension_reason = models.CharField(max_length=32, blank=True, default="")
    suspension_evidence = models.JSONField(default=dict, blank=True)
    blacklisted = models.BooleanField(default=False, db_index=True)
    last_probe_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["verified_complete", "-missing_rows", "last_attempt_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "symbol"],
                name="uniq_archive_endpoint_symbol",
            )
        ]
        indexes = [
            models.Index(
                fields=["blacklisted", "suspended_at", "last_probe_at"],
                name="archive_probe_due_idx",
            ),
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

    # See StockTransactionTick for why db_index is off here: the LIKE twins these
    # produced were never scanned. `symbol` is covered by the unique constraint
    # below, which leads with it; `date` keeps a plain btree via Meta.indexes.
    symbol = models.CharField(max_length=64)
    date = models.CharField(max_length=10)  # Jalali format YYYY-MM-DD
    time = models.CharField(max_length=8, blank=True, default="")
    tno = models.IntegerField(default=0)
    tvol = models.BigIntegerField(default=0)
    tval = models.BigIntegerField(default=0)
    pmin = models.DecimalField(max_digits=20, decimal_places=4, default=0, null=True, blank=True)
    pmax = models.DecimalField(max_digits=20, decimal_places=4, default=0, null=True, blank=True)
    py = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pf = models.DecimalField(max_digits=20, decimal_places=4, default=0, null=True, blank=True)
    pl = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    plc = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    plp = models.FloatField(default=0.0)
    pc = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pcc = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    pcp = models.FloatField(default=0.0)
    is_adjusted = models.BooleanField(default=False)
    ingested_at = models.DateTimeField(null=True, blank=True)
    # Lineage breadcrumb, written on every row and read only when tracing one
    # ingest by hand. Both indexes it carried recorded ~0 scans across 4M rows.
    last_correlation_id = models.CharField(max_length=64, blank=True, default="")

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
    # Partition dimension ONLY -- see migration 0031. The Jalali `date` above stays
    # the domain key: it is what the provider speaks and what every unique
    # constraint and reader uses, so nothing in portfolio/services changes.
    # TimescaleDB cannot range-partition a varchar, hence this second column.
    # Date only, matching migration 0031: this table's 4M backfilled rows sit at
    # midnight Tehran, and a `time` column carrying the day's last trade would
    # silently give new rows a different meaning from old ones.
    ts = JalaliDerivedDateTime(blank=True, date_field="date")

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date", "is_adjusted", "ts"],
                name="uniq_stock_history_symbol_date_adj",
            )
        ]
        indexes = [
            # (symbol, date) used to live here as a 307 MB index that took 5
            # scans, because `uniq_stock_history_symbol_date_adj` above is
            # (symbol, date, is_adjusted) and already serves every query with a
            # symbol -- it took 13.8M scans over the same period. A date-only
            # btree is the one access path the unique constraint cannot serve.
            models.Index(fields=["date"], name="stock_history_date_idx"),
        ]


class RealLegalHistory(models.Model):
    """Daily real/legal participation, independent of price-history coverage."""

    # 1.6M rows whose only reader is the archive verifier counting its own
    # writes; every index on this table recorded 0-2 scans. `symbol`+`date` are
    # covered by the unique constraint below.
    symbol = models.CharField(max_length=64)
    date = models.CharField(max_length=10)
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
    quality = models.CharField(max_length=24, default="validated")
    reconciliation_error = models.DecimalField(
        max_digits=8, decimal_places=6, null=True, blank=True
    )

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date"],
                name="uniq_real_legal_symbol_date",
            )
        ]


class MarketCandle(models.Model):
    """OHLCV candlestick time series data.

    Daily timeframes, in descending order of authority:

    * `ADJUSTED` / `UNADJUSTED` come from the provider (Candlestick.php type 3
      and 2) and are written append-only with `bulk_create(ignore_conflicts=True)`.
    * `AGGREGATE` is derived from intraday ticks by the nightly aggregators so
      the current session has a close before the provider publishes one.

    `AGGREGATE` must never be written into the `ADJUSTED` slot. The archive
    ingest path cannot overwrite an existing row, so a tick-derived
    approximation parked on `(symbol, ADJUSTED, date)` would permanently
    displace the provider's real close for that day -- in the exact series
    every valuation, returns and integrity path reads.
    """

    UNADJUSTED = "1d_unadj"
    ADJUSTED = "1d_adj"
    AGGREGATE = "1d_agg"

    # `symbol` is covered by the unique constraint below, which leads with it,
    # and its own two indexes took 23 and 75 scans. `timeframe` and `date_time`
    # KEEP db_index: unlike everywhere else in this module their LIKE twins are
    # genuinely hot (11,851 and 2,970 scans), so something really does prefix-
    # match them and dropping those would be a regression.
    symbol = models.CharField(max_length=64)
    timeframe = models.CharField(max_length=16, db_index=True)  # e.g., 1m, 5m, 15m, 30m, 60m, 1d_adj, 1d_unadj, 1d_agg
    date_time = models.CharField(max_length=32, db_index=True)
    open_price = models.DecimalField(max_digits=20, decimal_places=4, default=0, null=True, blank=True)
    high_price = models.DecimalField(max_digits=20, decimal_places=4, default=0, null=True, blank=True)
    low_price = models.DecimalField(max_digits=20, decimal_places=4, default=0, null=True, blank=True)
    close_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    volume = models.BigIntegerField(default=0)
    ingested_at = models.DateTimeField(null=True, blank=True)
    # Lineage breadcrumb; both its indexes recorded ~0 scans. See DailyStockHistory.
    last_correlation_id = models.CharField(max_length=64, blank=True, default="")
    # Partition dimension ONLY -- see migration 0031. The Jalali `date_time` above stays
    # the domain key: it is what the provider speaks and what every unique
    # constraint and reader uses, so nothing in portfolio/services changes.
    # TimescaleDB cannot range-partition a varchar, hence this second column.
    ts = JalaliDerivedDateTime(blank=True, date_field="date_time")

    class Meta:
        ordering = ["-date_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "timeframe", "date_time", "ts"],
                name="uniq_market_candle_symbol_tf_dt",
            )
        ]
        indexes = [
            models.Index(fields=["timeframe", "symbol", "date_time"]),
        ]


class StockTransactionTick(models.Model):
    """Intraday trade transaction tick records.

    Indexing note: `db_index=True` on a CharField makes Django build TWO indexes,
    a plain btree and a `varchar_pattern_ops` twin for LIKE. On 41.6M rows each
    twin cost 271 MB and `pg_stat_user_indexes` recorded ZERO scans for both of
    them -- nothing here is ever prefix-matched. `symbol` is dropped outright
    because the unique constraint below already leads with it; `date` keeps a
    plain btree (it had real traffic) declared as an explicit `Index`, which
    produces the btree WITHOUT the LIKE twin.
    """

    symbol = models.CharField(max_length=64)
    date = models.CharField(max_length=10)
    time = models.CharField(max_length=8)
    row = models.IntegerField(default=0)
    price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    volume = models.BigIntegerField(default=0)
    canceled = models.BooleanField(default=False)
    # Partition dimension ONLY -- see migration 0031. The Jalali `date`/`time` above stays
    # the domain key: it is what the provider speaks and what every unique
    # constraint and reader uses, so nothing in portfolio/services changes.
    # TimescaleDB cannot range-partition a varchar, hence this second column.
    ts = JalaliDerivedDateTime(blank=True, date_field="date", time_field="time")

    class Meta:
        ordering = ["date", "row"]
        indexes = [
            models.Index(fields=["date"], name="tick_date_idx"),
        ]
        constraints = [
            # `time` is part of the key because Transaction.php reuses a row
            # number for a cancelled trade: the same row appears once at the
            # trade time and once at the cancellation time. Keying on row alone
            # dropped ~8% of every busy day (937 of 11,652 on one sample) and
            # left it arbitrary which of the twins survived.
            models.UniqueConstraint(
                fields=["symbol", "date", "row", "time", "ts"],
                name="uniq_stock_tick_symbol_date_row_time",
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

    # Title-based classification (marketdata.codal_classification.classify), filled
    # retroactively by `classify_codal_announcements`. `tier` is null until that
    # command has run on a row -- never defaulted to Tier 3 -- so "not yet
    # classified" stays distinguishable from a real Tier 3 verdict.
    doc_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    tier = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    classified_by = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        ordering = ["-date_publish", "-time_publish"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "code", "date_publish", "time_publish"],
                name="uniq_codal_symbol_code_publish",
            )
        ]
        indexes = [
            models.Index(fields=["symbol", "tier"], name="codal_symbol_tier_idx"),
        ]


class DerivativeContract(models.Model):
    """Current metadata for a provider-listed option or futures contract."""

    class Kind(models.TextChoices):
        TSE_OPTION = "tse_option", "TSE option"
        IME_OPTION = "ime_option", "IME option"
        IME_FUTURE = "ime_future", "IME future"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    contract_code = models.CharField(max_length=96)
    underlying_code = models.CharField(max_length=96, blank=True, default="")
    expiry_date = models.CharField(max_length=10, blank=True, default="")
    contract_size = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    active = models.BooleanField(default=True)
    provider_payload = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "contract_code"], name="uniq_derivative_contract_kind_code"
            )
        ]
        indexes = [models.Index(fields=["kind", "expiry_date"], name="marketdata__kind_0a4bd7_idx")]


class DerivativeSnapshot(models.Model):
    """Append-only live quote captured for a derivative contract."""

    contract = models.ForeignKey(
        DerivativeContract, on_delete=models.CASCADE, related_name="snapshots"
    )
    observed_at = models.DateTimeField(db_index=True)
    last_price = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True)
    bid_price = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True)
    ask_price = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    open_interest = models.BigIntegerField(null=True, blank=True)
    provider_payload = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["contract", "-observed_at"], name="marketdata__contrac_1eb881_idx")]


class CodalReport(models.Model):
    """Versioned extraction state for one immutable Codal announcement."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FETCHING = "fetching", "Fetching"
        PARSED = "parsed", "Parsed"
        NEEDS_REVIEW = "needs_review", "Needs review"
        UNSUPPORTED = "unsupported_template", "Unsupported template"
        BLOCKED_NETWORK = "blocked_network", "Blocked network"
        BLOCKED_STORAGE = "blocked_storage", "Blocked storage"
        FAILED = "failed", "Failed"

    class Quality(models.TextChoices):
        VALIDATED = "validated", "Validated"
        DEGRADED = "degraded", "Degraded"
        REVIEW = "needs_review", "Needs review"
        UNKNOWN = "unknown", "Unknown"

    announcement = models.OneToOneField(
        CodalAnnouncement, on_delete=models.PROTECT, related_name="report"
    )
    category = models.IntegerField(
        choices=CodalAnnouncement.Category.choices, null=True, blank=True, db_index=True
    )
    report_type = models.CharField(max_length=80, blank=True, default="")
    letter_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    period_start = models.CharField(max_length=10, blank=True, default="")
    period_end = models.CharField(max_length=10, blank=True, default="", db_index=True)
    is_audited = models.BooleanField(null=True, blank=True)
    is_consolidated = models.BooleanField(default=False)
    is_correction = models.BooleanField(default=False)
    revision_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="revisions"
    )
    parser_version = models.CharField(max_length=32, default="1")
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    quality = models.CharField(
        max_length=24, choices=Quality.choices, default=Quality.UNKNOWN, db_index=True
    )
    error_code = models.CharField(max_length=64, blank=True, default="")
    extracted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-announcement__date_publish", "-announcement__time_publish"]


class CodalArtifact(models.Model):
    class Kind(models.TextChoices):
        EXCEL = "excel", "Excel"
        HTML = "html", "HTML"
        PDF = "pdf", "PDF"
        ATTACHMENT = "attachment", "Attachment"

    class FetchStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        STORED = "stored", "Stored"
        BLOCKED_NETWORK = "blocked_network", "Blocked network"
        BLOCKED_STORAGE = "blocked_storage", "Blocked storage"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    report = models.ForeignKey(CodalReport, on_delete=models.CASCADE, related_name="artifacts")
    source_url = models.URLField(max_length=2048)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    s3_key = models.CharField(max_length=512, blank=True, default="")
    checksum_sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    content_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.PositiveBigIntegerField(default=0)
    fetch_status = models.CharField(
        max_length=32, choices=FetchStatus.choices, default=FetchStatus.PENDING, db_index=True
    )
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "kind", "source_url"], name="uniq_codal_artifact_source"
            )
        ]


class CodalParsedTable(models.Model):
    report = models.ForeignKey(CodalReport, on_delete=models.CASCADE, related_name="parsed_tables")
    artifact = models.ForeignKey(
        CodalArtifact, null=True, blank=True, on_delete=models.SET_NULL, related_name="parsed_tables"
    )
    name = models.CharField(max_length=255, blank=True, default="")
    sheet_name = models.CharField(max_length=255, blank=True, default="")
    table_index = models.PositiveIntegerField(default=0)
    headers = models.JSONField(default=list)
    rows = models.JSONField(default=list)
    source_coordinates = models.JSONField(default=dict)
    parser_version = models.CharField(max_length=32, default="1")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "artifact", "sheet_name", "table_index"],
                name="uniq_codal_parsed_table",
            )
        ]


class CodalSection(models.Model):
    report = models.ForeignKey(CodalReport, on_delete=models.CASCADE, related_name="sections")
    artifact = models.ForeignKey(
        CodalArtifact, null=True, blank=True, on_delete=models.SET_NULL, related_name="sections"
    )
    heading = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField()
    section_index = models.PositiveIntegerField(default=0)
    source_coordinates = models.JSONField(default=dict)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "artifact", "section_index"], name="uniq_codal_section"
            )
        ]


class CodalFact(models.Model):
    report = models.ForeignKey(CodalReport, on_delete=models.CASCADE, related_name="facts")
    table = models.ForeignKey(
        CodalParsedTable, null=True, blank=True, on_delete=models.SET_NULL, related_name="facts"
    )
    section = models.ForeignKey(
        CodalSection, null=True, blank=True, on_delete=models.SET_NULL, related_name="facts"
    )
    fact_code = models.CharField(max_length=160, db_index=True)
    numeric_value = models.DecimalField(max_digits=38, decimal_places=12, null=True, blank=True)
    text_value = models.TextField(blank=True, default="")
    unit = models.CharField(max_length=64, blank=True, default="")
    currency = models.CharField(max_length=16, blank=True, default="")
    period_start = models.CharField(max_length=10, blank=True, default="")
    period_end = models.CharField(max_length=10, blank=True, default="", db_index=True)
    dimensions = models.JSONField(default=dict)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, default=1)
    quality = models.CharField(max_length=24, default="validated", db_index=True)
    parser_version = models.CharField(max_length=32, default="1")
    source_coordinates = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["fact_code", "period_end", "quality"])]


class CorporateAction(models.Model):
    class Kind(models.TextChoices):
        SPLIT = "split", "Split"
        CAPITAL_INCREASE = "capital_increase", "Capital increase"
        DIVIDEND = "dividend", "Dividend"
        UNKNOWN = "unknown", "Unknown"

    class Source(models.TextChoices):
        FACTOR_RATIO = "derived_from_factor_ratio", "Derived from factor ratio"
        CODAL = "codal", "Codal"

    symbol = models.CharField(max_length=64, db_index=True)
    date = models.CharField(max_length=10, db_index=True)
    factor = models.DecimalField(max_digits=20, decimal_places=10)
    kind = models.CharField(
        max_length=24, choices=Kind.choices, default=Kind.UNKNOWN
    )
    source = models.CharField(
        max_length=32, choices=Source.choices, default=Source.FACTOR_RATIO
    )

    class Meta:
        ordering = ["symbol", "-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date"],
                name="uniq_corporate_action_symbol_date",
            )
        ]


class GoldCurrencyHistory(models.Model):
    """Gold, Fiat Currency, and Crypto daily and 24h price history."""

    class Source(models.TextChoices):
        PROVIDER = "provider", "Provider"
        AGGREGATE = "aggregate", "Live-price aggregate"

    symbol = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=120, blank=True, default="")
    unit = models.CharField(max_length=32, blank=True, default="")
    date = models.CharField(max_length=10, db_index=True)
    open_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    high_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    low_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    close_price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.PROVIDER
    )
    ingested_at = models.DateTimeField(null=True, blank=True)
    last_correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

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


class RejectedRecord(models.Model):
    """A provider record that failed validation, kept instead of discarded.

    Dropping a bad record silently is how the warehouse filled with garbage
    nobody could see. Keeping every copy would be unbounded, so rows are keyed
    on (endpoint, symbol, date, reason) and carry a counter plus one sample of
    the offending payload -- enough to diagnose, small enough to leave alone.
    """

    endpoint = models.CharField(max_length=64, db_index=True)
    symbol = models.CharField(max_length=64, blank=True, default="", db_index=True)
    date = models.CharField(max_length=10, blank=True, default="")
    reason = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict)
    occurrences = models.IntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    disposition = models.CharField(max_length=24, default="quarantined", db_index=True)
    recovered_at = models.DateTimeField(null=True, blank=True)
    destination_reference = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-last_seen"]
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "symbol", "date", "reason"],
                name="uniq_rejected_endpoint_symbol_date_reason",
            )
        ]

    def __str__(self) -> str:
        return f"{self.endpoint}/{self.symbol}@{self.date}: {self.reason}"


class SystemLogEvent(models.Model):
    """Database-backed system log event repository shared across Celery workers and Django processes."""

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=20)
    category = models.CharField(max_length=50, db_index=True)
    logger_name = models.CharField(max_length=100)
    message = models.TextField()
    service = models.CharField(max_length=50, default="backend", db_index=True)

    class Meta:
        ordering = ["-timestamp"]


class WorkflowRun(models.Model):
    """One structured terminal outcome for one logical workflow job."""

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        RETRY = "retry", "Retry"
        SKIPPED = "skipped", "Skipped"
        BLOCKED_NETWORK = "blocked_network", "Blocked network"
        BLOCKED_STORAGE = "blocked_storage", "Blocked storage"
        FAILED = "failed", "Failed"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    workflow = models.CharField(max_length=80, db_index=True)
    task_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    endpoint = models.CharField(max_length=80, blank=True, default="", db_index=True)
    symbol = models.CharField(max_length=64, blank=True, default="", db_index=True)
    outcome = models.CharField(max_length=32, choices=Outcome.choices, db_index=True)
    source = models.CharField(max_length=255, blank=True, default="")
    destination_table = models.CharField(max_length=128, blank=True, default="")
    rows_received = models.PositiveIntegerField(default=0)
    rows_accepted = models.PositiveIntegerField(default=0)
    rows_created = models.PositiveIntegerField(default=0)
    rows_updated = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    http_attempts = models.PositiveSmallIntegerField(default=0)
    quota_attempts = models.PositiveSmallIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workflow", "outcome", "created_at"]),
            models.Index(fields=["endpoint", "symbol", "created_at"]),
        ]


class OperationalMetricSnapshot(models.Model):
    """15-minute ops snapshot: counts, bytes, completeness, queues, Codal."""

    captured_at = models.DateTimeField(unique=True)
    database_counts = models.JSONField(default=dict)
    table_bytes = models.JSONField(default=dict)
    archive = models.JSONField(default=dict)
    quota = models.JSONField(default=dict)
    queues = models.JSONField(default=dict)
    codal_status = models.JSONField(default=dict)
    workflow_15m = models.JSONField(default=dict)
    workers = models.JSONField(default=dict)
    disk = models.JSONField(default=dict)

    class Meta:
        ordering = ["-captured_at"]


class SymbolIntegrity(models.Model):
    """Integrity gate checks per symbol."""
    symbol = models.CharField(max_length=64, db_index=True, unique=True)
    source = models.CharField(max_length=8, blank=True, default="")
    coverage_ratio = models.FloatField(default=0.0)
    max_gap_days = models.IntegerField(default=0)
    rejected_count = models.IntegerField(default=0)
    passes_gate = models.BooleanField(default=False, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]


class AssetMetricSnapshot(models.Model):
    symbol = models.CharField(max_length=64, db_index=True)
    asset_class = models.CharField(max_length=16, blank=True, default="")
    as_of = models.CharField(max_length=10, db_index=True)
    window_days = models.PositiveSmallIntegerField(default=365)
    total_return = models.FloatField(default=0.0)
    annualized_volatility = models.FloatField(default=0.0)
    sharpe = models.FloatField(default=0.0)
    sortino = models.FloatField(default=0.0)
    max_drawdown = models.FloatField(default=0.0)
    beta = models.FloatField(null=True, blank=True)
    correlation = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-as_of", "-sharpe"]
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "as_of", "window_days"],
                name="uniq_asset_metric_symbol_asof_window",
            )
        ]
