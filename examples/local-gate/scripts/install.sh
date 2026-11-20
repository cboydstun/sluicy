#!/usr/bin/env bash
# Install dependencies with this machine's toolchain. sluicy runs it after a merge that changes the
# lockfile ([deps]) and at `sluicy init`, both with a bare PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/toolchain.sh
npm ci --no-audit --no-fund
