#!/usr/bin/env bash
# Local CI: what a GitHub Actions workflow would run, run here instead. Stages run only when their files
# changed against CI_BASE (default origin/main); --all (or CI_ALL=1) runs everything. sluicy reads the
# "✗ " lines as the failure summary (`[gate] red_line`), so keep that prefix.
#   SKIP_BUILD=1   skip the build stage (a sluicy waiver: `--waive SKIP_BUILD="reason"`)
set -uo pipefail
cd "$(dirname "$0")/.."
ERRORS=0
base="${CI_BASE:-origin/main}"
changed="$(git diff --name-only "$base" 2> /dev/null; git ls-files --others --exclude-standard)"
all="${CI_ALL:-}"
[ "${1:-}" = "--all" ] && all=1
touched() { [ -n "$all" ] || grep -qE "$1" <<< "$changed"; }

step() {
  local name="$1"
  shift
  echo "  \$ $*"
  local t0=$SECONDS
  if "$@"; then
    echo "✓ $name ($((SECONDS - t0))s)"
  else
    echo "✗ $name ($((SECONDS - t0))s)"
    ERRORS=$((ERRORS + 1))
  fi
}

# ---- edit below: one block per stage ----
if touched '^(src|app|lib|test|package\.json|package-lock\.json|tsconfig)'; then
  step "install" npm ci --no-audit --no-fund
  step "lint" npm run -s lint
  step "typecheck" npm run -s type-check
  step "test" npm test --silent
  [ -n "${SKIP_BUILD:-}" ] || step "build" npm run -s build
fi
# ---- edit above ----

if [ "$ERRORS" -gt 0 ]; then
  echo "✗ $ERRORS check(s) failed"
  exit 1
fi
echo "✓ All checks passed"
