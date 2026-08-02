"""Permissions that gate paid features.

The tier decision itself lives in `accounts.features`; this module only exposes
DRF-shaped wrappers around it. `RequiresFeature("<capability>")` is the form to
reach for in new code — it names *which* paid feature is being gated, so the
registry stays the single answer to "what does Pro get?".
"""
from rest_framework.permissions import BasePermission

from .features import PRO, RequiresFeature, has_feature, limit_for, tier_of  # noqa: F401


class IsPro(BasePermission):
    """Only allow PRO-tier users. Free users get a 403 with a clear message.

    Retained for gates with no finer-grained capability name (e.g. the
    `/api/auth/pro-check/` probe). Feature endpoints use `RequiresFeature`.
    """

    message = "This insight requires a Pro subscription."

    def has_permission(self, request, view):
        return tier_of(request.user) == PRO
