"""Authentication and account endpoints."""
from django.conf import settings
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import User
from .permissions import IsPro
from .serializers import (
    ChangePasswordSerializer,
    PasswordAwareTokenRefreshSerializer,
    RegisterSerializer,
    UserSerializer,
)


REFRESH_COOKIE = "ps_refresh"


def _tokens(user: User) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


def _set_refresh_cookie(response, request, refresh: str):
    get_token(request)
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        secure=getattr(settings, "JWT_COOKIE_SECURE", not settings.DEBUG),
        httponly=True,
        samesite="Strict",
        path="/api/",
    )
    return response


class CookieTokenObtainPairView(TokenObtainPairView):
    serializer_class = TokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        refresh = response.data.pop("refresh")
        return _set_refresh_cookie(response, request, refresh)


@method_decorator(csrf_protect, name="dispatch")
class CookieTokenRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        refresh = request.COOKIES.get(REFRESH_COOKIE)
        if not refresh:
            return Response({"detail": "Refresh session is missing."}, status=401)
        serializer = PasswordAwareTokenRefreshSerializer(data={"refresh": refresh})
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        rotated = payload.pop("refresh", refresh)
        return _set_refresh_cookie(Response(payload), request, rotated)


class CsrfView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({"csrf_token": get_token(request)})


def _clear_refresh_cookie(response):
    response.delete_cookie(REFRESH_COOKIE, path="/api/", samesite="Strict")
    return response


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        refresh = request.COOKIES.get(REFRESH_COOKIE)
        if refresh:
            try:
                RefreshToken(refresh).blacklist()
            except TokenError:
                pass
        return _clear_refresh_cookie(Response(status=204))


@method_decorator(csrf_protect, name="dispatch")
class LogoutAllView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        for token in OutstandingToken.objects.filter(user=request.user):
            BlacklistedToken.objects.get_or_create(token=token)
        return _clear_refresh_cookie(Response(status=204))


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        access, refresh = _tokens(user)
        return _set_refresh_cookie(Response(
            {"user": UserSerializer(user).data, "access": access},
            status=status.HTTP_201_CREATED,
        ), request, refresh)


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ChangePasswordView(APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        access, refresh = _tokens(user)
        return _set_refresh_cookie(Response({
            "detail": "Password updated successfully.", "access": access,
        }), request, refresh)



class ProCheckView(APIView):
    """Tiny endpoint proving the IsPro gate works (used by the demo)."""

    permission_classes = [IsPro]

    def get(self, request):
        return Response({"message": "You are seeing Pro-only content."})
