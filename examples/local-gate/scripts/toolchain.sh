# Sourced by scripts/ci.sh and scripts/install.sh. sluicy's worker, its [deps] and init commands, and the
# gate run as launchd/systemd jobs with a bare PATH (`[gate] env` reaches only the gate), so find this
# machine's tools here. Directories are prepended in turn, so the last one that exists wins. Edit to taste.
_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for _d in /usr/local/bin /opt/homebrew/bin "$HOME/.local/share/pnpm" \
          "$HOME/.nvm/versions/node/v$(cat "$_root/.nvmrc" 2> /dev/null)/bin" \
          "$HOME/.local/share/fnm/aliases/default/bin"; do
  [ -d "$_d" ] && PATH="$_d:$PATH"
done
export PATH
unset _d _root
