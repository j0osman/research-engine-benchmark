#!/usr/bin/env python3
"""Runs an equivalent QuantConnect LEAN multi-asset portfolio backtest (N
tickers on one shared timeline, one OnData(slice) call per aligned step —
see lean_project/main_portfolio.py's PortfolioBenchAlgorithm) and prints a
single JSON
result line to stdout. Counterpart to reamer_portfolio_runner.py /
backtrader_portfolio_runner.py / nautilus_portfolio_runner.py.

Same one-time engine build/setup as lean_runner.py (.NET 10 SDK, LEAN built
from source, the qc_lean conda env, the Python shim files, the OpenSSL 1.1
compat libraries) — see that script's docstring in full; not repeated here.

Usage: lean_portfolio_runner.py <csv_dir> <n_tickers> <minimal|realistic>
  csv_dir must contain T000.csv .. T0{n_tickers-1}.csv (built by
  build_portfolio_dataset.py --format csv) — copied into
  lean_engine/Data/alternative/bench/ under upper-cased names, same
  case-sensitivity reason as lean_runner.py.
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

from lean_runner import make_run_dir, stage_csv

RUNNERS       = Path(__file__).resolve().parent
STRESS_DIR    = RUNNERS.parent
PROJECT       = STRESS_DIR / "lean_project"
LEAN_ENGINE   = STRESS_DIR.parent.parent / "tests" / "stress" / "lean_engine"
LAUNCHER_DLL  = LEAN_ENGINE / "Launcher" / "bin" / "Release" / "QuantConnect.Lean.Launcher.dll"
SSL_COMPAT_DIR = LEAN_ENGINE / "ssl_compat"  # see lean_runner.py for why this exists
DATA_FOLDER   = LEAN_ENGINE / "Data"
DATA_DEST     = DATA_FOLDER / "alternative" / "bench"
CONFIG_TEMPLATE = PROJECT / "config.template.json"
DOTNET_EXE    = Path.home() / ".dotnet" / "dotnet"
PYTHONNET_PYDLL = Path.home() / "miniconda3" / "envs" / "qc_lean" / "lib" / "libpython3.11.so"


def build_config(run_dir, n_tickers, strat_name):
    text = CONFIG_TEMPLATE.read_text()

    def setval(text, key, newval):
        pattern = rf'("{key}"\s*:\s*)"[^"]*"'
        return re.sub(pattern, rf'\1"{newval}"', text, count=1)

    text = setval(text, "algorithm-type-name", "PortfolioBenchAlgorithm")
    text = setval(text, "algorithm-language", "Python")
    # main_portfolio.py, not main.py — LEAN's AlgorithmPythonWrapper does not
    # actually select the class by algorithm-type-name; it picks whichever
    # QCAlgorithm subclass sorts last in dir(module), so a module with both
    # ReamerBenchAlgorithm and PortfolioBenchAlgorithm silently always ran
    # the former (confirmed directly — see main_portfolio.py's docstring).
    text = setval(text, "algorithm-location", str(PROJECT / "main_portfolio.py"))
    text = setval(text, "data-folder", str(DATA_FOLDER))

    marker = '"parameters": {\n    // Intrinio account user and password'
    replacement = (
        '"parameters": {\n'
        f'    "n_tickers": "{n_tickers}",\n'
        f'    "strategy": "{strat_name}",\n'
        '    // Intrinio account user and password'
    )
    if marker not in text:
        sys.exit("lean_portfolio_runner: config.template.json's parameters marker not found")
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
    csv_dir, n_tickers, strat_name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    csv_dir = Path(csv_dir)

    ensure_ssl_compat_symlinks()
    DATA_DEST.mkdir(parents=True, exist_ok=True)
    bars_per_ticker = None
    for i in range(n_tickers):
        ticker = f"T{i:03d}"
        src = csv_dir / f"{ticker}.csv"
        stage_csv(src, DATA_DEST / f"{ticker}.CSV")
        if bars_per_ticker is None:
            with open(src) as f:
                bars_per_ticker = sum(1 for _ in f)

    run_dir = make_run_dir()
    config_path = build_config(run_dir, n_tickers, strat_name)

    env = os.environ.copy()
    env["PATH"] = f"{DOTNET_EXE.parent}:{env.get('PATH', '')}"
    env["DOTNET_ROOT"] = str(DOTNET_EXE.parent)
    env["PYTHONNET_PYDLL"] = str(PYTHONNET_PYDLL)
    env["LD_LIBRARY_PATH"] = f"{SSL_COMPAT_DIR}:{env.get('LD_LIBRARY_PATH', '')}"

    # On a 128-thread EPYC (Ubuntu 26.04, .NET 10.0.10, Python 3.11 via
    # Python.NET) LEAN sometimes deadlocks on the first time step of a
    # multi-feed Python custom-data run: a data-feed worker waits for the GIL
    # inside a Python import while the main thread waits in the CLR, 0 bars
    # are processed, LEAN's isolator stops the algorithm after 20 minutes and
    # dotnet then never exits. A healthy run never spends more than a second
    # on one time step, so an attempt whose current step has run for
    # HANG_STEP_S is killed (its own PID only) and retried, up to
    # MAX_ATTEMPTS. Every hung attempt is reported in the result line; run_s
    # is the successful attempt alone.
    HANG_STEP_S, MAX_ATTEMPTS = 45, 10
    log_path = run_dir / "log.txt"
    hung = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log_from = log_path.stat().st_size if log_path.exists() else 0
        so_path, se_path = run_dir / "runner_stdout.txt", run_dir / "runner_stderr.txt"
        with open(so_path, "w") as so, open(se_path, "w") as se:
            t0 = time.perf_counter()
            proc = subprocess.Popen(
                [str(DOTNET_EXE), "exec", str(LAUNCHER_DLL), "--config", str(config_path)],
                stdout=so, stderr=se, stdin=subprocess.DEVNULL, cwd=run_dir, env=env,
            )
            stuck = False
            while proc.poll() is None:
                time.sleep(1)
                if log_path.exists():
                    with open(log_path, errors="replace") as f:
                        f.seek(log_from)
                        for mm, ss in re.findall(r"CurrentTimeStepElapsed: (\d+):(\d+)", f.read()):
                            if int(mm) * 60 + int(ss) >= HANG_STEP_S:
                                stuck = True
                if stuck:
                    proc.kill()
                    proc.wait()
                    break
            elapsed = time.perf_counter() - t0
        if not stuck:
            break
        hung.append(round(elapsed, 1))
        print(f"lean_portfolio_runner: attempt {attempt} hung on one time step for "
              f">= {HANG_STEP_S}s after {elapsed:.1f}s; killed, retrying", file=sys.stderr)
    else:
        sys.exit(f"lean portfolio run for strategy={strat_name} hung on all {MAX_ATTEMPTS} attempts")
    out = subprocess.CompletedProcess(proc.args, proc.returncode,
                                      so_path.read_text(errors="replace"),
                                      se_path.read_text(errors="replace"))

    peak_rss_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024

    m = re.search(r"REAMER_BENCH_TRADES=(\d+)", out.stdout)
    if m is None:
        sys.exit(f"lean portfolio run for strategy={strat_name} produced no REAMER_BENCH_TRADES "
                 f"line — check for a runtime error:\n{out.stdout[-4000:]}\n{out.stderr[-2000:]}")
    trades = int(m.group(1))
    m_orders = re.search(r"REAMER_BENCH_ORDERS=(\d+)", out.stdout)
    m_pnl = re.search(r"REAMER_BENCH_NET_PNL=(-?[\d.]+)", out.stdout)

    m_steps = re.search(r"REAMER_BENCH_STEPS_SEEN=(\d+)", out.stdout)
    steps_seen = int(m_steps.group(1)) if m_steps else None
    # Same guard as lean_runner.py's bars_requested check, at the step level
    # (steps ≈ bars_per_ticker, since one OnData call = one aligned step
    # across all N tickers) — a real wall-clock truncation would show up
    # here as a large gap; 0.5% tolerance for the same disclosed
    # day-boundary-style edge effect already seen in the single-ticker case.
    if steps_seen is not None and bars_per_ticker is not None \
            and abs(steps_seen - bars_per_ticker) > bars_per_ticker * 0.005:
        sys.exit(
            f"lean portfolio run for strategy={strat_name} processed {steps_seen:,} steps "
            f"but ~{bars_per_ticker:,} were expected (bars_per_ticker) — a gap this large "
            f"is almost certainly LEAN's wall-clock 'now' clamp truncating this run. Not "
            f"reporting a number silently computed against the wrong step count."
        )

    print(json.dumps({
        "engine": "quantconnect_lean",
        "strategy": strat_name,
        "n_tickers": n_tickers,
        "bars": bars_per_ticker * n_tickers,
        "run_s": round(elapsed, 3),
        "total_s": round(elapsed, 3),
        "orders": int(m_orders.group(1)) if m_orders else None,
        "round_trips": trades,
        "net_pnl": round(float(m_pnl.group(1)), 6) if m_pnl else None,
        "steps_processed": steps_seen,
        "peak_rss_mb": round(peak_rss_mb, 1),
        "includes_clr_python_startup": True,
        "attempts": len(hung) + 1,
        "hung_attempts_s": hung,
    }))


if __name__ == "__main__":
    main()
