"""Canonicalise stored email addresses to lowercase.

`UserManager.normalize_email` now lowercases the whole address so that
`get_by_natural_key` can stay an exact match on the unique index. That is only
true if every row already written is canonical, which is what this fixes.

Measured against production before writing it: 7 users, 0 with a non-lowercase
address, 0 that would collide when folded. So this is a no-op there and exists
for the databases nobody measured -- a developer's local copy, a restored
backup, any environment that took a signup before the fix.

It refuses rather than guesses. If two rows fold onto one address the migration
raises, because the alternatives are both worse than a failed deploy: dropping
one row destroys a member's portfolio, and keeping both leaves the exact
ambiguity `get_by_natural_key` is now assuming cannot exist. A human has to say
which account is the real one.
"""
from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


def lowercase_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")

    clashing = list(
        User.objects.values(folded=Lower("email"))
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("folded", flat=True)
    )
    if clashing:
        raise RuntimeError(
            "Cannot canonicalise email addresses: these differ only by case and "
            f"would collide on the unique index: {clashing}. Decide which account "
            "is authoritative and merge or delete the other by hand, then re-run "
            "the migration."
        )

    User.objects.exclude(email=Lower("email")).update(email=Lower("email"))


def noop(apps, schema_editor):
    """Irreversible by nature -- the original casing is not recorded anywhere."""


class Migration(migrations.Migration):

    dependencies = [("accounts", "0001_squashed")]

    operations = [migrations.RunPython(lowercase_emails, noop)]
