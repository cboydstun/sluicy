#!/bin/bash
# Every sluicy test suite. Exits non-zero on the first red one.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
run() { echo "== $*"; "$@"; }
run python3 tests/test_config.py
run python3 tests/test_install.py
run python3 plan-review/test_plan_review.py
run python3 tests/test_sluicy.py
run bash deck/test_deck_sh.sh
if command -v cargo > /dev/null; then
    run cargo test --release --quiet --manifest-path deck/Cargo.toml
    run cargo build --release --quiet --manifest-path deck/Cargo.toml
    DECK_BIN=deck/target/release/deck run python3 deck/test_deck.py
else
    echo "== cargo not found: skipping the deck's tests"
fi
echo "all green"
