"""Cross-sectional momentum, exactly as backtested:

  * prices sampled every 4 hours (close of the 1h candle that ends at 00/04/08/12/16/20 UTC)
  * score = return over LOOKBACK_DAYS / std of 4-hour returns over the last 7 days
  * weights = score minus the average score of all eligible coins, scaled so |weights| sum to 1
    -> long the coins that went up most (relative to their own volatility), short the weakest;
       longs and shorts are the same size, so the bet is on coins vs each other, not on the market
"""
import numpy as np
import pandas as pd

H_MS = 3_600_000
BAR_H = 4
VOL_BARS = 42                    # 7 days of 4h returns
MIN_HISTORY_BARS = 180           # 30 days of 4h bars before a coin can be traded


def four_hour_closes(c1h):
    """1h candles (t_ms = open time) -> Series of 4h closes indexed by the 4h close time (ms)."""
    close_time = c1h.t_ms.values + H_MS
    keep = close_time % (BAR_H * H_MS) == 0
    return pd.Series(c1h.close.values[keep], index=close_time[keep])


def target_weights(closes_by_symbol, lookback_days=7, gross=1.0):
    """closes_by_symbol: {symbol: Series of 4h closes}. Returns ({symbol: weight}, {symbol: score})."""
    P = pd.DataFrame(closes_by_symbol).sort_index()
    k = int(lookback_days * 24 / BAR_H)
    r = P.pct_change(fill_method=None)
    vol = r.rolling(VOL_BARS, min_periods=12).std()
    score = (P / P.shift(k) - 1) / vol
    age = P.notna().cumsum()
    score = score.where(age >= max(k, MIN_HISTORY_BARS))
    last = score.iloc[-1].dropna()
    last = last[np.isfinite(last)]
    if len(last) < 4:
        return {}, last.to_dict()
    z = last - last.mean()
    w = gross * z / z.abs().sum()
    return w.to_dict(), last.to_dict()
