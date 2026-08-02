from django.contrib import admin

from .models import Invitation, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "tier", "email_verified_at", "is_staff", "date_joined")
    list_filter = ("tier", "is_staff")
    search_fields = ("email",)
    ordering = ("-date_joined",)


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "expires_at", "used_at", "created_by", "created_at")
    list_filter = ("used_at",)
    search_fields = ("email", "token_hash")
    readonly_fields = ("token_hash", "used_at", "used_by", "created_at")
