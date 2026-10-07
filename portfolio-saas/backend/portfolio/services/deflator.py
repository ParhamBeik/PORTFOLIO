import datetime as dt
import pandas as pd
import jdatetime
from django.conf import settings

from config.settings import CpiUnavailable  # re-exported: callers catch this here

__all__ = ["CpiUnavailable", "normalize_basis", "cpi_for_date", "to_basis"]

_BASIS_ALIASES = {
    None: "nominal_toman",
    "nominal": "nominal_toman",
    "nominal_toman": "nominal_toman",
    "usd_real": "usd_denominated",
    "usd_denominated": "usd_denominated",
    "real_toman": "real_toman",
    "usdt_denominated": "usdt_denominated",
}


def normalize_basis(basis: str | None) -> str:
    """Return the canonical valuation basis while accepting one-release aliases."""
    try:
        return _BASIS_ALIASES[basis]
    except KeyError as exc:
        raise ValueError(
            "basis must be nominal_toman, usd_denominated, usdt_denominated, or real_toman"
        ) from exc


def cpi_for_date(value) -> float:
    """Linearly interpolate the configured annual CPI index within a Jalali year.

    Raises CpiUnavailable when the date's own Jalali year has no CPI value at
    all (e.g. the current year before it's been added to the table) — this
    must never fall back to a stale number. If only the *next* year's anchor
    is missing (the normal case for the newest year in the table, since next
    year's index isn't published yet), degrade to a flat rate for the rest of
    the current year rather than guessing — this is not the same failure as
    not knowing the current year's own value.
    """
    if isinstance(value, str):
        value = dt.date.fromisoformat(value[:10])
    if isinstance(value, pd.Timestamp):
        value = value.date()
    if isinstance(value, dt.datetime):
        value = value.date()
    jdate = jdatetime.date.fromgregorian(date=value)
    start = settings.CPI_FOR(jdate.year)
    try:
        end = settings.CPI_FOR(jdate.year + 1)
    except CpiUnavailable:
        # ponytail: no anchor for next year yet; flat through year-end instead
        # of extrapolating. Upgrade automatically once next year's CPI lands
        # in CPI_BY_JALALI_YEAR_EXTRA.
        end = start
    days = 366 if jdatetime.date(jdate.year, 12, 29).isleap() else 365
    elapsed = (jdate - jdatetime.date(jdate.year, 1, 1)).days
    return start + (end - start) * elapsed / days

def _jalali_to_gregorian_index(dates: pd.Series) -> pd.DatetimeIndex:
    """Jalali "1403-10-19" strings -> tz-aware Gregorian DatetimeIndex."""
    def convert(value):
        try:
            y, m, d = (int(part) for part in str(value).split("-"))
            g = jdatetime.date(y, m, d).togregorian()
            return dt.datetime(g.year, g.month, g.day, tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            return pd.NaT

    return pd.DatetimeIndex([convert(v) for v in dates])

def to_basis(
    series: pd.Series,
    basis: str,
    usd_series: pd.Series | None = None,
) -> pd.Series:
    """Convert a price series to nominal Toman or USD-denominated values.

    `nominal` and `usd_real` remain temporary aliases. Currency conversion
    uses its own observed series and carries a rate for at most five calendar
    days; unavailable rates remain unavailable.
    Index of the series is assumed to be DatetimeIndex.
    """
    basis = normalize_basis(basis)
    if basis == "nominal_toman":
        return series
    if basis == "real_toman":
        cpi = pd.Series(
            [cpi_for_date(value) for value in series.index],
            index=series.index,
            dtype=float,
        )
        return series / cpi * 100.0
    if basis in ("usd_denominated", "usdt_denominated"):
        if usd_series is None:
            from marketdata.models import GoldCurrencyHistory
            from marketdata.provenance import BRS_SERIES_ENDPOINTS, rejected_pairs

            symbol = "USDT_IRT" if basis == "usdt_denominated" else "USD"
            rates = GoldCurrencyHistory.objects.filter(symbol=symbol, close_price__gt=0)
            since = None
            if not series.index.empty:
                jdate = jdatetime.date.fromgregorian(date=series.index.max().date())
                rates = rates.filter(
                    date__lte=f"{jdate.year:04d}-{jdate.month:02d}-{jdate.day:02d}"
                )
                # The alignment below looks back at most 5 days (merge_asof
                # tolerance), so no older rate can be used; a sixth day of pad
                # covers the UTC/Tehran date edge. It loaded the whole history.
                floor = jdatetime.date.fromgregorian(
                    date=series.index.min().date() - dt.timedelta(days=6)
                )
                since = f"{floor.year:04d}-{floor.month:02d}-{floor.day:02d}"
                rates = rates.filter(date__gte=since)
            rejected = rejected_pairs([symbol], BRS_SERIES_ENDPOINTS, since=since)
            rows = list(
                rates.exclude(date__in=[day for sym, day in rejected if sym == symbol])
                .order_by("date")
                .values_list("date", "close_price")
            )

            if not rows:
                return series * float("nan")

            dates, closes = zip(*rows)
            usd_series = pd.Series(
                pd.to_numeric(pd.Series(closes), errors="coerce").values,
                index=_jalali_to_gregorian_index(pd.Series(dates)),
            )
            usd_series = usd_series[usd_series.index.notna()]
            usd_series = usd_series[usd_series > 0]
            usd_series = usd_series.groupby(usd_series.index).last()

        # Reindexing to only the requested dates makes `ffill(limit=5)` count
        # observations, not days: five sparse points can span years. Retain the
        # observation date and enforce a real elapsed-time bound instead.
        target = pd.DataFrame({
            "when": pd.to_datetime(series.index, utc=True),
            "position": range(len(series)),
        }).sort_values("when")
        rates = pd.DataFrame({
            "when": pd.to_datetime(usd_series.index, utc=True),
            "rate": usd_series.to_numpy(),
        }).sort_values("when")
        aligned = pd.merge_asof(
            target, rates, on="when", direction="backward",
            tolerance=pd.Timedelta(days=5),
        ).sort_values("position")
        usd_aligned = pd.Series(aligned["rate"].to_numpy(), index=series.index)
        usd_aligned = usd_aligned.where(usd_aligned > 0)
        return series / usd_aligned
