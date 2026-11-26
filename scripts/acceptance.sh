#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p artifacts
python3 scripts/run_acceptance.py 2>&1 | tee artifacts/acceptance.log
