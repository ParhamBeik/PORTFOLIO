import datetime as dt
import pandas as pd
import jdatetime

_BASIS_ALIASES = {
    "nominal": "nominal_toman",
    "nominal_toman": "nominal_toman",
    "usd_real": "usd_denominated",
    "usd_denominated": "usd_denominated",
}


def normalize_basis(basis: str) -> str:
    """Return the canonical valuation basis while accepting one-release aliases."""
    try:
        return _BASIS_ALIASES[basis]
    except KeyError as exc:
        raise ValueError("basis must be nominal_toman or usd_denominated") from exc

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

    `nominal` and `usd_real` remain temporary aliases. USD conversion only uses
    rates already known at each timestamp and carries them for at most five
    sessions; unavailable rates remain unavailable.
    Index of the series is assumed to be DatetimeIndex.
    """
    basis = normalize_basis(basis)
    if basis == "nominal_toman":
        return series
    if basis == "usd_denominated":
        if usd_series is None:
            from marketdata.models import GoldCurrencyHistory

            if not series.index.empty:
                max_gregorian_date = series.index.max()
                jdate = jdatetime.date.fromgregorian(date=max_gregorian_date.date())
                max_jalali_str = f"{jdate.year:04d}-{jdate.month:02d}-{jdate.day:02d}"
                rows = (
                    GoldCurrencyHistory.objects
                    .filter(symbol="USD", date__lte=max_jalali_str)
                    .order_by("date")
                    .values_list("date", "close_price")
                )
            else:
                rows = (
                    GoldCurrencyHistory.objects
                    .filter(symbol="USD")
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

        usd_aligned = usd_series.reindex(series.index).ffill(limit=5)
        usd_aligned = usd_aligned.where(usd_aligned > 0)
        return series / usd_aligned
