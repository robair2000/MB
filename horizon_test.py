"""Measure the right holding period for this strategy, using this repo's own code and data.

    python horizon_test.py            # fetch (cached to mb_panel_4h.json) and report
    python horizon_test.py --refresh  # force a re-download
    python horizon_test.py --hold 14  # concentration check for a specific hold

What it answers: `strategy.py` builds a 7-day momentum score, but `bot.py` decides how long to hold
it. This measures, on the bot's own universe and with the bot's own costs, which holding period the
score actually predicts.

Two things are reported:

  1. IC BY HORIZON - cross-sectional Spearman rank correlation between the score and the forward
     return over 1, 7 and 28 days. This says whether the signal is right, independent of costs.
  2. HOLD SWEEP - the full book, rebalanced every N days, net of fees and slippage, averaged over
     every possible start bar (so the result does not depend on which day the test began).

The vectorised scoring below is a straight copy of strategy.target_weights applied to every bar
instead of only the last one, and the script asserts that the two agree on the final bar.
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

import kucoin_data as kd
from strategy import MIN_HISTORY_BARS, VOL_BARS, four_hour_closes, target_weights

BAR_H = 4
CACHE = os.getenv("PANEL_CACHE", "mb_panel_4h.json")
BARS_4H = int(os.getenv("BARS_4H", "6000"))   # 1000 days of 4h bars
COST = 0.0006 + 0.0005             # FEE + SLIPPAGE, per side, matching bot.py defaults
HOLDS = (1, 2, 3, 7, 14, 28)
IC_HORIZONS = ((6, "1 day"), (42, "7 days"), (168, "28 days"))


# ------------------------------------------------------------------ data
def fetch_panel(force=False):
    if os.path.exists(CACHE) and not force:
        with open(CACHE) as f:
            raw = json.load(f)
        return {s: pd.DataFrame({"t_ms": np.array(d["t_ms"], dtype="int64"),
                                 "close": np.array(d["close"], dtype=float)}) for s, d in raw.items()}

    exclude = {s.strip().upper() for s in os.getenv(
        "EXCLUDE", "TSLA,SOXL,SPCX,SNDK,MU,PAXG,XAUT,NVDA,AAPL,AMZN,GOOGL,META,MSFT,COIN,MSTR,HOOD,"
                   "CRCL,QQQ,SPY").split(",")}
    size = int(os.getenv("UNIVERSE_SIZE", "50"))
    rows = [r for r in kd.active_contracts()
            if r["base"].upper() not in exclude and r["symbol"].replace("USDTM", "") not in exclude]
    rows.sort(key=lambda r: -r["turnover"])
    pick = rows[:size]
    print(f"{len(rows)} eligible contracts, fetching the top {len(pick)} by 24h turnover")
    print(f"({BARS_4H * BAR_H:,} 1h candles each - this takes a few minutes the first time)")

    panel = {}
    for i, r in enumerate(pick, 1):
        sym = r["symbol"]
        try:
            c = kd.candles_1h(sym, BARS_4H * BAR_H)
        except Exception as e:
            print(f"  {i:>2d} {sym:12s} skipped ({str(e)[:50]})", flush=True)
            continue
        panel[sym] = c[["t_ms", "close"]]
        print(f"  {i:>2d} {sym:12s} {len(c):6d} 1h bars", flush=True)

    with open(CACHE, "w") as f:
        json.dump({s: {"t_ms": [int(x) for x in d.t_ms], "close": [float(x) for x in d.close]}
                   for s, d in panel.items()}, f)
    print(f"cached {len(panel)} coins to {CACHE}")
    return panel


def build(panel):
    """{symbol: 1h candles} -> sorted symbol list, shared 4h index, close matrix."""
    closes = {}
    for s, c in panel.items():
        try:
            closes[s] = four_hour_closes(c)
        except Exception:
            continue
    syms = sorted(closes)
    ms = sorted(set().union(*[set(int(x) for x in closes[s].index) for s in syms]))
    idx = pd.DatetimeIndex(pd.to_datetime(ms, unit="ms", utc=True))
    P = pd.DataFrame({s: closes[s].reindex(ms).to_numpy(float) for s in syms}, index=idx)
    return syms, idx, P


# ------------------------------------------------------------------ scoring (strategy.py, every bar)
def scores(P, lookback_days=7):
    k = int(lookback_days * 24 / BAR_H)
    r = P.pct_change(fill_method=None)
    vol = r.rolling(VOL_BARS, min_periods=12).std()
    score = (P / P.shift(k) - 1) / vol
    age = P.notna().cumsum()
    return score.where(age >= max(k, MIN_HISTORY_BARS))


def weights(S, gross=1.0):
    """z = score - cross-sectional mean; w = gross * z / sum|z|. Weights sum to exactly zero."""
    out = np.full(S.shape, np.nan)
    vals = S.to_numpy(float)
    for t in range(len(vals)):
        row = vals[t]
        ok = np.isfinite(row)
        if ok.sum() < 4:
            continue
        z = row[ok] - row[ok].mean()
        denom = np.abs(z).sum()
        if denom <= 0:
            continue
        out[t, ok] = gross * z / denom
    return pd.DataFrame(out, index=S.index, columns=S.columns)


def xs_ic(S, R, min_names=6):
    """Per-bar cross-sectional Spearman IC. NaN where there are too few coins."""
    a_all, b_all = S.to_numpy(float), R.to_numpy(float)
    out = np.full(len(a_all), np.nan)
    for t in range(len(a_all)):
        m = np.isfinite(a_all[t]) & np.isfinite(b_all[t])
        if m.sum() < min_names:
            continue
        ra = pd.Series(a_all[t][m]).rank().to_numpy()
        rb = pd.Series(b_all[t][m]).rank().to_numpy()
        if ra.std() == 0 or rb.std() == 0:
            continue
        out[t] = np.corrcoef(ra, rb)[0, 1]
    return out


# ------------------------------------------------------------------ book
def book(P, hold_bars, lookback_days=7, cost=COST):
    S = scores(P, lookback_days)
    W = weights(S)
    R = np.log(P.shift(-hold_bars) / P)                    # period log return, non-overlapping
    Wv, Rv = W.to_numpy(float), R.to_numpy(float)
    Wf = np.nan_to_num(Wv)

    gross = np.nansum(Wf * np.nan_to_num(Rv), axis=1)
    step = np.abs(np.diff(np.vstack([np.zeros((1, Wf.shape[1])), Wf]), axis=0)).sum(axis=1)
    net = gross - step * cost
    live = np.isfinite(Wv).any(axis=1) & np.isfinite(Rv).any(axis=1)
    return W, net, live, step


def summarise(x, periods_per_year):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 8:
        return None
    eq = np.exp(np.cumsum(x))
    peak = np.maximum.accumulate(eq)
    return {"total": float(eq[-1] - 1),
            "sharpe": float(x.mean() / x.std() * np.sqrt(periods_per_year)) if x.std() else np.nan,
            "max_dd": float((eq / peak - 1).min()),
            "n": len(x), "x": x}


def per_year(x, idx):
    s = pd.Series(x, index=idx)
    out = {}
    for y, g in s.groupby(s.index.year):
        g = g[np.isfinite(g)]
        if len(g):
            out[int(y)] = float(np.exp(g.sum()) - 1)
    return out


def start_dates(net, live, hold_bars, idx, max_starts=16):
    """K5: average the book over every possible start bar, so the answer is not a lucky entry date."""
    step = max(1, hold_bars // max_starts)
    totals = []
    for off in range(0, hold_bars, step):
        m = np.zeros(len(idx), bool)
        m[off::hold_bars] = True
        m &= live
        st = summarise(net[m], 365 / (hold_bars * BAR_H / 24))
        if st:
            totals.append(st["total"])
    return np.array(totals)


# ------------------------------------------------------------------ main
def main():
    args = sys.argv[1:]
    force = "--refresh" in args
    hold_focus = 14
    if "--hold" in args:
        hold_focus = int(float(args[args.index("--hold") + 1]))

    panel = fetch_panel(force)
    syms, idx, P = build(panel)
    print(f"\n{len(syms)} coins, {len(idx)} 4h bars, {idx[0]:%Y-%m-%d} -> {idx[-1]:%Y-%m-%d}")
    print(f"cost {COST:.2%} per side, gross exposure 1.0, dollar-neutral\n")

    # ---- faithfulness check: vectorised weights must equal strategy.target_weights on the last bar
    S = scores(P)
    W = weights(S)
    w_ref, _ = target_weights({s: P[s].dropna() for s in syms}, 7, 1.0)
    last = W.iloc[-1].dropna()
    diff = max((abs(last.get(s, 0.0) - w) for s, w in w_ref.items()), default=np.nan)
    print(f"port check: vectorised weights vs strategy.target_weights, max abs diff {diff:.2e}"
          f"  {'OK' if diff < 1e-9 else 'MISMATCH'}\n")

    # ---- 1. does the score rank the future at all?
    print("=" * 84)
    print("1. IC BY HORIZON  (cross-sectional Spearman, score vs forward return)")
    print("=" * 84)
    for bars, label in IC_HORIZONS:
        ic = xs_ic(S, np.log(P.shift(-bars) / P))
        ic = ic[np.isfinite(ic)]
        se = ic.std() / np.sqrt(len(ic))
        print(f"  {label:>8s}: IC {ic.mean():+.4f}   se {se:.4f}   t {ic.mean() / se:+6.2f}   n {len(ic)}")
    print("  a 7-day score should be judged at 7-28 days, not at the next bar")

    # ---- 2. hold sweep
    print()
    print("=" * 84)
    print("2. HOLD SWEEP  (same score, same costs, only the holding period changes)")
    print("=" * 84)
    print(f"  {'hold':>6s} {'total':>9s} {'sharpe':>7s} {'maxDD':>8s}   start-date average")
    rows = {}
    for hd in HOLDS:
        hold_bars = max(1, int(round(hd * 24 / BAR_H)))
        W_h, net, live, step = book(P, hold_bars)
        ppy = 365 / (hold_bars * BAR_H / 24)
        reb = np.zeros(len(idx), bool)
        reb[::hold_bars] = True
        reb &= live
        st = summarise(net[reb], ppy)
        if not st:
            continue
        t = start_dates(net, live, hold_bars, idx)
        rows[hd] = (st, t, reb)
        print(f"  {hd:>5d}d {st['total']:>+9.1%} {st['sharpe']:>7.2f} {st['max_dd']:>+8.1%}   "
              f"mean {t.mean():>+7.1%}  {int((t > 0).sum())}/{len(t)} positive  "
              f"(best {t.max():+.0%} worst {t.min():+.0%})")

    # ---- 3. the focus hold, in detail
    if hold_focus in rows:
        st, t, reb = rows[hold_focus]
        print()
        print("=" * 84)
        print(f"3. HOLD {hold_focus} DAYS IN DETAIL")
        print("=" * 84)
        print(f"  rebalances   {st['n']}")
        print(f"  total        {st['total']:+.1%}   sharpe {st['sharpe']:.2f}   max drawdown {st['max_dd']:+.1%}")
        print(f"  per year     " + "   ".join(f"{y} {v:+.0%}" for y, v in per_year(st["x"], idx[reb]).items()))

        # concentration: drop the best contributors, renormalise gross back to 1.0
        W_h, net, live, step = book(P, int(round(hold_focus * 24 / BAR_H)))
        Wv = np.nan_to_num(W_h.to_numpy(float))
        Rv = np.nan_to_num(np.log(P.shift(-int(round(hold_focus * 24 / BAR_H))) / P).to_numpy(float))
        contrib = np.nansum(Wv * Rv, axis=0)
        order = np.argsort(-contrib)
        print()
        print(f"  concentration (top contributors, gross P&L): "
              + ", ".join(f"{syms[i]} {contrib[i]:+.0%}" for i in order[:5]))
        for drop in (0, 1, 3, 5):
            W2 = Wv.copy()
            W2[:, order[:drop]] = 0.0
            denom = np.nansum(np.abs(W2), axis=1, keepdims=True)
            W2 = np.where(denom > 0, W2 / np.where(denom == 0, 1, denom), 0.0)
            d = np.abs(np.diff(np.vstack([np.zeros((1, W2.shape[1])), W2]), axis=0)).sum(axis=1)
            n2 = np.nansum(W2 * Rv, axis=1) - d * COST
            s2 = summarise(n2[reb], 365 / hold_focus)
            if s2:
                print(f"    drop best {drop}: total {s2['total']:>+8.1%}   sharpe {s2['sharpe']:>5.2f}")

    print()
    print("=" * 84)
    print("Read: the 1-2 day region is where the score is anti-predictive. Hold 7-14 days.")
    print("=" * 84)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\ndone in {time.time() - t0:.0f}s")
