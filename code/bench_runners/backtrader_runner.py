#!/usr/bin/env python3
"""Runs one equivalent Backtrader backtest and prints a single JSON result
line to stdout. Counterpart to reamer_runner.py — same two workloads
(minimal no-op, 20-bar Donchian breakout with real order flow) so the
comparison is apples-to-apples, not REAMER-favorable by construction.

Usage: backtrader_runner.py <pickled_dataframe_path> <minimal|realistic>
Requires: pip install backtrader pandas
"""
import json
import sys
import time

T0 = time.perf_counter()

import backtrader as bt
import pandas as pd


class MinimalBT(bt.Strategy):
    def next(self):
        _ = self.data.close[0]


class RealisticBT(bt.Strategy):
    """20-bar Donchian channel breakout — same signal as reamer_runner.py's
    RealisticStrategy / lean_project/main.py's realistic branch. channel_high
    / channel_low are built on data.high(-1) / data.low(-1) (each line
    shifted back one bar) so the 20-bar window covers the 20 bars strictly
    before the current one, matching reamer_runner.py's high[-21:-1] /
    low[-21:-1] slicing exactly — the current bar's own high/low never
    leaks into its own breakout level.

    Reversals are submitted as a single order sized to flip the net
    position directly (e.g. self.buy(size=1000.0 - pos) from a short position)
    rather than close() + buy()/sell() as two separate orders — matches
    REAMER's netting semantics, where a single orders(buy_market(qty)) call
    with qty > position.qty closes the old leg and opens the new one
    atomically. Without this, Backtrader would submit 2 orders per reversal
    against REAMER's 1, which isn't a real workload difference, just two
    benchmarks disagreeing about how to submit the same net position change.

    Counted via notify_trade()'s isclosed flag rather than notify_order()'s
    Completed status, for the same reason: reamer_py's `trades` field is
    documented as "count of completed round-trips"
    (engine/backtest_result.hpp), not count of order fills. A single
    flip order still closes exactly one round-trip and opens a new one —
    Backtrader's own Trade tracking already splits that correctly, firing
    isclosed=True once for the closed leg, which is the correct
    apples-to-apples counterpart.
    """
    params = dict(period=20)

    def __init__(self):
        self.channel_high = bt.indicators.Highest(self.data.high(-1), period=self.p.period)
        self.channel_low = bt.indicators.Lowest(self.data.low(-1), period=self.p.period)
        self.round_trips = 0
        self.fills = 0

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills += 1

    def notify_trade(self, trade):
        if trade.isclosed:
            self.round_trips += 1

    def next(self):
        if len(self) < self.p.period + 1:
            return
        pos = self.position.size
        close = self.data.close[0]
        if close > self.channel_high[0] and pos <= 0:
            self.buy(size=1000.0 - pos)
        elif close < self.channel_low[0] and pos >= 0:
            self.sell(size=1000.0 + pos)


def peak_rss_mb():
    try:
        import resource
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024)
    except ImportError:
        return None


def main():
    df_path, strat_name = sys.argv[1], sys.argv[2]
    df = pd.read_pickle(df_path)

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(MinimalBT if strat_name == "minimal" else RealisticBT)
    cerebro.broker.setcash(10_000_000.0)

    t0 = time.perf_counter()
    strats = cerebro.run()
    run_s = time.perf_counter() - t0

    print(json.dumps({
        "engine": "backtrader",
        "strategy": strat_name,
        "bars": len(df),
        "run_s": round(run_s, 3),
        "total_s": round(time.perf_counter() - T0, 3),
        "fills": getattr(strats[0], "fills", 0),
        "round_trips": getattr(strats[0], "round_trips", 0),
        "net_pnl": round(cerebro.broker.getvalue() - 10_000_000.0, 6),
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }))


if __name__ == "__main__":
    main()
