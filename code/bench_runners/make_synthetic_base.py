#!/usr/bin/env python3
"""Writes a synthetic base series for people without the GBP/USD export.

The published runs used a GBP/USD MetaTrader 5 export, which is not
redistributed. This script writes a seeded random walk of the same length
and record layout, so the dataset builders and runners work unchanged.

Trade counts and PnL will not match the published ones, because the prices
differ. This file was added for this repository; it did not produce any
published figure.

Usage: make_synthetic_base.py <out.bin> [n_bars] [seed]
"""
import sys

import numpy as np

INTERVAL_S = 900
BAR_DTYPE = np.dtype([
    ("ts", "<i8"), ("open", "<f8"), ("high", "<f8"),
    ("low", "<f8"), ("close", "<f8"), ("volume", "<f8"),
])


def main():
    out_path = sys.argv[1]
    n_bars = int(sys.argv[2]) if len(sys.argv) > 2 else 58_942
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    rng = np.random.default_rng(seed)

    close = 1.25 * np.exp(np.cumsum(rng.normal(0.0, 0.0006, n_bars)))
    open_ = np.concatenate(([1.25], close[:-1]))
    wick = np.abs(rng.normal(0.0, 0.0003, (2, n_bars)))
    bars = np.zeros(n_bars, dtype=BAR_DTYPE)
    bars["ts"] = 1_704_067_200 + INTERVAL_S * np.arange(n_bars)
    bars["open"] = open_
    bars["high"] = np.maximum(open_, close) * (1.0 + wick[0])
    bars["low"] = np.minimum(open_, close) * (1.0 - wick[1])
    bars["close"] = close
    bars["volume"] = rng.integers(100, 5_000, n_bars)
    bars.tofile(out_path)
    print(f"wrote {n_bars:,} bars -> {out_path}")


if __name__ == "__main__":
    main()
