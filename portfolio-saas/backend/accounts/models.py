"""User model with a subscription tier.

`tier` is the single source of truth for free-vs-paid gating. A real billing
provider (Stripe) flips this field via webhook; `set_tier` is the only other path.

Login is by email (no username field), so the model ships an email-based manager.
The default UserManager still requires a username positional arg and would crash
`create_user(email=..., password=...)` — including the register endpoint.
"""
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """create_user / create_superuser keyed on email instead of username."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Tier(models.TextChoices):
        FREE = "FREE", "Free"
        PRO = "PRO", "Pro"

    # Email is the login identifier.
    username = None
    email = models.EmailField(unique=True)
    tier = models.CharField(
        max_length=8, choices=Tier.choices, default=Tier.FREE, db_index=True
    )
    # Stripe customer id once billing is wired in.
    customer_id = models.CharField(max_length=64, blank=True, default="")

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def is_pro(self) -> bool:
        return self.tier == self.Tier.PRO
