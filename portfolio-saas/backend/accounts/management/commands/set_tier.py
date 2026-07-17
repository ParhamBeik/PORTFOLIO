"""Set a user's subscription tier from the shell (support / dev override).

This is the only non-billing path to change a tier. In production a tier should
normally be set by the billing webhook; this command exists for support cases
and for exercising PRO features locally without a payment.

    python manage.py set_tier user@example.com PRO
    python manage.py set_tier user@example.com FREE
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from accounts.services import set_user_tier


class Command(BaseCommand):
    help = "Set a user's subscription tier (FREE or PRO)."

    def add_arguments(self, parser):
        parser.add_argument("email", help="User email.")
        parser.add_argument("tier", choices=[User.Tier.FREE, User.Tier.PRO])

    def handle(self, *args, email, tier, **options):
        User = get_user_model()
        user = User.objects.filter(email=email).first()
        if not user:
            raise CommandError(f"No user with email {email!r}.")
        set_user_tier(user, tier)
        self.stdout.write(self.style.SUCCESS(
            f"{user.email} is now {user.tier}."
        ))
