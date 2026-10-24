# Momentum paper bot (KuCoin Futures)

Paper trading only: public KuCoin data, **no API keys, no real orders**.

## What it does
Once a day (00:00 UTC by default) it takes the ~50 most traded KuCoin USDT perpetuals and ranks them by
their move over the last 7 days, divided by their own volatility. It then goes **long the strongest
and short the weakest**, in equal dollar amounts. Every hour it marks the paper positions to market,
charges or credits funding, and logs equity.

## Backtest it came from
- **2 years, ~45 coins (tuning period):** +33% after market-order fees, 15 of 24 months positive,
  max drawdown −38%.
- **Locked newest 30 days (one-shot test):** +51%. A few coins (IRYS) did much of it; without the
  best 3 coins it was +14%.
- **Bias warning:** the coin list was today's top 50, so coins that collapsed and were delisted are
  missing. That flatters momentum. Paper trading is the real test.

## Files
- `bot.py`: the bot (`python bot.py`, `python bot.py --once`, `python bot.py --report`)
- `strategy.py`: the ranking and weights (identical to the backtest; checked to 1e-15)
- `kucoin_data.py`: public KuCoin data with stale-candle protection

## Render
Background worker, Starter plan is enough (no AI model). Add a disk mounted at `/data` and set
`DATA_DIR=/data`, so the state and results survive deploys. `render.yaml` has a ready blueprint.

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `MODE` | `paper` | only paper is supported |
| `UNIVERSE_SIZE` | `50` | how many of the most traded contracts to rank |
| `PAIRS` | (empty) | optional fixed list, e.g. `ETH,SOL,XRP` (overrides UNIVERSE_SIZE) |
| `EXCLUDE` | stock/gold tokens | bases never traded |
| `LOOKBACK_DAYS` | `7` | momentum window |
| `REBALANCE_HOUR_UTC` | `0` | 0, 4, 8, 12, 16 or 20 |
| `REBALANCE_DAYS` | `1` | days between rebalances |
| `GROSS_LEVERAGE` | `1.0` | total long+short size / equity |
| `FEE` / `SLIPPAGE` | `0.0006` / `0.0005` | per side, applied to every simulated trade |
| `MIN_TRADE_USDT` | `5` | smaller adjustments are skipped |
| `START_CASH` | `1000` | paper starting balance |
| `FUNDING` | `true` | charge/credit funding hourly from KuCoin's current rate |
| `DATA_DIR` | `.` | where `momentum_state.json`, `momentum_equity.csv`, `momentum_trades.csv` go |

Kill switch: create a file named `STOP`.
