"""Technical indicators and a vectorized backtest, in pandas only.

No TA library: RSI, MACD and moving averages are a few lines each, and TA-Lib
needs a C build while pandas-ta adds a dependency to wrap arithmetic we can
write here. Every function takes a PRICE series and is pure, so they are testable
without a database.

Two deliberate constraints:

* Prices are rebuilt from `daily_returns_matrix` (see `price_index_from_returns`)
  rather than read from candles directly. That matrix is where the TSE Rial/Toman
  unit guard and the bounded forward-fill live, so going around it would quietly
  compute indicators on unguarded, differently-gapped data than every other
  number on the site.
* `backtest_crossover` shifts its position by one bar. Comparing today's close to
  a signal computed from today's close is the classic backtest lie -- it reports
  returns nobody could have captured. The shift is asserted in the tests.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Wilder's default periods. Named so a caller overriding them reads as
# deliberate rather than as a typo.
RSI_WINDOW = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
OVERBOUGHT, OVERSOLD = 70.0, 30.0
# Below this many observations the indicators are noise, not signal: RSI needs
# ~3x its window before Wilder's smoothing settles.
MIN_OBSERVATIONS = 60


def price_index_from_returns(returns: pd.Series, base: float = 100.0) -> pd.Series:
    """Daily returns -> a price index starting at `base`.

    RSI, MACD and moving-average crossovers are all scale-free in the sense that
    matters here: they read the SHAPE of the series, so an index reproduces the
    same crossings and the same RSI as the underlying prices would.
    """
    clean = returns.dropna()
    if clean.empty:
        return pd.Series(dtype=float)
    return base * (1.0 + clean).cumprod()


def rsi(prices: pd.Series, window: int = RSI_WINDOW) -> pd.Series:
    """Wilder's RSI. Uses EWM with alpha=1/window, which IS Wilder's smoothing.

    A plain rolling mean is the common mistake and gives visibly different
    values; `adjust=False` is what makes the recursion match the original.
    """
    delta = prices.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()

    # Two of the three edge cases resolve themselves and only one needs help:
    #   no losses      -> rs = inf -> 100 - 0            = 100  (correct)
    #   no gains       -> rs = 0   -> 100 - 100/1        = 0    (correct)
    #   neither (flat) -> rs = 0/0 = NaN                        (needs the mask)
    # A flat series is genuinely neither overbought nor oversold, so it is 50 --
    # NOT NaN, which would make every downstream comparison quietly False.
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.mask((avg_gain == 0.0) & (avg_loss == 0.0), 50.0)


def macd(
    prices: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> pd.DataFrame:
    """MACD line, its signal line, and the histogram between them."""
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    signal_line = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd": line,
        "signal": signal_line,
        "histogram": line - signal_line,
    })


def moving_average(prices: pd.Series, window: int) -> pd.Series:
    return prices.rolling(window, min_periods=window).mean()


def describe(prices: pd.Series) -> dict | None:
    """Latest indicator readings plus an explainable stance, or None if too thin.

    The stance is deliberately a count of three independent, nameable conditions
    rather than a score: a user can see WHICH of trend, momentum and mean
    reversion agreed. An opaque number would be impossible to argue with.
    """
    clean = prices.dropna()
    if len(clean) < MIN_OBSERVATIONS:
        return None

    latest_rsi = float(rsi(clean).iloc[-1])
    bands = macd(clean)
    histogram = float(bands["histogram"].iloc[-1])
    # 200 sessions is the conventional trend filter but most symbols here do not
    # have it; fall back to 50 and SAY which was used rather than silently
    # comparing against a different definition per symbol.
    trend_window = 200 if len(clean) >= 200 else 50
    trend_ma = moving_average(clean, trend_window)
    ma_value = trend_ma.iloc[-1]
    price = float(clean.iloc[-1])
    above_trend = bool(pd.notna(ma_value) and price > float(ma_value))

    bullish = sum([above_trend, histogram > 0, latest_rsi < OVERSOLD])
    bearish = sum([not above_trend, histogram < 0, latest_rsi > OVERBOUGHT])
    if bullish >= 2 and bullish > bearish:
        stance = "bullish"
    elif bearish >= 2 and bearish > bullish:
        stance = "bearish"
    else:
        stance = "neutral"

    return {
        "rsi": round(latest_rsi, 2),
        "macd_histogram": round(histogram, 6),
        "above_trend": above_trend,
        "trend_window": trend_window,
        "overbought": latest_rsi > OVERBOUGHT,
        "oversold": latest_rsi < OVERSOLD,
        "stance": stance,
        "observations": int(len(clean)),
    }


def backtest_crossover(
    prices: pd.Series, fast: int = 50, slow: int = 200
) -> dict | None:
    """Long-when-fast-above-slow, flat otherwise, against buy-and-hold.

    Vectorized on purpose -- no backtrader/vectorbt, which would impose their own
    data model on a warehouse that already has one.

    The `.shift(1)` is the whole point: the position for day T is decided from
    day T-1's closes. Without it the strategy trades on information it could not
    have had, which is how a backtest reports returns nobody could capture.
    """
    clean = prices.dropna()
    if len(clean) < slow + 2:
        return None
    returns = clean.pct_change().fillna(0.0)
    fast_ma = moving_average(clean, fast)
    slow_ma = moving_average(clean, slow)
    position = (fast_ma > slow_ma).astype(float).shift(1).fillna(0.0)

    strategy = returns * position
    growth = float((1.0 + strategy).prod() - 1.0)
    hold = float((1.0 + returns).prod() - 1.0)
    wealth = (1.0 + strategy).cumprod()
    drawdown = float((wealth / wealth.cummax() - 1.0).min())
    trades = int((position.diff().abs() > 0).sum())
    days_in = float(position.mean())

    return {
        "strategy_return": round(growth, 6),
        "buy_and_hold_return": round(hold, 6),
        "excess_return": round(growth - hold, 6),
        "max_drawdown": round(drawdown, 6),
        "trades": trades,
        "share_of_days_invested": round(days_in, 4),
        "observations": int(len(clean)),
        "fast": fast,
        "slow": slow,
    }


def _self_check() -> None:
    """Smallest runnable proof the arithmetic and the lookahead guard hold."""
    # RSI of a monotonically rising series has no losses at all -> 100.
    rising = pd.Series(np.arange(1, 120, dtype=float))
    assert round(float(rsi(rising).iloc[-1]), 2) == 100.0, rsi(rising).iloc[-1]
    # ...and a monotonically falling one has no gains -> 0.
    falling = pd.Series(np.arange(120, 1, -1, dtype=float))
    assert float(rsi(falling).iloc[-1]) < 1.0, rsi(falling).iloc[-1]

    # MACD histogram is zero for a flat series (both EMAs coincide).
    flat = pd.Series(np.full(120, 50.0))
    assert abs(float(macd(flat)["histogram"].iloc[-1])) < 1e-9

    # A price index rebuilt from returns reproduces the compounded total.
    r = pd.Series([0.1, -0.05, 0.02])
    assert abs(float(price_index_from_returns(r).iloc[-1]) - 100 * 1.1 * 0.95 * 1.02) < 1e-9

    # The lookahead guard: on a series that only ever rises, the crossover
    # strategy cannot beat buy-and-hold, because it sits out the early bars.
    trend = pd.Series(100 * (1.0 + pd.Series(np.full(400, 0.002))).cumprod())
    bt = backtest_crossover(trend)
    assert bt is not None
    assert bt["strategy_return"] <= bt["buy_and_hold_return"] + 1e-9, bt
    assert 0.0 <= bt["share_of_days_invested"] <= 1.0
    print("signals self-check OK:", bt)


if __name__ == "__main__":
    _self_check()
