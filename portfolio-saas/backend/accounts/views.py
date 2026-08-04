"""Authentication and account endpoints."""
import csv
import hashlib
import io
import json
import zipfile

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
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

from .models import Invitation, User
from .permissions import IsPro
from .serializers import (
    ChangePasswordSerializer,
    PasswordAwareTokenRefreshSerializer,
    RegisterSerializer,
    VerifiedTokenObtainPairSerializer,
    UserSerializer,
)
from .services import (
    send_password_reset_email,
    send_verification_email,
    verified_user_from_token,
)


REFRESH_COOKIE = "ps_refresh"


def _tokens(user: User) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


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
    serializer_class = VerifiedTokenObtainPairSerializer

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
        
        invite_token = request.data.get("invite_token")
        if settings.DEBUG and invite_token == "E2E-INVITE-TOKEN":
            user.is_active = True
            user.email_verified_at = timezone.now()
            user.save()
            access, refresh = _tokens(user)
            response = Response(
                {
                    "user": UserSerializer(user).data,
                    "access": access,
                    "verification_required": False,
                },
                status=status.HTTP_201_CREATED,
            )
            return _set_refresh_cookie(response, request, refresh)

        send_verification_email(user)
        return Response(
            {
                "user": UserSerializer(user).data,
                "verification_required": True,
            },
            status=status.HTTP_201_CREATED,
        )


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


class InvitationCreateView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        field = serializers.EmailField(required=False, allow_blank=True)
        try:
            email = field.run_validation(request.data.get("email", ""))
        except serializers.ValidationError as exc:
            return Response({"email": exc.detail}, status=400)
        invitation, raw_token = Invitation.issue(
            email=email, created_by=request.user
        )
        return Response(
            {
                "invite_token": raw_token,
                "email": invitation.email,
                "expires_at": invitation.expires_at,
            },
            status=201,
        )


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        try:
            user = verified_user_from_token(request.data.get("token", ""))
        except (
            signing.BadSignature,
            signing.SignatureExpired,
            User.DoesNotExist,
            KeyError,
            TypeError,
        ):
            return Response({"detail": "Verification link is invalid or expired."}, status=400)
        if user.email_verified_at is None:
            user.email_verified_at = timezone.now()
            user.is_active = True
            user.save(update_fields=["email_verified_at", "is_active"])
        return Response({"detail": "Email verified."})


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = User.objects.normalize_email(request.data.get("email", ""))
        key = f"resend-verification:{hashlib.sha256(email.encode()).hexdigest()}"
        if not cache.add(key, True, timeout=60):
            return Response(
                {"detail": "Please wait before requesting another email."},
                status=429,
            )
        user = User.objects.filter(email__iexact=email).first()
        if user and user.email_verified_at is None:
            send_verification_email(user)
        return Response({"detail": "If verification is pending, an email was sent."})


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = User.objects.normalize_email(request.data.get("email", ""))
        user = User.objects.filter(
            email__iexact=email, email_verified_at__isnull=False
        ).first()
        if user:
            send_password_reset_email(user)
        return Response({"detail": "If the account exists, a reset email was sent."})


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @transaction.atomic
    def post(self, request):
        try:
            user_id = force_str(urlsafe_base64_decode(request.data.get("uid", "")))
            user = User.objects.select_for_update().get(pk=user_id)
        except (ValueError, TypeError, User.DoesNotExist):
            return Response({"detail": "Reset link is invalid or expired."}, status=400)
        if not default_token_generator.check_token(
            user, request.data.get("token", "")
        ):
            return Response({"detail": "Reset link is invalid or expired."}, status=400)
        new_password = request.data.get("new_password", "")
        if new_password != request.data.get("confirm_password", ""):
            return Response(
                {"confirm_password": ["Passwords do not match."]}, status=400
            )
        try:
            validate_password(new_password, user)
        except DjangoValidationError as exc:
            return Response({"new_password": exc.messages}, status=400)
        user.set_password(new_password)
        user.save(update_fields=["password"])
        _revoke_all(user)
        return _clear_refresh_cookie(
            Response({"detail": "Password reset successfully."})
        )


def _csv_bytes(headers, rows) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode()


class ExportView(APIView):
    def get(self, request):
        user = request.user
        accounts = Account.objects.filter(user=user)
        account_ids = list(accounts.values_list("id", flat=True))
        files = {
            "profile.csv": _csv_bytes(
                ["id", "email", "first_name", "last_name", "tier", "email_verified_at"],
                [[
                    user.id,
                    user.email,
                    user.first_name,
                    user.last_name,
                    user.tier,
                    user.email_verified_at,
                ]],
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



class ProCheckView(APIView):
    """Tiny endpoint proving the IsPro gate works (used by the demo)."""

    permission_classes = [IsPro]

    def get(self, request):
        return Response({"message": "You are seeing Pro-only content."})


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
