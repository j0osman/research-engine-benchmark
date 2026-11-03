#!/usr/bin/env python3
"""Final tables for the four-way results draft, from a pulled results/ dir.

Supersession rules (draft-fourway-method.md §3.11):
  - fourway: LEAN and reamer_py rows at 50K/500K come from fourway_rerun.jsonl
    (fixed main.py / fixed binding); the originals are superseded.
  - breadth: reamer_py rows come from portfolio_rerun.jsonl; LEAN 10-ticker
    realistic rep 1 (deadlocked, killed) is replaced by its rerun row.
  - Rows whose runner printed "skipped: cut by the operator" (budget cut) are
    excluded, not counted as failures.
  - LEAN breadth wall time is the successful attempt's run_s (whole dotnet
    process); outer_wall_s there includes killed hung attempts.

Usage: analyze_final.py <results_dir>
"""
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

R = Path(sys.argv[1])


def rows(name):
    p = R / name
    return [json.loads(l) for l in open(p)] if p.exists() else []


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def rng(xs):
    xs = [x for x in xs if x is not None]
    return (min(xs), max(xs)) if xs else (None, None)


def fills(r):
    for k in ("fills", "orders"):
        if r.get(k) is not None:
            return r[k]
    return None


def trips(r):
    for k in ("round_trips", "trades", "closed_trades"):
        if r.get(k) is not None:
            return r[k]
    return None


# ---------------------------------------------------------------- single ticker
SUPERSEDED_BARS = {"lean", "reamer_py"}
bars = [r for r in rows("fourway.jsonl")
        if not (r["variant"] in SUPERSEDED_BARS and r["tier"] in (50000, 500000))]
bars += rows("fourway_rerun.jsonl")
g = defaultdict(list)
for r in bars:
    if r["ok"]:
        g[(r["tier"], r["strategy"], r["variant"])].append(r)

print("## Single ticker (medians; [min-max] wall)\n")
print("| tier | strategy | variant | n | wall s | wall range | run_s | wall bars/s | RSS MB | RSS range | fills | trips | PnL set |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for (t, s, v), rs in sorted(g.items()):
    w = [r["outer_wall_s"] for r in rs]
    m = [r["outer_max_rss_mb"] for r in rs]
    pn = sorted({round(r["net_pnl"], 2) for r in rs if r.get("net_pnl") is not None})
    fl = sorted({fills(r) for r in rs if fills(r) is not None})
    tp = sorted({trips(r) for r in rs if trips(r) is not None})
    bp = sorted({r["bars_processed"] for r in rs if r.get("bars_processed")})
    print(f"| {t:,} | {s} | {v} | {len(rs)} | {med(w):.2f} | {min(w):.2f}-{max(w):.2f} | "
          f"{med([r.get('run_s') for r in rs]):.3f} | {t / med(w):,.0f} | {med(m):.1f} | "
          f"{min(m):.1f}-{max(m):.1f} | {fl} | {tp} | {pn}{' bars ' + str(bp) if bp else ''} |")

# Fit wall = a + b*bars per engine/strategy
print("\n## Linear fits wall = a + b*bars (medians per tier)\n")
print("| strategy | variant | tiers | a (s) | 1/b (bars/s) | R2 | RSS a (MB) | bytes/bar |")
print("|---|---|---|---|---|---|---|---|")
for s in ("minimal", "realistic"):
    for v in sorted({k[2] for k in g}):
        pts = sorted((t, med([r["outer_wall_s"] for r in g[(t, s, v)]]),
                      med([r["outer_max_rss_mb"] for r in g[(t, s, v)]]))
                     for (t, s2, v2) in g if s2 == s and v2 == v)
        if len(pts) < 2:
            continue
        xs, ys, ms = zip(*pts)
        n = len(xs); mx, my, mm = sum(xs) / n, sum(ys) / n, sum(ms) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        a = my - b * mx
        ss_res = sum((y - a - b * x) ** 2 for x, y in zip(xs, ys))
        ss_tot = sum((y - my) ** 2 for y in ys)
        r2 = 1 - ss_res / ss_tot if ss_tot and n > 2 else float("nan")
        bm = sum((x - mx) * (y - mm) for x, y in zip(xs, ms)) / sxx
        am = mm - bm * mx
        print(f"| {s} | {v} | {','.join(f'{x // 1000}K' for x in xs)} | {a:.3f} | {1 / b:,.0f} | "
              f"{r2:.4f} | {am:.0f} | {bm * 1024 * 1024:.0f} |")

# ---------------------------------------------------------------- breadth
pf = []
for r in rows("portfolio.jsonl"):
    if r["variant"] == "reamer_py":
        continue
    if r["variant"] == "lean" and r["n_tickers"] == 100:
        continue  # budget cut
    if r["variant"] == "lean" and r["n_tickers"] == 10 and r["rep"] == 1 and r["strategy"] == "realistic":
        continue  # deadlocked, replaced by rerun
    pf.append(r)
pf += rows("portfolio_rerun.jsonl")
gb = defaultdict(list)
for r in pf:
    gb[(r["n_tickers"], r["strategy"], r["variant"])].append(r)

print("\n## Breadth (35,064 bars per ticker)\n")
print("| tickers | strategy | variant | ok/n | wall s (med) | wall range | RSS MB (med) | fills | trips | PnL set | hung attempts |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for (n, s, v), rs in sorted(gb.items()):
    ok = [r for r in rs if r["ok"]]
    if v == "lean":
        w = [r["run_s"] for r in ok]
    else:
        w = [r["outer_wall_s"] for r in ok]
    m = [r["outer_max_rss_mb"] for r in ok]
    hung = [len(r.get("hung_attempts_s") or []) for r in ok]
    print(f"| {n} | {s} | {v} | {len(ok)}/{len(rs)} | {med(w):.2f} | {min(w):.2f}-{max(w):.2f} | {med(m):.1f} | "
          f"{sorted({fills(r) for r in ok})} | {sorted({trips(r) for r in ok})} | "
          f"{sorted({round(r['net_pnl'], 2) for r in ok})} | {hung if v == 'lean' else ''} |")

print("\n### Breadth scaling exponent (wall ~ tickers^k, 10 -> 100, or 10 -> 50)\n")
import math
for s in ("minimal", "realistic"):
    for v in sorted({k[2] for k in gb}):
        def w(n):
            ok = [r for r in gb.get((n, s, v), []) if r["ok"]]
            return med([r["run_s"] if v == "lean" else r["outer_wall_s"] for r in ok]) if ok else None
        hi = 100 if w(100) else 50
        if w(10) and w(hi):
            print(f"- {s} {v}: k = {math.log(w(hi) / w(10)) / math.log(hi / 10):.2f} (10->{hi})")

# ---------------------------------------------------------------- parallel
print("\n## Parallel (K concurrent 500K realistic)\n")
batch = [r for r in rows("parallel.jsonl") if r.get("record") == "batch"]
base = {r["variant"]: r["backtests_per_hour"] for r in batch if r["k"] == 1}
print("| variant | K | ok | batch wall s | median run s | backtests/h | efficiency | aggregate bars/s | sum RSS MB |")
print("|---|---|---|---|---|---|---|---|---|")
for r in sorted(batch, key=lambda r: (r["variant"], r["k"])):
    eff = r["backtests_per_hour"] / (r["k"] * base[r["variant"]]) if r["ok_copies"] == r["k"] and r["k"] else None
    print(f"| {r['variant']} | {r['k']} | {r['ok_copies']}/{r['k']} | {r['batch_wall_s']:.2f} | "
          f"{r['median_run_s'] if r['median_run_s'] is not None else '-'} | {r['backtests_per_hour'] or 0:,.0f} | "
          f"{f'{eff:.2f}' if eff else '-'} | {r['aggregate_bars_per_s'] or 0:,} | {r['sum_peak_rss_mb'] or 0:,.0f} |")
pcopies = [r for r in rows("parallel.jsonl") if r.get("record") != "batch" and r.get("ok")]
pp = defaultdict(set)
for r in pcopies:
    pp[r["variant"]].add(round(r["net_pnl"], 5))
print("\nParallel PnL sets:", dict(pp))

# ---------------------------------------------------------------- determinism
print("\n## Determinism\n")
dg = defaultdict(list)
for r in rows("determinism.jsonl"):
    dg[r["variant"]].append(r)
for v, rs in dg.items():
    print(f"- {v}: {sum(r['ok'] for r in rs)}/{len(rs)} ok; PnL {sorted({r.get('net_pnl') for r in rs})}; "
          f"fills {sorted({fills(r) for r in rs})}; trips {sorted({trips(r) for r in rs})}; "
          f"hashes {sorted({(r.get('ledger_sha256') or '')[:12] for r in rs})}")
print("\nRepeat-run agreement, all engines, all reps used above (distinct PnL / fills per cell):")
cells = defaultdict(lambda: [0, set(), set()])
for (t, s, v), rs in g.items():
    if s == "realistic":
        c = cells[v]; c[0] += len(rs)
        if len({round(r["net_pnl"], 6) for r in rs}) > 1 or len({fills(r) for r in rs}) > 1:
            c[1].add(t)
for (n, s, v), rs in gb.items():
    ok = [r for r in rs if r["ok"]]
    if s == "realistic" and ok:
        c = cells[v]; c[0] += len(ok)
        if len({round(r["net_pnl"], 6) for r in ok}) > 1 or len({fills(r) for r in ok}) > 1:
            c[2].add(n)
for v, (n, bad_t, bad_n) in sorted(cells.items()):
    print(f"- {v}: {n} realistic runs; tiers with >1 outcome {sorted(bad_t)}; breadth with >1 outcome {sorted(bad_n)}")
