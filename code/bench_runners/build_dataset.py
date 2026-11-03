#!/usr/bin/env python3
"""Builds a bar dataset of exactly n_bars by tiling a base .bin file with
continuous timestamps, writing either a raw .bin (numpy only) or a pickled
pandas DataFrame for Backtrader (numpy + pandas), depending on --format.

Kept as its own script, invoked via subprocess by
backtrader_comparison_bench.py under whichever interpreter already has the
needed dependency (--format bin under --reamer-python, which needs numpy
anyway for reamer_py itself; --format pkl under --bt-python, which has
pandas) — so the orchestrator itself never needs numpy/pandas and runs under
any bare system python3.

Usage: build_dataset.py <base_bin> <n_bars> <out_path> <bin|pkl|csv>

--format csv is for lean_runner.py's LEAN custom-data source: one
Time,Open,High,Low,Close,Volume line per bar, Time as
yyyyMMdd HH:mm:ss (LEAN's own Reader() parses this format directly).

csv timestamps are re-anchored to start 2000-01-01 UTC instead of the base
series' real calendar dates — confirmed directly and necessary: LEAN clamps
backtest end dates to real wall-clock "now," so a tier whose real-dated
timestamps (base series starts ~2024) would run past today gets silently
truncated mid-run with no error (500,000 bars requested at the real 2024
anchor processed only ~65,000 before hitting "now"). Re-anchoring to 2000
gives ~26 years of headroom before "now," enough for the full
--compare-bars tier (500K bars ≈ 14.26 years) to run uncut. It is not
enough for --scale-bars (5,000,000 bars ≈ 142 years, which cannot fit
before "now" from any anchor still describing 2020s-era software) — that
tier remains genuinely impossible for LEAN under this date model, not a
data-prep problem this re-anchoring fixes. The re-anchoring only changes
which calendar dates the real, unmodified price values are labeled with —
same synthetic-extension spirit as the tiling itself (see build_bars());
only lean_runner.py's csv path needs this, the other three engines never
apply any wall-clock check to the data they're given.
"""
import sys

import numpy as np

INTERVAL_S = 900  # matches generate_stress_datasets.py — 15-minute bars

BAR_DTYPE = np.dtype([
    ("ts", "<i8"), ("open", "<f8"), ("high", "<f8"),
    ("low", "<f8"), ("close", "<f8"), ("volume", "<f8"),
])


def build_bars(n_bars: int, base: np.ndarray) -> np.ndarray:
    """Tile (and truncate to exactly n_bars) with continuous timestamps."""
    span = int(base["ts"][-1] - base["ts"][0]) + INTERVAL_S
    copies_needed = -(-n_bars // len(base))  # ceil div
    tiles = []
    for k in range(copies_needed):
        tile = base.copy()
        tile["ts"] += k * span
        tiles.append(tile)
    return np.concatenate(tiles)[:n_bars]


def main():
    base_bin, n_bars, out_path, fmt = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    base = np.fromfile(base_bin, dtype=BAR_DTYPE)
    arr = build_bars(n_bars, base)

    if fmt == "bin":
        arr.tofile(out_path)
    elif fmt == "pkl":
        import pandas as pd
        idx = pd.to_datetime(arr["ts"], unit="s", utc=True)
        df = pd.DataFrame({
            "open": arr["open"], "high": arr["high"], "low": arr["low"],
            "close": arr["close"], "volume": arr["volume"],
        }, index=idx)
        df.to_pickle(out_path)
    elif fmt == "csv":
        import datetime
        LEAN_ANCHOR = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        offset_s = int(LEAN_ANCHOR.timestamp()) - int(arr["ts"][0])
        with open(out_path, "w") as f:
            for row in arr:
                dt = datetime.datetime.fromtimestamp(int(row["ts"]) + offset_s, tz=datetime.timezone.utc)
                f.write(
                    f"{dt:%Y%m%d %H:%M:%S},{row['open']:.5f},{row['high']:.5f},"
                    f"{row['low']:.5f},{row['close']:.5f},{max(row['volume'], 1.0):.0f}\n"
                )
    else:
        sys.exit(f"unknown format: {fmt!r} (expected bin, pkl, or csv)")

    print(f"wrote {len(arr):,} bars -> {out_path}")


if __name__ == "__main__":
    main()
