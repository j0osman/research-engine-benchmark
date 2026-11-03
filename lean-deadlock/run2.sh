#!/bin/bash
RUN=~/rb/reamer-research/bench/comparison/bench_runners
for i in 1 2 3 4; do
  d=~/rb/diag/retry_$i; mkdir -p $d
  s=$(date +%s.%N)
  out=$(LEAN_RUN_DIR=$d PYTHONPATH=$RUN timeout -s KILL 900 ~/venv-bench/bin/python $RUN/lean_portfolio_runner.py ~/rb/data/pf10/csv 10 realistic </dev/null 2>$d/err.txt | tail -1)
  e=$(date +%s.%N)
  echo "try=$i wall=$(echo "$e - $s" | bc) $(echo "$out" | grep -o "\"run_s\": [0-9.]*\|\"net_pnl\": [-0-9.]*\|\"attempts\": [0-9]*\|\"hung_attempts_s\": \[[^]]*\]" | tr "\n" " ") $(grep -c hung $d/err.txt)"
done
