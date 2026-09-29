"""Helpers shared by more than one view module.

Request-scope parsing (`_scope`, `_int_param`), the valuation-basis
rescalers every money-returning endpoint runs its payload through, and
the analytics concurrency cap. Nothing here talks to a URL; if a helper
is only used by one module it belongs in that module instead."""
from decimal import Decimal
from functools import wraps
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from ..services.deflator import cpi_for_date, normalize_basis
from ..services.valuation import LIABILITY_MONEY_FIELDS


def concurrency_cap(view_func):
    """Redis/cache-backed concurrency cap on expensive analytics views.

    Rejects immediately with HTTP 429 + Retry-After if concurrent requests exceed
    the ceiling (2 per user, 5 globally across workers). Never queues.
    """
    @wraps(view_func)
    def wrapper(self, request, *args, **kwargs):
        from django.core.cache import cache

        user_ident = (
            request.user.id
            if getattr(request, "user", None) and request.user.is_authenticated
            else request.META.get("REMOTE_ADDR", "anon")
        )
        user_key = f"concurrency:analytics:user:{user_ident}"
        global_key = "concurrency:analytics:global"

        if cache.add(user_key, 1, timeout=180):
            current_user = 1
        else:
            try:
                current_user = cache.incr(user_key)
            except ValueError:
                current_user = 1

        if cache.add(global_key, 1, timeout=180):
            current_global = 1
        else:
            try:
                current_global = cache.incr(global_key)
            except ValueError:
                current_global = 1

        # Sourced from settings rather than written inline. The two ceilings sit
        # beside DRF's throttle rates, which have always been env-tunable, and
        # the e2e suite is told by playwright.config.js to raise its limits for
        # a run -- but these two could not be raised at all, so a parallel run
        # of the optimizer specs failed on "Too many concurrent optimization
        # requests" with no lever to pull. Defaults are the previous literals,
        # so nothing changes unless someone sets them.
        per_user = int(getattr(settings, "ANALYTICS_MAX_CONCURRENT_PER_USER", 2))
        global_cap = int(getattr(settings, "ANALYTICS_MAX_CONCURRENT_GLOBAL", 5))
        if current_user > per_user or current_global > global_cap:
            try:
                u = cache.decr(user_key)
                if u <= 0:
                    cache.delete(user_key)
            except ValueError:
                cache.delete(user_key)
            try:
                g = cache.decr(global_key)
                if g <= 0:
                    cache.delete(global_key)
            except ValueError:
                cache.delete(global_key)
            resp = Response(
                {"detail": "Too many concurrent optimization requests. Please try again shortly."},
                status=429,
            )
            resp["Retry-After"] = "5"
            return resp

        try:
            return view_func(self, request, *args, **kwargs)
        finally:
            try:
                u = cache.decr(user_key)
                if u <= 0:
                    cache.delete(user_key)
            except ValueError:
                cache.delete(user_key)
            try:
                g = cache.decr(global_key)
                if g <= 0:
                    cache.delete(global_key)
            except ValueError:
                cache.delete(global_key)

    return wrapper


def _scope(request):
    """Resolve the active portfolio from `?account=<id>`, owned by the user.

    Returns the Account or None. None means "all portfolios" only when the
    parameter is absent; invalid or unowned ids are explicit client errors.
    """
    raw = request.query_params.get("account")
    if not raw:
        return None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("account must be an integer id.")
    account = request.user.accounts.filter(pk=account_id).first()
    if account is None:
        raise NotFound("Account not found.")
    return account


def _with_usd(valuation: dict) -> dict:
    """Attach a USD equivalent of the total using the USD price in Tomans.

    Omitted entirely when there is no USD rate (M3): a real 0 would be
    indistinguishable from 'we know the rate and it is zero'.
    """
    prices = valuation.get("prices", {})
    usd_rate = Decimal(prices.get("usd_cash", 0) or 0)
    if usd_rate:
        valuation["total_usd"] = valuation["total"] / usd_rate
    return valuation


def _int_param(request, name, default, *, clamp=None, allowed=None, strict=True):
    """Read one integer query param; returns `(value, error_response_or_None)`.

    Nine views hand-rolled this and had already drifted apart -- `except
    ValueError` in one place and `except (TypeError, ValueError)` in the next,
    a silent fallback here and a 400 there. `strict=False` keeps the two
    endpoints that deliberately fall back to their default instead of failing.
    """
    try:
        value = int(request.query_params.get(name) or default)
    except (TypeError, ValueError):
        if strict:
            return None, Response({"detail": f"{name} must be an integer."}, status=400)
        value = default
    if allowed is not None and value not in allowed:
        options = ", ".join(str(a) for a in allowed[:-1])
        return None, Response(
            {"detail": f"{name} must be {options}, or {allowed[-1]}."}, status=400
        )
    if clamp:
        value = max(clamp[0], min(value, clamp[1]))
    return value, None


def _fx_rate(prices, basis):
    """The Toman-per-unit rate for a denominated basis, and which currency it was.

    USDT falls back to USD rather than refusing: a USDT-denominated view with no
    USDT quote is still answerable, and naming the rate actually used matters
    more than the request failing.
    """
    if basis == "usdt_denominated":
        rate = Decimal(prices.get("usdt_irt", 0) or 0)
        if rate > 0:
            return rate, "USDT"
    return Decimal(prices.get("usd_cash", 0) or 0), "USD"


def _rescale(valuation, factor, *, to_foreign_currency=False):
    """Divide every monetary field of a valuation payload by `factor`, in place.

    One walk for both re-expressions below (FX and CPI) -- they differ only in
    where the divisor comes from, and a second copy of this traversal is how a
    newly added money field ends up deflated on one basis but not the other.

    `to_foreign_currency` says the result is no longer denominated in Iranian
    money, which is the one case where a Rial-quoted TSE price has to be brought
    onto the Toman scale before the divide. Deflating to constant Tomans does
    not: real Rial is still Rial, and the label stays honest.
    """
    def scale_items(items):
        for item in items or []:
            # A TSE quote is Rial while its `value` is Toman -- the division lands
            # on the product, never the price. Dividing that Rial price straight
            # by an FX rate produces a "dollar" price ten times too big, so
            # `quantity x unit_price` came out at ten times the `value` beside it:
            # the very mismatch the Rial label was added to remove, moved onto the
            # foreign bases. Normalise the price to Toman FIRST, then convert, and
            # say that it is no longer Rial.
            from marketdata.currency import TSE_RIAL_PER_TOMAN

            if to_foreign_currency and item.get("unit_price_currency") == "rial":
                if item.get("unit_price") is not None:
                    item["unit_price"] = float(
                        Decimal(str(item["unit_price"])) / TSE_RIAL_PER_TOMAN
                    )
                item["unit_price_currency"] = "toman"
            # `price_per_sqm_tomans` is money too: a property left in Toman while
            # its own value column converted would read as an absurd unit price.
            for field in ("value", "unit_price", "price_per_sqm_tomans"):
                if item.get(field) is not None:
                    item[field] = float(Decimal(str(item[field])) / factor)

    def scale_liabilities(rows):
        # `total_liabilities` below is the sum of exactly these rows. Converting
        # the sum and not its addends is the same trap one line further down,
        # one level deeper: an itemised debt list that does not add up to the
        # total printed above it, in a payload that has declared its basis.
        # `value_user` rebuilds these as fresh dicts per account, so the root
        # list and the per-account lists are separate objects and each is
        # divided exactly once.
        # Every money column on the row, not just the netted one: a loan also
        # reports what was borrowed and what is paid monthly, and those are
        # money in exactly the same way. `LIABILITY_MONEY_FIELDS` is the list,
        # kept beside the builder that produces the rows.
        for row in rows or []:
            for field in LIABILITY_MONEY_FIELDS:
                if row.get(field) is not None:
                    row[field] = float(Decimal(str(row[field])) / factor)

    valuation["total"] = Decimal(str(valuation.get("total", 0) or 0)) / factor
    if valuation.get("cash_tomans") is not None:
        valuation["cash_tomans"] = Decimal(str(valuation["cash_tomans"])) / factor
    if valuation.get("total_usd") is not None:
        valuation["total_usd"] = Decimal(str(valuation["total_usd"])) / factor
    # Debt is money. It is already netted out of `total`, so leaving it in Toman
    # only shows up when something reads the field on its own -- which is exactly
    # the "deflated on one basis but not the other" trap this single walk exists
    # to close, and it would report a mortgage at 42,000x under a dollar basis.
    if valuation.get("total_liabilities") is not None:
        valuation["total_liabilities"] = float(
            Decimal(str(valuation["total_liabilities"])) / factor
        )
    # Switched-off rows are still displayed, so they are re-expressed alongside
    # the counted ones even though they are absent from the total.
    scale_items(valuation.get("items"))
    scale_items(valuation.get("hidden_items"))
    scale_liabilities(valuation.get("liabilities"))
    for account in valuation.get("accounts") or []:
        if account.get("total") is not None:
            account["total"] = Decimal(str(account["total"])) / factor
        if account.get("cash_tomans") is not None:
            account["cash_tomans"] = Decimal(str(account["cash_tomans"])) / factor
        # `value_user` puts a `total_liabilities` on every account as well as on
        # the root, so converting only the root left the aggregate debt in
        # dollars beside each account's debt in Toman, in one payload.
        if account.get("total_liabilities") is not None:
            account["total_liabilities"] = float(
                Decimal(str(account["total_liabilities"])) / factor
            )
        scale_items(account.get("items"))
        scale_items(account.get("hidden_items"))
        scale_liabilities(account.get("liabilities"))
    return valuation


def _express_usd_real(valuation: dict, basis: str = "usd_denominated") -> dict:
    """Re-express a live Toman valuation in USD or USDT using the live rate."""
    basis = normalize_basis(basis)
    rate, source = _fx_rate(valuation.get("prices", {}), basis)
    if rate > 0:
        _rescale(valuation, rate, to_foreign_currency=True)
        # Past this point the total *is* the USD/USDT figure.
        valuation["total_usd"] = valuation["total"]
        valuation["basis"] = basis
    else:
        # No rate -- currency fetch down, or a cold cache -- so nothing was
        # converted and the figures are still Toman. Stamping the requested
        # basis anyway was survivable while the client read the picker and was
        # wrong in the same direction; now that it trusts this field, saying
        # "usd_denominated" over Toman renders a 33-billion-Toman portfolio as
        # $33,600,000,000. The snapshot endpoint answers the same way.
        valuation["basis"] = "nominal_toman"
    valuation["conversion_source"] = source
    return valuation


def _express_real_toman(valuation: dict) -> dict:
    """Deflate a live Toman valuation by the CPI index for today.

    That index is an SCI release for verified years and an operator projection
    beyond them, so the payload carries `cpi_estimated_years` and `cpi_source`
    to say which was used. Never assume the number here is published data.
    """
    _rescale(valuation, Decimal(str(cpi_for_date(timezone.now()))) / Decimal("100"))
    valuation["basis"] = "real_toman"
    valuation["cpi_vintage_year"] = settings.CPI_VERIFIED_THROUGH_YEAR
    # The deflator may have used an estimated anchor. Say so rather than letting
    # a projected index pass for a published one.
    valuation["cpi_estimated_years"] = sorted(settings.CPI_ESTIMATED_YEARS)
    valuation["cpi_source"] = settings.CPI_SOURCE
    return valuation
