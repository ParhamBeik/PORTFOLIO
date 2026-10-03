"""User model.

Login is by email (no username field), so the model ships an email-based
manager; the default UserManager requires a username positional arg and would
crash `create_user(email=..., password=...)`.
"""
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """create_user / create_superuser keyed on email instead of username."""

    use_in_migrations = True

    def normalize_email(self, email):
        """Lowercase the WHOLE address, not just the domain.

        Django's version lowercases only the domain, so `Case@example.com` and
        `case@example.com` are two different strings, the unique index never
        sees them as the same person, and signing up with the other casing
        silently creates a second empty portfolio -- to the member it looks like
        their data vanished. Mail providers treat the local part
        case-insensitively in practice, and this codebase already did too in one
        place: password reset has always looked up `email__iexact`, so a member
        could reset a password for an address they could not log in with.

        Storing one canonical form is what lets `get_by_natural_key` stay an
        exact match on the unique index instead of an unindexed `iexact`.
        """
        return super().normalize_email(email or "").lower()

    def get_by_natural_key(self, username):
        """Authenticate case-insensitively.

        Every credential path -- `authenticate()`, the JWT obtain serializer,
        `manage.py changepassword` -- funnels through here, so normalizing once
        at this seam covers them all. Safe as an exact match because every
        stored address is already canonical: `normalize_email` above enforces it
        on write and migration 0002 fixed the rows written before it existed.
        """
        return self.get(**{self.model.USERNAME_FIELD: self.normalize_email(username)})

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault(
            "role",
            "admin" if extra_fields.get("is_staff") or extra_fields.get("is_superuser") else "user",
        )
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields["role"] = "admin"
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        USER = "user", "User"

    class RiskProfile(models.TextChoices):
        CONSERVATIVE = "conservative", "Conservative"
        BALANCED = "balanced", "Balanced"
        GROWTH = "growth", "Growth"

    # Email is the login identifier.
    username = None
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=5, choices=Role.choices, default=Role.USER)
    risk_profile = models.CharField(
        max_length=12, choices=RiskProfile.choices, default=RiskProfile.BALANCED
    )
    # Dead: the payment integration and its Payment model are gone, and nothing has
    # ever written this. Dropped in the Phase 3 schema migration.
    customer_id = models.CharField(max_length=64, blank=True, default="")

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(role="admin", is_staff=True, is_superuser=True)
                    | models.Q(role="user", is_staff=False, is_superuser=False)
                ),
                name="user_role_matches_django_flags",
            )
        ]

    def save(self, *args, **kwargs):
        # Django admin and DRF still depend on these internal compatibility flags.
        update_fields = kwargs.get("update_fields")
        admin = self.role == self.Role.ADMIN
        self.is_staff = admin
        self.is_superuser = admin
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"role", "is_staff", "is_superuser"}
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Tear the portfolio down innermost-first, then delete the user.

        Django's collector gathers an entire cascade before deleting anything,
        so a PROTECT pointing at something else inside the same cascade is a
        deadlock, not an ordering it can resolve. This graph has two of them:

          * `LedgerEntry.import_batch` PROTECTs `ImportBatch`, which is CASCADEd
            from `Account` -- so deleting an account that ever ran a CSV import
            raised ProtectedError. Pre-existing, and it broke account closure
            for those users.
          * `Holding.asset` / `LedgerEntry.asset` / `Liability.asset` PROTECT
            `Asset`, which is CASCADEd from the user for real estate (a property
            belongs to one person and must not outlive them into the shared
            catalog) -- so closing the account failed once you added a property.

        Both dissolve the same way: remove the rows that hold the PROTECT
        references first, so by the time the account and user cascades run
        nothing points at the things they are about to take with them. Ordered
        here rather than in the delete-account view so every caller -- admin,
        shell, management command -- gets it.
        """
        from portfolio.models import Holding, LedgerEntry, Liability

        accounts = self.accounts.all()
        LedgerEntry.all_objects.filter(account__in=accounts).delete()
        Holding.objects.filter(account__in=accounts).delete()
        Liability.objects.filter(account__in=accounts).delete()
        accounts.delete()
        return super().delete(*args, **kwargs)
