#!/usr/bin/env python3
"""
Hedged Grid Bot — Backtester
================================
Simulates the EXACT logic observed in the live bot screenshots:
  1. Open BUY 0.01 + SELL 0.01 simultaneously
  2. Price drops by grid_step → add BUY (recovery, bigger lot) + SELL (hedge, 0.01)
  3. Price rises by grid_step → add SELL (recovery, bigger lot) + BUY (hedge, 0.01)
  4. When net P/L of all positions >= basket_tp → close everything → profit
  5. Immediately open next cycle

Uses intra-bar path simulation (O→L→H→C or O→H→L→C) to accurately
capture multiple grid fills and TP exits within a single 1h bar.

Install:
    pip install yfinance pandas numpy matplotlib

Run:
    python backtest.py                          # Gold, 24 months
    python backtest.py --months 36              # 3 years
    python backtest.py --step 4.0 --tp 12.0     # tune parameters
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("pip install yfinance pandas numpy matplotlib")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False

# ──────────────────────────────────────────────────────────────────────────────
# Lot sequence (exact match to live bot screenshots)
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_MULTS = [1, 1, 2, 3, 5, 7, 11, 17, 25]

def build_lots(base: float, levels: int) -> List[float]:
    seq = []
    for i in range(levels):
        if i < len(_DEFAULT_MULTS):
            seq.append(round(base * _DEFAULT_MULTS[i], 2))
        else:
            prev = seq[-1]
            seq.append(round(math.ceil(prev * 1.5 * 100) / 100, 2))
    return seq

# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

BUY, SELL = 0, 1

@dataclass
class Pos:
    direction: int
    volume: float
    entry: float

    def pnl(self, bid: float, ask: float, cs: float) -> float:
        if self.direction == BUY:
            return (bid - self.entry) * self.volume * cs
        return (self.entry - ask) * self.volume * cs


@dataclass
class CycleRecord:
    open_bar: int
    close_bar: int
    open_time: object
    close_time: object
    max_level: int
    num_positions: int
    net_pnl: float
    recovery_dir: str
    duration_bars: int

# ──────────────────────────────────────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────────────────────────────────────

def _norm(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns and "Adj Close" in df.columns:
        df = df.rename(columns={"Adj Close": "Close"})
    return df

def download(ticker: str, months: int) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30 * months)
    chunks = []
    t = start
    while t < end:
        t_end = min(t + timedelta(days=720), end)
        try:
            c = yf.download(ticker, start=t.strftime("%Y-%m-%d"),
                            end=t_end.strftime("%Y-%m-%d"),
                            interval="1h", progress=False, auto_adjust=True)
            if not c.empty:
                chunks.append(_norm(c))
        except Exception as e:
            print(f"  [WARN] {e}")
        t = t_end + timedelta(days=1)
    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks).sort_index()
    df = df.loc[~df.index.duplicated(keep="first")]
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

# ──────────────────────────────────────────────────────────────────────────────
# Backtester with intra-bar simulation
# ──────────────────────────────────────────────────────────────────────────────

class HedgedGridBacktest:

    def __init__(
        self,
        df: pd.DataFrame,
        grid_step: float = 5.0,
        basket_tp: float = 15.0,
        base_lot: float = 0.01,
        max_levels: int = 9,
        contract_size: float = 100.0,
        spread: float = 0.50,
        session_start: int = 1,
        session_end: int = 22,
    ):
        self.df = df
        self.step = grid_step
        self.tp = basket_tp
        self.base = base_lot
        self.max_levels = max_levels
        self.cs = contract_size
        self.half_sp = spread / 2
        self.sess_start = session_start
        self.sess_end = session_end

        self.lots = build_lots(base_lot, max_levels)
        self.records: List[CycleRecord] = []
        self.equity_curve: List[float] = []

    def _basket_pnl(self, positions: List[Pos], mid: float) -> float:
        bid = mid - self.half_sp
        ask = mid + self.half_sp
        return sum(p.pnl(bid, ask, self.cs) for p in positions)

    def _intra_bar_path(self, o: float, h: float, l: float, c: float) -> List[float]:
        """Simulate price path within a bar using OHLC.
        If bar is bullish (c > o): O → L → H → C
        If bar is bearish (c < o): O → H → L → C
        We also add intermediate steps every grid_step to catch triggers."""
        if c >= o:
            raw = [o, l, h, c]
        else:
            raw = [o, h, l, c]

        # Add finer steps between each raw point
        path = []
        for j in range(len(raw) - 1):
            start, end = raw[j], raw[j + 1]
            dist = abs(end - start)
            steps = max(1, int(dist / (self.step * 0.5)))
            for k in range(steps):
                path.append(start + (end - start) * k / steps)
        path.append(raw[-1])
        return path

    def run(self) -> None:
        O = self.df["Open"].values
        H = self.df["High"].values
        L = self.df["Low"].values
        C = self.df["Close"].values
        T = self.df.index
        n = len(C)

        positions: List[Pos] = []
        last_buy = 0.0
        last_sell = 0.0
        level = 0
        rec_dir: Optional[int] = None
        cycle_bar = 0
        cum_pnl = 0.0

        for i in range(n):
            hour = pd.Timestamp(T[i]).hour
            in_session = self.sess_start <= hour < self.sess_end

            # ── No positions → open pair if in session ───────────────────
            if not positions:
                if not in_session:
                    self.equity_curve.append(cum_pnl)
                    continue
                mid = O[i]
                positions.append(Pos(BUY, self.base, mid + self.half_sp))
                positions.append(Pos(SELL, self.base, mid - self.half_sp))
                last_buy = mid + self.half_sp
                last_sell = mid - self.half_sp
                level = 0
                rec_dir = None
                cycle_bar = i
                self.equity_curve.append(cum_pnl)
                continue

            # ── Simulate intra-bar price path ────────────────────────────
            path = self._intra_bar_path(O[i], H[i], L[i], C[i])
            closed = False

            for mid in path:
                # Check TP
                net = self._basket_pnl(positions, mid)
                if net >= self.tp:
                    cum_pnl += self.tp
                    self.records.append(CycleRecord(
                        open_bar=cycle_bar, close_bar=i,
                        open_time=T[cycle_bar], close_time=T[i],
                        max_level=level, num_positions=len(positions),
                        net_pnl=self.tp,
                        recovery_dir="BUY" if rec_dir == BUY else
                                     "SELL" if rec_dir == SELL else "NONE",
                        duration_bars=i - cycle_bar,
                    ))
                    positions.clear()
                    closed = True
                    break

                # Check grid level triggers (multiple can fire per bar)
                # Once recovery direction is locked, only that side adds levels.
                while level < self.max_levels - 1:
                    bid = mid - self.half_sp
                    ask = mid + self.half_sp
                    buy_trig = last_buy - self.step
                    sell_trig = last_sell + self.step

                    triggered = False

                    # BUY recovery: price must drop
                    if (rec_dir is None or rec_dir == BUY) and bid <= buy_trig:
                        level += 1
                        if rec_dir is None:
                            rec_dir = BUY
                        r_lot = self.lots[level]
                        entry_buy = buy_trig + self.half_sp
                        entry_sell = buy_trig - self.half_sp
                        positions.append(Pos(BUY, r_lot, entry_buy))
                        positions.append(Pos(SELL, self.base, entry_sell))
                        last_buy = entry_buy
                        last_sell = entry_sell
                        triggered = True

                    # SELL recovery: price must rise
                    elif (rec_dir is None or rec_dir == SELL) and ask >= sell_trig:
                        level += 1
                        if rec_dir is None:
                            rec_dir = SELL
                        r_lot = self.lots[level]
                        entry_sell = sell_trig - self.half_sp
                        entry_buy = sell_trig + self.half_sp
                        positions.append(Pos(SELL, r_lot, entry_sell))
                        positions.append(Pos(BUY, self.base, entry_buy))
                        last_sell = entry_sell
                        last_buy = entry_buy
                        triggered = True

                    if not triggered:
                        break

                    # Re-check TP after new level
                    net = self._basket_pnl(positions, mid)
                    if net >= self.tp:
                        cum_pnl += self.tp
                        self.records.append(CycleRecord(
                            open_bar=cycle_bar, close_bar=i,
                            open_time=T[cycle_bar], close_time=T[i],
                            max_level=level, num_positions=len(positions),
                            net_pnl=self.tp,
                            recovery_dir="BUY" if rec_dir == BUY else
                                         "SELL" if rec_dir == SELL else "NONE",
                            duration_bars=i - cycle_bar,
                        ))
                        positions.clear()
                        closed = True
                        break

                if closed:
                    break

            # After processing bar
            if closed:
                # If still in session, immediately open new pair at close
                if in_session:
                    mid = C[i]
                    positions.append(Pos(BUY, self.base, mid + self.half_sp))
                    positions.append(Pos(SELL, self.base, mid - self.half_sp))
                    last_buy = mid + self.half_sp
                    last_sell = mid - self.half_sp
                    level = 0
                    rec_dir = None
                    cycle_bar = i

            if positions:
                self.equity_curve.append(cum_pnl + self._basket_pnl(positions, C[i]))
            else:
                self.equity_curve.append(cum_pnl)

        # Close remaining at end
        if positions:
            net = self._basket_pnl(positions, C[-1])
            cum_pnl += net
            self.records.append(CycleRecord(
                open_bar=cycle_bar, close_bar=n - 1,
                open_time=T[cycle_bar], close_time=T[-1],
                max_level=level, num_positions=len(positions),
                net_pnl=round(net, 2),
                recovery_dir="BUY" if rec_dir == BUY else
                             "SELL" if rec_dir == SELL else "NONE",
                duration_bars=n - 1 - cycle_bar,
            ))

# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────

def report(records: List[CycleRecord], lots: List[float]) -> None:
    if not records:
        print("\n  No cycles completed.")
        return

    pnls = [r.net_pnl for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)

    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    monthly: Dict[str, float] = {}
    for r in records:
        mo = pd.Timestamp(r.close_time).strftime("%Y-%m")
        monthly[mo] = monthly.get(mo, 0.0) + r.net_pnl

    level_dist: Dict[int, int] = {}
    for r in records:
        level_dist[r.max_level] = level_dist.get(r.max_level, 0) + 1

    wrate = len(wins) / len(pnls) * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    avg_dur = sum(r.duration_bars for r in records) / len(records)

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  HEDGED GRID BACKTEST RESULTS")
    print(bar)
    print(f"  Period           : {records[0].open_time} → {records[-1].close_time}")
    print(f"  Total cycles     : {len(records)}")
    print(f"  Winning          : {len(wins)}  ({wrate:.1f}%)")
    print(f"  Losing           : {len(losses)}  ({100 - wrate:.1f}%)")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Total P/L        : ${total:+.2f}")
    print(f"  Profit factor    : {pf:.2f}")
    print(f"  Max drawdown     : ${max_dd:.2f}")
    print(f"  Avg win          : ${avg_win:+.2f}")
    print(f"  Avg loss         : ${avg_loss:+.2f}")
    print(f"  Avg duration     : {avg_dur:.1f} bars ({avg_dur:.0f}h)")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Lot sequence     : {lots}")
    print(f"  Max exposure     : {sum(lots):.2f} recovery + {len(lots)*0.01:.2f} hedge")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Depth distribution:")
    for lv in sorted(level_dist):
        pct = level_dist[lv] / len(records) * 100
        vis = "█" * min(int(pct), 50)
        print(f"    Level {lv}: {level_dist[lv]:>5} ({pct:5.1f}%) {vis}")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Monthly P/L (last 12):")
    for mo in sorted(monthly)[-12:]:
        v = monthly[mo]
        sign = "+" if v >= 0 else "-"
        color = "\033[92m" if v >= 0 else "\033[91m"
        reset = "\033[0m"
        vis = "█" * min(int(abs(v) / 5), 40)
        print(f"    {mo}  {color}{sign}${abs(v):8.2f}{reset}  {vis}")
    print(bar)


def plot(records: List[CycleRecord], equity: List[float],
         df: pd.DataFrame, params: str) -> None:
    if not HAS_PLOT or not records or not equity:
        return

    fig, axes = plt.subplots(3, 1, figsize=(16, 10),
                             gridspec_kw={"height_ratios": [3, 1.5, 1]})
    fig.suptitle(f"Hedged Grid Backtest — {params}", fontsize=13, fontweight="bold")

    idx = df.index[len(df) - len(equity):]
    eq = np.array(equity)

    ax1 = axes[0]
    ax1.plot(df.index, df["Close"].values, color="#1565C0", lw=0.5, alpha=0.8)
    for r in records:
        c = "#43A047" if r.net_pnl > 0 else "#E53935"
        try:
            ax1.axvspan(r.open_time, r.close_time, alpha=0.06, color=c)
        except Exception:
            pass
    ax1.set_ylabel("Gold Price")
    ax1.grid(True, alpha=0.2)

    ax2 = axes[1]
    ax2.plot(idx, eq, color="#FF6F00", lw=1.2)
    ax2.axhline(0, color="gray", ls="--", lw=0.5)
    ax2.fill_between(idx, eq, 0, where=eq >= 0, color="#43A047", alpha=0.2)
    ax2.fill_between(idx, eq, 0, where=eq < 0, color="#E53935", alpha=0.2)
    ax2.set_ylabel("Cumul. P/L ($)")
    ax2.grid(True, alpha=0.2)

    ax3 = axes[2]
    running_max = np.maximum.accumulate(eq)
    dd = eq - running_max
    ax3.fill_between(idx, dd, 0, color="#E53935", alpha=0.4)
    ax3.set_ylabel("Drawdown ($)")
    ax3.grid(True, alpha=0.2)

    plt.tight_layout()
    fname = "backtest_hedged_grid.png"
    plt.savefig(fname, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  Chart saved: {fname}")


def save_csv(records: List[CycleRecord]) -> None:
    if not records:
        return
    rows = [{
        "open_time": r.open_time, "close_time": r.close_time,
        "max_level": r.max_level, "positions": r.num_positions,
        "net_pnl": round(r.net_pnl, 2),
        "recovery_dir": r.recovery_dir, "duration_bars": r.duration_bars,
    } for r in records]
    fname = "backtest_hedged_grid_trades.csv"
    pd.DataFrame(rows).to_csv(fname, index=False)
    print(f"  Trades saved: {fname}")

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Hedged Grid Backtester")
    p.add_argument("--ticker", default="GC=F", help="yfinance ticker (default: GC=F)")
    p.add_argument("--months", type=int, default=24, help="Months of data (default: 24)")
    p.add_argument("--step", type=float, default=5.0, help="Grid step in points (default: 5.0)")
    p.add_argument("--tp", type=float, default=15.0, help="Basket TP in $ (default: 15.0)")
    p.add_argument("--base-lot", type=float, default=0.01)
    p.add_argument("--max-levels", type=int, default=9)
    p.add_argument("--spread", type=float, default=0.50)
    p.add_argument("--contract-size", type=float, default=100.0)
    p.add_argument("--session-start", type=int, default=1)
    p.add_argument("--session-end", type=int, default=22)
    args = p.parse_args()

    lots = build_lots(args.base_lot, args.max_levels)

    print(f"\n{'─'*62}")
    print(f"  Hedged Grid Backtester")
    print(f"  Ticker: {args.ticker} | Months: {args.months}")
    print(f"  Step: {args.step} | TP: ${args.tp} | Levels: {args.max_levels}")
    print(f"  Lots: {lots}")
    print(f"  Total recovery: {sum(lots):.2f} lot")
    print(f"{'─'*62}")

    print(f"\n  Downloading {args.months} months of 1h data...")
    df = download(args.ticker, args.months)

    if df.empty or len(df) < 100:
        print(f"  [ERROR] Only {len(df)} bars")
        return

    print(f"  Got {len(df):,} bars ({df.index[0].date()} → {df.index[-1].date()})")

    bt = HedgedGridBacktest(
        df, grid_step=args.step, basket_tp=args.tp,
        base_lot=args.base_lot, max_levels=args.max_levels,
        contract_size=args.contract_size, spread=args.spread,
        session_start=args.session_start, session_end=args.session_end,
    )

    print("  Running simulation...")
    bt.run()

    report(bt.records, lots)
    params = f"step={args.step} tp=${args.tp} levels={args.max_levels}"
    plot(bt.records, bt.equity_curve, df, params)
    save_csv(bt.records)

    print(f"\n{'─'*62}")
    print("  DONE")
    print(f"{'─'*62}\n")


if __name__ == "__main__":
    main()
