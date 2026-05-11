#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

INSTALL_CMD=(pip install -e ".[dev]")
VERIFY_CMD=(python -m pytest tests/ -x -q)
START_CMD=(python -m markbot)

echo "==> Working directory: $PWD"
echo "==> Syncing dependencies"
"${INSTALL_CMD[@]}"

echo "==> Running baseline verification"
if "${VERIFY_CMD[@]}"; then
    echo "==> Baseline verification PASSED"
else
    echo "==> Baseline verification FAILED — fix before adding new work"
    exit 1
fi

echo "==> Startup command"
printf '    %q' "${START_CMD[@]}"
printf '\n'

if [ "${RUN_START_COMMAND:-0}" = "1" ]; then
    echo "==> Starting the app"
    exec "${START_CMD[@]}"
fi

echo "Set RUN_START_COMMAND=1 if you want init.sh to launch the app directly."
