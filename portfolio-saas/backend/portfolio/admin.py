from django.apps import apps
from django.contrib import admin
from decimal import Decimal, ROUND_HALF_UP

from .models import Account, Asset, Holding, LedgerEntry, Liability, Price


class HoldingInline(admin.TabularInline):
    model = Holding
    extra = 1


class LiabilityInline(admin.TabularInline):
    model = Liability
    extra = 0


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        "key", "name", "asset_class", "quote_unit", "valuation_unit",
        "exposure_group", "is_manual", "is_house",
    )
    list_filter = ("asset_class", "is_manual")
    search_fields = ("key", "name")
    readonly_fields = ("quote_unit", "valuation_unit", "exposure_group", "quantity_scale")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "broker", "created_at")
    list_filter = ("broker",)
    search_fields = ("name", "user__email")
    inlines = [HoldingInline, LiabilityInline]


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = ("asset", "price", "price_unit", "price_unit_verified", "source", "fetched_at")
    list_filter = ("source", "price_unit", "price_unit_verified")
    search_fields = ("asset__key",)
    readonly_fields = ("fetched_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "account", "asset", "kind", "quantity_display", "amount_display", "removed_at")
    list_filter = ("kind", "asset__asset_class", ("removed_at", admin.EmptyFieldListFilter))
    search_fields = ("account__name", "asset__key", "account__user__email")
    date_hierarchy = "timestamp"
    list_select_related = ("asset", "account")

    def get_queryset(self, request):
        # Operators must see removed rows; the default manager hides them.
        return LedgerEntry.all_objects.select_related(*self.list_select_related)

    @admin.display(description="Quantity")
    def quantity_display(self, obj):
        if obj.quantity is None:
            return "—"
        value = Decimal(obj.quantity)
        return str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f")

    @admin.display(description="Amount (Toman)")
    def amount_display(self, obj):
        if obj.amount_tomans is None:
            return "—"
        value = Decimal(obj.amount_tomans)
        rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return str(int(rounded)) if value == rounded else f"{int(rounded)} (from {value})"


class PortfolioDiagnosticsAdmin(admin.ModelAdmin):
    """Expose historical and supporting tables without bypassing domain writes."""

    list_per_page = 50

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in apps.get_app_config("portfolio").get_models():
    if not admin.site.is_registered(model):
        admin.site.register(model, PortfolioDiagnosticsAdmin)
