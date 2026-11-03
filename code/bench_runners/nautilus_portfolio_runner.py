#!/usr/bin/env python3
"""Runs an equivalent NautilusTrader multi-instrument backtest (N instruments
on one BacktestEngine, not N independent single-instrument runs) and prints a
single JSON result line to stdout. Counterpart to reamer_portfolio_runner.py
/ backtrader_portfolio_runner.py. Reads the same T{i:03d}.bin files
build_portfolio_dataset.py produces for Reamer Research.

Nautilus's on_bar fires once per bar per subscribed instrument, so the
strategy keeps per-instrument state and looks up the instrument from
bar.bar_type. Venue setup (zero fee model, 1e9 bar volume, MARGIN account)
and the NAUTILUS_PATCH_RECORD_TRADE switch are the same as
nautilus_runner.py; see that file for why each is needed.

Usage: nautilus_portfolio_runner.py <bin_dir> <n_tickers> <minimal|realistic>
Requires: pip install nautilus_trader pandas
"""
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

T0 = time.perf_counter()

if os.environ.get("NAUTILUS_PATCH_RECORD_TRADE") == "1":
    from nautilus_trader.analysis.analyzer import PortfolioAnalyzer
    PortfolioAnalyzer.record_trade = lambda self, *a, **kw: None

from nautilus_runner import BAR_DTYPE, _zero_fee_model, net_pnl, peak_rss_mb

CHANNEL = 20
SIZE = 1000.0


def main():
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model import Bar, BarSpecification, BarType, Money, Venue
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import (
        AccountType, AggregationSource, BarAggregation, OrderSide, PriceType,
        OmsType, TimeInForce,
    )
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    bin_dir, n_tickers, strat_name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    cash = 10_000_000 * n_tickers

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BT-001"), run_analysis=False,
        logging=LoggingConfig(bypass_logging=True)))
    venue = Venue("SIM")
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(cash, USD)],
        fee_model=_zero_fee_model(),
    )

    instruments = []
    bar_types = []
    all_bars = []
    for i in range(n_tickers):
        ticker = f"T{i:03d}"
        # default_fx_ccy, not equity(): equity()'s 2-decimal prices flatten
        # FX-range data. The symbol's last three letters are its quote
        # currency, so every "T000USD".."T999USD" settles in USD.
        instrument = TestInstrumentProvider.default_fx_ccy(f"{ticker}USD", venue)
        bar_spec = BarSpecification(15, BarAggregation.MINUTE, PriceType.LAST)
        bar_type = BarType(instrument.id, bar_spec, AggregationSource.EXTERNAL)
        instruments.append(instrument)
        bar_types.append(bar_type)
        engine.add_instrument(instrument)

        arr = np.fromfile(Path(bin_dir) / f"{ticker}.bin", dtype=BAR_DTYPE)
        for row in arr:
            ts = int(row["ts"]) * 1_000_000_000
            all_bars.append(Bar(
                bar_type=bar_type,
                open=instrument.make_price(float(row["open"])),
                high=instrument.make_price(float(row["high"])),
                low=instrument.make_price(float(row["low"])),
                close=instrument.make_price(float(row["close"])),
                volume=instrument.make_qty(1e9),
                ts_event=ts, ts_init=ts,
            ))

    engine.add_data(all_bars, sort=True)

    class PortfolioConfig(StrategyConfig, frozen=True):
        bar_types: list
        instrument_ids: list

    class MinimalPortfolio(Strategy):
        def on_start(self):
            for bt_ in self.config.bar_types:
                self.subscribe_bars(bt_)

        def on_bar(self, bar):
            _ = bar.close

    class RealisticPortfolio(Strategy):
        """nautilus_runner.py's RealisticStrategy applied per instrument."""

        def __init__(self, config):
            super().__init__(config)
            self.highs = {iid: deque(maxlen=CHANNEL) for iid in config.instrument_ids}
            self.lows = {iid: deque(maxlen=CHANNEL) for iid in config.instrument_ids}
            self.fills = 0
            self.orders = 0
            self.instruments = {}

        def on_start(self):
            for iid in self.config.instrument_ids:
                self.instruments[iid] = self.cache.instrument(iid)
            for bt_ in self.config.bar_types:
                self.subscribe_bars(bt_)

        def on_order_filled(self, event):
            self.fills += 1

        def on_bar(self, bar):
            iid = bar.bar_type.instrument_id
            highs, lows = self.highs[iid], self.lows[iid]
            high, low, close = float(bar.high), float(bar.low), float(bar.close)
            if len(highs) == CHANNEL:
                pos = float(self.portfolio.net_position(iid))
                instrument = self.instruments[iid]
                if close > max(highs) and pos <= 0:
                    self.orders += 1
                    self.submit_order(self.order_factory.market(
                        iid, OrderSide.BUY, instrument.make_qty(SIZE - pos), TimeInForce.GTC))
                elif close < min(lows) and pos >= 0:
                    self.orders += 1
                    self.submit_order(self.order_factory.market(
                        iid, OrderSide.SELL, instrument.make_qty(SIZE + pos), TimeInForce.GTC))
            highs.append(high)
            lows.append(low)

    config = PortfolioConfig(bar_types=bar_types, instrument_ids=[i.id for i in instruments])
    strat = (MinimalPortfolio if strat_name == "minimal" else RealisticPortfolio)(config=config)
    engine.add_strategy(strat)

    t0 = time.perf_counter()
    engine.run()
    run_s = time.perf_counter() - t0

    pnl = net_pnl(engine, venue, cash)
    fills = getattr(strat, "fills", 0)
    orders = getattr(strat, "orders", 0)
    # Every order after a ticker's first closes one round trip (each flips),
    # same counting as nautilus_runner.py, so subtract one per traded ticker.
    traded = sum(1 for iid in strat.config.instrument_ids
                 if engine.cache.orders(instrument_id=iid)) if orders else 0
    engine.dispose()

    print(json.dumps({
        "engine": "nautilus_trader",
        "strategy": strat_name,
        "n_tickers": n_tickers,
        "bars": len(all_bars),
        "run_s": round(run_s, 3),
        "total_s": round(time.perf_counter() - T0, 3),
        "orders": orders,
        "fills": fills,
        "round_trips": orders - traded,
        "net_pnl": round(pnl, 6),
        "record_trade_patched": os.environ.get("NAUTILUS_PATCH_RECORD_TRADE") == "1",
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }))


if __name__ == "__main__":
    main()
