// Runs one Reamer Research backtest through the C++ reference wrapper and
// prints a single JSON result line to stdout. Compiled counterpart to
// reamer_runner.py: same .bin dataset, same minimal and 20-bar Donchian
// workloads, same target-position logic.
//
// Usage: reamer_cpp_runner <bin_path> <minimal|realistic>
//
// Build:
//   g++ -O2 -std=c++20 -Ireference-cpp/include -Istrategy/abi \
//       bench/comparison/bench_runners/reamer_cpp_runner.cpp \
//       -Lbuild -lreamer_research -Wl,-rpath,$PWD/build -o build/reamer_cpp_runner

#include <reamer_research.hpp>

#include <sys/resource.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <span>
#include <vector>

namespace {

constexpr size_t kChannel = 20;
constexpr double kSize = 1000.0;

struct RawBar {  // 48-byte record written by build_dataset.py
  int64_t ts;
  double open, high, low, close, volume;
};

}  // namespace

int main(int argc, char* argv[]) {
  const auto t0 = std::chrono::steady_clock::now();
  if (argc != 3) {
    std::fprintf(stderr, "usage: %s <bin_path> <minimal|realistic>\n", argv[0]);
    return 2;
  }
  const bool realistic = std::strcmp(argv[2], "realistic") == 0;

  std::ifstream in(argv[1], std::ios::binary | std::ios::ate);
  const size_t n = static_cast<size_t>(in.tellg()) / sizeof(RawBar);
  std::vector<RawBar> raw(n);
  in.seekg(0);
  in.read(reinterpret_cast<char*>(raw.data()), n * sizeof(RawBar));

  std::vector<ReamerOhlcvBar> bars(n);
  for (size_t i = 0; i < n; ++i) {
    bars[i].ts = raw[i].ts;
    bars[i].open = raw[i].open;
    bars[i].high = raw[i].high;
    bars[i].low = raw[i].low;
    bars[i].close = raw[i].close;
    bars[i].volume = raw[i].volume;
    bars[i].notional = 0.0;
    bars[i].tick_count = -1;
  }

  // Ring buffers of the 20 bars before the current one.
  double highs[kChannel], lows[kChannel];
  size_t seen = 0;
  double target = 0.0;

  auto on_bar = [&](std::span<const ReamerOhlcvBar> window, size_t /*ticker_count*/,
                    std::span<const double> positions,
                    std::span<const uint8_t> /*window_valid*/,
                    std::span<const ReamerPositionState> /*position_states*/,
                    std::span<const ReamerOpenOrder> /*open_orders*/,
                    std::span<ReamerOrderRequest> out_orders,
                    std::span<ReamerOrderAction> /*out_actions*/,
                    size_t& /*action_count*/) -> size_t {
    const ReamerOhlcvBar& bar = window.back();
    if (!realistic) {
      volatile double c = bar.close;
      (void)c;
      return 0;
    }
    size_t count = 0;
    if (seen >= kChannel) {
      const double hi = *std::max_element(highs, highs + kChannel);
      const double lo = *std::min_element(lows, lows + kChannel);
      if (bar.close > hi && target <= 0) target = kSize;
      else if (bar.close < lo && target >= 0) target = -kSize;

      const double position = positions[0];
      if (position != target && !out_orders.empty()) {
        const bool buy = position < target;
        ReamerOrderRequest order = {};
        order.ticker_id = 0;
        order.order_type = buy ? REAMER_RESEARCH_ORDER_TYPE_BUY : REAMER_RESEARCH_ORDER_TYPE_SELL;
        order.kind = REAMER_RESEARCH_ORDER_KIND_MARKET;
        order.side = buy ? REAMER_RESEARCH_SIDE_BUY : REAMER_RESEARCH_SIDE_SELL;
        order.tif = REAMER_RESEARCH_TIF_GTC;
        order.valid = true;
        order.qty = buy ? target - position : position - target;
        out_orders[0] = order;
        count = 1;
      }
    }
    highs[seen % kChannel] = bar.high;
    lows[seen % kChannel] = bar.low;
    ++seen;
    return count;
  };

  ReamerBacktestConfig config = {};
  config.commission_mode = REAMER_RESEARCH_COMMISSION_ROUND_TRIP;
  config.ohlcv_type = REAMER_RESEARCH_OHLCV_BID;
  config.rng_seed = 42;
  config.roll_days_before_monthend = 5;

  std::vector<std::vector<ReamerOhlcvBar>> bars_by_ticker;
  bars_by_ticker.push_back(std::move(bars));

  const auto t_run = std::chrono::steady_clock::now();
  auto result = reamer_research_ref::run_backtest(
      bars_by_ticker, {"BENCH"}, on_bar, config, 10'000'000.0, 1.0, 1);
  const auto t_end = std::chrono::steady_clock::now();
  if (!result) {
    std::fprintf(stderr, "run_backtest failed\n");
    return 1;
  }
  const auto summary = result->summary();

  rusage ru{};
  getrusage(RUSAGE_SELF, &ru);
  const double run_s = std::chrono::duration<double>(t_end - t_run).count();
  const double total_s =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  std::printf(
      "{\"engine\": \"reamer_research_cpp\", \"strategy\": \"%s\", \"bars\": %zu, "
      "\"run_s\": %.3f, \"total_s\": %.3f, \"fills\": %zu, \"round_trips\": %zu, "
      "\"net_pnl\": %.6f, \"peak_rss_mb\": %.1f}\n",
      argv[2], n, run_s, total_s, summary.filled_orders, summary.closed_trades_count,
      summary.net_pnl, ru.ru_maxrss / 1024.0);
  return 0;
}
