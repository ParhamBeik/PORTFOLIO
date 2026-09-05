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

import jdatetime
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

# Import optimization models to register OptimizationSnapshot with Django's app registry
from . import optimization_models  # noqa: F401

# House valuation baseline, shared by the model default, the ledger rebuild and
# the valuation engine — three copies of a number that must agree is a defect
# waiting to happen, so it lives here and every call site imports it.
#
# There is deliberately no default mortgage constant: since migration 0017 a
# mortgage is a `Liability` row, and a house with no declared mortgage has none.
HOUSE_AREA_SQM = Decimal("90.2")

# A property's price is entered and stored in MILLIONS of Toman per square meter,
# because that is the unit the market quotes in ("this place is 100 a meter").
# This is the one place that convention is written down; `valuation._house_value`
# and the API serializers both scale by it rather than repeating a literal.
HOUSE_PRICE_SCALE = Decimal("1000000")

# Catalog rows whose UNIT is itself divisible, keyed rather than classed because
# the class does not decide it: `gold_18k_gram` is sold by the gram while every
# other Gold row is a coin or a bar you count, and `usdt_irt` is a token divisible
# to six places while the other Cash rows are banknotes. See `Asset.quantity_step`,
# which is the one place this question is answered.
DIVISIBLE_QUANTITY_KEYS = frozenset({"gold_18k_gram", "usdt_irt"})



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
    # NULL = the shared global catalog every user sees. A non-null owner is a row
    # one user minted for themselves, and only real estate is ever minted this way
    # (see HoldingListCreateView). That restriction is load-bearing: every loop
    # over the global catalog -- the live price fetch, nightly_asset_metrics, the
    # returns universe -- already excludes `is_house=True`, so none of them need an
    # owner filter. Widen this to another asset class and those loops must be
    # audited first.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_assets",
        null=True,
        blank=True,
    )
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
    # Physical-underlying proxy for manual assets that have no provider symbol: a
    # Swiss gold bar IS gold, so its risk is measured from `gold_18k_gram`'s series
    # rather than from the handful of live ticks its manual valuation produced.
    # Used by the risk breakdown only (see returns.resolve_universe) -- never by
    # the optimizer, where duplicate columns would make the covariance singular.
    proxy_key = models.SlugField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["asset_class", "name"]

    def clean(self):
        if not self.is_active:
            return
        if self.is_house or self.is_manual:
            return
        from marketdata.models import MarketInstrument

        if not MarketInstrument.objects.exists():
            return
        if self.asset_class not in (
            self.AssetClass.GOLD,
            self.AssetClass.STOCK,
            self.AssetClass.CASH,
            self.AssetClass.CRYPTO,
        ):
            raise ValidationError(
                "Active assets must be verified stocks, gold, cash/currency, or crypto instruments."
            )
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

    @property
    def quantity_step(self) -> str:
        """The smallest amount of this asset it makes sense to hold one of.

        Nearly everything in this catalog is COUNTED -- a share, a coin, a bar,
        a banknote -- and an editor that steps those by 0.0001 offers a quantity
        that cannot exist. Three units genuinely divide: crypto, gold sold by
        the gram, and the tether token. `Holding.quantity` stores six decimal
        places, so that is the floor for the divisible ones.

        A property is not measured in units at all -- its `quantity` column
        holds the price of a square meter in millions of Toman -- so it steps
        freely and the editor labels that field as a price, not a count.

        Advisory: this is what the editor offers, not a constraint the API
        enforces. A holding that is already fractional keeps its value; nothing
        here rounds one.
        """
        if self.is_house:
            return "any"
        if (
            self.asset_class == self.AssetClass.CRYPTO
            or self.key in DIVISIBLE_QUANTITY_KEYS
        ):
            return "0.000001"
        return "1"

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
    # Opt-in double-entry cash. When False a buy is just a position: the money is
    # assumed to have come from outside the tracked portfolio. Booking every buy
    # as a funded purchase meant any portfolio that had never recorded a deposit
    # was refused ("Insufficient cash balance") the first time it recorded one --
    # the single most common reason a trade could not be saved. Flipped on
    # automatically by the first cash entry; see ledger._cash_delta.
    track_cash = models.BooleanField(
        default=False,
        help_text="True when this portfolio records cash movements, so trades settle against a balance.",
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
    area_sqm = models.DecimalField(max_digits=10, decimal_places=2, default=HOUSE_AREA_SQM)
    mortgage_deduction_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, default=Decimal("0")
    )
    # This user's own name for their copy of the asset ("Dad's gold bar", "Home").
    # Blank falls back to the catalog name. It lives here rather than on Asset
    # because the catalog is shared: renaming there would rename the asset for
    # every other user of the application.
    display_name = models.CharField(max_length=120, blank=True, default="")
    # Switched off: still listed, still owned, but excluded from every figure the
    # app computes -- value, allocation, risk, performance and the historical net
    # worth line. Lets someone list a primary residence without it dominating a
    # portfolio they actually trade. See services/visibility.py.
    is_hidden = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        ordering = ["asset__asset_class", "asset__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "asset"], name="uniq_asset_per_account"
            )
        ]

    @property
    def label(self) -> str:
        """What to call this holding on screen: the owner's name for it, else the
        catalog's. Persian first, because that is how TSE symbols are recognized.

        For a stock the ticker comes BEFORE `name_fa`: a TSE holding is known by
        its symbol (کاما, خگستر), not by the registered company name, and
        `name_fa` carries the latter -- "گسترش‌سرمایه‌گذاری‌ایران‌خودرو" where the
        owner is looking for "خگستر". Only `tse_symbol` is used; `brs_symbol` is
        a provider code (IR_COIN_EMAMI) nobody says out loud, and gold/FX
        `name_fa` is already the recognizable name.
        """
        return (
            self.display_name
            or self.asset.tse_symbol
            or self.asset.name_fa
            or self.asset.name
            or self.asset.key
        )

    @property
    def price_per_sqm_tomans(self) -> Decimal | None:
        if not self.asset.is_house:
            return None
        return Decimal(self.quantity) * HOUSE_PRICE_SCALE


def owner_display_names(accounts=None) -> dict[int, str]:
    """Nicknames for owner-minted assets, keyed by asset id.

    `Holding.label` answers this for a row you already have. Screens that list
    ASSETS rather than holdings -- the Ops console, the Comparison pickers, the
    Best Overall gap table -- have no holding in hand and read the catalog name
    instead. For a property that name is the asset class, because a property is
    minted per owner and `_mint_property_asset` has nothing else to put there.
    So all three independently showed someone's house as "Real Estate", and each
    was fixed on its own page as it was noticed. This is the rule, once.

    Only owner-minted rows are answered. A shared catalog asset is the same
    instrument for everybody and keeps the name everybody knows it by, whatever
    one holder happens to have nicknamed their slice.
    """
    holdings = Holding.objects.filter(asset__owner__isnull=False).exclude(display_name="")
    if accounts is not None:
        holdings = holdings.filter(account__in=accounts)
    return dict(holdings.values_list("asset_id", "display_name"))


class Price(models.Model):
    """Global, append-only live price series in Toman (`price_unit=IRT`).

    Written by the live fetch loop after each cycle. Read pattern: latest price
    per asset via DISTINCT ON (asset) ORDER BY fetched_at DESC.

    `price_unit` / `price_unit_verified` mark provider unit confidence.
    TSE stock rows (`Asset.tse_symbol`) are stored as **Rial** (price_unit=IRR)
    so qty×price matches the 1/10 broker-share convention. Gold/FX/manual rows
    stay Toman (IRT). Analytics that need a pure-Toman TSE series still use
    `tse_close_to_toman()` on warehouse candles, not these rows.
    """

    class Unit(models.TextChoices):
        IRT = "IRT", "Tomans"
        IRR = "IRR", "Rials"
        UNKNOWN = "UNKNOWN", "Unknown"

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="prices")
    price = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    fetched_at = models.DateTimeField(auto_now_add=True, db_index=True)
    source = models.CharField(max_length=16, default="API")

    # New metadata: provider-declared unit and whether it has been verified by
    # an operator or automated evidence check. Null/blank allowed for older rows.
    price_unit = models.CharField(
        max_length=16,
        choices=Unit.choices,
        default=Unit.UNKNOWN,
        help_text="Declared unit for this price value (IRT=Tomans, IRR=Rials, UNKNOWN)",
    )
    price_unit_verified = models.BooleanField(
        default=False,
        help_text="True when a human or automated check has verified the unit for this provider/asset",
    )

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [
            # Latest-price-per-asset lookups.
            models.Index(fields=["asset", "-fetched_at"], name="idx_price_asset_time"),
        ]


class DailyPriceAverage(models.Model):
    """One averaged live price per asset per calendar day, with its sample size.

    Not a substitute for `Price` (still the freshest tick for current
    valuation) and not a substitute for marketdata's historical warehouse
    (still the source of truth for real OHLC history). This is the live
    domain's own durable daily record -- written nightly by
    `portfolio.tasks.aggregate_daily_price_averages` from that day's
    source="API" Price ticks -- for anyone who wants "what was the average
    live price on day X, from how many observations" without rescanning raw
    ticks. `portfolio.services.returns._load_live_price_panel` computes its
    own per-day mean directly from `Price` instead of reading this table, so
    the returns matrix keeps full historical depth for assets (e.g. crypto)
    that have no warehouse coverage at all and would otherwise be capped at
    however far back the nightly rollup has been running.
    """

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="daily_price_averages"
    )
    # Jalali YYYY-MM-DD, matching the date keys warehouse tables use.
    date = models.CharField(max_length=10, db_index=True)
    avg_price = models.DecimalField(max_digits=20, decimal_places=4)
    sample_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "date"], name="uniq_daily_price_average_asset_date"
            )
        ]
        indexes = [
            models.Index(fields=["asset", "-date"], name="daily_price_avg_asset_date_idx"),
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
        # Real estate has no market feed, so its worth is an operator mark. Each
        # mark is a dated event rather than an edit of the previous one: before
        # this existed the house carried a single price across all of history,
        # which hid every rial of appreciation from the net-worth chart and
        # baked today's price into the opening balance, understating TWR.
        # `quantity` holds price-per-sqm in millions, matching the house
        # convention used by Holding.quantity; `area_sqm` travels with it.
        # Marks REPLACE rather than accumulate -- see timeline.house_marks_as_of.
        VALUATION_MARK = "valuation_mark", "Valuation mark"
        # افزایش سرمایه: the company issues shares to existing holders for no
        # money. Deliberately NOT an opening_position (which declares cost basis
        # unknown and would void the real basis of everything bought before it)
        # and NOT a zero-price buy (`price_tomans <= 0` is the "no price
        # recorded" sentinel, same problem). Quantity rises, cash does not move,
        # and the average cost per share falls -- which is the whole point:
        # 4.2bn Toman spread over 26M shares instead of 11.6M.
        RIGHTS_ISSUE = "rights_issue", "Rights issue (افزایش سرمایه)"

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
    # Provider-scale unit price at execution; 0 when the asset had no price yet.
    # TSE uses the legacy Rial/one-tenth-share convention; other assets use Toman.
    price_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True
    )
    amount_tomans = models.DecimalField(
        max_digits=24, decimal_places=4, null=True, blank=True
    )
    area_sqm = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    mortgage_deduction_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True
    )
    note = models.CharField(max_length=200, blank=True, default="")
    timestamp = models.DateTimeField(db_index=True, default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    # Rows are appended far more often than corrected, which is why the cost
    # basis cache in `performance._position_metrics` keyed itself on
    # (count, max id, id sum) and got away with it. An in-place edit moves none
    # of those three, so correcting a quantity or declaring what you paid left
    # the P&L table showing the old answer for the full hour of the cache's
    # life. This column is what makes an edit visible to that key.
    updated_at = models.DateTimeField(auto_now=True)
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
    # What was actually PAID per unit, when that differs from what the row is
    # worth. Only an opening (and a house's valuation mark, which is how a
    # property added after the baseline is recorded) carries it.
    #
    # `price_tomans` cannot do this job. On an opening it means "the price at
    # the moment the position was declared", which is what seeds the first
    # `Price` row of a manual asset -- today's number, not the acquisition
    # number. Overloading it would make recording a gram of gold you bought in
    # 1402 quietly restate today's gold price as 10 million Toman.
    #
    # Unit convention matches `price_tomans` exactly: the asset's own quote
    # unit, so Rial for a TSE share and Toman for everything else. The one
    # exception is the one `quantity` already makes -- for a house this is
    # MILLIONS of Toman per square meter (HOUSE_PRICE_SCALE), because that is
    # the unit a property is bought and sold in.
    cost_basis_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True
    )
    reversal_of = models.OneToOneField(
        "self",
        on_delete=models.CASCADE,
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
                condition=(
                    ~models.Q(
                        kind__in=[
                            "opening_position", "buy", "sell", "valuation_mark",
                        ]
                    )
                    | (
                        models.Q(asset__isnull=False)
                        & models.Q(quantity__isnull=False)
                        & models.Q(quantity__gt=0)
                    )
                ),
                name="ledger_asset_event_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(kind="dividend") | models.Q(asset__isnull=False)
                ),
                name="ledger_dividend_asset",
            ),
            models.CheckConstraint(
                condition=(
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
    is_session_close = models.BooleanField(
        default=False,
        help_text="True when every held TSE asset used the verified close for its completed session.",
    )

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["user", "-timestamp"], name="idx_snapshot_user_time"),
            # Backs the daily history grouping in SnapshotListView, which always
            # filters on user + account together before partitioning by day.
            models.Index(fields=["user", "account", "timestamp"], name="idx_snapshot_user_acct_time"),
        ]


class Liability(models.Model):
    """A debt subtracted from net worth.

    Three shapes, and the difference is not cosmetic:

    * `bank_loan` — money borrowed from an institution and repaid on a
      schedule. What is owed today is not what was borrowed, so the balance is
      DERIVED from the terms (`outstanding_tomans`) rather than re-typed every
      month. A loan whose balance is a hand-maintained number is a number that
      is wrong for twenty-nine days out of thirty, and it is wrong in the
      direction that flatters the portfolio.
    * `secured_debt` — a debt attached to one asset: the mortgage on a house,
      a loan taken against a position. `asset` is what makes it secured, and
      the whole valuation stack already rides that link — a hidden or excluded
      asset takes its debt with it, so netting the debt of an asset nobody is
      counting cannot drop net worth by the loan alone.
    * `other` — anything the user simply wants subtracted. `amount_tomans` is
      the whole story.

    A mortgage is BOTH a bank loan and secured, which is why the kind and the
    asset link are separate fields rather than one enum: the kind says how the
    balance is computed, the asset says what the balance rides with.
    """

    class Kind(models.TextChoices):
        BANK_LOAN = "bank_loan", "Bank loan"
        SECURED_DEBT = "secured_debt", "Debt secured on an asset"
        OTHER = "other", "Other"

    # How the outstanding balance was arrived at, reported next to it. A number
    # the user typed and a number the schedule produced are not the same claim,
    # and a UI that prints them identically invites the reader to trust the
    # stale one as much as the live one.
    class BalanceBasis(models.TextChoices):
        AMORTIZED = "amortized", "Amortized from the loan terms"
        INSTALLMENTS = "installments", "Remaining installments"
        DECLARED = "declared", "As entered"

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="liabilities"
    )
    label = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.OTHER
    )
    # The declared balance, and the fallback for every liability that has no
    # schedule. Still the authority for `other`; for a loan with terms it is
    # what was last written down, and `outstanding_tomans` supersedes it.
    amount_tomans = models.DecimalField(max_digits=20, decimal_places=4)
    lender = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Bank or institution the money is owed to.",
    )
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="liabilities", null=True, blank=True
    )
    # Owned by `services.ledger.rebuild_projections`, which recreates these from
    # the house marks on every replay and therefore deletes them first. The flag
    # is what keeps that reap off the rows a person typed: a user's mortgage also
    # names an asset, so "has an asset" cannot be the test. Never writable over
    # the API — a client that could set it could make its own row disappear on
    # the next trade.
    derived = models.BooleanField(
        default=False,
        help_text="Maintained by the ledger replay rather than entered by hand.",
    )

    # --- Repayment schedule. All optional; see `balance_basis`. ---
    principal_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        help_text="Amount originally borrowed.",
    )
    annual_rate_pct = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text="Nominal annual rate, percent (18 means 18%).",
    )
    term_months = models.PositiveIntegerField(
        null=True, blank=True, help_text="Total number of monthly installments."
    )
    monthly_installment_tomans = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        help_text="Installment actually paid, when it is known but the rate is not.",
    )
    # Stored Gregorian (DateField), but counted in Jalali months -- see
    # `installments_paid`. An Iranian loan's installments fall on a Jalali month
    # day, and Jalali months are 31/30/29 days long, so counting Gregorian
    # months drifts a payment either side of every anniversary.
    started_on = models.DateField(
        null=True, blank=True, help_text="Date the first installment was due."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.label} ({self.get_kind_display()})"

    # ------------------------------------------------------------------
    # Repayment maths. This is the ONE place a balance is derived; the
    # valuation engine, the snapshot writer and the serializer all call
    # `outstanding_tomans` rather than reading `amount_tomans` directly.
    # ------------------------------------------------------------------

    @property
    def balance_basis(self) -> str:
        """Which rule `outstanding_tomans` will use, in priority order."""
        if (
            self.principal_tomans is not None
            and self.annual_rate_pct is not None
            and self.term_months
            and self.started_on is not None
        ):
            return self.BalanceBasis.AMORTIZED
        if (
            self.monthly_installment_tomans is not None
            and self.term_months
            and self.started_on is not None
        ):
            return self.BalanceBasis.INSTALLMENTS
        return self.BalanceBasis.DECLARED

    def installments_paid(self, as_of=None) -> int:
        """Whole Jalali months elapsed since the first installment, clamped.

        Clamped at both ends: a loan dated in the future has paid nothing, and
        one past its term has paid all of it. Without the upper clamp the
        amortization formula runs past maturity and returns a NEGATIVE balance,
        which reads as an asset.
        """
        if self.started_on is None or not self.term_months:
            return 0
        as_of = as_of or timezone.now()
        as_of_date = getattr(as_of, "date", lambda: as_of)()
        start = jdatetime.date.fromgregorian(date=self.started_on)
        now = jdatetime.date.fromgregorian(date=as_of_date)
        months = (now.year - start.year) * 12 + (now.month - start.month)
        if now.day < start.day:
            months -= 1
        return max(0, min(int(self.term_months), months))

    def scheduled_installment_tomans(self) -> Decimal | None:
        """The monthly payment the terms imply, or the one the user declared.

        The standard annuity payment: P·r/(1-(1+r)^-n). Printing it beside the
        installment the user actually pays is how a mistyped rate becomes
        visible — the two should agree, and when they do not, one of them is
        wrong.
        """
        if self.balance_basis != self.BalanceBasis.AMORTIZED:
            return self.monthly_installment_tomans
        principal = Decimal(self.principal_tomans)
        n = int(self.term_months)
        rate = self._monthly_rate()
        if rate == 0:
            return (principal / n).quantize(Decimal("0.0001"))
        growth = (Decimal(1) + rate) ** n
        return (principal * rate * growth / (growth - Decimal(1))).quantize(
            Decimal("0.0001")
        )

    def outstanding_tomans(self, as_of=None) -> Decimal:
        """What is still owed, as of a moment.

        Three rules, tried in order (`balance_basis` names which one fired):

        1. Full terms → the amortized principal balance. Early installments are
           mostly interest, so a straight-line "principal minus payments made"
           understates the debt for most of a loan's life — by a third of the
           balance at the midpoint of a typical 20%/5-year loan.
        2. Installment and term, no rate → what is left to PAY, which is the
           honest reading of a loan quoted the way Iranian banks quote them
           ("60 million, thirty-six installments"): the interest is already
           inside the installment.
        3. Neither → the declared `amount_tomans`, unchanged.

        Never negative: a matured loan is settled, not an asset.
        """
        basis = self.balance_basis
        if basis == self.BalanceBasis.DECLARED:
            return Decimal(self.amount_tomans)

        paid = self.installments_paid(as_of)
        n = int(self.term_months)

        if basis == self.BalanceBasis.INSTALLMENTS:
            remaining = Decimal(self.monthly_installment_tomans) * (n - paid)
            return max(Decimal("0"), remaining).quantize(Decimal("0.0001"))

        principal = Decimal(self.principal_tomans)
        rate = self._monthly_rate()
        if rate == 0:
            remaining = principal * Decimal(n - paid) / Decimal(n)
        else:
            growth = (Decimal(1) + rate) ** n
            paid_growth = (Decimal(1) + rate) ** paid
            remaining = principal * (growth - paid_growth) / (growth - Decimal(1))
        return max(Decimal("0"), remaining).quantize(Decimal("0.0001"))

    def payoff_on(self):
        """The Jalali month the last installment falls in, as a Gregorian date."""
        if self.started_on is None or not self.term_months:
            return None
        start = jdatetime.date.fromgregorian(date=self.started_on)
        total = (start.month - 1) + int(self.term_months)
        year = start.year + total // 12
        month = total % 12 + 1
        # The 31-day months stop at month 6 and Esfand is shortest, so a start
        # day of 31 has no counterpart in most payoff months. `j_days_in_month`
        # already gives Esfand its common-year 29, which in a leap year moves
        # the payoff one day early rather than off the end of the calendar.
        day = min(start.day, jdatetime.j_days_in_month[month - 1])
        return jdatetime.date(year, month, day).togregorian()

    def _monthly_rate(self) -> Decimal:
        if self.annual_rate_pct is None:
            return Decimal("0")
        return Decimal(self.annual_rate_pct) / Decimal("1200")


from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


@receiver([post_save, post_delete], sender=LedgerEntry)
def _on_ledger_entry_changed(sender, instance, **kwargs):
    if getattr(instance, "account_id", None):
        try:
            from portfolio.tasks import debounce_my_optimal_refresh
            debounce_my_optimal_refresh(instance.account_id)
        except Exception:
            pass


@receiver([post_save, post_delete], sender=Holding)
def _on_holding_changed(sender, instance, **kwargs):
    if getattr(instance, "account_id", None):
        try:
            from portfolio.tasks import debounce_my_optimal_refresh
            debounce_my_optimal_refresh(instance.account_id)
        except Exception:
            pass

