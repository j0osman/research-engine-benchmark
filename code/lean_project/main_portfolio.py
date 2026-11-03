from AlgorithmImports import *
import os

# Portfolio counterpart to main.py's ReamerBenchAlgorithm — kept in its own
# module rather than added to main.py because LEAN's AlgorithmPythonWrapper
# does NOT select the class by config's algorithm-type-name: it iterates
# every QCAlgorithm subclass in the module (AlgorithmFactory/Python/Wrappers/
# AlgorithmPythonWrapper.cs, ~line 106) and keeps overwriting the instance
# with no break, so whichever subclass sorts last in dir(module) silently
# wins regardless of what the config asked for. Confirmed directly: with
# both ReamerBenchAlgorithm and PortfolioBenchAlgorithm in main.py, LEAN
# always ran ReamerBenchAlgorithm (alphabetically last) even when config
# requested PortfolioBenchAlgorithm — it does say in its own Loader.cs
# error text "there is one and only one class derived from QCAlgorithm",
# i.e. this is a documented constraint, not a workaround for a bug.


class BenchBar(PythonData):
    """Same custom data source as main.py's BenchBar — duplicated here
    (rather than imported) because it must live in the same module as the
    algorithm class per the one-class-per-file constraint above."""

    def GetSource(self, config, date, isLiveMode):
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


class PortfolioBenchAlgorithm(QCAlgorithm):
    """Counterpart to reamer_portfolio_runner.py / backtrader_portfolio_runner.py
    — N tickers (T000.CSV .. T0{N-1}.CSV, from build_portfolio_dataset.py
    --format csv) on one shared timeline, one OnData(slice) call per aligned
    step with all N symbols available inside it — LEAN's own Slice model
    already does this natively, so this is actually a more direct fit than
    the single-ticker case was, not a bolted-on adaptation. Same 20-bar
    Donchian channel breakout per ticker, same 1,000-unit
    reversal-as-single-order netting, as the other three engines' portfolio
    runners."""

    def Initialize(self):
        n_tickers = int(self.GetParameter("n_tickers", "50"))
        strat = self.GetParameter("strategy", "minimal")

        # Same 2000-01-01 re-anchor as ReamerBenchAlgorithm, same reason —
        # see build_portfolio_dataset.py's csv-format docstring. Portfolio
        # scale (default ~1 year/ticker) needs far less headroom than the
        # single-ticker 500K-bar tier did, but the margin costs nothing.
        self.SetStartDate(1999, 1, 1)
        self.SetEndDate(2020, 1, 1)
        self.SetCash(10_000_000 * n_tickers)  # matches reamer_portfolio_runner.py's per-ticker scaling
        self.SetBenchmark(lambda dt: 0)
        self.Portfolio.MarginCallModel = MarginCallModel.Null

        self.tickers = [f"T{i:03d}" for i in range(n_tickers)]
        self.symbols = [self.AddData(BenchBar, f"{t}.CSV", Resolution.Minute).Symbol for t in self.tickers]
        self.strategy = strat
        self.highs = {s: [] for s in self.symbols}
        self.lows = {s: [] for s in self.symbols}
        self.steps_seen = 0
        self.orders = 0
        self.cash = 10_000_000 * n_tickers

    def OnData(self, data):
        # One call per aligned step across all N tickers — matches
        # reamer_portfolio_runner.py's on_bar shape, not once-per-ticker-bar
        # (that's NautilusTrader's shape; see nautilus_portfolio_runner.py).
        self.steps_seen += 1

        if self.strategy == "minimal":
            for s in self.symbols:
                if s in data and data[s] is not None:
                    _ = data[s].Close
            return

        # realistic: 20-bar Donchian channel breakout per ticker, same
        # logic/params as reamer_portfolio_runner.py's RealisticPortfolio /
        # backtrader_portfolio_runner.py's RealisticPortfolioBT. Same
        # before-appending-current-bar ordering as main.py's
        # ReamerBenchAlgorithm, so the channel never includes the bar it's
        # being evaluated against.
        for s in self.symbols:
            if s not in data or data[s] is None:
                continue
            pos = self.Portfolio[s].Quantity
            highs, lows = self.highs[s], self.lows[s]
            if len(highs) >= 20:
                channel_high = max(highs[-20:])
                channel_low = min(lows[-20:])
                close = data[s].Close
                if close > channel_high and pos <= 0:
                    self.MarketOrder(s, 1000.0 - pos)
                    self.orders += 1
                elif close < channel_low and pos >= 0:
                    self.MarketOrder(s, -(1000.0 + pos))
                    self.orders += 1
            highs.append(data[s].High)
            lows.append(data[s].Low)
            if len(highs) > 20:
                highs.pop(0)
                lows.pop(0)

    def OnEndOfAlgorithm(self):
        round_trips = len(self.TradeBuilder.ClosedTrades) if hasattr(self, "TradeBuilder") else 0
        self.Debug(f"REAMER_BENCH_TRADES={round_trips}")
        self.Debug(f"REAMER_BENCH_STEPS_SEEN={self.steps_seen}")
        self.Debug(f"REAMER_BENCH_ORDERS={self.orders}")
        self.Debug(f"REAMER_BENCH_NET_PNL={self.Portfolio.TotalPortfolioValue - self.cash}")
