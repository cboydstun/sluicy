#!/bin/bash
# deck.sh must show its build: a note first, and cargo without --quiet (a silent rebuild looked frozen).
set -euo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deck.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/deck/target/release" "$tmp/bin"
cp "$src" "$tmp/deck/deck.sh"
touch "$tmp/deck/Cargo.toml"
printf '#!/bin/bash\necho "$*" > "%s/cargo-args"\n' "$tmp" > "$tmp/bin/cargo"
printf '#!/bin/bash\necho "deck ran: $*"\n' > "$tmp/deck/target/release/deck"
chmod +x "$tmp/bin/cargo" "$tmp/deck/target/release/deck"
fail() {
    echo "FAIL: $*" >&2
    exit 1
}
out="$(PATH="$tmp/bin:$PATH" bash "$tmp/deck/deck.sh" tui 2> "$tmp/err")"
grep -q '^deck: checking build' "$tmp/err" || fail "no build note on stderr: $(cat "$tmp/err")"
args="$(cat "$tmp/cargo-args")"
case " $args " in *" --quiet "*) fail "cargo ran with --quiet: $args" ;; esac
case " $args " in *"build --release "*) ;; *) fail "unexpected cargo args: $args" ;; esac
[ "$out" = "deck ran: tui" ] || fail "binary not exec'd with its args: $out"
# Without cargo it runs the binary it has, and says how to get one when there is none.
rm "$tmp/bin/cargo"
out="$(PATH="$tmp/bin:/usr/bin:/bin" bash "$tmp/deck/deck.sh" x 2> /dev/null)"
[ "$out" = "deck ran: x" ] || fail "no-cargo run did not exec the binary: $out"
rm "$tmp/deck/target/release/deck"
if PATH="$tmp/bin:/usr/bin:/bin" bash "$tmp/deck/deck.sh" x 2> "$tmp/err"; then fail "ran with no binary"; fi
grep -q "install.sh" "$tmp/err" || fail "no install hint: $(cat "$tmp/err")"
echo "deck.sh: 2 passed"
