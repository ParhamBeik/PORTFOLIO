from django.urls import path
from .views import (
    ChangePasswordView,
    CookieTokenObtainPairView,
    CsrfView,
    LogoutAllView,
    LogoutView,
    MeView,
    ProCheckView,
    RegisterView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", CookieTokenObtainPairView.as_view(), name="login"),
    path("csrf/", CsrfView.as_view(), name="csrf"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("pro-check/", ProCheckView.as_view(), name="pro-check"),
]
