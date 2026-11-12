# sluicy

**One gate at a time.** A local release queue for running many AI coding sessions (or people) against
the same repositories on one machine.

Each session works on its own branch. When the work is done, the session submits it. sluicy then
merges it onto the latest main, runs your existing pre-push gate, pushes, and (if you ask) deploys
**exactly that commit**. It runs one gate at a time across every repository on the machine, so two
test suites never fight over memory and freeze the laptop.

It comes with:

- **the queue** (`sluicy`): tickets, merge `--no-ff`, gate, push, deploy, crash recovery that never
  redeploys on its own, holds after a rollback, waivers on the record, a preflight that runs your quick
  checks minutes after submit, and a conductor that notes what went wrong on red tickets and cleans
  up finished worktrees.
- **memory brakes**: a gate waits until there is room before it starts. While it runs it lives in a
  capped, swapless systemd scope, and if the machine starts to stall the scope is stopped and the
  ticket requeued with fewer workers.
- **Claude Code hooks**:
  - a *guard* that refuses a session's direct `git push origin main` or deploy;
  - *auto-ship*, which submits a session's branch when its last commit says `Ship: yes`;
  - *plan review*, which asks for two adversarial reviewer subagents before a plan leaves plan mode.
- **Session Deck** (`deck`): a terminal (or local web) dashboard of every Claude session, what needs
  you, the queue, memory and worktrees.

sluicy is standard-library Python (3.11+) plus one Rust binary for the deck. It has no daemon and no
server: the worker starts when there is work and exits when the queue is empty.

## Requirements

- Linux, or Windows with **WSL2** and systemd enabled (`[boot] systemd=true` in `/etc/wsl.conf`).
  macOS is not supported yet: the brakes and scopes need systemd, cgroups and `/proc`.
- Python 3.11+, git.
- [Claude Code](https://claude.com/claude-code) for the hooks and the deck. The queue itself works by
  hand with any tool, or none.

## Install

```bash
git clone https://github.com/cboydstun/sluicy ~/.local/share/sluicy/src
~/.local/share/sluicy/src/install.sh
```

`install.sh` does four things:

- puts `sluicy`, `plan-review` and `deck` in `~/.local/bin`;
- installs the conductor's timer (every 5 minutes) and the gate's systemd slice;
- adds three hooks to `~/.claude/settings.json`, keeping everything already there and saving a backup;
- builds the deck with cargo, or downloads it.

The hooks do nothing in a repository without a `.sluicy.toml`, so your other projects are unaffected.
`--no-hooks` leaves `~/.claude/settings.json` alone, and `--no-deck` skips the deck. On x86_64 without
cargo it downloads the deck from the latest release; elsewhere install Rust and it builds the deck instead.
`./uninstall.sh` reverses all of it.

## Use it in a repository

1. Add `.sluicy.toml` at the repository root. Start from [`examples/vercel.sluicy.toml`](examples/vercel.sluicy.toml)
   if your host deploys every push to main, or from [`examples/vps.sluicy.toml`](examples/vps.sluicy.toml)
   if you deploy with your own script.
2. `sluicy config check` shows what sluicy understood.
3. `sluicy init` registers the repository and clones its permanent checkout (under
   `~/.local/share/sluicy/checkouts/`), where the worker merges and gates.
4. Give the repository a gate: a `.githooks/pre-push` (or any `pre-push` hook) that runs your checks
   and fails the push when they fail. sluicy pushes through it; it does not replace it.

Then:

```bash
sluicy submit --branch fix/login --ride            # merge and push; the push is the deploy
sluicy submit --branch fix/login --deploy api,web  # merge, push, then deploy those components
sluicy wait 12                                     # 0 live/merged, 1 failed, 2 needs a person, 4 superseded, 75 still running
sluicy status                                      # tickets across every repository, and holds
```

### Auto-ship

When a Claude Code session's last commit carries a `Ship: yes` line, the Stop hook submits its branch
for it. It asks to deploy whichever components the branch touched, or to ride along if it touched
none. It then tells the session to `sluicy wait` and report back. Commits without the line are never
shipped automatically.

Auto-ship only ever submits the session's own worktree. It never submits:

- the main checkout or another session's worktree;
- while a ticket for that branch is running or waiting on a person;
- after a hand-made ticket failed, or after two auto tickets in a row failed;
- over a held component.

Switch it off for one worktree with `touch "$(git rev-parse --git-dir)/sluicy-hold"`, or everywhere with
`touch ~/.local/state/sluicy/auto-ship-off`.

### Guards

Guards are off until you `touch ~/.local/state/sluicy/guards-on`. With them on, the session hook refuses
`git push … main`, `git push --no-verify` and any command matching your `[guard] deploy_patterns`.
Commands that only read are allowed, and so is anything quoted: `grep "a|b" deploy.sh` is fine.

Your own pre-push hook and deploy script can ask too:

- `sluicy guard push <sha>` succeeds only for the queue's own push;
- `sluicy guard deploy` succeeds only for the queue's own deploy;
- `SLUICY_BYPASS="<reason>"` overrides either one, and is logged.

## Configuration reference

Everything except `project` is optional, and an unknown key is an error.
[`examples/vps.sluicy.toml`](examples/vps.sluicy.toml) shows every key with a comment.

| Table          | What it says                                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------------- |
| `project`      | A short name, unique on the machine.                                                                           |
| `[repo]`       | `remote`, `branch` (default `origin`/`main`), `ssh_keepalive`.                                                 |
| `[components]` | `[components.<name>] path = "dir"`, `deploy_args = [...]`. A branch touching `dir/` deploys `<name>`.          |
| `[deploy]`     | `kind = "none"` (the push is the deploy) or `"command"`; `command` (`{args}`, `{sha}`), `recreate_arg`, `smoke_command`, `exit_codes`. |
| `[stamps]`     | `command`, given component names, prints `name=<sha>`: what production runs now.                               |
| `[gate]`       | `waivers`, `never_waive`, `strip_env`, `env`, `mem_reserve_gb`, failure patterns, `quick_stop`, `quick_note`.  |
| `[preflight]`  | `command` + `env` for the quick checks; `link_dirs` shared with the checkout; `exclude`.                         |
| `[deps]`       | `"package-lock.json" = ["npm", "ci"]`: run after a merge changes a matching file.                              |
| `[init]`       | `copy_files` (e.g. `.env`) and `install` commands for `sluicy init`.                                           |
| `[guard]`      | `deploy_patterns`, `allow_args` (regexes).                                                                     |
| `[hooks]`      | `on_live`: a command run after a deploy goes live.                                                             |
| `[plan_review]`| `enabled`, `focus` (extra things for the second reviewer to check).                                            |
| `[deck]`       | `lease_command`: what the deck's Production panel shows as the deploy lease.                                    |

### The deploy contract

With `kind = "command"`, sluicy runs your deploy command from the permanent checkout, at main, with
the merged commit in `{sha}`. Exit codes (each can be renamed in `[deploy.exit_codes]`):

| Code  | Meaning                                                       | sluicy                                                        |
| ----- | ------------------------------------------------------------- | ------------------------------------------------------------- |
| 0     | live                                                          | ticket `live`, holds on its components cleared                |
| 10    | another deploy holds the lease                                | retries every minute, 30 times                                |
| 11    | the deploy's own preflight refused; nothing changed           | ticket `failed`                                               |
| 12    | the build failed; the old build still serves                  | ticket `failed`                                               |
| 13    | it went live, failed its checks, was rolled back              | ticket `rolled_back`; components **held** until a new commit or `sluicy allow hold` |
| other | anything else                                                 | ticket `unknown`; components held; look at production         |

A deploy only goes ahead when every commit between what production runs (the stamps) and the merge
belongs to a ticket that asked to deploy that component, or is allowed with `sluicy allow`. This
stops one ticket shipping another session's half-finished work. Without a stamps command, sluicy
compares against the last commit it deployed itself.

### What sluicy gives your scripts

- **The gate** (`git push` through your pre-push hook) gets:
  - `SLUICY_TICKET`, `SLUICY_PROJECT` and `SLUICY_BRAKED` (how many times this ticket was braked);
  - `SLUICY_GATE_SCOPED=1`, because it already runs in a capped scope. Skip your own cap if you have one;
  - `GATE_LOGS`, a directory for per-stage logs, which are copied into the ticket when it fails;
  - your `[gate] env`, plus `1` for each waiver the ticket carries.
- **The deploy** gets `SLUICY_TICKET` and `SLUICY_PROJECT`.
- **`on_live`** gets `SLUICY_TICKET`, `SLUICY_PROJECT`, `SLUICY_MERGED_SHA`, `SLUICY_COMPONENTS` and
  `SLUICY_OLD_STAMP_<COMPONENT>`.
- **`sluicy scope -- <cmd>`** runs a hand-started gate in the same capped scope.

### Memory

Before a gate starts, the worker waits until at least `SLUICY_MIN_AVAIL_GB` (14) is free and memory
pressure is at most `SLUICY_MAX_PSI_SOME` (10%), held for 30 s. While the gate runs it polls every 2 s.
After three bad readings (full pressure > 5%, some > 30%, or < 3 GB free) it stops the gate's scope and
requeues the ticket. The third brake fails it. The gate's `MemoryMax` is min(75% of RAM,
RAM − `mem_reserve_gb`), with no swap. Every threshold is an environment variable; see `sluicy/sluicy.py`.

## Session Deck

Run `deck tui` (terminal) or `deck` (local web page on 127.0.0.1, with a fresh token each start) inside a
repository. It shows:

- every Claude Code session, with a **Needs you** list for sessions waiting on a permission prompt,
  a question or a plan;
- the queue for every project, and that repository's production stamps;
- memory, swap and the biggest processes;
- worktrees, and a clean-up list from the conductor.

**New session** starts `claude --bg` in plan mode in its own worktree. **Ask questions first** adds a
short instruction to research and ask before planning. Replace that text with your own in
`~/.config/sluicy/preamble.txt`.

Optional environment:

- `DECK_LOCKS="gate=/tmp/my-gate.lock;e2e=/tmp/e2e.lock"` names flock files to show;
- `DECK_SLUICY` points at a sluicy other than the one on PATH.

## Plan review

With `[plan_review] enabled = true`, a session cannot leave plan mode until two reviewer subagents have
read the plan: one tries to disprove its claims about the code, the other attacks the plan itself. The
session revises the plan and records the review with `plan-review record`. The review is tied to the
plan's exact text. Switch it off with `PLAN_REVIEW=off` or `touch ~/.local/state/plan-review/off`.

## Files

| Path                               | What                                                                 |
| ---------------------------------- | -------------------------------------------------------------------- |
| `~/.local/state/sluicy/`           | `sluicy.db`, per-ticket logs, `alerts.log`, `cleanup.json`, switches |
| `~/.local/share/sluicy/checkouts/` | one permanent checkout per project, plus `<name>-preflight`          |
| `~/.config/systemd/user/`          | `sluicy-conduct.{service,timer}`, `sluicy-gate.slice`                |

## Tests

```bash
tests/run.sh   # queue, config, install, plan review, deck (cargo test + black-box), deck.sh
```

All tests are sandboxed: the gate, deploy, ssh, systemctl and claude are fakes in a temp directory.

## License

MIT. See [LICENSE](LICENSE).
