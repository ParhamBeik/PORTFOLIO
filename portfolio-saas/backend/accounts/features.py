"""Central tier -> capability and tier -> limit registry.

One place answers "what does this tier get?". Views name a capability
(`RequiresFeature("optimization")`) and serializers ask for a ceiling
(`limit_for(user, "portfolios")`); neither compares tiers by hand. Adding a
paid feature means adding a row here, not another ad-hoc check in a view.

Note the backtest quota is deliberately NOT modelled here: it is a per-day
counter keyed on `BacktestUserQuota` and driven by the long-standing
`DAILY_BACKTEST_LIMIT` setting, which deployments and tests already override.
Folding it in would rename a working, tested knob for symmetry alone.
"""
from django.conf import settings
from rest_framework.permissions import BasePermission

from .models import User

FREE = User.Tier.FREE
PRO = User.Tier.PRO

# Capabilities that require a paid tier. Anything not listed is available to
# any authenticated user, so the absence of a name here is itself the answer.
PRO_FEATURES = frozenset(
    {
        "analytics",
        "asset_ranking",
        "asset_returns",
        "backtest",
        "discovery",
        "frontier",
        "insights",
        "market_announcements",
        "market_compare",
        "market_shareholders",
        "optimization",
    }
)

# Per-tier ceilings. None means unlimited. Each key maps to the settings names
# that override it, so limits stay configurable per deployment (the blueprint
# asks for a "high configurable portfolio limit" on Pro).
_LIMITS = {
    "portfolios": {FREE: 3, PRO: None},
}
_LIMIT_SETTINGS = {
    "portfolios": {FREE: "FREE_PORTFOLIO_LIMIT", PRO: "PRO_PORTFOLIO_LIMIT"},
}


def tier_of(user) -> str:
    """The effective tier of `user`. Anonymous and lapsed Pro both read FREE."""
    if user and user.is_authenticated and user.is_pro():
        return PRO
    return FREE


def has_feature(user, key: str) -> bool:
    """True when `user` may use the capability `key`."""
    if not (user and user.is_authenticated):
        return False
    if key not in PRO_FEATURES:
        return True
    return tier_of(user) == PRO


def limit_for(user, key: str):
    """Ceiling for `key` at this user's tier. None means unlimited."""
    tier = tier_of(user)
    override = getattr(settings, _LIMIT_SETTINGS[key][tier], None)
    if override is not None:
        return int(override)
    return _LIMITS[key][tier]


def RequiresFeature(key: str):
    """Build a DRF permission gating on a registry capability.

    A factory rather than a parameterised class because DRF instantiates
    `permission_classes` entries itself and cannot pass constructor args.
    """
    if key not in PRO_FEATURES:
        raise ValueError(f"Unknown paid capability: {key!r}")

    class _RequiresFeature(BasePermission):
        message = "This feature requires a Pro subscription."

        def has_permission(self, request, view):
            return has_feature(request.user, key)

    _RequiresFeature.__name__ = f"Requires_{key}"
    return _RequiresFeature
