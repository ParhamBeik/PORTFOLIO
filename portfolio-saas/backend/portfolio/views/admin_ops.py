"""Staff-only maintenance endpoints.

Separated so the `IsAdminUser` surface is small enough to audit at a
glance, and so a destructive operation can never be one import away
from an ordinary user-facing view."""
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAdminUser
from portfolio.management.commands.clean_mispriced_data import audit_and_repair_prices
from .ledger import admin_logger


class AdminCleanPricesScanView(APIView):
    """Scan database for mispriced price rows and corrupted snapshots (Admin only)."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        stats = audit_and_repair_prices(fix=False)
        return Response(stats)


class AdminCleanPricesExecuteView(APIView):
    """Execute database cleanup: delete corrupted price rows, repair snapshots, and log action (Admin only).

    Destructive and irreversible (permanently deletes Price/Snapshot rows), so
    it requires the caller to echo back CONFIRM_PHRASE rather than firing on a
    bare POST — a single accidental click must not be enough to trigger it.
    """

    permission_classes = [IsAdminUser]
    CONFIRM_PHRASE = "DELETE MISPRICED DATA"

    def post(self, request):
        if request.data.get("confirm") != self.CONFIRM_PHRASE:
            return Response(
                {"detail": f'This is destructive and irreversible. Send {{"confirm": "{self.CONFIRM_PHRASE}"}} to execute.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        stats = audit_and_repair_prices(fix=True)
        admin_logger.info("[ADMIN_ACTION] %s executed price cleanup: %s", request.user.email, stats)
        return Response(stats, status=status.HTTP_200_OK)
