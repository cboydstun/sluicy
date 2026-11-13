# Changelog

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
