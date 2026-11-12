#!/usr/bin/env python3
"""Plan-review gate: a plan must be adversarially reviewed before it can leave plan mode.

ExitPlanMode is a session's request to leave plan mode and start changing things. The PreToolUse
hook (installed by sluicy into ~/.claude/settings.json) runs `plan_review.py hook` on it and, in a
repository whose `.sluicy.toml` says `[plan_review] enabled = true`, refuses until the plan being
presented carries a recorded review: two independent reviewer subagents, the plan revised from what
they found, and `plan_review.py record` run on the final plan file. Everywhere else it allows.

A review is bound to the plan's text (sha256), so a plan edited after its review, or a new plan in
the same session, needs a new one. Standard library only.

    plan_review.py hook                                   # PreToolUse hook (stdin: hook JSON)
    plan_review.py record --session ID --plan-file P --reviews R
    plan_review.py status [--session ID]

Off switches: `~/.local/state/plan-review/off` (every session) or PLAN_REVIEW=off in the
session's environment.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sluicy"))
try:
    import config as sluicy_config
except ImportError:  # run on its own, without sluicy beside it: no repo can opt in, so it gates nothing
    sluicy_config = None

MIN_REVIEW_CHARS = 300
REVIEWER_HEADING = re.compile(r"(?im)^#{1,4}\s*reviewer\b")


def home():
    root = Path(os.environ.get("PLAN_REVIEW_HOME") or Path.home() / ".local/state/plan-review")
    root.mkdir(parents=True, exist_ok=True)
    return root


def gate_off():
    return os.environ.get("PLAN_REVIEW", "").strip().lower() == "off" or (home() / "off").exists()


def digest(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def safe_id(session):
    return re.sub(r"[^A-Za-z0-9._-]", "_", session or "unknown")


def state_path(session):
    return home() / f"{safe_id(session)}.json"


def load_state(session):
    try:
        return json.loads(state_path(session).read_text())
    except (OSError, ValueError):
        return {}


def read_text(path):
    try:
        return Path(path).expanduser().read_text()
    except (OSError, TypeError):
        return None


def plan_candidates(tool_input, state):
    """Every text the plan being presented could be: the tool's own `plan`, the file it names, and
    the file the review was recorded against. A match on any one counts."""
    texts = []
    if isinstance(tool_input.get("plan"), str) and tool_input["plan"].strip():
        texts.append(tool_input["plan"])
    for path in (tool_input.get("planFilePath"), state.get("planFile")):
        text = read_text(path) if path else None
        if text and text.strip():
            texts.append(text)
    return texts


def repo_settings(cwd):
    """(enabled, focus) from the nearest `.sluicy.toml` above `cwd`. No file, or a broken one: off."""
    if sluicy_config is None:
        return False, ""  # cannot read repo settings: never gate a repo that did not ask for it
    f = sluicy_config.find(cwd or os.getcwd(), stop="/")
    if not f:
        return False, ""
    try:
        pr = sluicy_config.load(f)["plan_review"]
    except sluicy_config.ConfigError:
        return False, ""
    return pr["enabled"], pr["focus"]


def instructions(session, plan_file, focus=""):
    plan_arg = plan_file or "<the plan file in ~/.claude/plans/>"
    me = Path(__file__).resolve()
    extra = f"\n     This project also asks Reviewer B to check: {focus}" if focus else ""
    return f"""Plan-review gate: this plan has not had an adversarial review, so it cannot leave plan mode yet.

Do this, then call ExitPlanMode again:

1. Spawn TWO reviewer subagents in ONE message, so they run in parallel and independently
   (Agent tool, subagent_type "general-purpose"). Tell each it is READ-ONLY (no edits, no commits,
   no pushes, no deploys, no ssh writes). Give each the plan file path ({plan_arg}) and the goal,
   NOT your conclusions, and ask for blockers first, each with file:line evidence.
   - Reviewer A (facts): try to DISPROVE every claim the plan makes about the code, data and
     production: that the bug is where it says, that a helper/route/field exists or does not,
     that a change is safe. Read the code; report each claim as confirmed / wrong / unverified.
   - Reviewer B (plan): ATTACK the plan itself. Back-compat with what is already deployed or
     installed (clients, cached pages, other services that call this one), auth and data scoping,
     an existing helper it duplicates, migrations and data already in production, deploy order,
     the checks the project's gate will trip, missing tests, and simpler alternatives.{extra}
2. Revise the plan file from what they found. Add a short "## Adversarial review" section to it:
   each blocker found, and what changed (or why it was rejected).
3. Write the two reviews (a "## Reviewer A" and a "## Reviewer B" section, their findings in
   brief) to a file in your scratchpad, then run:
     python3 "{me}" record --session {session} --plan-file {plan_arg} --reviews <that file>
4. Call ExitPlanMode again. The review is bound to the plan's exact text: run `record` again after
   any later edit to the plan file, and re-review if the edit changes the approach."""


def hook():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if data.get("tool_name") != "ExitPlanMode" or gate_off():
        return 0
    enabled, focus = repo_settings(data.get("cwd"))
    if not enabled:
        return 0
    session = data.get("session_id") or "unknown"
    tool_input = data.get("tool_input") or {}
    state = load_state(session)
    recorded = state.get("planHash")
    if recorded and any(digest(t) == recorded for t in plan_candidates(tool_input, state)):
        return 0
    reason = instructions(session, tool_input.get("planFilePath") or state.get("planFile"), focus)
    if recorded:
        reason = "The plan changed since its review was recorded.\n\n" + reason
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def record(args):
    plan = read_text(args.plan_file)
    if not plan or not plan.strip():
        print(f"No plan at {args.plan_file}", file=sys.stderr)
        return 1
    reviews = read_text(args.reviews)
    if not reviews or len(reviews.strip()) < MIN_REVIEW_CHARS:
        print(f"Reviews file {args.reviews} is missing or under {MIN_REVIEW_CHARS} characters.", file=sys.stderr)
        return 1
    if len(REVIEWER_HEADING.findall(reviews)) < 2:
        print("Reviews file needs a '## Reviewer A' and a '## Reviewer B' section.", file=sys.stderr)
        return 1
    sid = safe_id(args.session)
    (home() / f"{sid}.reviews.md").write_text(reviews)
    state = {
        "session": args.session,
        "planFile": str(Path(args.plan_file).expanduser().resolve()),
        "planHash": digest(plan),
        "reviews": str(home() / f"{sid}.reviews.md"),
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    state_path(args.session).write_text(json.dumps(state, indent=2))
    print(f"Recorded review for {args.plan_file}. Call ExitPlanMode now; edit the plan again and you must re-record.")
    return 0


def status(args):
    if gate_off():
        print("gate: OFF")
    files = [state_path(args.session)] if args.session else sorted(home().glob("*.json"), key=os.path.getmtime)[-10:]
    for f in files:
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            print(f"{f.stem}: no review recorded")
            continue
        current = read_text(s.get("planFile"))
        fresh = "current" if current and digest(current) == s.get("planHash") else "STALE (plan edited since)"
        print(f"{s.get('session')}: {s.get('recordedAt')} {s.get('planFile')} [{fresh}]")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    r = sub.add_parser("record")
    r.add_argument("--session", required=True)
    r.add_argument("--plan-file", required=True)
    r.add_argument("--reviews", required=True)
    s = sub.add_parser("status")
    s.add_argument("--session")
    args = p.parse_args(argv)
    if args.cmd == "hook":
        try:
            return hook()
        except Exception as exc:  # a broken gate must not trap a session in plan mode
            print(json.dumps({"systemMessage": f"plan-review gate error, allowed: {exc}"}))
            return 0
    return record(args) if args.cmd == "record" else status(args)


if __name__ == "__main__":
    sys.exit(main())
