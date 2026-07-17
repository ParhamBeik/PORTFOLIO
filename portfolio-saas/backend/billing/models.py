from django.db import models


class WebhookEvent(models.Model):
    """Idempotency guard for Stripe webhook delivery.

    Stripe retries an event until it gets a 2xx, so without this the same tier
    flip would be applied repeatedly. The unique `event_id` makes the second
    delivery a no-op: the insert fails inside the same transaction that applies
    the change, so the whole thing rolls back and we just acknowledge.
    """

    event_id = models.CharField(max_length=120, unique=True)
    type = models.CharField(max_length=120)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.type} ({self.event_id})"
