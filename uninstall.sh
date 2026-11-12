#!/bin/bash
# Undo install.sh: the commands, the timer, the gate slice and the Claude Code hooks. The queue's state
# (~/.local/state/sluicy) and the project checkouts (~/.local/share/sluicy) are kept; delete them by hand.
set -euo pipefail
root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
bin="$HOME/.local/bin"
for name in sluicy plan-review deck; do
    if [ -L "$bin/$name" ] && [[ "$(readlink "$bin/$name")" == "$root"/* ]]; then
        rm "$bin/$name"
        echo "removed $bin/$name"
    fi
done
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if command -v systemctl > /dev/null; then
    systemctl --user disable --now sluicy-conduct.timer 2> /dev/null || true
fi
rm -f "$units/sluicy-conduct.timer" "$units/sluicy-conduct.service" "$units/sluicy-gate.slice"
command -v systemctl > /dev/null && systemctl --user daemon-reload 2> /dev/null || true
python3 "$root/sluicy/claude_hooks.py" uninstall
echo "sluicy uninstalled; its state in ~/.local/state/sluicy and checkouts in ~/.local/share/sluicy are kept"
