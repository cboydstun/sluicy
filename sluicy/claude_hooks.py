"""Add sluicy's three Claude Code hooks to a settings.json, or take them out again. Every other hook and
setting is kept as it was; running it twice changes nothing.

    python3 claude_hooks.py install   --root <sluicy checkout> [--settings ~/.claude/settings.json]
    python3 claude_hooks.py uninstall [--settings ...]

The hooks do nothing in a repository without `.sluicy.toml`, so user-level settings are safe.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

MARK = "#sluicy"  # every command sluicy owns ends with this, so it finds its own entries again


def hooks(root):
    sy = Path(root) / "sluicy" / "sluicy.py"
    pr = Path(root) / "plan-review" / "plan_review.py"

    def cmd(script, *args):
        return f'f="{script}"; [ -f "$f" ] || exit 0; python3 "$f" {" ".join(args)} {MARK}'

    return {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": cmd(sy, "guard", "hook"), "timeout": 10}]},
            {"matcher": "ExitPlanMode", "hooks": [{"type": "command", "command": cmd(pr, "hook"), "timeout": 10}]},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": cmd(sy, "autoship"), "timeout": 60}]}],
    }


def strip(settings):
    """Remove sluicy's own hook entries; drop groups and events left empty."""
    all_hooks = settings.get("hooks")
    if not isinstance(all_hooks, dict):
        return settings
    for event in list(all_hooks):
        groups = all_hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            inner = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))] if isinstance(g, dict) else None
            if inner is None:
                kept.append(g)
            elif inner:
                kept.append({**g, "hooks": inner})
        if kept:
            all_hooks[event] = kept
        else:
            del all_hooks[event]
    if not all_hooks:
        settings.pop("hooks", None)
    return settings


def install(settings, root):
    settings = strip(settings)
    all_hooks = settings.setdefault("hooks", {})
    for event, groups in hooks(root).items():
        all_hooks.setdefault(event, []).extend(groups)
    return settings


def load(path):
    if not path.exists():
        return {}
    text = path.read_text()
    return json.loads(text) if text.strip() else {}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["install", "uninstall"])
    p.add_argument("--root")
    p.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    args = p.parse_args(argv)
    path = Path(args.settings).expanduser()
    try:
        before = load(path)
    except ValueError as err:
        print(f"{path} is not valid JSON ({err}); fix it first, nothing was changed", file=sys.stderr)
        return 1
    after = install(json.loads(json.dumps(before)), args.root) if args.action == "install" else strip(json.loads(json.dumps(before)))
    if after == before:
        print(f"{path}: already {'set' if args.action == 'install' else 'clear'}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.before-sluicy-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(after, indent=2) + "\n")
    tmp.replace(path)
    print(f"{path}: sluicy hooks {'installed' if args.action == 'install' else 'removed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
