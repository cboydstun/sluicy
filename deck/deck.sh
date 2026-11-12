#!/bin/bash
# Run the Session Deck: the installed binary, or (in a source checkout with cargo) a fresh build.
#   deck tui     the terminal deck, no browser
#   deck [...]   the web page on 127.0.0.1
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
bin="$here/target/release/deck"
if command -v cargo > /dev/null && [ -f "$here/Cargo.toml" ]; then
    # Incremental: a no-op when nothing changed. index.html is compiled in, so a page edit rebuilds.
    # Never --quiet: a silent rebuild (or a wait on another cargo's build lock) looks like a frozen deck.
    echo "deck: checking build (a rebuild after a deck change takes a moment)…" >&2
    nice -n 10 cargo build --release --manifest-path "$here/Cargo.toml"
elif [ ! -x "$bin" ]; then
    echo "deck: no deck binary at $bin; run install.sh (it downloads one) or install Rust: https://rustup.rs" >&2
    exit 1
fi
exec "$bin" "$@"
