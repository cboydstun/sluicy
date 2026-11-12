# Changelog

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
