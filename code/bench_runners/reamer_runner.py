#!/usr/bin/env python3
"""Runs one Reamer Research backtest through the ctypes reference binding
and prints a single JSON result line to stdout. Isolated in its own process
(invoked via subprocess by backtrader_comparison_bench.py) so peak-RSS
measurement is clean per engine.

Usage: reamer_runner.py <bin_path> <minimal|realistic>

Needs PYTHONPATH=<reamer-research>/reference-python and REAMER_RESEARCH_LIB
pointing at libreamer_research.so (or the test-licence build, in which case
REAMER_BENCH_TEST_LICENSE=1 sets up the bypass licence).
"""
import json
import os
import resource
import sys
import time
from collections import deque

import numpy as np

T0 = time.perf_counter()

from reamer_research_binding import run_backtest
from engine.orders import buy_market, sell_market

TICKER = "BENCH"
CHANNEL = 20
SIZE = 1000.0
BAR_DTYPE = np.dtype([
    ("ts", "<i8"), ("open", "<f8"), ("high", "<f8"),
    ("low", "<f8"), ("close", "<f8"), ("volume", "<f8"),
])


class MinimalStrategy:
    lookback = 1

    def on_bar(self, data):
        _ = data[TICKER].close[-1]
        return None


class RealisticStrategy:
    """20-bar Donchian breakout, channel over the 20 bars before the current
    one. Target +1000 on a close above the channel high, -1000 on a close
    below the channel low, reached with one order sized to reverse. Same
    rule as every other runner in bench_runners/. position() is the state
    after this bar's fills (EXECUTION_SPEC.md §6), so the order is the target
    minus the position."""
    lookback = 1

    def __init__(self):
        self.highs = deque(maxlen=CHANNEL)
        self.lows = deque(maxlen=CHANNEL)
        self.target = 0.0

    def on_bar(self, data):
        tv = data[TICKER]
        high, low, close = float(tv.high[-1]), float(tv.low[-1]), float(tv.close[-1])
        order = None
        if len(self.highs) == CHANNEL:
            if close > max(self.highs) and self.target <= 0:
                self.target = SIZE
            elif close < min(self.lows) and self.target >= 0:
                self.target = -SIZE
            position = data.position(TICKER)
            if position < self.target:
                order = buy_market(self.target - position, ticker=TICKER)
            elif position > self.target:
                order = sell_market(position - self.target, ticker=TICKER)
        self.highs.append(high)
        self.lows.append(low)
        return order


def main():
    bin_path, strategy = sys.argv[1], sys.argv[2]
    if os.environ.get("REAMER_BENCH_TEST_LICENSE") == "1":
        from tests.license_fixture import setup_test_license
        setup_test_license()

    raw = np.fromfile(bin_path, dtype=BAR_DTYPE)
    bars = np.zeros(len(raw), dtype=[
        ("ts", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
        ("close", "f8"), ("volume", "f8"), ("notional", "f8"), ("tick_count", "i8"),
    ])
    for f in BAR_DTYPE.names:
        bars[f] = raw[f]
    bars["tick_count"] = -1

    config = {
        "initial_capital": 10_000_000.0,
        "leverage": 1.0,
        "commission_per_unit": 0.0,
        "commission_mode": "round_trip",
        "slippage": 0.0,
        "price_volatility": 0.0,
        "spread": 0.0,
        "ohlcv_type": "bid",
        "rng_seed": 42,
        "roll_days_before_monthend": 5,
    }
    strat_cls = MinimalStrategy if strategy == "minimal" else RealisticStrategy

    t_run = time.perf_counter()
    result = run_backtest(strat_cls, config, {TICKER: bars})
    run_s = time.perf_counter() - t_run
    if not result:
        sys.exit("run_backtest failed")

    print(json.dumps({
        "engine": "reamer_research_py",
        "strategy": strategy,
        "bars": len(bars),
        "run_s": round(run_s, 3),
        "total_s": round(time.perf_counter() - T0, 3),
        "fills": int(result.trades),
        "round_trips": int(result.closed_trades_count),
        "net_pnl": round(float(result.net_pnl), 6),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }))


if __name__ == "__main__":
    main()
