"""Momentum paper bot for KuCoin Futures (public data only, no API keys, no real orders).

Once a day it ranks the most traded USDT perpetuals by their last-7-day move (relative to their own
volatility), goes long the strongest and short the weakest in equal amounts, and holds until the next
rebalance. Every hour it marks the paper positions to market and charges/credits funding.

    python bot.py            # runs forever
    python bot.py --once     # one cycle (rebalances now if none happened yet), then exits
    python bot.py --report   # print the paper results so far
    touch STOP               # kill switch, exits at the next hourly check
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import kucoin_data as kd
from strategy import four_hour_closes, target_weights


def _f(name, default):
    return float(os.getenv(name, default))


MODE = os.getenv("MODE", "paper").lower()
UNIVERSE_SIZE = int(os.getenv("UNIVERSE_SIZE", "50"))          # most traded contracts to rank
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "").split(",") if p.strip()]   # optional fixed list
EXCLUDE = {s.strip().upper() for s in os.getenv(
    "EXCLUDE", "TSLA,SOXL,SPCX,SNDK,MU,PAXG,XAUT,NVDA,AAPL,AMZN,GOOGL,META,MSFT,COIN,MSTR,HOOD,CRCL,QQQ,SPY").split(",")}
LOOKBACK_DAYS = _f("LOOKBACK_DAYS", "7")
REBALANCE_HOUR_UTC = int(os.getenv("REBALANCE_HOUR_UTC", "0"))  # must be a multiple of 4 (0,4,8,...)
REBALANCE_DAYS = int(os.getenv("REBALANCE_DAYS", "1"))
GROSS = _f("GROSS_LEVERAGE", "1.0")                           # sum of |positions| / equity
FEE = _f("FEE", "0.0006")                                     # taker fee per side
SLIPPAGE = _f("SLIPPAGE", "0.0005")                           # per side
MIN_TRADE_USDT = _f("MIN_TRADE_USDT", "5")                    # skip smaller adjustments
START_CASH = _f("START_CASH", "1000")
FUNDING = os.getenv("FUNDING", "true").lower() in ("1", "true", "yes")
DATA_DIR = os.getenv("DATA_DIR", ".")
STATE_PATH = os.path.join(DATA_DIR, os.getenv("STATE_FILE", "momentum_state.json"))
EQUITY_CSV = os.path.join(DATA_DIR, "momentum_equity.csv")
TRADES_CSV = os.path.join(DATA_DIR, "momentum_trades.csv")
CANDLES = 750                                                  # 1h candles per coin (31 days)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# ------------------------------------------------------------------ state
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"cash": START_CASH, "pos": {}, "last_rebalance": None, "last_mark": None,
            "fees": 0.0, "funding": 0.0, "started": time.time(), "rebalances": 0}


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def equity(st, prices):
    """cash + open P&L. Positions: {sym: {"qty": signed coins, "entry": price}}."""
    return st["cash"] + sum(p["qty"] * (prices.get(s, p["entry"]) - p["entry"]) for s, p in st["pos"].items())


# ------------------------------------------------------------------ universe + data
def universe():
    if PAIRS:
        return [p if p.endswith("USDTM") else ("XBT" if p == "BTC" else p) + "USDTM" for p in PAIRS]
    rows = [r for r in kd.active_contracts() if r["base"].upper() not in EXCLUDE
            and r["symbol"].replace("USDTM", "") not in EXCLUDE]
    rows.sort(key=lambda r: -r["turnover"])
    return [r["symbol"] for r in rows[:UNIVERSE_SIZE]]


def last_prices(symbols):
    out = {}
    for s in symbols:
        try:
            out[s] = float(kd.candles_1h(s, 3).close.iloc[-1])
        except Exception as e:
            log(f"{s}: price unavailable ({e})")
    return out


# ------------------------------------------------------------------ actions
def mark_and_fund(st, prices, hours):
    """Charge / credit funding for the hours since the last mark (long pays a positive rate)."""
    if not FUNDING or not st["pos"] or hours <= 0:
        return
    total = 0.0
    for s, p in st["pos"].items():
        rate = kd.funding_rate(s)
        px = prices.get(s, p["entry"])
        pay = p["qty"] * px * rate * hours / 8.0
        st["cash"] -= pay
        total -= pay
    st["funding"] += total


def rebalance(st, fetch=None):
    fetch = fetch or (lambda s: kd.candles_1h(s, CANDLES))
    syms = universe()
    closes, prices = {}, {}
    for s in syms + [x for x in st["pos"] if x not in syms]:
        try:
            c = fetch(s)
            closes[s] = four_hour_closes(c)
            prices[s] = float(c.close.iloc[-1])
        except Exception as e:
            log(f"{s}: skipped ({e})")
    ranked = {s: closes[s] for s in syms if s in closes}
    weights, scores = target_weights(ranked, LOOKBACK_DAYS, GROSS)
    if not weights:
        log("not enough coins with data - rebalance skipped")
        return False
    eq = equity(st, prices)
    # realise open P&L at today's prices, then trade to the new targets
    for s, p in st["pos"].items():
        px = prices.get(s, p["entry"])
        st["cash"] += p["qty"] * (px - p["entry"])
        p["entry"] = px
    turnover, fees = 0.0, 0.0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in sorted(set(weights) | set(st["pos"]), key=lambda x: -abs(weights.get(x, 0))):
        if s not in prices:
            continue
        px = prices[s]
        cur = st["pos"].get(s, {}).get("qty", 0.0)
        tgt = weights.get(s, 0.0) * eq / px
        dq = tgt - cur
        if abs(dq) * px < MIN_TRADE_USDT and tgt != 0:
            continue
        fill = px * (1 + SLIPPAGE * (1 if dq > 0 else -1))
        cost = abs(dq) * px * FEE + abs(dq) * abs(fill - px)        # fee + slippage
        st["cash"] -= cost
        fees += cost
        turnover += abs(dq) * px
        if abs(tgt) * px < MIN_TRADE_USDT:
            st["pos"].pop(s, None)
        else:
            st["pos"][s] = {"qty": tgt, "entry": px}
        append_csv(TRADES_CSV, ["time", "symbol", "qty_change", "price", "new_weight", "cost"],
                   [now, s, round(dq, 8), px, round(weights.get(s, 0.0), 4), round(cost, 4)])
    st["fees"] += fees
    st["rebalances"] += 1
    st["last_rebalance"] = time.time()
    longs = sorted([(s, w) for s, w in weights.items() if w > 0], key=lambda x: -x[1])
    shorts = sorted([(s, w) for s, w in weights.items() if w < 0], key=lambda x: x[1])
    log(f"REBALANCE #{st['rebalances']} | equity {eq:.2f} | {len(weights)} coins ranked | turnover {turnover:.0f} USDT | cost {fees:.2f}")
    log("  top longs : " + ", ".join(f"{s.replace('USDTM', '')} {w:+.1%}" for s, w in longs[:6]))
    log("  top shorts: " + ", ".join(f"{s.replace('USDTM', '')} {w:+.1%}" for s, w in shorts[:6]))
    return True


def due(st, now=None):
    now = now or time.time()
    h = datetime.fromtimestamp(now, timezone.utc).hour
    if st["last_rebalance"] is None:
        return True
    return h == REBALANCE_HOUR_UTC and now - st["last_rebalance"] > REBALANCE_DAYS * 86400 - 3 * 3600


def cycle(st):
    now = time.time()
    if due(st, now):
        rebalance(st)
    prices = last_prices(list(st["pos"]))
    hours = (now - st["last_mark"]) / 3600 if st["last_mark"] else 0
    mark_and_fund(st, prices, min(hours, 3))
    st["last_mark"] = now
    eq = equity(st, prices)
    gross = sum(abs(p["qty"]) * prices.get(s, p["entry"]) for s, p in st["pos"].items())
    log(f"equity {eq:.2f} ({eq / START_CASH - 1:+.2%} since start) | positions {len(st['pos'])} | gross {gross:.0f} USDT "
        f"| fees paid {st['fees']:.2f} | funding {st['funding']:+.2f}")
    append_csv(EQUITY_CSV, ["time", "equity", "positions", "gross", "fees", "funding"],
               [datetime.now(timezone.utc).isoformat(timespec="seconds"), round(eq, 4), len(st["pos"]),
                round(gross, 2), round(st["fees"], 4), round(st["funding"], 4)])
    save_state(st)


def report():
    if not os.path.exists(EQUITY_CSV):
        print("no results yet")
        return
    rows = list(csv.DictReader(open(EQUITY_CSV)))
    eqs = [float(r["equity"]) for r in rows]
    peak, dd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    print(f"from {rows[0]['time']} to {rows[-1]['time']}: equity {eqs[-1]:.2f} ({eqs[-1] / START_CASH - 1:+.2%}) "
          f"| max drawdown {dd:.1%} | fees {rows[-1]['fees']} | funding {rows[-1]['funding']}")


def main():
    if "--report" in sys.argv:
        return report()
    if MODE != "paper":
        raise SystemExit("This bot is paper-only for now (MODE=paper). It never places real orders.")
    if REBALANCE_HOUR_UTC % 4:
        raise SystemExit("REBALANCE_HOUR_UTC must be 0, 4, 8, 12, 16 or 20")
    log(f"MOMENTUM PAPER BOT | universe {'fixed ' + str(len(PAIRS)) if PAIRS else 'top ' + str(UNIVERSE_SIZE)} "
        f"| lookback {LOOKBACK_DAYS:g}d | rebalance every {REBALANCE_DAYS}d at {REBALANCE_HOUR_UTC:02d}:00 UTC "
        f"| gross {GROSS:g}x | fee {FEE:.3%} + slippage {SLIPPAGE:.3%}")
    st = load_state()
    while True:
        if os.path.exists("STOP"):
            log("STOP file found, exiting")
            break
        try:
            cycle(st)
        except Exception as e:
            log(f"cycle error: {e}")
        if "--once" in sys.argv:
            break
        wait = 3600 - time.time() % 3600 + 90               # just after each hour (candle closed)
        time.sleep(wait)


if __name__ == "__main__":
    main()
