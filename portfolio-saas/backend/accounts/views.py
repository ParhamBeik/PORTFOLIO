"""Authentication and account endpoints."""
import csv
import io
import json
import zipfile

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import generics, serializers, status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from portfolio.models import Account, Holding, ImportBatch, LedgerEntry

from .models import User
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


def _session_expires_at() -> str:
    return (timezone.now() + settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]).isoformat()


def _revoke_all(user: User) -> None:
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


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
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        refresh = response.data.pop("refresh")
        response.data["session_expires_at"] = _session_expires_at()
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
        payload["session_expires_at"] = _session_expires_at()
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
        _revoke_all(request.user)
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
        response = Response(
            {
                "user": UserSerializer(user).data,
                "access": access,
                "session_expires_at": _session_expires_at(),
            },
            status=status.HTTP_201_CREATED,
        )
        return _set_refresh_cookie(response, request, refresh)


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @transaction.atomic
    def delete(self, request):
        if request.data.get("confirmation") != "DELETE":
            return Response(
                {"confirmation": ["Type DELETE to confirm."]}, status=400
            )
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not user.check_password(request.data.get("password", "")):
            return Response({"password": ["Password is incorrect."]}, status=400)
        _revoke_all(user)
        user.delete()
        return _clear_refresh_cookie(Response(status=204))


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        _revoke_all(user)
        access, refresh = _tokens(user)
        return _set_refresh_cookie(Response({
            "detail": "Password updated successfully.", "access": access,
        }), request, refresh)


def _csv_bytes(headers, rows) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode()


class ExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        accounts = Account.objects.filter(user=user)
        account_ids = list(accounts.values_list("id", flat=True))
        files = {
            "profile.csv": _csv_bytes(
                ["id", "email", "first_name", "last_name"],
                [[user.id, user.email, user.first_name, user.last_name]],
            ),
            "accounts.csv": _csv_bytes(
                ["id", "name", "broker", "goal", "cash_balance_tomans", "ledger_complete"],
                accounts.values_list(
                    "id", "name", "broker", "goal", "cash_balance_tomans", "ledger_complete"
                ),
            ),
            "ledger.csv": _csv_bytes(
                ["id", "account_id", "kind", "asset_id", "quantity", "price_tomans", "amount_tomans", "timestamp", "source", "external_id"],
                LedgerEntry.objects.filter(account_id__in=account_ids).values_list(
                    "id", "account_id", "kind", "asset_id", "quantity",
                    "price_tomans", "amount_tomans", "timestamp", "source",
                    "external_id",
                ),
            ),
            "holdings.csv": _csv_bytes(
                ["id", "account_id", "asset_id", "quantity", "area_sqm", "mortgage_deduction_tomans"],
                Holding.objects.filter(account_id__in=account_ids).values_list(
                    "id", "account_id", "asset_id", "quantity", "area_sqm",
                    "mortgage_deduction_tomans",
                ),
            ),
            "imports.csv": _csv_bytes(
                ["id", "account_id", "file_hash", "row_count", "created_at"],
                ImportBatch.objects.filter(account_id__in=account_ids).values_list(
                    "id", "account_id", "file_hash", "row_count", "created_at"
                ),
            ),
        }
        manifest = {
            "format": "lattice-account-export-v1",
            "generated_at": timezone.now().isoformat(),
            "files": sorted(files),
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, content in files.items():
                archive.writestr(name, content)
        response = HttpResponse(output.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = 'attachment; filename="lattice-export.zip"'
        return response


class AdminUserListView(generics.ListAPIView):
    """Staff-only search/list view of registered users."""

    permission_classes = [IsAdminUser]
    serializer_class = UserSerializer

    def get_queryset(self):
        queryset = User.objects.all().order_by("-date_joined")
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(email__icontains=search)
        return queryset
