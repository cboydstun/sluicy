# Moving a live queue onto sluicy

sluicy grew out of *Shipyard*, a single-repository version of this queue that lived inside one project's
`tools/` directory. These are the steps for moving a repository that runs that older queue onto sluicy
without losing or replaying a ticket, and without a moment when a session runs unguarded. They apply to
any older install whose state lives in one directory (`shipyard.db`, `logs/`, `guards-on`).

The ideas behind the order:

- **There is never a second live queue.** The old queue is switched off for good before the new one takes over.
- **No session goes unguarded.** Old hooks keep calling the old path, and a shim forwards them.
- **Nothing is moved that something old still reads.** Files are copied, and the originals stay.

## Before the day

1. **Install sluicy:** `./install.sh`. It does nothing yet, because no repository has a `.sluicy.toml`.
2. **Add the shim.** In the repository, replace the old tool's entry point with a shim that runs the old code
   until the old state directory holds a `MIGRATED` file, then `exec`s `sluicy` with the same arguments.
   Ship that change, and the repository's `.sluicy.toml`, **through the old queue**.
3. **Accept both names in the repository's own scripts.** Read `SLUICY_TICKET` before the old ticket
   variable, skip the gate's own memory scope on `SLUICY_GATE_SCOPED`, and honour `GATE_LOGS`.
4. **Check the file:** `sluicy config check` in the repository.

## The cutover (a quiet moment)

1. **Stop the old queue for good:**
   - switch off its auto-ship and **leave that switch in place permanently**, since older worktrees still
     carry the old hooks;
   - disable its conductor timer;
   - wait until its `status` shows no active ticket;
   - stop any worker or preflight unit still running.
2. **List the worktrees** (`git worktree list`) and note any branch with an unshipped `Ship: yes` commit.
   They will be submitted to sluicy at their next session stop, or by hand.
3. **Migrate the state:** `sluicy migrate-shipyard <old state dir> --project <name>`. It refuses while the old
   queue has active work. It copies tickets, holds, allows, debts, what is live, logs and `guards-on`, then
   writes `MIGRATED` into the old directory and makes the old database read-only. From now on the shim
   forwards to sluicy.
4. **Register the repository:** `sluicy init` in it. To reuse the old permanent checkout, pass
   `--checkout <path>`. Delete the old preflight worktree and run `git worktree prune`; sluicy makes its own.
5. **Smoke test:**
   - submit a docs-only branch with `sluicy submit --ride` and wait for `merged`;
   - confirm a session's `git push origin main` is refused (guards on);
   - confirm `sluicy status` shows the migrated history.

## Going back

1. Remove `MIGRATED` from the old directory and make its database writable again.
2. Re-enable its conductor timer.
3. Remove its auto-ship switch.

The shim then runs the old code again. `guards-on` was copied, not moved, so nothing needs restoring.

## Later

Once every running session has restarted (the user-level sluicy hooks load at session start), delete the
shim, the old code and the repository's project-level hooks.
