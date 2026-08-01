"""Core portfolio domain models.

Scale design:
- `Price` is a single append-only table shared across ALL users. Market prices
  (gold, USD, KAMA, ...) are global, so we fetch them once and every user's
  valuation reflects the update. This is why the system scales: fetch cost is
  O(sources), not O(users).
- "Latest price per asset" is read with one Postgres DISTINCT ON query over a
  (asset, fetched_at) index, then cached. No per-asset queries.
- `Snapshot` rows are written on the cron after each fetch (bulk, one per user),
  so the write rate is bounded by schedule frequency, not by user count or price
  volatility. Current value is computed live from holdings x latest prices.
"""
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone



class Asset(models.Model):
    """Catalog of investable assets (seeded by `seed_assets`).

    `key` matches the price-map keys produced by portfolio.live.extractor, e.g.
    'emami_coin', 'kama_stock'. Holdings reference assets by FK but the price
    lookup keys flow through here so the engine stays in one place.
    """

    class AssetClass(models.TextChoices):
        GOLD = "Gold", "Gold"
        CASH = "Cash", "Cash / Currency"
        STOCK = "Stock", "Stock"
        REAL_ESTATE = "Real Estate", "Real Estate"
        CRYPTO = "Crypto", "Crypto"

    class Currency(models.TextChoices):
        IRT = "IRT", "Tomans"
        USD = "USD", "USD"

    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    name_fa = models.CharField(max_length=120, blank=True, default="")
    asset_class = models.CharField(
        max_length=16, choices=AssetClass.choices, default=AssetClass.GOLD
    )
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.IRT)
    # Manual assets (e.g. Swiss gold bars) are priced from settings, not APIs.
    is_manual = models.BooleanField(default=False)
    # Real estate is valued by a formula, not quantity x unit price.
    is_house = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    # Join keys into the marketdata warehouse (symbol-string keyed, no FK):
    # TSE ticker for stocks (e.g. "کاما"), BrsApi symbol for gold/currency/crypto
    # (e.g. "IR_COIN_EMAMI"). Blank = no history source for this asset.
    tse_symbol = models.CharField(max_length=64, blank=True, default="")
    brs_symbol = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["asset_class", "name"]

    def clean(self):
        if not self.is_active:
            return
        if self.is_house:
            return
        from marketdata.models import MarketInstrument

        if not MarketInstrument.objects.exists():
            return
        if self.asset_class not in (self.AssetClass.GOLD, self.AssetClass.STOCK, self.AssetClass.CASH):
            raise ValidationError("Active assets must be verified stocks, gold, or cash/currency instruments.")
        source = "tsetmc" if self.asset_class == self.AssetClass.STOCK else "brs"
        symbol = self.tse_symbol if source == "tsetmc" else self.brs_symbol
        if not symbol:
            raise ValidationError("Active assets require a provider symbol.")

        if not MarketInstrument.objects.filter(
            source=source,
            symbol=symbol,
            eligible=True,
        ).exists():
            raise ValidationError("Asset is not eligible in the verified provider catalog.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class Account(models.Model):
    """A user's portfolio group (e.g. 'Main', 'Brokerage', 'Cash drawer').

    This models the 'one user -> many accounts' requirement. Real brokerage
    linking would attach broker credentials / OAuth tokens here later; for now
    a named group with holdings is the honest v1.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="accounts"
    )
    name = models.CharField(max_length=120)
    broker = models.CharField(max_length=120, blank=True, default="")
    # Free-form purpose tag for this portfolio (e.g. "Retirement", "Trading",
    # "Speculative", "Cash"). Drives the per-portfolio settings surface.
    goal = models.CharField(max_length=40, blank=True, default="")
    tracking_started_at = models.DateTimeField(null=True, blank=True)
    cash_balance_tomans = models.DecimalField(
        max_digits=24, decimal_places=4, default=0
    )
    ledger_complete = models.BooleanField(
        default=False,
        help_text="True when opening balances and subsequent cash flows are complete.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="uniq_account_name_per_user")
        ]

    def __str__(self) -> str:
        return f"{self.user.email} / {self.name}"


class Holding(models.Model):
    """Quantity of one asset held in one account. Mirrors current_state.json."""

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="holdings"
    )
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="holdings")
    quantity = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    # For houses, quantity stores price-per-sqm-million (the formula input).
    area_sqm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("90.2"))
    mortgage_deduction_tomans = models.DecimalField(max_digits=20, decimal_places=4, default=Decimal("400000000"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        ordering = ["asset__asset_class", "asset__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "asset"], name="uniq_asset_per_account"
            )
        ]


class Price(models.Model):
    """Global, append-only price time-series in Tomans.

    Written by `pricing` after each fetch. Read pattern: latest price per asset
    via DISTINCT ON (asset) ORDER BY fetched_at DESC, backed by the index below.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="prices")
    price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    fetched_at = models.DateTimeField(auto_now_add=True, db_index=True)
    source = models.CharField(max_length=16, default="API")

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [
            # Latest-price-per-asset lookups.
            models.Index(fields=["asset", "-fetched_at"], name="idx_price_asset_time"),
        ]


class ImportBatch(models.Model):
    """Idempotency record for one committed CSV ledger import."""

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="import_batches"
    )
    file_hash = models.CharField(max_length=64)
    row_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "file_hash"], name="uniq_import_file_per_account"
            )
        ]


class LedgerEntry(models.Model):
    """Immutable account event and source of truth for positions and cash.

    `Holding.quantity` is DERIVED state (the running sum of buys minus sells);
    this table records the EVENTS that produced it. Keeping the ledger means:
      * the net-worth chart can annotate the exact buy/sell moments;
      * true time-weighted return is computable later (cash flows are separable
        from market moves — snapshot deltas alone conflate the two);
      * holdings can always be rebuilt from history if the schema changes.

    `price_tomans` is the unit price captured at execution (the latest Price at
    the time of the trade), so historical valuation of the event never depends
    on today's price map. Rows are never updated; the latest trade for an asset
    may be deleted explicitly to correct an input mistake.
    """

    class Side(models.TextChoices):
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"

    class Kind(models.TextChoices):
        OPENING_POSITION = "opening_position", "Opening position"
        OPENING_CASH = "opening_cash", "Opening cash"
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"
        DIVIDEND = "dividend", "Dividend"
        FEE = "fee", "Fee"

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="transactions"
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    # Always positive; `side` carries the direction.
    quantity = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    # Unit price in Tomans at execution; 0 when the asset had no price yet.
    price_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True
    )
    amount_tomans = models.DecimalField(
        max_digits=24, decimal_places=4, null=True, blank=True
    )
    note = models.CharField(max_length=200, blank=True, default="")
    timestamp = models.DateTimeField(db_index=True, default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(
        max_length=16,
        choices=(
            ("manual", "manual"),
            ("csv", "csv"),
            ("system", "system"),
            ("imported", "imported (legacy)"),
            ("inferred", "inferred (legacy)"),
        ),
        default="manual"
    )
    import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.PROTECT,
        related_name="entries",
        null=True,
        blank=True,
    )
    external_id = models.CharField(max_length=120, blank=True, default="")
    reversal_of = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        related_name="reversed_by",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["account", "-timestamp"], name="idx_txn_account_time"),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    ~models.Q(kind__in=["opening_position", "buy", "sell"])
                    | (
                        models.Q(asset__isnull=False)
                        & models.Q(quantity__isnull=False)
                        & models.Q(quantity__gt=0)
                    )
                ),
                name="ledger_asset_event_fields",
            ),
            models.CheckConstraint(
                check=(
                    ~models.Q(kind="dividend") | models.Q(asset__isnull=False)
                ),
                name="ledger_dividend_asset",
            ),
            models.CheckConstraint(
                check=(
                    ~models.Q(kind__in=["opening_cash", "deposit", "withdrawal", "dividend", "fee"])
                    | models.Q(amount_tomans__gt=0)
                ),
                name="ledger_cash_event_amount",
            ),
            models.UniqueConstraint(
                fields=["account", "external_id"],
                condition=~models.Q(external_id=""),
                name="uniq_ledger_external_id_per_account",
            ),
        ]

    def __init__(self, *args, **kwargs):
        side = kwargs.pop("side", None)
        if side is not None and "kind" not in kwargs:
            kwargs["kind"] = side
        super().__init__(*args, **kwargs)

    @property
    def side(self):
        return self.kind if self.kind in self.Side.values else ""

    @side.setter
    def side(self, value):
        self.kind = value

    @property
    def occurred_at(self):
        return self.timestamp

    @occurred_at.setter
    def occurred_at(self, value):
        self.timestamp = value

    @property
    def unit_price_tomans(self):
        return self.price_tomans

    @unit_price_tomans.setter
    def unit_price_tomans(self, value):
        self.price_tomans = value

    def __str__(self) -> str:
        asset = self.asset.key if self.asset_id else "cash"
        return f"{self.kind} {self.quantity or self.amount_tomans} {asset}"


# One-release Python compatibility for callers importing the former model name.
Transaction = LedgerEntry


class Snapshot(models.Model):
    """Per-user net worth at a point in time, for history charts.

    Written in bulk by the cron after each price fetch (one row per user). The
    global price map is NOT duplicated here: it lives once in the Price table, so
    a snapshot only stores the derived total (M4 — was a per-user-per-fetch copy
    of the whole map).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="snapshots"
    )
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="snapshots", null=True, blank=True
    )
    total_value_tomans = models.DecimalField(max_digits=24, decimal_places=4, default=0)
    timestamp = models.DateTimeField(db_index=True, default=timezone.now)
    is_estimated = models.BooleanField(
        default=False,
        help_text="True for downtime-gap backfilled rows (fabricated from recovery-time prices, not real history).",
    )

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["user", "-timestamp"], name="idx_snapshot_user_time"),
        ]


class BacktestRun(models.Model):
    """Execution status and metadata for a multi-year backtest run."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="backtest_runs",
        null=True,
        blank=True,
    )
    params_hash = models.CharField(max_length=64, db_index=True)
    basis = models.CharField(
        max_length=16,
        choices=(("nominal", "nominal"), ("usd_real", "usd_real")),
        default="nominal",
    )
    universe = models.JSONField(null=True, blank=True)
    universe_hash = models.CharField(max_length=64)
    integrity_version = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    progress = models.IntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class BacktestYear(models.Model):
    """Results for a single year of a backtest run."""

    run = models.ForeignKey(
        BacktestRun, on_delete=models.CASCADE, related_name="years"
    )
    cutoff_date = models.CharField(max_length=32)  # Jalali date string
    scenario = models.CharField(max_length=32)
    target_weights = models.JSONField()
    realized_metrics = models.JSONField()
    benchmark_delta = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    excluded_symbols = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["cutoff_date", "scenario"]


class Watchlist(models.Model):
    """Watchlist scoped per Account."""

    account = models.OneToOneField(
        Account, on_delete=models.CASCADE, related_name="watchlist"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class WatchlistItem(models.Model):
    """Individual candidate instrument in a Watchlist with overrides."""

    watchlist = models.ForeignKey(
        Watchlist, on_delete=models.CASCADE, related_name="items"
    )
    symbol = models.CharField(max_length=64)
    force_include = models.BooleanField(default=False)
    force_exclude = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["watchlist", "symbol"],
                name="uniq_watchlist_item_symbol",
            )
        ]


class BacktestUserQuota(models.Model):
    """Tracks daily backtest submission counts per user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="backtest_quotas"
    )
    day = models.DateField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "day"],
                name="uniq_backtest_quota_user_day",
            )
        ]
