from rest_framework import serializers

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from .models import User


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "password",
            "first_name",
            "last_name",
        )
        read_only_fields = ("id",)

    def validate_password(self, value):
        # ModelSerializer.create bypasses AUTH_PASSWORD_VALIDATORS, so enforce
        # them here; user attrs let UserAttributeSimilarityValidator compare.
        user = User(
            email=self.initial_data.get("email", ""),
            first_name=self.initial_data.get("first_name", ""),
            last_name=self.initial_data.get("last_name", ""),
        )
        try:
            validate_password(value, user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    @transaction.atomic
    def create(self, validated_data):
        email = User.objects.normalize_email(validated_data["email"])
        user = User.objects.create_user(
            email=email,
            password=validated_data["password"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            is_active=True,
        )
        return user


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "is_staff",
            "is_superuser",
            # What the account panel needs to say "member since" and to show
            # whether this person can reach the operator console. Both were
            # already decided server-side and simply never told to the client,
            # so the app could not answer "am I an admin?" from its own UI.
            "date_joined",
            "last_login",
        )
        read_only_fields = (
            "id",
            "email",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
        )


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, required=True)
    new_password = serializers.CharField(write_only=True, required=True)
    confirm_password = serializers.CharField(write_only=True, required=True)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate(self, data):
        if data["new_password"] != data["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": ["New passwords do not match."]}
            )
        user = self.context["request"].user
        try:
            validate_password(data["new_password"], user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)})
        return data

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save()
        return user


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, data):
        if data["new_password"] != data["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": ["New passwords do not match."]}
            )
        user = self.context.get("user")
        try:
            validate_password(data["new_password"], user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)})
        return data


class PasswordAwareTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        """Reject a refresh whose user is gone or whose password has changed.

        `super().validate` is inside the guard too, not only the user lookup.
        It re-reads the token and blacklists it on rotation, so it raises a bare
        `TokenError` of its own -- which DRF does not translate and which
        therefore surfaced as a 500 rather than a 401. The window between the
        two reads is small but real: a concurrent refresh of the same token
        blacklists it in between, so the exception fired exactly when two tabs
        restored a session at once.
        """
        try:
            JWTAuthentication().get_user(self.token_class(attrs["refresh"]))
            return super().validate(attrs)
        except TokenError as exc:
            raise AuthenticationFailed("Refresh session is invalid.") from exc
