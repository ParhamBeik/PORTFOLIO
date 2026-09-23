from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from .models import User


class RoleChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if (
            self.instance.pk
            and self.instance.role == User.Role.ADMIN
            and self.instance.is_active
            and (cleaned.get("role") != User.Role.ADMIN or not cleaned.get("is_active"))
            and not User.objects.filter(role=User.Role.ADMIN, is_active=True)
            .exclude(pk=self.instance.pk).exists()
        ):
            raise ValidationError("Keep at least one active admin before demoting or deactivating this user.")
        return cleaned


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Django's own user admin, adapted to an email-keyed user.

    This subclasses `auth.admin.UserAdmin` rather than `ModelAdmin` for one
    concrete reason: a plain `ModelAdmin` renders `password` as an ordinary
    CharField pre-filled with the stored hash, and **saving that form writes
    whatever text is in the box back as the hash** -- so an operator who opens a
    member to tick a checkbox and hits Save silently destroys that person's
    ability to log in, with no error and no way to tell it happened. `UserAdmin`
    replaces it with a read-only hash plus the separate set-password form.

    `is_active` is the only ban lever in this codebase (there is no suspension
    model), so it has to be visible on the changelist and filterable -- it was
    neither. The in-app equivalent is `AdminUserDetailView`, which additionally
    revokes live refresh tokens; this form does not, so a ban applied here takes
    effect for the access token immediately and for a refresh token when it next
    rotates. Prefer the Ops console for bans.

    `username` is None on this model, so every fieldset and both of
    `add_fieldsets`/`ordering` must be restated -- the inherited ones name a
    field that does not exist and would raise at form build time.
    """

    list_display = ("email", "is_active", "role", "date_joined", "last_login")
    form = RoleChangeForm
    list_filter = ("is_active", "role")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-date_joined",)
    filter_horizontal = ()
    readonly_fields = ("date_joined", "last_login")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        (
            "Permissions",
            {"fields": ("is_active", "role")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_active", "role"),
            },
        ),
    )


class GroupDiagnosticsAdmin(admin.ModelAdmin):
    """Show legacy groups without making them a second authorization system."""

    list_display = ("name", "member_count")
    search_fields = ("name",)
    readonly_fields = ("name", "permissions", "members")
    fields = readonly_fields

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.user_set.count()

    @admin.display(description="Members")
    def members(self, obj):
        return ", ".join(obj.user_set.values_list("email", flat=True)) or "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class TokenDiagnosticsAdmin(admin.ModelAdmin):
    """Expose revocation evidence, never a replayable raw JWT."""

    list_display = ("user", "jti", "created_at", "expires_at")
    search_fields = ("user__email", "jti")
    readonly_fields = list_display
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class BlacklistDiagnosticsAdmin(TokenDiagnosticsAdmin):
    list_display = ("token", "blacklisted_at")
    search_fields = ("token__user__email", "token__jti")
    readonly_fields = list_display
    fields = readonly_fields


for model, model_admin in (
    (Group, GroupDiagnosticsAdmin),
    (OutstandingToken, TokenDiagnosticsAdmin),
    (BlacklistedToken, BlacklistDiagnosticsAdmin),
):
    if admin.site.is_registered(model):
        admin.site.unregister(model)
    admin.site.register(model, model_admin)
