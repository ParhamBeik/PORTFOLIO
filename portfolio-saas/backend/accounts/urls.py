from django.urls import path
from .views import (
    AdminPasswordResetLinkView,
    AdminUserListView,
    ChangePasswordView,
    CookieTokenObtainPairView,
    CsrfView,
    ExportView,
    LogoutAllView,
    LogoutView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RegisterView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("export/", ExportView.as_view(), name="export"),
    path("login/", CookieTokenObtainPairView.as_view(), name="login"),
    path("csrf/", CsrfView.as_view(), name="csrf"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("admin/users/", AdminUserListView.as_view(), name="admin-users"),
    path(
        "admin/password-reset-link/",
        AdminPasswordResetLinkView.as_view(),
        name="admin-password-reset-link",
    ),
]
