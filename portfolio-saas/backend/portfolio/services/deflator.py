import datetime as dt
import pandas as pd
import jdatetime

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
    """Convert price series to the specified basis ('nominal' or 'usd_real').

    For 'usd_real', divides the series by the daily USD exchange rate.
    Index of the series is assumed to be DatetimeIndex.
    """
    if basis == "nominal":
        return series
    if basis == "usd_real":
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
                return series

            dates, closes = zip(*rows)
            usd_series = pd.Series(
                pd.to_numeric(pd.Series(closes), errors="coerce").values,
                index=_jalali_to_gregorian_index(pd.Series(dates)),
            )
            usd_series = usd_series[usd_series.index.notna()]
            usd_series = usd_series[usd_series > 0]
            usd_series = usd_series.groupby(usd_series.index).last()

        # Align usd_series to series.index
        usd_aligned = usd_series.reindex(series.index).ffill().bfill()
        usd_aligned = usd_aligned.fillna(1.0).replace(0.0, 1.0)
        return series / usd_aligned

    return series
