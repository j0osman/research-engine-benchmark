#!/bin/bash
RUN=~/rb/reamer-research/bench/comparison/bench_runners
for w in "" 8 "" 8 "" 8; do
  d=~/rb/diag/run_w${w:-def}_$RANDOM; mkdir -p $d
  s=$(date +%s.%N)
  out=$(LEAN_RUN_DIR=$d LEAN_WORKERS=$w PYTHONPATH=$RUN timeout -s KILL 120 ~/venv-bench/bin/python ~/rb/diag/lean_diag.py ~/rb/data/pf10/csv 10 realistic </dev/null 2>/dev/null | tail -1 | cut -c1-200)
  rc=$?; e=$(date +%s.%N)
  echo "workers=${w:-128default} rc=$rc wall=$(echo "$e - $s" | bc) $out | $(grep -c "data-feed-workers-count" $d/config.json) $(grep -o "will use [0-9]* workers" $d/log.txt | head -1)"
done
