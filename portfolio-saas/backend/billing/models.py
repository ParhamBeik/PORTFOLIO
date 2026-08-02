from django.conf import settings
from django.db import models


class Payment(models.Model):
    """One Zarinpal payment attempt for a Pro upgrade.

    The flow: `ZarinpalRequestView` creates a PENDING row (authority issued by
    Zarinpal), the user pays on Zarinpal's hosted page, Zarinpal redirects them
    to our callback with `Authority` + `Status`, we verify server-side and flip
    the row to VERIFIED (activating Pro in the same step via `activate_pro`).

    Idempotency rests on two things: `authority` is unique, and activation only
    fires when status != VERIFIED. A callback replay or a page refresh finds an
    already-verified row and no-ops.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )
    former_customer_id = models.UUIDField(null=True, blank=True, db_index=True)
    # Zarinpal's per-payment token; unique so a duplicate callback can't fork state.
    authority = models.CharField(max_length=64, unique=True)
    amount_rial = models.PositiveBigIntegerField()
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    ref_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Payment {self.authority} ({self.status})"
