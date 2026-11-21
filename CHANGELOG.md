# Changelog

## Unreleased

- macOS support (`platform_macos`). Gates run uncapped, and the memory governor brakes them from the
  kernel's memorystatus figures. Workers run as detached processes, the conductor timer is a launchd
  agent, and `ps`/`lsof` stand in for `/proc`. `install.sh` and `uninstall.sh` accept Darwin.
- `[gate.env_linux]` and `[gate.env_darwin]`: merged over `[gate] env` on that OS. Older sluicy rejects
  these keys, so update every machine before a project adds them.
- `SLUICY_PLATFORM` overrides the detected OS.
- Tests pass on macOS: `run.sh` resolves TMPDIR (`/var` vs `/private/var`), and the install test uses
  the running python.
- macOS fixes from review:
  - A failing or slow `sysctl` no longer kills the worker in the middle of a gate. It reads as "memory
    unknown", as on Linux.
  - A worker started by the conductor no longer inherits the launch agent's nice 19 and Background
    policy. The agent runs at standard priority, and the gate itself runs at nice 10, as Linux's scope does.
  - A brake or an orphaned push also stops processes that left the gate's process group but still work
    in the ticket's checkout (dev servers, test databases). It runs after the group has had its 30 s to
    exit, and never touches a process on a terminal (your shell), one that started before the push (an
    editor's language server, a background Claude session), an app bundle or Claude session started
    during it, or anything those started. It decides from one `ps` read plus `lsof`, re-checks each process's start time just before signalling it (so a
    reused pid is left alone), and logs it when `ps` or `lsof` can't be read instead of silently stopping
    nothing.
  - The launch agent uses Homebrew's unversioned `python3` link when it is the same interpreter, so a
    Python upgrade doesn't strand it. `bootstrap` retries once while the old job unloads.
  - A memory figure the governor can't read alerts once, and if it happens during a gate it says the brake is off.
  - Deck: processes, memory, CPU, load and session liveness come from libproc, sysctl and Mach on
    macOS. Before this the header read "0% memory available" and every session looked dead.
- macOS fixes from the Linux-side review:
  - A failing `sysctl` no longer fails the push through the gate's (unenforced) memory cap.
  - The launch agent carries the installing shell's PATH, so the conductor and the workers it starts
    find Homebrew's git, node and npm.
  - Every registered project's checkout, `-preflight` and `-map` tree counts as sluicy's own, not just
    the default checkouts root, so a gate in a custom checkout is never flagged as a hog.
  - A brake also SIGKILLs what is left of the gate's process group after 30 s (no scope does it).
  - An orphaned-push sweep runs only when there was an orphan.
  - `lsof` reads this user's processes only, and keeps its output when it exits 1; paths are compared
    resolved (`/private/tmp`, symlinked checkouts).
  - The sweep never sends SIGKILL to a pid that already exited.
  - `install.sh`/`uninstall.sh` no longer need `readlink -f` (BSD has it only from macOS 12.3).
- A `[gate.env*]` PATH applies to the push only; preflight, the map and the worker keep their own PATH
  instead of losing it.

## 0.2.1 - 2026-11-13

- Code map: a ticket that landed before the map reached it (the usual case on a busy queue, since the map
  waits for gates) is mapped at its own merge, `base_sha..merged_sha`, instead of showing 0 files. It no
  longer needs the session's worktree. A ticket whose commit reached main through another ticket is marked
  skipped once instead of being retried every conductor pass. Merged graphs are cached as the next ticket's
  base.

## 0.2.0 - 2026-11-12

- The code map, with [graphify](https://github.com/Graphify-Labs/graphify), off unless `[graph] enabled = true`:
  - `sluicy map` (started after preflight and by the conductor) stores each ticket's impact: files, areas,
    hubs, neighbours. It runs in its own worktree, only while no gate runs and memory is free, and never
    changes a ticket's state.
  - Overlap notes on the newer of two queued tickets that meet in one area.
  - `[[graph.rules]]`: report a ticket that adds a forbidden dependency (merged graph vs main's).
  - `sluicy review <ticket>`: a review brief for a person or a reviewer agent.
  - `status`, `wait`, the conductor's red-ticket notes and the Deck's queue (Map column) show the map.
  - Plan review points the reviewers at the graph when the repository has one.
  - graphify runs with `GRAPHIFY_BACKEND=none` and without LLM credentials in its environment.
- Install 0.2.0 everywhere before adding `[graph]`: 0.1.0 rejects the unknown table.

## 0.1.0 - 2026-11-12

First public release.

- One machine-wide queue for every repository that has a `.sluicy.toml`: merge, gate, push and deploy one
  ticket at a time, with holds, allows, waivers, preflight, requeue and a conductor.
- Per-repository config: components, deploy command and exit-code contract, stamps, gate environment and
  failure patterns, dependency steps, guard patterns, `on_live` hook. A repository whose host deploys every
  push (`deploy.kind = "none"`) needs only a project name.
- The gate's push runs in a memory-capped, swapless systemd scope; memory brakes stop and requeue it.
- Claude Code hooks (guard, auto-ship on `Ship: yes`, plan review), installed user-level and inert outside
  sluicy repositories. Plan review is opt-in per repository.
- Session Deck: terminal and local web dashboard for concurrent Claude Code sessions. The release carries an
  x86_64 Linux binary; elsewhere install.sh builds it with cargo.
- `sluicy migrate-shipyard` imports the state of an older single-project install.
