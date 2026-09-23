from rest_framework.permissions import BasePermission


class IsRoleAdmin(BasePermission):
    """Application authorization uses the role, not Django compatibility flags."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.role == "admin")
