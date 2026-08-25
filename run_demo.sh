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
echo "  -> ledgerguard/evaluation/outputs/benchmark.md"

echo
echo "Starting the dashboard..."
$PY -m uvicorn ledgerguard.backend.app:app --port 8137 --log-level warning &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT

# The server runs the controller once at startup. With a live investigator that
# is ~15 sequential model calls, so warm it before announcing readiness rather
# than letting the first page load block for minutes mid-demo.
echo "Warming up (this runs the controller once; with a live model it takes a few minutes)..."
until curl -sf http://127.0.0.1:8137/api/health >/dev/null 2>&1; do
  kill -0 $SERVER_PID 2>/dev/null || { echo "server exited during warmup"; exit 1; }
  sleep 2
done

echo
echo "Verifying every demo beat..."
$PY -m ledgerguard.tests.rehearsal_check | tail -5

echo
echo "LedgerGuard dashboard: http://127.0.0.1:8137"
echo "Ctrl-C to stop."
wait $SERVER_PID
