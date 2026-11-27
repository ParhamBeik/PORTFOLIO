from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import User


@shared_task
def delete_unconfirmed_users() -> int:
    """Remove public sign-ups that never opened their confirmation link.

    They could never sign in, so they own no data; staff accounts are created
    confirmed and are excluded anyway.
    """
    cutoff = timezone.now() - timedelta(days=settings.UNCONFIRMED_USER_DAYS)
    deleted, _ = User.objects.filter(
        email_confirmed_at__isnull=True, date_joined__lt=cutoff, role=User.Role.USER
    ).delete()
    return deleted
