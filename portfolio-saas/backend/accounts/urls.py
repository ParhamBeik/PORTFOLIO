from django.urls import path
from .views import (
    ChangePasswordView,
    CookieTokenObtainPairView,
    CsrfView,
    ExportView,
    InvitationCreateView,
    LogoutAllView,
    LogoutView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    ProCheckView,
    RegisterView,
    ResendVerificationView,
    VerifyEmailView,
    AdminUserListView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("invitations/", InvitationCreateView.as_view(), name="invitations"),
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path(
        "resend-verification/",
        ResendVerificationView.as_view(),
        name="resend-verification",
    ),
    path(
        "password-reset/request/",
        PasswordResetRequestView.as_view(),
        name="password-reset-request",
    ),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("export/", ExportView.as_view(), name="export"),
    path("login/", CookieTokenObtainPairView.as_view(), name="login"),
    path("csrf/", CsrfView.as_view(), name="csrf"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("pro-check/", ProCheckView.as_view(), name="pro-check"),
    path("admin/users/", AdminUserListView.as_view(), name="admin-users"),
]
