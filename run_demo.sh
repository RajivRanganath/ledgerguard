#!/usr/bin/env bash
# Reliable local demo. One command, no arguments.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Running the P0 test suite..."
$PY -m pytest ledgerguard/tests -q

echo
echo "Regenerating the benchmark from the frozen holdout..."
$PY -m ledgerguard.evaluation.benchmark >/dev/null

echo
echo "LedgerGuard dashboard: http://127.0.0.1:8137"
echo "Ctrl-C to stop."
exec $PY -m uvicorn ledgerguard.backend.app:app --port 8137 --log-level warning
