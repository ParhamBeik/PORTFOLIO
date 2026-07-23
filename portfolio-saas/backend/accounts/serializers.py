from rest_framework import serializers

from .models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ("id", "email", "password", "first_name", "last_name")
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

    def create(self, validated_data):
        return User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
        )


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "tier",
            "is_pro",
            "pro_expires_at",
            "is_staff",
            "is_superuser",
        )
        read_only_fields = (
            "id",
            "email",
            "tier",
            "is_pro",
            "pro_expires_at",
            "is_staff",
            "is_superuser",
        )
