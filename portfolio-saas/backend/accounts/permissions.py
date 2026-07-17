"""Permissions that gate paid features."""
from rest_framework.permissions import BasePermission


class IsPro(BasePermission):
    """Only allow PRO-tier users. Free users get a 403 with a clear message."""

    message = "This insight requires a Pro subscription."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_pro())
