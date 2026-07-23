from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView

from .views import ChangePasswordView, MeView, ProCheckView, RegisterView

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", TokenObtainPairView.as_view(), name="login"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("pro-check/", ProCheckView.as_view(), name="pro-check"),
]

