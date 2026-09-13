from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


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

    list_display = ("email", "is_active", "is_staff", "is_superuser", "date_joined", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-date_joined",)
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("date_joined", "last_login")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_active", "is_staff"),
            },
        ),
    )
