from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "tier", "email_verified_at", "is_staff", "date_joined")
    list_filter = ("tier", "is_staff")
    search_fields = ("email",)
    ordering = ("-date_joined",)

