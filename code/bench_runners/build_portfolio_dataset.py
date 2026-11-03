#!/usr/bin/env python3
"""Builds an N-ticker synthetic portfolio dataset: N independent OHLCV
series sharing the same timestamp grid (so union alignment sees them as
fully overlapping, matching "N tickers over the same period"),
each a differently-scaled/offset slice of a base stress dataset. Writes N
.bin files (reamer_py), N pickled DataFrames (Backtrader), or N csv files
(LEAN's custom-data source) into out_dir, depending on --format.

Usage: build_portfolio_dataset.py <base_bin> <bars_per_ticker> <n_tickers> <out_dir> <bin|pkl|csv>

--format csv re-anchors timestamps to 2000-01-01, same reason and same
fix as build_dataset.py's csv branch — LEAN clamps backtests to real
wall-clock "now". At portfolio scale (default 35,064 bars/ticker ≈ 1 year)
this is a bigger safety margin than actually needed, unlike the
single-ticker 500K/5M tiers where it was load-bearing — kept for
consistency and because bars-per-ticker is a caller-supplied argument, not
fixed at 1 year.
"""
import sys
from pathlib import Path

import numpy as np

BAR_DTYPE = np.dtype([
    ("ts", "<i8"), ("open", "<f8"), ("high", "<f8"),
    ("low", "<f8"), ("close", "<f8"), ("volume", "<f8"),
])


def make_ticker(base: np.ndarray, bars_per_ticker: int, ticker_idx: int) -> np.ndarray:
    n_base = len(base)
    if bars_per_ticker > n_base:
        sys.exit(f"bars_per_ticker ({bars_per_ticker:,}) exceeds base dataset size ({n_base:,})")
    offset = (ticker_idx * 9973) % (n_base - bars_per_ticker + 1)  # decorrelate series
    window = base[offset:offset + bars_per_ticker].copy()
    scale = 0.5 + 1.5 * ((ticker_idx * 37 + 11) % 97) / 97.0  # spread prices 0.5x-2.0x apart
    for field in ("open", "high", "low", "close"):
        window[field] = window[field] * scale
    window["ts"] = base["ts"][:bars_per_ticker]  # shared timeline across all tickers
    return window


def main():
    base_bin        = sys.argv[1]
    bars_per_ticker = int(sys.argv[2])
    n_tickers       = int(sys.argv[3])
    out_dir         = Path(sys.argv[4])
    fmt             = sys.argv[5]

    out_dir.mkdir(parents=True, exist_ok=True)
    base = np.fromfile(base_bin, dtype=BAR_DTYPE)

    if fmt == "bin":
        for i in range(n_tickers):
            make_ticker(base, bars_per_ticker, i).tofile(out_dir / f"T{i:03d}.bin")
    elif fmt == "pkl":
        import pandas as pd
        for i in range(n_tickers):
            arr = make_ticker(base, bars_per_ticker, i)
            idx = pd.to_datetime(arr["ts"], unit="s", utc=True)
            df = pd.DataFrame({
                "open": arr["open"], "high": arr["high"], "low": arr["low"],
                "close": arr["close"], "volume": arr["volume"],
            }, index=idx)
            df.to_pickle(out_dir / f"T{i:03d}.pkl")
    elif fmt == "csv":
        import datetime
        LEAN_ANCHOR = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        for i in range(n_tickers):
            arr = make_ticker(base, bars_per_ticker, i)
            offset_s = int(LEAN_ANCHOR.timestamp()) - int(arr["ts"][0])
            with open(out_dir / f"T{i:03d}.csv", "w") as f:
                for row in arr:
                    dt = datetime.datetime.fromtimestamp(int(row["ts"]) + offset_s, tz=datetime.timezone.utc)
                    f.write(
                        f"{dt:%Y%m%d %H:%M:%S},{row['open']:.5f},{row['high']:.5f},"
                        f"{row['low']:.5f},{row['close']:.5f},{max(row['volume'], 1.0):.0f}\n"
                    )
    else:
        sys.exit(f"unknown format: {fmt!r} (expected bin, pkl, or csv)")

    print(f"wrote {n_tickers} tickers x {bars_per_ticker:,} bars -> {out_dir}")


if __name__ == "__main__":
    main()
