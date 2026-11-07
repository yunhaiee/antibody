#!/bin/zsh
# Full reproducible run: training + learning, held-out experiment, ablations, report.
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
F='warn|httpx'
$PY -u learn.py                                  2>&1 | grep -v -i -E $F > runs/learn.log
$PY -c "import gate,json; print(json.dumps({w: gate.current_set(w)[0] for w in 'ABC'}))" 2>/dev/null > runs/pre_experiment_versions.json
$PY -u run_compare.py compare                    2>&1 | grep -v -i -E $F > runs/compare.log
$PY -u run_compare.py compare --antibody               2>&1 | grep -v -i -E $F >> runs/compare.log
$PY -u run_compare.py compare --antibody --no-enforce  2>&1 | grep -v -i -E $F >> runs/compare.log
$PY -u run_compare.py compare --versions               2>&1 | grep -v -i -E $F >> runs/compare.log
$PY -u reuse.py compare                          2>&1 | grep -v -i -E $F > runs/reuse.log
$PY -u ablation.py                               2>&1 | grep -v -i -E $F > runs/ablation.log
$PY report.py --readme
echo PIPELINE_DONE
