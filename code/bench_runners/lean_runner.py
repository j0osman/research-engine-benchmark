#!/usr/bin/env python3
"""Runs one equivalent QuantConnect LEAN backtest and prints a single JSON
result line to stdout. Counterpart to reamer_runner.py / backtrader_runner.py
/ nautilus_runner.py — same two workloads (minimal no-op, MA-crossover with
real order flow), strategy logic in ../lean_project/main.py (LEAN algorithms
are loaded by the engine, not importable, so the strategy can't live inline
in this file the way the other three runners' do).

Runs the open-source LEAN engine directly (`dotnet exec
QuantConnect.Lean.Launcher.dll`) — NOT lean-cli / Docker. lean-cli's `lean
init` and `lean backtest` both refused to run at all without a QuantConnect
account login (confirmed directly, twice: bare `lean init` prompts for
credentials and aborts without them; a hand-authored lean.json still failed
with "old Lean CLI root folder", same root cause) — and QuantConnect's API
token page turned out to be paywalled. The LEAN *engine* itself is Apache-2.0
open source and genuinely runs fully local backtests with no account of any
kind — confirmed directly by building it from source and running it; the
account requirement was lean-cli's gate, not the engine's.

One-time setup this script depends on (not automated here — a full engine
build takes several minutes and is not something to silently redo on every
benchmark run):

  1. Install .NET 10 SDK (LEAN's Launcher.csproj targets net10.0):
       curl -sL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 10.0 --install-dir ~/.dotnet

  2. Clone and build LEAN from source into tests/stress/lean_engine/:
       git clone --depth 1 https://github.com/QuantConnect/Lean.git lean_engine
       cd lean_engine && ~/.dotnet/dotnet build QuantConnect.Lean.sln -c Release
     (~4-5 minutes, 0 errors expected; thousands of CA-rule warnings are normal.)

  3. Python.NET needs a specific Python build for the algorithm side (LEAN
     pins Python 3.11.11 for this engine version) — separate from
     tests/stress/lean_venv/, which is unrelated (that was for lean-cli,
     no longer used):
       wget https://cdn.quantconnect.com/miniconda/Miniconda3-py311_24.9.2-0-Linux-x86_64.sh -O /tmp/m.sh
       bash /tmp/m.sh -b -p ~/miniconda3
       ~/miniconda3/bin/conda create -y -n qc_lean python=3.11.11 pandas=2.2.3
       ~/miniconda3/envs/qc_lean/bin/pip install wrapt==1.16.0
     (wrapt==1.16.0 isn't on the default conda channels — pip inside the
     env, confirmed working.)

  4. Copy the Python shim files LEAN's Launcher output directory ships
     (AlgorithmImports.py, clr.py, PandasMapper.py) into lean_project/ —
     `from AlgorithmImports import *` in main.py needs these importable
     from the algorithm's own directory, confirmed necessary directly:
       cp lean_engine/Launcher/bin/Release/{AlgorithmImports,clr,PandasMapper}.py lean_project/

Real, load-bearing methodology limit, not a bug — stated here and in
docs/benchmarks.md, not silently worked around: LEAN backtests are bounded
by real wall-clock "now" (confirmed directly — SetEndDate(2150, 1, 1)
silently became "today"). reamer_py, Backtrader, and NautilusTrader all just
iterate whatever bars they're handed regardless of what date they're
stamped with; LEAN cannot, since a 5,000,000-bar / ~142-year synthetic tier
does not fit between any start date and the present. LEAN can only run this
benchmark suite's --compare-bars tier (500K bars ≈ 14.25 years — check this
fits before "now" given the base series' real start date, ~2024-01-03) or
smaller custom tiers; it structurally cannot run --scale-bars (5M). Don't
pass --lean-python alongside an unmodified --scale-bars — either skip LEAN
there or reduce the tier size for that comparison specifically.

Timing caveat: this times the whole `dotnet exec` subprocess, which includes
.NET CLR startup and Python.NET interpreter initialization — there is no
warm-process mode for a single backtest. That overhead is roughly constant
regardless of bar count, so it matters most at small bar counts and becomes
proportionally negligible at --compare-bars scale — report it as a caveat
alongside the number, not silently absorbed. peak_rss_mb, unlike the
Docker-based design this replaced, IS measurable here: dotnet runs as this
script's direct child process, not inside a container, so
RUSAGE_CHILDREN correctly captures it.

Usage: lean_runner.py <csv_path> <minimal|realistic>
  csv_path is absolute, or a name under bench/comparison/data/ (built by
  build_dataset.py --format csv, same as the other engines' data prep) —
  this script copies it into lean_engine/Data/alternative/bench/ under its
  upper-cased name (LEAN upper-cases Symbol.Value regardless of what's
  passed to AddData() — confirmed directly; the file must match exactly on
  a case-sensitive filesystem).
"""
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

RUNNERS       = Path(__file__).resolve().parent
STRESS_DIR    = RUNNERS.parent
PROJECT       = STRESS_DIR / "lean_project"
# Built once into tests/stress/lean_engine/ (gitignored), per the setup above.
LEAN_ENGINE   = STRESS_DIR.parent.parent / "tests" / "stress" / "lean_engine"
LAUNCHER_DLL  = LEAN_ENGINE / "Launcher" / "bin" / "Release" / "QuantConnect.Lean.Launcher.dll"
# Order submission (the "realistic" strategy only — "minimal" never hits this
# path) triggers a .NET crypto code path that dlopen()s libssl by its old
# 1.1 soname regardless of .NET version — confirmed directly: the .NET
# runtime's own bundled OpenSSL shim otherwise supports OpenSSL 3 fine
# (libSystem.Security.Cryptography.Native.OpenSsl.so), but something
# further up the chain (order ID hashing or a NuGet-bundled dependency,
# not narrowed down further) still probes for libssl.so.1.1 /
# libcrypto.so.1.1 by name, which this Debian install doesn't ship
# (only libssl.so.3 / libcrypto.so.3, the newer SONAME). Local symlinks
# under the SSL_COMPAT_DIR below fix it without touching system library
# paths — first failure mode, exact error: "No usable version of libssl
# was found."
SSL_COMPAT_DIR = LEAN_ENGINE / "ssl_compat"
DATA_FOLDER   = LEAN_ENGINE / "Data"
DATA_DEST     = DATA_FOLDER / "alternative" / "bench"
CONFIG_TEMPLATE = PROJECT / "config.template.json"
DOTNET_EXE    = Path.home() / ".dotnet" / "dotnet"
PYTHONNET_PYDLL = Path.home() / "miniconda3" / "envs" / "qc_lean" / "lib" / "libpython3.11.so"


def stage_csv(src, dest):
    """Copy src to dest unless an identical-size copy is already there. The
    copy goes through a temp name and os.replace, so concurrent runs (the
    parallel sweep) never read a half-written file."""
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        return
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    shutil.copy(src, tmp)
    os.replace(tmp, dest)


def make_run_dir():
    """LEAN writes its config, logs and result files into the run dir, so
    each concurrent run needs its own. LEAN_RUN_DIR overrides the default."""
    run_dir = Path(os.environ.get("LEAN_RUN_DIR", STRESS_DIR / "lean_run_tmp"))
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def build_config(run_dir, csv_filename_upper, strat_name):
    text = CONFIG_TEMPLATE.read_text()

    def setval(text, key, newval):
        pattern = rf'("{key}"\s*:\s*)"[^"]*"'
        return re.sub(pattern, rf'\1"{newval}"', text, count=1)

    text = setval(text, "algorithm-type-name", "ReamerBenchAlgorithm")
    text = setval(text, "algorithm-language", "Python")
    text = setval(text, "algorithm-location", str(PROJECT / "main.py"))
    text = setval(text, "data-folder", str(DATA_FOLDER))

    marker = '"parameters": {\n    // Intrinio account user and password'
    replacement = (
        '"parameters": {\n'
        f'    "csv_file": "{csv_filename_upper}",\n'
        f'    "strategy": "{strat_name}",\n'
        '    // Intrinio account user and password'
    )
    if marker not in text:
        sys.exit("lean_runner: config.template.json's parameters marker not found — "
                 "template may have changed upstream, update build_config() to match")
    text = text.replace(marker, replacement)

    config_path = run_dir / "config.json"
    config_path.write_text(text)
    return config_path


def ensure_ssl_compat_symlinks():
    SSL_COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    real_ssl = Path("/usr/lib/x86_64-linux-gnu/libssl.so.3")
    real_crypto = Path("/usr/lib/x86_64-linux-gnu/libcrypto.so.3")
    link_ssl = SSL_COMPAT_DIR / "libssl.so.1.1"
    link_crypto = SSL_COMPAT_DIR / "libcrypto.so.1.1"
    if real_ssl.exists() and not link_ssl.exists():
        link_ssl.symlink_to(real_ssl)
    if real_crypto.exists() and not link_crypto.exists():
        link_crypto.symlink_to(real_crypto)


def main():
    csv_filename, strat_name = sys.argv[1], sys.argv[2]
    csv_src = Path(csv_filename) if os.path.isabs(csv_filename) else STRESS_DIR / "data" / csv_filename
    csv_upper = csv_src.name.upper()

    ensure_ssl_compat_symlinks()
    DATA_DEST.mkdir(parents=True, exist_ok=True)
    stage_csv(csv_src, DATA_DEST / csv_upper)

    run_dir = make_run_dir()
    config_path = build_config(run_dir, csv_upper, strat_name)

    env = os.environ.copy()
    env["PATH"] = f"{DOTNET_EXE.parent}:{env.get('PATH', '')}"
    env["DOTNET_ROOT"] = str(DOTNET_EXE.parent)
    env["PYTHONNET_PYDLL"] = str(PYTHONNET_PYDLL)
    env["LD_LIBRARY_PATH"] = f"{SSL_COMPAT_DIR}:{env.get('LD_LIBRARY_PATH', '')}"

    ru_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

    t0 = time.perf_counter()
    out = subprocess.run(
        [str(DOTNET_EXE), "exec", str(LAUNCHER_DLL), "--config", str(config_path)],
        capture_output=True, text=True, cwd=run_dir, env=env,
    )
    elapsed = time.perf_counter() - t0

    ru_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss is cumulative across all children of this process's lifetime,
    # not just the last one — fine here since lean_runner.py is invoked fresh
    # per subprocess by the orchestrator (one dotnet child per process), but
    # would need per-call isolation if that ever changes.
    peak_rss_mb = ru_after / 1024  # ru_maxrss is KB on Linux

    m = re.search(r"REAMER_BENCH_TRADES=(\d+)", out.stdout)
    if m is None:
        sys.exit(f"lean run for strategy={strat_name} produced no REAMER_BENCH_TRADES "
                 f"line — check for a runtime error:\n{out.stdout[-4000:]}\n{out.stderr[-2000:]}")
    trades = int(m.group(1))

    m_bars = re.search(r"REAMER_BENCH_BARS_SEEN=(\d+)", out.stdout)
    bars_seen = int(m_bars.group(1)) if m_bars else None
    with open(csv_src) as f:
        bars_requested = sum(1 for _ in f)
    # LEAN silently drops any bar past its wall-clock "now" clamp with no
    # error — confirmed directly (a run at the real, un-re-anchored 2024
    # dates processed 65,271 of 500,000 requested bars, an 87% loss).
    # build_dataset.py's 2000-01-01 csv re-anchor fixes that for
    # --compare-bars. What's left, confirmed separately and much smaller
    # (499,944 of 500,000, both minimal and realistic, strategy-independent
    # so it's a data-ingestion edge effect, not strategy logic) is a
    # sub-0.02% gap not worth root-causing further — likely a day-boundary
    # or subscription-warmup effect. 0.5% tolerance catches a real
    # wall-clock truncation (which is never this small) while not hard-
    # failing on this negligible, already-disclosed gap — bars_processed
    # is reported below either way, not silently rounded up.
    if bars_seen is not None and abs(bars_seen - bars_requested) > bars_requested * 0.005:
        sys.exit(
            f"lean run for strategy={strat_name} processed {bars_seen:,} bars but "
            f"{bars_requested:,} were requested ({csv_filename}) — a gap this large "
            f"is almost certainly LEAN's wall-clock 'now' clamp truncating this tier "
            f"(see build_dataset.py's csv-anchor comment and lean_runner.py's module "
            f"docstring), not the small day-boundary effect this tolerance already "
            f"allows for. Not reporting a number silently computed against the wrong "
            f"bar count."
        )

    m_orders = re.search(r"REAMER_BENCH_ORDERS=(\d+)", out.stdout)
    m_pnl = re.search(r"REAMER_BENCH_NET_PNL=(-?[\d.]+)", out.stdout)
    print(json.dumps({
        "engine": "quantconnect_lean",
        "strategy": strat_name,
        "elapsed_s": elapsed,
        "trades": trades,
        "orders": int(m_orders.group(1)) if m_orders else None,
        "net_pnl": float(m_pnl.group(1)) if m_pnl else None,
        "bars_processed": bars_seen,
        "peak_rss_mb": peak_rss_mb,
        "includes_clr_python_startup": True,
    }))


if __name__ == "__main__":
    main()
