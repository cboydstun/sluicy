#!/bin/bash
# Put "Session Deck" in the GNOME app grid: a launcher that opens the terminal deck in its own
# GNOME Terminal window, run in the repository you start it from:
#   bash deck/install-desktop.sh [repo]       install or update (repo: default the current directory)
#   bash deck/install-desktop.sh --remove     take it out again
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
file="$dir/sluicy-session-deck.desktop"
if [ "${1:-}" = --remove ]; then
    rm -f "$file"
    echo "removed $file"
    exit 0
fi
command -v gnome-terminal > /dev/null || { echo "gnome-terminal not found" >&2; exit 1; }
repo="$(cd "${1:-.}" && pwd)"
mkdir -p "$dir"
cat > "$file" << EOF
[Desktop Entry]
Type=Application
Name=Session Deck
Comment=Claude sessions, the sluicy queue and the laptop, in a terminal
Path=$repo
Exec=gnome-terminal --title="Session Deck" --geometry=200x50 --working-directory="$repo" -- bash "$here/deck.sh" tui
Icon=utilities-system-monitor
Terminal=false
Categories=Development;Utility;
EOF
command -v update-desktop-database > /dev/null && update-desktop-database "$dir" 2> /dev/null || true
echo "installed $file (runs $here/deck.sh tui in $repo)"
