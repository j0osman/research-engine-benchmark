# Diagnostic only: lean_portfolio_runner with data-feed-workers-count injected from LEAN_WORKERS.
import os, sys
import lean_portfolio_runner as r
orig = r.build_config
def build_config(run_dir, n, s):
    p = orig(run_dir, n, s)
    w = os.environ.get("LEAN_WORKERS")
    if w:
        t = p.read_text()
        t = t.replace("\"environment\": \"backtesting\",", "\"environment\": \"backtesting\",\n  \"data-feed-workers-count\": %s," % w, 1)
        p.write_text(t)
    return p
r.build_config = build_config
r.main()
