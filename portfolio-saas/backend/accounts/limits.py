"""Per-member caps: how many portfolios, holdings and imports one member may have.

Prices are shared across every member, but holdings, ledgers and import files
are not -- each one costs disk on a box with no backups and work in the shared
workers. Public sign-up means a stranger can create an account, so the caps live
here, in one place, and each write path asks before it adds a row.
"""
from django.conf import settings


def limit(user, name: str) -> int:
    """The cap for one member: the site default times their multiplier."""
    base = {
        "accounts": settings.MAX_ACCOUNTS_PER_USER,
        "holdings": settings.MAX_HOLDINGS_PER_USER,
        "imports_per_day": settings.MAX_IMPORTS_PER_USER_PER_DAY,
    }[name]
    return base * max(1, getattr(user, "limit_multiplier", 1) or 1)


def holding_count(user) -> int:
    from portfolio.models import Holding

    return Holding.objects.filter(account__user=user).count()


HOLDINGS_FULL = (
    "You have reached the limit of {n} holdings. Remove one you no longer track, "
    "or ask an admin to raise your limit."
)
