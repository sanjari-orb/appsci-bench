#!/usr/bin/env bash
# Generic task-environment smoke check.
#
# Validates base-image tooling only. Runtime paths (/data, /fixture, /scripts)
# are created by TaskRunner bundle extraction — not pre-baked in the image.
set -euo pipefail

fail=0

if [[ ! -d /work ]]; then
  echo "MISSING dir: /work"
  fail=1
fi

if touch /work/.smoke_write_test 2>/dev/null; then
  rm -f /work/.smoke_write_test
else
  echo "NOT writable: /work"
  fail=1
fi

if ! python --version; then
  echo "python not available"
  fail=1
fi

if ! python -c "import loguru"; then
  echo "loguru not available (expected in appsci-base)"
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "env smoke: FAILED"
  exit 1
fi

echo "env smoke: OK"
