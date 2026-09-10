from decimal import Decimal
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Account,
    Asset,
    Holding,
    LedgerEntry,
    Transaction,
    Liability,
)
from .services.catalog import resolve_asset_key, visible_to
from .services.ledger import (
    PriceResolutionError,
    assert_not_before_history,
    resolve_historical_price,
)


class AssetSerializer(serializers.ModelSerializer):
    # What one of this asset is, so the wizard's quantity box steps by a share
    # rather than by a fraction of one. Same declaration the valuation rows and
    # the catalog search carry; see `Asset.quantity_step`.
    quantity_step = serializers.CharField(read_only=True)

    class Meta:
        model = Asset
        # `tse_symbol` is exposed because it is the unit discriminator, not just
        # a join key: a non-empty one means this asset's prices are quoted in
        # Rial. The add-transaction dialog labels its price field from it, and
        # must use the same test the server does (currency.is_tse_priced).
        fields = ("id", "key", "name", "name_fa", "asset_class", "currency",
                  "is_manual", "is_house", "is_active", "tse_symbol",
                  "quantity_step")


class HoldingSerializer(serializers.ModelSerializer):
    # `asset_key` is optional so a brand-new property can be created in the same
    # POST that opens the holding -- there is no catalog row to point at yet.
    quantity = serializers.DecimalField(
        max_digits=20,
        decimal_places=6,
        min_value=Decimal("0"),
        required=False,
    )
    asset_key = serializers.SlugRelatedField(
        source="asset", slug_field="key", queryset=Asset.objects.none(), required=False
    )
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    asset_name_fa = serializers.CharField(source="asset.name_fa", read_only=True)
    asset_class = serializers.CharField(source="asset.asset_class", read_only=True)
    is_house = serializers.BooleanField(source="asset.is_house", read_only=True)
    is_manual = serializers.BooleanField(source="asset.is_manual", read_only=True)
    unit_price_tomans = serializers.DecimalField(
        max_digits=20,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=False,
        write_only=True,
    )
    # A property is described the way its owner describes it: how big, and what a
    # square meter is worth. `quantity` stores the second of those in millions of
    # Toman (see HOUSE_AREA_SQM / valuation._house_value), which is meaningless on
    # screen, so it is never the field the client reads or writes for a house.
    price_per_sqm_million = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"),
        required=False, write_only=True,
    )
    # What a square meter COST, as opposed to what one is worth today. Same
    # unit as `price_per_sqm_million` and stored on the ledger mark rather than
    # the holding, because a property is a series of dated marks and the
    # purchase price is a fact about the acquisition, not about the latest one.
    purchase_price_per_sqm_million = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"),
        required=False, write_only=True,
    )
    price_per_sqm_tomans = serializers.SerializerMethodField()
    gross_value_tomans = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()
    # Only supplied when minting a new property; ignored otherwise.
    new_property_name = serializers.CharField(
        max_length=120, required=False, write_only=True
    )
    # Lets a client date a revaluation it is entering after the fact. Declared
    # here so DRF parses it: the view used to read it straight off request.data
    # and hand the raw STRING to a datetime comparison, so dating a property
    # mark crashed with a TypeError instead of being accepted or refused.
    occurred_at = serializers.DateTimeField(required=False, write_only=True)

    class Meta:
        model = Holding
        fields = ("id", "asset_key", "asset_name", "asset_name_fa", "asset_class", "is_house", "is_manual",
                  "quantity", "unit_price_tomans", "area_sqm", "mortgage_deduction_tomans",
                  "display_name", "label", "is_hidden",
                  "price_per_sqm_million", "purchase_price_per_sqm_million",
                  "price_per_sqm_tomans", "gross_value_tomans",
                  "new_property_name", "occurred_at",
                  "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped per request so one user cannot attach another user's property to
        # their own account by guessing its key. `visible_to` is that rule, held
        # in one place now that five other call sites needed it too.
        user = getattr(self.context.get("request"), "user", None)
        self.fields["asset_key"].queryset = Asset.objects.filter(
            is_active=True
        ).filter(visible_to(user))

    def get_label(self, obj) -> str:
        return obj.label

    def get_price_per_sqm_tomans(self, obj):
        price = obj.price_per_sqm_tomans
        return None if price is None else str(price)

    def get_gross_value_tomans(self, obj):
        price = obj.price_per_sqm_tomans
        return None if price is None else str(price * Decimal(obj.area_sqm))

    def validate_quantity(self, value):
        if value == 0 and self.context.get("request") and self.context["request"].method == "POST":
            raise serializers.ValidationError("Quantity must be positive.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        creating = request is not None and request.method == "POST"
        # A house's price-per-sqm and the generic `quantity` are the same column;
        # accept either name and normalise here so no downstream branch has to.
        if "price_per_sqm_million" in attrs:
            attrs["quantity"] = attrs.pop("price_per_sqm_million")
        if creating:
            if not attrs.get("asset") and not attrs.get("new_property_name"):
                raise serializers.ValidationError(
                    {"asset_key": "Choose an asset, or name a new property."}
                )
            if attrs.get("quantity") is None:
                raise serializers.ValidationError({"quantity": "This field is required."})
            if attrs.get("new_property_name") and not attrs.get("area_sqm"):
                raise serializers.ValidationError(
                    {"area_sqm": "A property needs its size in square meters."}
                )
        return attrs


class AccountSerializer(serializers.ModelSerializer):
    holdings = HoldingSerializer(many=True, read_only=True)

    class Meta:
        model = Account
        fields = (
            "id", "name", "broker", "goal", "holdings", "tracking_started_at",
            "cash_balance_tomans", "ledger_complete", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "tracking_started_at", "cash_balance_tomans", "ledger_complete",
            "created_at", "updated_at",
        )


class LedgerEntryInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=LedgerEntry.Kind.choices)
    asset_key = serializers.SlugField(required=False)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"), required=False
    )
    unit_price_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    amount_tomans = serializers.DecimalField(
        max_digits=24, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    area_sqm = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    mortgage_deduction_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    # What was PAID per unit, for a position being declared rather than bought.
    # Distinct from `unit_price_tomans`, which is what it is worth now. Millions
    # of Toman per square meter for a property, matching `quantity`.
    cost_basis_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    occurred_at = serializers.DateTimeField(required=False, default=timezone.now)
    source = serializers.ChoiceField(choices=("manual", "csv"), default="manual")
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    external_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Cannot be in the future.")
        return value


class LedgerEntrySerializer(serializers.ModelSerializer):
    asset_key = serializers.CharField(source="asset.key", allow_null=True, read_only=True)
    asset_name = serializers.CharField(source="asset.name", allow_null=True, read_only=True)
    asset_name_fa = serializers.CharField(source="asset.name_fa", allow_null=True, read_only=True)
    occurred_at = serializers.DateTimeField(source="timestamp", read_only=True)
    unit_price_tomans = serializers.DecimalField(
        source="price_tomans", max_digits=20, decimal_places=4, allow_null=True, read_only=True
    )
    pnl_tomans = serializers.SerializerMethodField()
    pnl_kind = serializers.SerializerMethodField()
    account_id = serializers.IntegerField(source="account.id", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    is_synthetic = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()
    # A property's `quantity` is a price per square meter, not a count, so the
    # client has to know which convention to render before it prints the number.
    is_house = serializers.BooleanField(source="asset.is_house", read_only=True, default=False)
    # Whether one of this asset divides, so the edit dialog asks for a whole
    # number where a whole number is the only answer. "any" for a cash move,
    # which has no asset to have a unit.
    quantity_step = serializers.CharField(
        source="asset.quantity_step", read_only=True, default="any"
    )
    # `unit_price_tomans` is misnamed for TSE rows: the stored quote is Rial, and
    # `amount_tomans`/`pnl_tomans` are Toman because the division lands on the
    # product. Say which currency the price is in so the client stops suffixing
    # every row " T" and printing a price that does not divide into the value.
    unit_price_currency = serializers.SerializerMethodField()
    # The ticker, for the rows whose catalog name is a company name nobody uses.
    asset_symbol = serializers.SerializerMethodField()
    # `amount_tomans` is only stored for the kinds that move money. This is what
    # the row is WORTH, derived when nothing was stored -- see entry_value_tomans.
    value_tomans = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = (
            "id", "kind", "asset_key", "asset_name", "asset_name_fa", "label",
            "asset_symbol",
            "is_house", "quantity", "quantity_step", "unit_price_tomans", "unit_price_currency",
            "amount_tomans", "value_tomans", "area_sqm", "mortgage_deduction_tomans",
            "cost_basis_tomans",
            "occurred_at", "source", "note", "external_id", "reversal_of",
            "created_at", "pnl_tomans", "pnl_kind",
            "account_id", "account_name", "is_synthetic",
        )
        read_only_fields = fields

    def get_unit_price_currency(self, obj):
        from marketdata.currency import is_tse_priced

        return "rial" if is_tse_priced(obj.asset) else "toman"

    def get_asset_symbol(self, obj):
        if not obj.asset_id:
            return None
        return obj.asset.tse_symbol or obj.asset.brs_symbol or None

    def get_value_tomans(self, obj):
        from .services.ledger import entry_value_tomans

        value = entry_value_tomans(obj)
        return None if value is None else str(value)

    def _pnl(self, obj):
        return (self.context.get("pnl") or {}).get(obj.pk) or {}

    def get_pnl_tomans(self, obj):
        return self._pnl(obj).get("pnl_tomans")

    def get_pnl_kind(self, obj):
        return self._pnl(obj).get("pnl_kind")

    def get_is_synthetic(self, obj):
        return False

    def get_label(self, obj) -> str | None:
        """The owner's name for the asset this entry moved.

        Supplied by the view as a {(account_id, asset_id): name} map rather than
        looked up per row -- the alternative is one query per ledger line. Falls
        back to the ticker, then the catalog name, when the holding is gone (a
        fully sold position keeps its history)."""
        if not obj.asset_id:
            return None
        from .services.ledger import ledger_label

        names = self.context.get("labels") or {}
        return ledger_label(obj.asset, names.get((obj.account_id, obj.asset_id), ""))


class LedgerEntryPatchSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"), required=False
    )
    unit_price_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    amount_tomans = serializers.DecimalField(
        max_digits=24, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    # A property's size is part of the row the ledger shows, so it has to be part
    # of the row the ledger can correct. Without it the endpoint accepted the
    # field, dropped it, and answered 200 -- a resize that looked saved and was not.
    area_sqm = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    # Correctable for the same reason `area_sqm` is: a purchase price entered
    # once and never fixable is a typo that outlives the portfolio.
    cost_basis_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, min_value=Decimal("0.0001"), required=False
    )
    occurred_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Cannot be in the future.")
        return value


class TradeInputSerializer(serializers.Serializer):
    """Validates a buy/sell request. `asset_key` resolves to an Asset in the view."""

    asset_key = serializers.SlugField()
    side = serializers.ChoiceField(choices=Transaction.Side.choices)
    quantity = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=Decimal("0.000001"))
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    timestamp = serializers.DateTimeField(required=False)
    price_tomans = serializers.DecimalField(max_digits=20, decimal_places=4, required=False, allow_null=True)
    source = serializers.ChoiceField(choices=(("manual", "manual"), ("imported", "imported"), ("inferred", "inferred")), default="manual")

    def validate(self, attrs):
        asset_key = attrs.get('asset_key')
        timestamp = attrs.get('timestamp') or timezone.now()
        price_tomans = attrs.get('price_tomans')
        
        if timestamp > timezone.now():
            raise serializers.ValidationError({"timestamp": "Transaction timestamp cannot be in the future."})

        # Scoped like every other asset_key entry point. This runs BEFORE the
        # view's own lookup, so leaving it unscoped meant a key belonging to
        # someone else got as far as price resolution and answered with its
        # error rather than a flat "invalid asset".
        user = getattr(self.context.get("request"), "user", None)
        asset = resolve_asset_key(user, asset_key)
        if asset is None:
            raise serializers.ValidationError({"asset_key": "Invalid or inactive asset."})

        try:
            assert_not_before_history(asset, timestamp)
            if not price_tomans:
                attrs["price_tomans"] = resolve_historical_price(asset, timestamp)
        except PriceResolutionError as exc:
            raise serializers.ValidationError({exc.field: str(exc)}) from exc

        attrs["timestamp"] = timestamp
        return attrs


class TransactionSerializer(serializers.ModelSerializer):
    """Read view of a ledger row for the trade history / chart markers."""

    asset_key = serializers.CharField(source="asset.key", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    is_latest_for_asset = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = ("id", "asset_key", "asset_name", "side", "quantity",
                  "price_tomans", "note", "timestamp", "is_latest_for_asset")

    def get_is_latest_for_asset(self, obj) -> bool:
        latest_id = (
            Transaction.objects.filter(
                account=obj.account, asset=obj.asset,
                reversal_of__isnull=True, reversed_by__isnull=True
            )
            .order_by("-timestamp", "-pk")
            .values_list("pk", flat=True)
            .first()
        )
        return latest_id == obj.pk


class LiabilitySerializer(serializers.ModelSerializer):
    asset_key = serializers.SlugRelatedField(
        source="asset",
        slug_field="key",
        queryset=Asset.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    # The asset's own name for it, so a mortgage says "Tehran flat" and not
    # "Real Estate" -- a property is minted per owner and the catalog row has
    # nothing but the asset class to put in `name`. Same rule as
    # `models.owner_display_names`, which is what the holdings screens use.
    asset_label = serializers.SerializerMethodField()

    # `amount_tomans` stops being required once a schedule can produce it, and
    # `outstanding_tomans` is what every consumer should read: the balance for
    # TODAY, derived where terms exist and the declared figure where they do
    # not. `balance_basis` says which, so the UI never presents a number the
    # user typed six months ago as though the schedule had just produced it.
    # `allow_null` because clearing is a real edit: switching a scheduled loan
    # back to a typed balance sends every term as an explicit null, and the
    # client must be able to say "no value" as distinct from "unchanged".
    amount_tomans = serializers.DecimalField(
        max_digits=20, decimal_places=4, required=False, allow_null=True
    )
    outstanding_tomans = serializers.SerializerMethodField()
    balance_basis = serializers.CharField(read_only=True)
    installments_paid = serializers.SerializerMethodField()
    scheduled_installment_tomans = serializers.SerializerMethodField()
    payoff_on = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same scoping as HoldingSerializer, for the same reason: a mortgage
        # names the property it is secured against, so an unscoped queryset lets
        # one user hang a liability off another user's house.
        user = getattr(self.context.get("request"), "user", None)
        self.fields["asset_key"].queryset = Asset.objects.filter(
            is_active=True
        ).filter(visible_to(user))

    class Meta:
        model = Liability
        fields = (
            "id",
            "account",
            "label",
            "kind",
            "lender",
            "amount_tomans",
            "outstanding_tomans",
            "balance_basis",
            "principal_tomans",
            "annual_rate_pct",
            "term_months",
            "monthly_installment_tomans",
            "scheduled_installment_tomans",
            "installments_paid",
            "started_on",
            "payoff_on",
            "asset_key",
            "asset_name",
            "asset_label",
            "derived",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "account", "derived", "created_at", "updated_at")

    def get_asset_label(self, obj):
        if not obj.asset_id:
            return None
        asset = obj.asset
        # An owner-minted property is named by whoever holds it; a shared
        # catalog row keeps the name everybody knows it by.
        if asset.owner_id:
            holding = asset.holdings.filter(account_id=obj.account_id).first()
            if holding and holding.display_name:
                return holding.display_name
        return asset.tse_symbol or asset.name_fa or asset.name

    def get_outstanding_tomans(self, obj):
        return str(obj.outstanding_tomans())

    def get_installments_paid(self, obj):
        return obj.installments_paid()

    def get_scheduled_installment_tomans(self, obj):
        value = obj.scheduled_installment_tomans()
        return None if value is None else str(value)

    def get_payoff_on(self, obj):
        payoff = obj.payoff_on()
        return payoff.isoformat() if payoff else None

    def validate(self, attrs):
        """Reject the combinations that would silently mean something else.

        Partial terms are the trap. `Liability.balance_basis` falls back a rung
        at a time, so a loan missing only its start date quietly stops
        amortizing and reports the figure typed at creation forever — right on
        the day it was entered, drifting every month after. Better to refuse
        the row than to accept it and answer with a number that ages.
        """
        merged = {
            field: attrs.get(field, getattr(self.instance, field, None))
            for field in (
                "kind", "asset", "amount_tomans", "principal_tomans",
                "annual_rate_pct", "term_months", "monthly_installment_tomans",
                "started_on",
            )
        }
        kind = merged["kind"] or Liability.Kind.OTHER

        if kind == Liability.Kind.SECURED_DEBT and merged["asset"] is None:
            raise serializers.ValidationError({
                "asset_key": "A secured debt must name the asset it is secured against."
            })

        schedule = (
            merged["principal_tomans"], merged["annual_rate_pct"],
            merged["term_months"], merged["monthly_installment_tomans"],
            merged["started_on"],
        )
        has_any_term = any(value is not None for value in schedule)
        if has_any_term:
            if merged["term_months"] is None or merged["started_on"] is None:
                raise serializers.ValidationError(
                    "A repayment schedule needs both a term and a start date."
                )
            if merged["principal_tomans"] is None and merged["monthly_installment_tomans"] is None:
                raise serializers.ValidationError(
                    "Give either the amount borrowed or the monthly installment."
                )
            if (
                merged["annual_rate_pct"] is not None
                and merged["principal_tomans"] is None
            ):
                raise serializers.ValidationError({
                    "principal_tomans": "An interest rate needs the amount borrowed to apply to."
                })
            # Principal, term and start date, with neither a rate nor an
            # installment, names no repayment rule: `balance_basis` clears
            # neither AMORTIZED nor INSTALLMENTS and drops to DECLARED, which
            # reports the amount typed rather than anything the schedule
            # implies. On a create there is nothing typed, so the row lands at
            # zero and the debt disappears from net worth entirely.
            if (
                merged["principal_tomans"] is not None
                and merged["annual_rate_pct"] is None
                and merged["monthly_installment_tomans"] is None
            ):
                raise serializers.ValidationError({
                    "annual_rate_pct": "Give the interest rate, or the monthly "
                                       "installment, so the balance can be derived."
                })
        elif merged["amount_tomans"] is None:
            raise serializers.ValidationError({
                "amount_tomans": "Enter what is owed, or the loan's repayment terms."
            })

        # A scheduled loan still stores a balance, because `amount_tomans` is
        # the column the database constrains and the fallback if the terms are
        # later cleared. Seed it from the schedule rather than making the
        # client compute a number the server already knows how to derive.
        if attrs.get("amount_tomans") is None and has_any_term:
            probe = Liability(
                amount_tomans=Decimal("0"),
                principal_tomans=merged["principal_tomans"],
                annual_rate_pct=merged["annual_rate_pct"],
                term_months=merged["term_months"],
                monthly_installment_tomans=merged["monthly_installment_tomans"],
                started_on=merged["started_on"],
            )
            # The probe is seeded at zero, so a basis of DECLARED would hand
            # back that zero as the balance. The rules above are meant to make
            # that unreachable; assert it rather than trust it, because the
            # failure is silent and reads as "this loan is paid off".
            if probe.balance_basis == Liability.BalanceBasis.DECLARED:
                raise serializers.ValidationError(
                    "These terms do not describe a repayment schedule. Give a "
                    "term, a start date, and either a rate with the amount "
                    "borrowed or the monthly installment."
                )
            attrs["amount_tomans"] = probe.outstanding_tomans()
        return attrs


# Serializer for OptimizationSnapshot persisted records
class OptimizationSnapshotSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    # source=account_id: the field carries the FK object otherwise, and
    # IntegerField(Account) raises a 500 on every account-scoped snapshot.
    account = serializers.IntegerField(source="account_id", allow_null=True, read_only=True)
    scenario = serializers.CharField(read_only=True)
    payload = serializers.JSONField(read_only=True)
    price_version = serializers.CharField(read_only=True)
    as_of = serializers.DateTimeField(allow_null=True, read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
