from django.contrib import admin
from django.contrib.auth.models import Group
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.models import User
from accounts.admin import RoleChangeForm
import pytest
from marketdata.models import ApiRequestQuota, WorkflowRun
from portfolio.models import Account, Asset, LedgerEntry, Price, Snapshot
from portfolio.optimization_models import OptimizationSnapshot


def test_django_admin_exposes_all_domain_and_security_models():
    from django.apps import apps

    for model in (User, Account, Asset, LedgerEntry, Price):
        assert admin.site.is_registered(model)
    for app_label in ("marketdata", "portfolio"):
        for model in apps.get_app_config(app_label).get_models():
            assert admin.site.is_registered(model), model._meta.label
    for model in (Group, OutstandingToken, BlacklistedToken,
                  ApiRequestQuota, WorkflowRun, Snapshot, OptimizationSnapshot):
        assert admin.site.is_registered(model)
    assert admin.site._registry[OutstandingToken].has_change_permission(None) is False
    assert "token" not in admin.site._registry[OutstandingToken].fields
    assert admin.site._registry[Snapshot].has_delete_permission(None) is False


def test_legacy_privilege_fields_are_hidden_and_asset_units_exposed():
    users = admin.site._registry[User]
    fields = repr(users.fieldsets) + repr(users.add_fieldsets) + repr(users.list_display)
    assert "role" in fields
    assert "is_staff" not in fields
    assert "is_superuser" not in fields
    assert "groups" not in fields

    assets = admin.site._registry[Asset]
    assert "currency" not in [field.name for field in Asset._meta.fields]
    assert "quote_unit" in assets.list_display

    prices = admin.site._registry[Price]
    assert not prices.has_add_permission(None)
    assert not prices.has_change_permission(None)
    assert not prices.has_delete_permission(None)


@pytest.mark.django_db
def test_last_active_admin_cannot_be_demoted_in_django_admin():
    user = User.objects.create_superuser(email="last-admin@test.test", password="x")
    form = RoleChangeForm(
        instance=user,
        data={
            "email": user.email, "password": user.password,
            "role": "user", "is_active": "on",
            "first_name": "", "last_name": "",
        },
    )
    assert not form.is_valid()
    assert "Keep at least one active admin" in str(form.errors)
