"""Promote family@portfolio.local to staff and permanent Pro.

Does not grant superuser. Warehouse CRUD stays off this mailbox.
LOCKED: do not run against production without explicit approval.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User


class Command(BaseCommand):
    help = "Promote family@portfolio.local to staff + permanent Pro (not superuser)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--i-approve-locked-operation", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        if not options["i_approve_locked_operation"] and not options["dry_run"]:
            raise CommandError(
                "Refusing: pass --i-approve-locked-operation (or --dry-run)."
            )
        email = "family@portfolio.local"
        try:
            user = User.objects.select_for_update().get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"Account {email} does not exist.") from exc
        changes = {
            "is_staff": True,
            "is_superuser": False,
            "is_active": True,
        }
        self.stdout.write(f"Would apply to {email}: {changes}" if options["dry_run"] else f"Applying to {email}: {changes}")
        if options["dry_run"]:
            return
        for k, v in changes.items():
            setattr(user, k, v)
        user.save(update_fields=list(changes.keys()))
        self.stdout.write(self.style.SUCCESS(f"Promoted {email}."))
