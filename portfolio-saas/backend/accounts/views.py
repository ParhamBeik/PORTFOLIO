"""Authentication and account endpoints."""
import csv
import hashlib
import io
import json
import logging
import time
import zipfile

from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import csrf_protect
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
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
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)
RESET_TOKEN = PasswordResetTokenGenerator()
RESET_ACCEPTED = "If an account exists for that email, a reset link has been sent."
RESET_INVALID = "This reset link is invalid or has expired."


REFRESH_COOKIE = "ps_refresh"

# How long a just-rotated refresh token keeps replaying its own response. Long
# enough to cover a lost round trip, short enough that a stolen token is still
# effectively single-use. See CookieTokenRefreshView.post.
ROTATION_GRACE_SECONDS = 30
# How long one request may hold the rotation for a token, and how long another
# will wait for its answer. Both are bounded so a dead request cannot wedge a
# session and a waiting one cannot occupy a worker.
ROTATION_LOCK_SECONDS = 5
ROTATION_WAIT_SECONDS = 3.0


def _tokens(user: User) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


def _session_expires_at() -> str:
    return (timezone.now() + settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]).isoformat()


def _revoke_all(user: User) -> None:
    """Blacklist every refresh token this user still holds, in one round trip.

    Two bounds, both of which the previous per-token `get_or_create` loop
    lacked. Refresh rotation writes an `OutstandingToken` row on every refresh,
    so a year-old account has thousands: the loop issued a SELECT and an INSERT
    for each, inside the request that changes a password or deletes an account.

    Already-expired tokens are skipped because blacklisting them buys nothing --
    `RefreshToken()` rejects them on expiry before the blacklist is consulted --
    and `prune_expired_refresh_tokens` deletes them nightly anyway.
    """
    live = OutstandingToken.objects.filter(user=user, expires_at__gte=timezone.now())
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in live],
        ignore_conflicts=True,
    )


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
        # JWT login never fires Django's user_logged_in, so last_login stays
        # null and Ops "People and books" prints an em dash. Stamp only here;
        # cookie refresh is not a new sign-in.
        ident = request.data.get(User.USERNAME_FIELD)
        if ident:
            User.objects.filter(
                **{User.USERNAME_FIELD: User.objects.normalize_email(ident)}
            ).update(last_login=timezone.now())
        refresh = response.data.pop("refresh")
        response.data["session_expires_at"] = _session_expires_at()
        return _set_refresh_cookie(response, request, refresh)


def _rotation_replay_key(refresh: str) -> str:
    """Cache key for the response a given refresh token already produced.

    Keyed on a hash of the token string rather than its `jti` so a token that is
    already blacklisted -- the whole case this exists for -- needs no decoding.
    """
    return f"jwt-rotation-replay:{hashlib.sha256(refresh.encode()).hexdigest()}"


def _await_replay(key: str, wait: float = ROTATION_WAIT_SECONDS):
    """Poll for the rotation another request is currently performing.

    The replay cache alone only covers a response that was *lost* -- it is
    written after the rotation completes, so two genuinely simultaneous
    refreshes (two tabs restoring at once) both miss it and one still loses.
    The loser waits here for the winner's answer instead of failing.
    """
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        cached = cache.get(key)
        if cached:
            return cached
        time.sleep(0.05)
    return None


def _replayable(payload: dict) -> bool:
    """True if the cached rotation is still safe to hand out again.

    A grace entry outlives a `logout-all` / password change by up to
    ROTATION_GRACE_SECONDS, and replaying it then would mint a fresh 30-minute
    access token for a session the user just revoked. The successor refresh is
    blacklisted by that revocation, so checking it closes the window.
    """
    successor = payload.get("refresh")
    if not successor:
        return False
    try:
        jti = RefreshToken(successor, verify=False).payload.get("jti")
    except TokenError:
        return False
    return not BlacklistedToken.objects.filter(token__jti=jti).exists()


@method_decorator(csrf_protect, name="dispatch")
class CookieTokenRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        """Exchange the refresh cookie for an access token, rotating the cookie.

        Rotation is single-use (`BLACKLIST_AFTER_ROTATION`), which makes the
        exchange fatal if the *response* is lost: the server has already burned
        the old token, so a browser that never committed the `Set-Cookie` is
        signed out on its next request. That is not a theoretical race -- the
        session restore fires on every page load, and clicking a nav link inside
        its round trip cancels it. Reproduced in four navigations.

        So the exchange is made idempotent for a short window: the response a
        token produced is cached under that token, and presenting it again
        inside ROTATION_GRACE_SECONDS replays the same answer instead of
        failing. Beyond the window the token is single-use again, which is the
        property rotation is for.
        """
        refresh = request.COOKIES.get(REFRESH_COOKIE)
        if not refresh:
            return Response({"detail": "Refresh session is missing."}, status=401)

        key = _rotation_replay_key(refresh)
        cached = cache.get(key)
        # Only one request may rotate a given token; the rest replay its answer.
        # `add` is the atomic claim (SETNX on Redis), and its short TTL means a
        # request that dies mid-rotation costs the next one a wait, not a wedge.
        if cached is None and not cache.add(f"{key}:lock", 1, ROTATION_LOCK_SECONDS):
            cached = _await_replay(key)
        if cached and _replayable(cached):
            replay = dict(cached)
            return _set_refresh_cookie(Response(replay), request, replay.pop("refresh"))

        serializer = PasswordAwareTokenRefreshSerializer(data={"refresh": refresh})
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        rotated = payload.pop("refresh", refresh)
        payload["session_expires_at"] = _session_expires_at()
        if rotated != refresh:
            cache.set(key, {**payload, "refresh": rotated}, ROTATION_GRACE_SECONDS)
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
        if not settings.REGISTRATION_OPEN:
            return Response(
                {"detail": "New memberships are currently closed."},
                status=status.HTTP_403_FORBIDDEN,
            )
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


def _user_from_uid(uid: str) -> User | None:
    try:
        pk = force_str(urlsafe_base64_decode(uid))
        return User.objects.select_for_update().get(pk=pk, is_active=True)
    except (ValueError, TypeError, OverflowError, UnicodeDecodeError, User.DoesNotExist):
        return None


def _reset_link(user: User) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = RESET_TOKEN.make_token(user)
    return f"{settings.FRONTEND_PASSWORD_RESET_URL}?uid={uid}&token={token}"


def _send_reset_mail(user: User) -> None:
    link = _reset_link(user)
    send_mail(
        subject="Reset your Holdings password",
        message=(
            "Reset your Holdings password by opening this link. "
            "It expires in three days.\n\n"
            f"{link}\n\n"
            "If you did not ask for this, you can ignore the email."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = User.objects.normalize_email(serializer.validated_data["email"])
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            try:
                _send_reset_mail(user)
            except Exception:
                logger.exception("password-reset: failed to send mail to uid=%s", user.pk)
        return Response({"detail": RESET_ACCEPTED})


class AdminPasswordResetLinkView(APIView):
    """Mint a reset link a superuser can hand over out of band.

    Password reset is a two-part system -- mint a token, deliver it -- and only
    delivery is broken here: there is no mail relay, so `PasswordResetRequestView`
    answers 200 and sends nothing, and a user who forgets their password has no
    route back into their own account. The token half works and is exercised by
    the existing confirm flow.

    So this exposes the working half. An operator reads the link and delivers it
    however they already reach that person; the user redeems it through the
    ordinary `password-reset/confirm/` endpoint. That turns "unrecoverable" into
    "recoverable with an operator in the loop", which is a support process rather
    than a dead end, and it needs no third-party account.

    **It grants no new power.** A Django superuser can already set another
    user's password outright. This is strictly weaker: the link is single-use,
    expires on the same schedule as an emailed one, and the operator never
    chooses or learns the password -- the user sets it themselves.

    Deliberately not a replacement for a mail relay: it does not scale past a
    handful of users and it puts an operator in every recovery. Configure a
    transactional-mail provider and self-service reset starts working with no
    change here.
    """

    permission_classes = [IsAdminUser]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({"detail": "Superuser access required."}, status=403)
        email = request.data.get("email") if isinstance(request.data, dict) else None
        if not isinstance(email, str) or not email.strip():
            return Response({"detail": "email is required."}, status=400)
        normalized = User.objects.normalize_email(email.strip())
        user = User.objects.filter(email__iexact=normalized, is_active=True).first()
        if user is None:
            # No enumeration concern here -- the caller is already staff and can
            # list every user -- so this says plainly what happened rather than
            # handing back a link-shaped answer for an address that has none.
            return Response({"detail": "No active user with that email."}, status=404)
        # Audited through the log rather than a model: `accounts` does not import
        # `marketdata`, and inverting that dependency to reach SystemLogEvent
        # would cost more than this line is worth. WARNING level so it stands out
        # in a stream that is otherwise INFO.
        logger.warning(
            "admin-password-reset-link issued by uid=%s for uid=%s",
            request.user.pk, user.pk,
        )
        return Response({
            "link": _reset_link(user),
            "expires_in_seconds": settings.PASSWORD_RESET_TIMEOUT,
            "detail": (
                "Single-use link. Deliver it to the account holder yourself; "
                "they set the password, you never see it."
            ),
        })


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    @transaction.atomic
    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"detail": RESET_INVALID}, status=400)
        uid = request.data.get("uid") or ""
        token = request.data.get("token") or ""
        if not isinstance(uid, str) or not isinstance(token, str):
            return Response({"detail": RESET_INVALID}, status=400)
        # Lock before checking the token: a concurrent reset must see the new
        # password hash, which makes this single-use token invalid.
        user = _user_from_uid(uid)
        if user is None or not RESET_TOKEN.check_token(user, token):
            return Response({"detail": RESET_INVALID}, status=400)
        serializer = PasswordResetConfirmSerializer(
            data=request.data, context={"user": user}
        )
        serializer.is_valid(raise_exception=True)
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        _revoke_all(user)
        return Response({"detail": "Password updated. Sign in with the new password."})


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
