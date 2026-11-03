#!/usr/bin/env python3
"""Runs one equivalent NautilusTrader backtest and prints a single JSON
result line to stdout. Counterpart to reamer_runner.py / backtrader_runner.py
— same two workloads (minimal no-op, 20-bar Donchian breakout with real order flow),
built directly from the same .bin numpy dataset both other runners use (no
separate Nautilus-specific dataset format needed — Bar objects are
constructed at runtime from the flat 48-byte records).

Usage: nautilus_runner.py <bin_path> <minimal|realistic>
Requires: pip install nautilus_trader pandas
"""
import json
import os
import sys
import time
from collections import deque

import numpy as np

T0 = time.perf_counter()

# Portfolio.__init__ unconditionally constructs a PortfolioAnalyzer and calls
# analyzer.record_trade() on every position close (nautilus_trader/portfolio/
# portfolio.pyx) — this is NOT gated by BacktestEngineConfig(run_analysis=False),
# which only skips the final aggregate report. record_trade() internally calls
# pandas.concat() to append a single row to a growing Series on every trade —
# O(n^2) with trade count. Confirmed via cProfile: at 40,000 bars / 2,638 trades
# this function alone was 55% of total wall time, and throughput degraded from
# ~512 trades/s to ~124 trades/s as trade count grew — a 500K-bar run with real
# order flow effectively never finishes. Neither reamer_py's nor Backtrader's
# timed runs compute an equivalent running per-trade PnL series, so leaving
# this active isn't a fair inclusion — same reasoning as run_analysis=False,
# just at the component level that flag doesn't reach. No BacktestEngineConfig
# option disables it; the analyzer is instantiated directly by Portfolio.
# Re-measured per release: NAUTILUS_PATCH_RECORD_TRADE=1 applies the patch,
# and a published run states whether it was needed.
if os.environ.get("NAUTILUS_PATCH_RECORD_TRADE") == "1":
    from nautilus_trader.analysis.analyzer import PortfolioAnalyzer
    PortfolioAnalyzer.record_trade = lambda self, *a, **kw: None

BAR_DTYPE = np.dtype([
    ("ts", "<i8"), ("open", "<f8"), ("high", "<f8"),
    ("low", "<f8"), ("close", "<f8"), ("volume", "<f8"),
])


def peak_rss_mb():
    try:
        import resource
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024)
    except ImportError:
        return None


def build_bars(bin_path, instrument, bar_type):
    from nautilus_trader.model import Bar

    arr = np.fromfile(bin_path, dtype=BAR_DTYPE)
    # Bar volume is set far above any order. Bar execution sizes each
    # simulated price level from the bar's volume (~165 units here), so a
    # 1,000-2,000 unit order filled in parts across the bar's prices: 23,354
    # fills for 11,818 orders at 500K bars. The other engines fill a market
    # order whole, and no strategy reads volume.
    bars = []
    for row in arr:
        ts = int(row["ts"]) * 1_000_000_000  # seconds -> ns
        bars.append(Bar(
            bar_type=bar_type,
            open=instrument.make_price(float(row["open"])),
            high=instrument.make_price(float(row["high"])),
            low=instrument.make_price(float(row["low"])),
            close=instrument.make_price(float(row["close"])),
            volume=instrument.make_qty(1e9),
            ts_event=ts, ts_init=ts,
        ))
    return bars


def net_pnl(engine, venue, starting_cash):
    """Realized balance change plus the open positions' unrealized PnL. The
    account balance alone leaves out the position still open at the end,
    which the other engines mark to the last close."""
    account = engine.portfolio.account(venue)
    balance = float(list(account.balances_total().values())[0])
    unrealized = 0.0
    for p in engine.cache.positions_open():
        u = engine.portfolio.unrealized_pnl(p.instrument_id)
        unrealized += float(u) if u is not None else 0.0
    return balance - starting_cash + unrealized


def _zero_fee_model():
    from nautilus_trader.backtest.models import FeeModel
    from nautilus_trader.model import Money

    class ZeroFeeModel(FeeModel):
        def get_commission(self, order, fill_qty, fill_px, instrument):
            return Money(0, instrument.quote_currency)

    return ZeroFeeModel()


def make_engine_and_instrument(ticker):
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model import BarSpecification, BarType, Money, Venue
    from nautilus_trader.model.enums import (
        AccountType, AggregationSource, BarAggregation, OmsType, PriceType,
    )
    from nautilus_trader.model.identifiers import TraderId
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    # GBP/USD, not equity(): the benchmark dataset is real GBP/USD FX data
    # (see generate_stress_datasets.py) at 5-decimal pip precision. equity()
    # instruments default to price_precision=2 (cents) — confirmed via direct
    # testing that this silently truncates 1.26153/1.26200/1.26184-style
    # prices all down to 1.26, flattening the close series for long stretches
    # and suppressing the 5/20 SMA crossover almost entirely (10,399 trades
    # here vs. reamer_py's 31,096 and Backtrader's 32,477 on the same 500K-bar
    # data — a ~3x gap, not the few-percent difference the other two engines
    # show each other). default_fx_ccy gives price_precision=5, matching the
    # data exactly. This bug was latent but invisible with the old crypto
    # dataset (~$2,700/unit, where 2-decimal rounding is a negligible 0.0004%
    # relative error); it only became catastrophic once the benchmark moved
    # to FX-range prices.
    instrument = TestInstrumentProvider.default_fx_ccy("GBP/USD", Venue("SIM"))
    bar_spec = BarSpecification(15, BarAggregation.MINUTE, PriceType.LAST)
    bar_type = BarType(instrument.id, bar_spec, AggregationSource.EXTERNAL)

    # run_analysis=False skips Nautilus's own post-run statistics *report* —
    # a separate, smaller concern from the record_trade() patch above (which
    # is the actual O(n^2) fix; run_analysis=False alone does not touch it).
    # bypass_logging=True: Nautilus logs every bar/order/position event at
    # INFO level by default (plus a large startup banner) — ~4KB/bar observed,
    # which both adds real per-event overhead and would balloon subprocess
    # output capture at scale. reamer_py runs with show_progress=False and
    # Backtrader prints nothing during Cerebro.run(); this keeps Nautilus's
    # timed region equally silent.
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BT-001"), run_analysis=False,
        logging=LoggingConfig(bypass_logging=True)))
    venue = Venue("SIM")
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,  # not CASH — strategy goes both long and short
        base_currency=instrument.quote_currency,
        starting_balances=[Money(10_000_000, instrument.quote_currency)],
        # default_fx_ccy charges a 0.002% taker fee; every other runner runs
        # commission-free. FixedFeeModel rejects zero, hence the subclass.
        fee_model=_zero_fee_model(),
    )
    engine.add_instrument(instrument)
    return engine, instrument, bar_type


def run_minimal(bin_path, ticker):
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    class MinimalConfig(StrategyConfig, frozen=True):
        bar_type: object

    class MinimalStrategy(Strategy):
        def on_start(self):
            self.subscribe_bars(self.config.bar_type)

        def on_bar(self, bar):
            _ = bar.close

    engine, instrument, bar_type = make_engine_and_instrument(ticker)
    bars = build_bars(bin_path, instrument, bar_type)
    engine.add_data(bars)
    engine.add_strategy(MinimalStrategy(config=MinimalConfig(bar_type=bar_type)))
    return engine, bars, {"trades": 0}


def run_realistic(bin_path, ticker):
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    class RealisticConfig(StrategyConfig, frozen=True):
        bar_type: object
        instrument_id: object

    class RealisticStrategy(Strategy):
        # Same 20-bar Donchian breakout as every other runner: channel over
        # the 20 bars before the current one, flip to +1000 / -1000 with one
        # order sized to reverse (NETTING OMS nets it like the others).
        # 1000 units: default_fx_ccy enforces a 1,000-unit minimum quantity,
        # so every runner trades 1000.
        def __init__(self, config):
            super().__init__(config)
            self.highs = deque(maxlen=20)
            self.lows = deque(maxlen=20)
            self.fills = 0
            self.orders = 0
            self.instrument = None  # resolved once in on_start, not per-bar

        def on_start(self):
            self.instrument = self.cache.instrument(self.config.instrument_id)
            self.subscribe_bars(self.config.bar_type)

        def on_order_filled(self, event):
            self.fills += 1

        def on_bar(self, bar):
            high, low, close = float(bar.high), float(bar.low), float(bar.close)
            if len(self.highs) == 20:
                pos = float(self.portfolio.net_position(self.config.instrument_id))
                if close > max(self.highs) and pos <= 0:
                    self.orders += 1
                    self.submit_order(self.order_factory.market(
                        self.config.instrument_id, OrderSide.BUY,
                        self.instrument.make_qty(1000.0 - pos), TimeInForce.GTC))
                elif close < min(self.lows) and pos >= 0:
                    self.orders += 1
                    self.submit_order(self.order_factory.market(
                        self.config.instrument_id, OrderSide.SELL,
                        self.instrument.make_qty(1000.0 + pos), TimeInForce.GTC))
            self.highs.append(high)
            self.lows.append(low)

    engine, instrument, bar_type = make_engine_and_instrument(ticker)
    bars = build_bars(bin_path, instrument, bar_type)
    engine.add_data(bars)
    strat = RealisticStrategy(config=RealisticConfig(bar_type=bar_type, instrument_id=instrument.id))
    engine.add_strategy(strat)
    return engine, bars, strat  # strat exposes .fills after run()


def main():
    bin_path, strat_name = sys.argv[1], sys.argv[2]
    ticker = "BENCH"

    if strat_name == "minimal":
        engine, bars, extra = run_minimal(bin_path, ticker)
    else:
        engine, bars, extra = run_realistic(bin_path, ticker)

    t0 = time.perf_counter()
    engine.run()
    run_s = time.perf_counter() - t0

    from nautilus_trader.model import Venue
    pnl = net_pnl(engine, Venue("SIM"), 10_000_000.0)
    # Bar execution can fill one order in several parts, so round trips
    # come from orders: every order after the first closes one (each flips).
    fills = 0 if isinstance(extra, dict) else extra.fills
    orders = 0 if isinstance(extra, dict) else extra.orders
    round_trips = max(orders - 1, 0)
    engine.dispose()

    print(json.dumps({
        "engine": "nautilus_trader",
        "strategy": strat_name,
        "bars": len(bars),
        "run_s": round(run_s, 3),
        "total_s": round(time.perf_counter() - T0, 3),
        "orders": orders,
        "fills": fills,
        "round_trips": round_trips,
        "net_pnl": round(pnl, 6),
        "record_trade_patched": os.environ.get("NAUTILUS_PATCH_RECORD_TRADE") == "1",
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }))


if __name__ == "__main__":
    main()
