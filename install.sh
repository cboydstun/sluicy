#!/bin/bash
# Install sluicy for this user: commands on PATH, the conductor timer, the gate's systemd slice, the
# Claude Code hooks (user-level, inert in any repository without .sluicy.toml) and the Session Deck.
#
#   ./install.sh               install or update (safe to run again)
#   ./install.sh --no-hooks    leave ~/.claude/settings.json alone
#   ./install.sh --no-deck     skip the Session Deck binary
#
# Then, in each repository: add a .sluicy.toml (see examples/) and run `sluicy init`.
set -euo pipefail
root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
hooks=1
deck=1
for arg in "$@"; do
    case "$arg" in
        --no-hooks) hooks=0 ;;
        --no-deck) deck=0 ;;
        -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done
say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "sluicy runs on Linux and WSL2 for now (it needs systemd and /proc)"
command -v git > /dev/null || die "git is required"
command -v python3 > /dev/null || die "python3 (3.11 or newer) is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 is $(python3 -V 2>&1 | cut -d' ' -f2); sluicy needs 3.11 or newer"
if ! systemctl --user show-environment > /dev/null 2>&1; then
    if grep -qi microsoft /proc/version 2> /dev/null; then
        say "warning: no systemd user session. On WSL2, put this in /etc/wsl.conf and run 'wsl --shutdown':"
        say "    [boot]"
        say "    systemd=true"
    else
        say "warning: no systemd user session; workers run as plain background processes and gates are not memory-capped"
    fi
fi

bin="$HOME/.local/bin"
mkdir -p "$bin"
chmod +x "$root/sluicy/sluicy.py" "$root/plan-review/plan_review.py" "$root/deck/deck.sh"
ln -sfn "$root/sluicy/sluicy.py" "$bin/sluicy"
ln -sfn "$root/plan-review/plan_review.py" "$bin/plan-review"
ln -sfn "$root/deck/deck.sh" "$bin/deck"
say "commands: $bin/sluicy, $bin/plan-review, $bin/deck"
case ":$PATH:" in *":$bin:"*) ;; *) say "warning: $bin is not on your PATH; add it in your shell profile" ;; esac

if command -v systemctl > /dev/null && systemctl --user show-environment > /dev/null 2>&1; then
    units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$units"
    cp "$root/share/sluicy-gate.slice" "$units/sluicy-gate.slice"
    systemctl --user daemon-reload
    say "gate slice: $units/sluicy-gate.slice"
    python3 "$root/sluicy/sluicy.py" timer
fi

if [ "$hooks" = 1 ]; then
    python3 "$root/sluicy/claude_hooks.py" install --root "$root"
fi

if [ "$deck" = 1 ]; then
    target="$root/deck/target/release/deck"
    if command -v cargo > /dev/null; then
        say "deck: building with cargo…"
        nice -n 10 cargo build --release --quiet --manifest-path "$root/deck/Cargo.toml"
    elif [ ! -x "$target" ] && ! command -v curl > /dev/null; then
        say "warning: neither cargo nor curl found; no deck. Install Rust (https://rustup.rs) and run this again"
    elif [ ! -x "$target" ]; then
        url="${SLUICY_RELEASES:-https://github.com/cboydstun/sluicy/releases/latest/download}/deck-$(uname -m)-linux"
        say "deck: downloading $url"
        mkdir -p "$(dirname "$target")"
        if curl -fsSL "$url" -o "$target.tmp"; then
            chmod +x "$target.tmp" && mv "$target.tmp" "$target"
        else
            rm -f "$target.tmp"
            say "warning: no deck binary for $(uname -m); install Rust (https://rustup.rs) and run this again"
        fi
    fi
fi
say "done. Next, in a repository: add .sluicy.toml (see $root/examples) and run 'sluicy init'."
