"""DRF exception handling for domain errors that any endpoint can raise.

`CpiUnavailable` is handled globally rather than in each view because *every*
endpoint accepting a `basis` query parameter can trigger it — valuation,
as-of valuation, performance, analytics, optimization, frontier, my-optimal.
Catching it per-view means the next basis-accepting endpoint added silently
returns 500 instead of an honest "this basis is unavailable".
"""
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from portfolio.services.deflator import CpiUnavailable


def handle(exc, context):
    """Translate domain exceptions DRF does not know about."""
    if isinstance(exc, CpiUnavailable):
        # 503, not 400: the request is valid, we simply do not hold the data
        # required to answer it. Inflation-adjusted figures must never fall
        # back to nominal ones wearing a "real" label.
        return Response(
            {
                "detail": str(exc),
                "reason": "cpi_unavailable",
                "basis": "real_toman",
                "requested_jalali_year": exc.jalali_year,
                "last_verified_jalali_year": exc.last_verified_year,
                "remedy": "Set CPI_BY_JALALI_YEAR_EXTRA with the missing year.",
            },
            status=503,
        )
    return drf_exception_handler(exc, context)
