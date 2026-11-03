#!/usr/bin/env python3
"""Runs an equivalent Backtrader multi-data backtest (N feeds on one
Cerebro, not N independent single-feed runs) and prints a single JSON
result line to stdout. Counterpart to reamer_portfolio_runner.py.

Usage: backtrader_portfolio_runner.py <pkl_dir> <n_tickers> <minimal|realistic>
Requires: pip install backtrader pandas
"""
import json
import sys
import time
from pathlib import Path

T0 = time.perf_counter()

import backtrader as bt
import pandas as pd


class MinimalPortfolioBT(bt.Strategy):
    def next(self):
        for d in self.datas:
            _ = d.close[0]


class RealisticPortfolioBT(bt.Strategy):
    """20-bar Donchian channel breakout per ticker — same signal as
    backtrader_runner.py's RealisticBT / reamer_portfolio_runner.py's
    RealisticPortfolio. Reversal-as-single-order + isclosed round-trip
    counting, same fix as the single-ticker runner."""
    params = dict(period=20)

    def __init__(self):
        self.channel_high = {d: bt.indicators.Highest(d.high(-1), period=self.p.period) for d in self.datas}
        self.channel_low = {d: bt.indicators.Lowest(d.low(-1), period=self.p.period) for d in self.datas}
        self.round_trips = 0
        self.fills = 0

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills += 1

    def notify_trade(self, trade):
        if trade.isclosed:
            self.round_trips += 1

    def next(self):
        for d in self.datas:
            if len(d) < self.p.period + 1:
                continue
            pos = self.getposition(d).size
            close = d.close[0]
            ch_high, ch_low = self.channel_high[d][0], self.channel_low[d][0]
            if close > ch_high and pos <= 0:
                self.buy(data=d, size=1000.0 - pos)
            elif close < ch_low and pos >= 0:
                self.sell(data=d, size=1000.0 + pos)


def peak_rss_mb():
    try:
        import resource
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024)
    except ImportError:
        return None


def main():
    pkl_dir, n_tickers, strat_name = sys.argv[1], int(sys.argv[2]), sys.argv[3]

    cerebro = bt.Cerebro(stdstats=False)
    for i in range(n_tickers):
        df = pd.read_pickle(Path(pkl_dir) / f"T{i:03d}.pkl")
        cerebro.adddata(bt.feeds.PandasData(dataname=df), name=f"T{i:03d}")
    cerebro.addstrategy(MinimalPortfolioBT if strat_name == "minimal" else RealisticPortfolioBT)
    cash = 10_000_000.0 * n_tickers
    cerebro.broker.setcash(cash)
    bars = sum(len(d.p.dataname) for d in cerebro.datas)

    t0 = time.perf_counter()
    strats = cerebro.run()
    run_s = time.perf_counter() - t0

    print(json.dumps({
        "engine": "backtrader",
        "strategy": strat_name,
        "n_tickers": n_tickers,
        "bars": bars,
        "run_s": round(run_s, 3),
        "total_s": round(time.perf_counter() - T0, 3),
        "fills": getattr(strats[0], "fills", 0),
        "round_trips": getattr(strats[0], "round_trips", 0),
        "net_pnl": round(cerebro.broker.getvalue() - cash, 6),
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }))


if __name__ == "__main__":
    main()
