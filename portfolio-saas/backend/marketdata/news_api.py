"""Server-to-server price contract for News Intelligence: catalog, series, snapshot.

Same gate as `shared_series`: a constant-time `X-News-Service-Key` check
against `NEWS_MARKET_SERVICE_KEY`, and 403 for everyone when the key is unset.
No user data is reachable from here -- `news_feed` reads public origins only.
"""
import hmac
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from . import news_feed

MAX_SNAPSHOT_KEYS = 40
MAX_SERIES_DAYS = 3650


class HasNewsServiceKey(BasePermission):
    message = "Service access denied"

    def has_permission(self, request, view):
        expected = settings.NEWS_MARKET_SERVICE_KEY
        supplied = request.headers.get("X-News-Service-Key", "")
        return bool(expected) and hmac.compare_digest(supplied, expected)


class _NewsServiceView(APIView):
    authentication_classes = []
    permission_classes = [HasNewsServiceKey]
    # One caller, already authenticated by the key, rendering ~25 tiles a page.
    throttle_classes = []


class NewsCatalogView(_NewsServiceView):
    def get(self, request):
        return Response({"groups": {k: {"name_fa": fa, "name_en": en}
                                    for k, (fa, en) in news_feed.GROUPS.items()},
                         "results": news_feed.catalog_rows()})


def _parse_day(raw, default):
    if not raw:
        return default
    return date.fromisoformat(raw)


class NewsSeriesView(_NewsServiceView):
    def get(self, request):
        item = news_feed.CATALOG.get(request.query_params.get("key", ""))
        if item is None:
            return Response({"detail": "Unknown series key"}, status=404)
        today = timezone.now().date()
        try:
            until = _parse_day(request.query_params.get("to"), today)
            since = _parse_day(request.query_params.get("from"), until - timedelta(days=30))
        except ValueError:
            return Response({"detail": "from/to must be YYYY-MM-DD"}, status=400)
        if since > until or (until - since).days > MAX_SERIES_DAYS:
            return Response({"detail": f"from must precede to, within {MAX_SERIES_DAYS} days"}, status=400)
        interval = request.query_params.get("interval", "1d")
        if interval not in {"1d", "1w"}:
            return Response({"detail": "interval must be 1d or 1w"}, status=400)
        try:
            return Response(news_feed.series(item, since, until, interval))
        except news_feed.FeedUnavailable:
            return Response({"key": item.key, "points": [], "caveats": ["source_unavailable"]},
                            status=503)


class NewsSnapshotView(_NewsServiceView):
    def get(self, request):
        keys = [k for k in request.query_params.get("keys", "").split(",") if k]
        keys = keys or list(news_feed.CATALOG)
        unknown = [k for k in keys if k not in news_feed.CATALOG]
        if unknown:
            return Response({"detail": "Unknown series key", "keys": unknown}, status=404)
        if len(keys) > MAX_SNAPSHOT_KEYS:
            return Response({"detail": f"At most {MAX_SNAPSHOT_KEYS} keys"}, status=400)
        return Response({"as_of": timezone.now(), "results": news_feed.snapshot(keys)})
