"""User model with a subscription tier.

`tier` is the single source of truth for free-vs-paid gating, qualified by
`pro_expires_at` (annual-prepay model: a verified payment stamps a 365-day
expiry). `is_pro()` is the live gate every permission/endpoint checks — it
returns False once the paid period lapses, without a separate downgrade job.

Login is by email (no username field), so the model ships an email-based
manager; the default UserManager requires a username positional arg and would
crash `create_user(email=..., password=...)`.
"""
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """create_user / create_superuser keyed on email instead of username."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        extra_fields.setdefault("email_verified_at", timezone.now())
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
    # Gateway customer reference (kept for audit; Zarinpal keys on Payment.authority).
    customer_id = models.CharField(max_length=64, blank=True, default="")
    # Google's stable per-account identifier ("sub" claim). Bound on first Google
    # sign-in so a later email change on the Google side can't orphan the login.
    google_sub = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Annual Pro expiry. None means "PRO with no expiry" (manual/grant tier).
    pro_expires_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def is_pro(self) -> bool:
        """A PRO tier whose paid period has not lapsed. None expiry never lapses."""
        if self.tier != self.Tier.PRO:
            return False
        if self.pro_expires_at is None:
            return True
        return timezone.now() < self.pro_expires_at



