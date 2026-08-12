#!/usr/bin/env bash
set -euo pipefail

if [ -f .fishagent.pid ]; then
  kill "$(cat .fishagent.pid)" 2>/dev/null || true
  rm -f .fishagent.pid
fi
