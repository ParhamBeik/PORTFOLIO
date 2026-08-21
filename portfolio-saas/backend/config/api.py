"""How this API meets the wire: JSON rendering and global error translation.

Decimal renders as a string because DRF's default encoder turns it into a float,
which loses precision on large Toman values. Clients coerce with `Number()` (the
frontend's `fmtNum` already does), so this stays wire-compatible while being
exact end-to-end.

`CpiUnavailable` is handled globally rather than per view because *every*
endpoint accepting a `basis` query parameter can raise it -- valuation, as-of
valuation, performance, analytics, optimization, frontier, my-optimal. Catching
it per view means the next basis-accepting endpoint added silently 500s instead
of answering honestly.
"""
from decimal import Decimal

from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder
from rest_framework.views import exception_handler as drf_exception_handler

from portfolio.services.deflator import CpiUnavailable


class DecimalAsStringEncoder(JSONEncoder):
    def default(self, obj):
        return str(obj) if isinstance(obj, Decimal) else super().default(obj)


class DecimalStringJSONRenderer(JSONRenderer):
    encoder_class = DecimalAsStringEncoder


def handle(exc, context):
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
                "remedy": (
                    "Set CPI_BY_JALALI_YEAR_EXTRA to the published index for "
                    "that year, or CPI_ESTIMATED_MONTHLY_RATE to project one "
                    "forward as a labelled estimate."
                ),
            },
            status=503,
        )
    return drf_exception_handler(exc, context)
