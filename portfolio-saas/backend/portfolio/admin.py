from django.contrib import admin

from .models import Account, Asset, Holding, LedgerEntry, Price, Snapshot
from .optimization_models import OptimizationSnapshot  # persisted optimization payloads


class HoldingInline(admin.TabularInline):
    model = Holding
    extra = 1


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "asset_class", "currency", "is_manual", "is_house")
    list_filter = ("asset_class", "is_manual")
    search_fields = ("key", "name")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "broker", "created_at")
    list_filter = ("broker",)
    search_fields = ("name", "user__email")
    inlines = [HoldingInline]


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = ("asset", "price", "price_unit", "price_unit_verified", "source", "fetched_at")
    list_filter = ("source", "price_unit", "price_unit_verified")
    search_fields = ("asset__key",)
    readonly_fields = ("fetched_at",)


admin.site.register(Holding)
admin.site.register(Snapshot)


@admin.register(OptimizationSnapshot)
class OptimizationSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "scenario", "account", "created_at")
    list_filter = ("scenario",)
    readonly_fields = ("payload", "created_at")
    search_fields = ("account__user__email",)

@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "account", "asset", "kind", "quantity", "amount_tomans")
    list_filter = ("kind", "asset__asset_class")
    search_fields = ("account__name", "asset__key", "account__user__email")
    date_hierarchy = "timestamp"
