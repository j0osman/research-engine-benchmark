#!/usr/bin/env python3
"""Open-fill sensitivity for the realistic strategy (four-way paper §3.9, §4.4).

Reamer Research fills a bar-origin market order at tick 1 of bar N+1's
synthetic tick path; Backtrader fills it at bar N+1's open. Tick 0 is pinned
to the open, but a market order cannot fill there and every bar has at least
4 ticks (engine/synthetic_ticks.cpp), so no config gives open fills.

The realistic strategy's decisions read only closes, highs, lows and its own
target, never a fill price, and 1,000 units never approaches the margin
limit, so the same fills happen on the same bars at any seed. Repricing every
fill but the end-of-run force-close at its bar's open is therefore exact: it
is the PnL the engine would report with open fills. A hand simulation (fill at
the next open, mark the end position at the last close) checks it
independently.

Per tier and seed, prints one JSON line: engine PnL at that seed, PnL repriced
at the open, the hand-simulation PnL, fills, and the fill-price offset from
the open (pips).

Usage: open_fill_sensitivity.py <bin_path> <seed>[,<seed>...]
Same environment as reamer_runner.py, with bench_runners/ on PYTHONPATH.
"""
import contextlib
import io
import json
import sys

import numpy as np

import reamer_runner

BAR_DTYPE = reamer_runner.BAR_DTYPE
PIP = 1e-4


def hand_sim(b):
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    pos = cash = target = 0.0
    pending = None
    fills = 0
    for i in range(len(b)):
        if pending is not None:
            cash -= pending * o[i]
            pos += pending
            fills += 1
            pending = None
        if i >= 20:
            hi, lo = h[i - 20:i].max(), l[i - 20:i].min()
            if c[i] > hi and target <= 0:
                target = 1000.0
            elif c[i] < lo and target >= 0:
                target = -1000.0
            if pos != target:
                pending = target - pos
    return cash + pos * c[-1], fills


def fills_from_ledger(ct):
    """(timestamp, price, signed qty) per fill, final force-close last."""
    def q(t, entry):
        s = 1.0 if t["side"] == "buy" else -1.0
        return s * t["qty"] if entry else -s * t["qty"]
    out = [(ct[0]["open_timestamp"], ct[0]["entry_price"], q(ct[0], True))]
    for k, t in enumerate(ct):
        nxt = q(ct[k + 1], True) if k + 1 < len(ct) else 0.0
        out.append((t["close_timestamp"], t["exit_price"], q(t, False) + nxt))
    return out


def main():
    bin_path, seeds = sys.argv[1], [int(s) for s in sys.argv[2].split(",")]
    b = np.fromfile(bin_path, dtype=BAR_DTYPE)
    ts_str = np.datetime_as_string(b["ts"].astype("datetime64[s]"), unit="s")
    row = {s.replace("T", " "): i for i, s in enumerate(ts_str)}
    hand_pnl, hand_fills = hand_sim(b)

    orig = reamer_runner.run_backtest
    for seed in seeds:
        cap = {}

        def run(strat, cfg, data, seed=seed):
            cap["r"] = orig(strat, dict(cfg, rng_seed=seed), data)
            return cap["r"]

        reamer_runner.run_backtest = run
        sys.argv = ["reamer_runner.py", bin_path, "realistic"]
        with contextlib.redirect_stdout(io.StringIO()):
            reamer_runner.main()
        r = cap["r"]
        fills = fills_from_ledger(r.closed_trades)
        idx = np.array([row[t] for t, _, _ in fills])
        px = np.array([p for _, p, _ in fills])
        qty = np.array([q for _, _, q in fills])
        off = px[:-1] - b["open"][idx[:-1]]          # force-close excluded
        engine_pnl = float(r.net_pnl)
        repriced = engine_pnl + float(np.sum(qty[:-1] * off))
        print(json.dumps({
            "bars": len(b), "seed": seed,
            "engine_pnl": round(engine_pnl, 6),
            "open_fill_pnl": round(repriced, 6),
            "hand_sim_pnl": round(hand_pnl, 6),
            "fills_excl_force_close": len(fills) - 1, "hand_sim_fills": hand_fills,
            "force_close_at_last_close": bool(idx[-1] == len(b) - 1 and px[-1] == b["close"][-1]),
            "fills_at_open": int(np.sum(np.abs(off) < 1e-12)),
            "offset_pips_mean": round(float(off.mean()) / PIP, 4),
            "offset_pips_abs_mean": round(float(np.abs(off).mean()) / PIP, 4),
            "offset_pips_min": round(float(off.min()) / PIP, 3),
            "offset_pips_max": round(float(off.max()) / PIP, 3),
        }), flush=True)


if __name__ == "__main__":
    main()
