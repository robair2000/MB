# The holding period

`REBALANCE_DAYS` used to default to `1`. It now defaults to `7`. This file is the evidence for that
change, and the reason the setting deserves more attention than `LOOKBACK_DAYS`.

Reproduce with `python horizon_test.py` (first run downloads ~50 coins x 24,000 hourly candles and
caches them to `mb_panel_4h.json`; later runs take seconds).

---

## The short version

`strategy.py` builds a **7-day** momentum score. It predicts **7-28 day** returns and is
**anti-predictive at 1 day**. Holding it for one day therefore harvests the one horizon where the
signal is backwards.

---

## 1. Is the score right?

Cross-sectional Spearman rank correlation between the score and the forward return, measured on every
4h bar:

| forward horizon | IC | t-stat | n |
|---|---|---|---|
| **1 day** | **-0.0108** | **-3.68** | 5815 |
| 7 days | +0.0227 | +7.38 | 5779 |
| 28 days | +0.0401 | +14.24 | 5653 |

The sign is not ambiguous and the t-stats are not marginal. The score has information; it just needs
time to pay off. At one day, short-term reversal dominates momentum and the ranking is inverted.

## 2. What that costs the book

Same score, same universe, same fees and slippage (0.06% + 0.05% per side), gross exposure 1.0,
dollar-neutral. Only `REBALANCE_DAYS` changes:

| hold | total | Sharpe | max drawdown | positive from N of M start bars |
|---|---|---|---|---|
| **1 day (old default)** | **-20.4%** | -0.21 | -65.9% | **0 / 6** |
| 2 days | -26.6% | -0.29 | -66.5% | 8 / 12 |
| 3 days | +53.7% | 0.53 | -39.9% | **18 / 18** |
| 7 days (new default) | +36.1% | 0.39 | -36.4% | 12 / 14 |
| 14 days | +136.1% | 1.24 | -24.8% | 10 / 12 |
| 28 days | -8.1% | -0.12 | -38.6% | 6 / 12 |

"Start bars" is the honest test: the same book is run from every possible starting offset (6 offsets
for a 1-day cadence, 18 for a 3-day cadence) and all of them are reported. A result that only works
from one entry date is not a result.

The lookback x hold grid is a **plateau, not a spike** - holds of 10-21 days with lookbacks of 5-14
days are all strongly positive. The bad region is specifically 1-2 days. That shape is what a real
effect looks like; a single lucky cell is not.

## 3. Costs point the same way

Daily rebalancing turns over roughly 0.26 gross per rebalance and pays about 0.03%/day, which is
roughly 11% a year. At a 7-day hold that falls to about 1.6%/yr. The 1-day cadence is paying the most
fees to trade the worst horizon.

## 4. What to actually expect

Two caveats matter more than the numbers above.

**Concentration.** At a 14-day hold, dropping the best contributors and renormalising gross exposure
back to 1.0:

| | total | Sharpe |
|---|---|---|
| full book | +136.1% | 1.24 |
| drop best 1 | +99.4% | 0.97 |
| drop best 3 | +47.2% | 0.52 |
| drop best 5 | **+6.6%** | 0.08 |
| drop best 8 | -21.5% | -0.28 |

The top five names account for roughly 130 of the 136 points. Capturing fat tails is what momentum
does, so this is not automatically a flaw - but it means **+136% is not an expectation**. +6.6% over
2.7 years is the honest floor if the tails do not show up.

**Survivorship.** Only 24 of the 50 coins in this universe have the full 2.7 years of history; 25
listed after October 2024. A backtest on today's top 50 applied backwards selects coins that survived
and are popular now. Live, the bot ranks by `turnoverOf24h` at rebalance time and trades a genuine
point-in-time list, so live behaviour is not directly comparable to any backtest here. KuCoin's public
API has no historical turnover ranking, so this cannot be fixed - only disclosed.

Restricting to the 25 mature coins gives +170.9% / Sharpe 1.64, which suggests the result is not
purely new-listing luck, but the coin *list* is still today's.

## 5. Recommendation

1. **`REBALANCE_DAYS=7`.** It is the most start-date-robust setting (12 of 14 offsets positive) and it
   is a large improvement on 1.
2. **Move to 14 only after paper confirms it.** It has the best headline number but depends on a
   handful of coins.
3. **Keep `GROSS_LEVERAGE=1.0`.** With five-name concentration and a -25% drawdown, leverage is the
   fastest way to turn this into a ruin.
4. **Log per-coin contribution.** After a month you should be able to see whether the return is broad
   or is resting on one name again.

## 6. Caveat on these numbers

They were measured on 2026-10-25 over 6000 4h bars (2024-01-29 -> 2026-10-25) using today's top 50 by
turnover. `horizon_test.py` re-downloads and re-ranks, so its output will drift as the universe and
the data change. The *shape* - bad at 1-2 days, good at 7-14 - is the finding; the exact percentages
are not.
