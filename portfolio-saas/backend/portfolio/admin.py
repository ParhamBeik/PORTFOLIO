from django.contrib import admin

from .models import Account, Asset, Holding, Price, Snapshot, Transaction


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
    list_display = ("asset", "price", "source", "fetched_at")
    list_filter = ("source",)
    search_fields = ("asset__key",)


admin.site.register(Holding)
admin.site.register(Snapshot)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "account", "asset", "side", "quantity", "price_tomans")
    list_filter = ("side", "asset__asset_class")
    search_fields = ("account__name", "asset__key", "account__user__email")
    date_hierarchy = "timestamp"
