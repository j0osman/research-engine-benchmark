from AlgorithmImports import *
import os

# Same two workloads as reamer_runner.py / backtrader_runner.py /
# nautilus_runner.py: "minimal" reads one close price and does nothing else;
# "realistic" runs a 20-bar Donchian channel breakout with real market-order
# flow, 1,000 units per trade, a reversal submitted as a single order sized
# to flip the net position directly rather than close-then-reopen (matches
# reamer_py's netting semantics — see backtrader_runner.py / nautilus_runner.py
# for the same fix applied there, for the same reason).
#
# Data is synthetic GBP/USD 15-minute OHLCV, tiled from a real base series
# (see build_dataset.py --format csv), fed in as LEAN custom data rather than
# QuantConnect's own history — the benchmark dataset is extended to
# arbitrary bar counts, which cannot correspond to any real QC-provided
# historical range. Run directly against the open-source LEAN engine
# (QuantConnect.Lean.Launcher.dll via `dotnet exec`) — not lean-cli/Docker;
# see lean_runner.py's docstring for why, and the caveat on backtest length.
#
# Strategy selection and the data file are read via QCAlgorithm parameters,
# set in the generated config.json's "parameters" block (self.GetParameter)
# — not CLI args, LEAN algorithms don't take those. See lean_runner.py.


class BenchBar(PythonData):
    """Custom data source: one Time,Open,High,Low,Close,Volume line per bar,
    written by build_dataset.py --format csv. GetSource() resolves the
    filename from a project parameter so the same algorithm serves every
    bar-count tier without editing this file per run."""

    def GetSource(self, config, date, isLiveMode):
        # Custom-data GetSource() must return a fully resolved path — it is
        # NOT automatically joined against the configured data-folder the
        # way built-in data types are; Globals.DataFolder gives that path
        # explicitly (confirmed necessary directly — a bare
        # "data/alternative/bench/..." string silently matched zero rows,
        # with no error raised, LEAN just reported "1 data point" total).
        #
        # config.Symbol.Value is upper-cased by LEAN's own symbol handling
        # regardless of what's passed to AddData() — also confirmed
        # directly (a lowercase-suffixed file on a case-sensitive Linux
        # filesystem silently matched nothing). lean_runner.py writes the
        # data file under its upper-cased name for exactly this reason;
        # this is not doing the upper-casing itself, just relying on it
        # already being true on disk.
        csv_file = config.Symbol.Value
        path = os.path.join(Globals.DataFolder, "alternative", "bench", csv_file)
        return SubscriptionDataSource(path, SubscriptionTransportMedium.LocalFile)

    def Reader(self, config, line, date, isLiveMode):
        if not line or line[0].isalpha():
            return None
        parts = line.split(",")
        bar = BenchBar()
        bar.Symbol = config.Symbol
        bar.Time = datetime.strptime(parts[0], "%Y%m%d %H:%M:%S")
        bar.Value = float(parts[4])  # Close
        bar["Open"] = float(parts[1])
        bar["High"] = float(parts[2])
        bar["Low"] = float(parts[3])
        bar["Close"] = float(parts[4])
        bar["Volume"] = float(parts[5])
        return bar


class ReamerBenchAlgorithm(QCAlgorithm):
    def Initialize(self):
        csv_file = self.GetParameter("csv_file", "TIER_500000.CSV")
        strat = self.GetParameter("strategy", "minimal")

        # LEAN clamps backtest end dates to real wall-clock "now" — confirmed
        # directly (SetEndDate(2150, 1, 1) silently became "today" in
        # practice, and a run at the real 2024-anchored dates silently
        # processed only ~65,000 of 500,000 requested bars, no error
        # raised). Unlike reamer_py/Backtrader/NautilusTrader, which just
        # iterate whatever bars they're handed regardless of what date
        # they're stamped with, LEAN's backtest model is bounded by real
        # elapsed calendar time. build_dataset.py's csv format re-anchors
        # timestamps to start 2000-01-01 specifically so --compare-bars
        # (500K bars ≈ 14.26 years) fits fully before "now" — this date
        # range matches that anchor. --scale-bars (5M bars ≈ 142 years)
        # still cannot fit under any reasonable anchor and remains
        # genuinely impossible for LEAN under this date model — a real
        # methodology limit, not a bug; see docs/benchmarks.md.
        # 500K bars run to 2020-01-26, not 14.26 years on: the base series has
        # weekend gaps, so the tiled csv spans about 20 years. An end date of
        # 2020-01-01 silently dropped the last 1,584 bars.
        self.SetStartDate(1999, 1, 1)  # predates the csv re-anchor (2000-01-01) with margin
        self.SetEndDate(2021, 1, 1)    # covers 500K bars (to 2020-01-26) with margin
        self.SetCash(10_000_000)
        # The csv times are UTC. Custom data otherwise defaults to New York
        # time, where each spring-forward 02:00-02:45 does not exist and those
        # four bars were silently dropped (64 bars at 500K).
        self.SetTimeZone(TimeZones.Utc)
        self.SetBenchmark(lambda dt: 0)  # no benchmark security fetch — not part of either engine's timed workload

        self.symbol = self.AddData(BenchBar, csv_file, Resolution.Minute, TimeZones.Utc).Symbol
        self.strategy = strat
        self.highs = []
        self.lows = []
        self.round_trips = 0
        self.bars_seen = 0
        self.orders = 0
        self.Portfolio.MarginCallModel = MarginCallModel.Null

    def OnData(self, data):
        if self.symbol not in data or data[self.symbol] is None:
            return
        bar = data[self.symbol]
        self.bars_seen += 1

        if self.strategy == "minimal":
            _ = bar.Close
            return

        # realistic: 20-bar Donchian channel breakout, same logic/params as
        # the other two runners' RealisticStrategy/RealisticBT. channel_high
        # / channel_low are computed from self.highs/self.lows BEFORE the
        # current bar gets appended below, so the 20-bar window is strictly
        # the 20 bars before this one — matches reamer_runner.py's
        # high[-21:-1] / low[-21:-1] slicing, current bar excluded.
        pos = self.Portfolio[self.symbol].Quantity
        if len(self.highs) >= 20:
            channel_high = max(self.highs[-20:])
            channel_low = min(self.lows[-20:])
            if bar.Close > channel_high and pos <= 0:
                self.MarketOrder(self.symbol, 1000.0 - pos)
                self.orders += 1
            elif bar.Close < channel_low and pos >= 0:
                self.MarketOrder(self.symbol, -(1000.0 + pos))
                self.orders += 1
        self.highs.append(bar.High)
        self.lows.append(bar.Low)
        if len(self.highs) > 20:
            self.highs.pop(0)
            self.lows.pop(0)

    def OnEndOfAlgorithm(self):
        # TradeBuilder.ClosedTrades comes back as a plain Python list via
        # Python.NET, not a .NET List<T> — .Count doesn't exist on it, len()
        # does (confirmed directly; .Count raised AttributeError at runtime).
        self.round_trips = len(self.TradeBuilder.ClosedTrades) if hasattr(self, "TradeBuilder") else 0
        self.Debug(f"REAMER_BENCH_TRADES={self.round_trips}")
        self.Debug(f"REAMER_BENCH_BARS_SEEN={self.bars_seen}")
        self.Debug(f"REAMER_BENCH_ORDERS={self.orders}")
        self.Debug(f"REAMER_BENCH_NET_PNL={self.Portfolio.TotalPortfolioValue - 10_000_000}")
