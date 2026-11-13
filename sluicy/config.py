"""`.sluicy.toml`: what one repository tells sluicy about itself.

Standard library only (tomllib, Python 3.11+). Every key is optional except `project`; an unknown key is an
error, so a typo never silently turns a safety check off.
"""
import re
import tomllib
from pathlib import Path

FILE = ".sluicy.toml"

# The deploy script's exit-code contract. 0 is always success; any code not listed is "unknown".
DEFAULT_EXIT_CODES = {"lease_busy": 10, "refused": 11, "build_failed": 12, "rolled_back": 13}

DEFAULTS = {
    "project": None,
    "repo": {"remote": "origin", "branch": "main", "ssh_keepalive": True},
    "components": {},
    "deploy": {
        "kind": "none",
        "command": [],
        "recreate_arg": None,
        "smoke_command": [],
        "exit_codes": dict(DEFAULT_EXIT_CODES),
    },
    "stamps": {"command": []},
    "gate": {
        "waivers": [],
        "never_waive": [],
        "strip_env": [],
        "strip_prefixes": ["GATE_"],
        "env": {},
        "mem_reserve_gb": 4.0,
        "red_line": r"^\s*✗ ",
        "case_head": r"",
        "suite_head": r"",
        "quick_stop": [],
        "quick_note": (
            "The quick checks failed. Fix it on your branch; a waiver with a reason only when nothing a "
            "user can see changed."
        ),
    },
    "preflight": {"command": [], "env": {}, "link_dirs": [], "exclude": []},
    "deps": {},
    "init": {"copy_files": [], "install": []},
    "cleanup": {"build_output": None},
    "guard": {"deploy_patterns": [], "allow_args": []},
    "hooks": {"on_live": []},
    "plan_review": {"enabled": False, "focus": ""},
    "deck": {"lease_command": []},
    # The code map (graphify, optional): impact, queue overlap, dependency rules, review briefs.
    "graph": {
        "enabled": False,
        "command": ["graphify"],
        "update_args": ["update", ".", "--force"],
        "out_dir": "graphify-out",
        "timeout_s": 600,
        "min_avail_gb": 4.0,
        "rules": [],
    },
}

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    pass


# Tables whose keys are the user's own (names, env vars), not a fixed schema.
FREE_TABLES = {"components", "deps", "env", "exit_codes"}


def _merge(defaults, given, where):
    for key in given:
        if key not in defaults:
            raise ConfigError(f"{where or 'top level'}: unknown key {key!r}")
    out = {}
    for key, default in defaults.items():
        value = given.get(key, default)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{where}{key}: must be a table")
            out[key] = dict(value) if key in FREE_TABLES else _merge(default, value, f"{where}{key}.")
        else:
            out[key] = value
    return out


def _strings(value, where):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: must be a list of strings")
    return value


def validate(raw, path="<config>"):
    """A complete config dict (defaults filled in) from the parsed TOML, or ConfigError."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: not a table")
    cfg = _merge(DEFAULTS, raw, "")
    if not isinstance(cfg["project"], str) or not NAME.match(cfg["project"]):
        raise ConfigError(f"{path}: `project` must be a short name (letters, digits, . _ -)")
    comps = {}
    for name, c in cfg["components"].items():
        if not NAME.match(name):
            raise ConfigError(f"components.{name}: bad name")
        if not isinstance(c, dict):
            raise ConfigError(f"components.{name}: must be a table")
        extra = set(c) - {"path", "deploy_args"}
        if extra:
            raise ConfigError(f"components.{name}: unknown key {sorted(extra)[0]!r}")
        if not isinstance(c.get("path"), str) or not c["path"].strip("/"):
            raise ConfigError(f"components.{name}.path: required")
        comps[name] = {"path": c["path"].strip("/"), "deploy_args": _strings(c.get("deploy_args", []), f"components.{name}.deploy_args")}
    cfg["components"] = comps
    d = cfg["deploy"]
    if d["kind"] not in ("none", "command"):
        raise ConfigError("deploy.kind: one of none, command")
    if d["kind"] == "command" and not d["command"]:
        raise ConfigError("deploy.command: required when deploy.kind = \"command\"")
    _strings(d["command"], "deploy.command")
    _strings(d["smoke_command"], "deploy.smoke_command")
    codes = dict(DEFAULT_EXIT_CODES)
    for k, v in d["exit_codes"].items():
        if k not in DEFAULT_EXIT_CODES:
            raise ConfigError(f"deploy.exit_codes: unknown key {k!r}")
        if not isinstance(v, int) or not 1 <= v <= 255:
            raise ConfigError(f"deploy.exit_codes.{k}: an exit code 1-255")
        codes[k] = v
    d["exit_codes"] = codes
    _strings(cfg["stamps"]["command"], "stamps.command")
    g = cfg["gate"]
    for key in ("waivers", "never_waive", "strip_env", "strip_prefixes", "quick_stop"):
        _strings(g[key], f"gate.{key}")
    bad = set(g["never_waive"]) - set(g["waivers"])
    if bad:
        raise ConfigError(f"gate.never_waive: {sorted(bad)[0]} is not in gate.waivers")
    for key in ("red_line", "case_head", "suite_head"):
        try:
            re.compile(g[key])
        except (re.error, TypeError) as err:
            raise ConfigError(f"gate.{key}: {err}") from None
    for table in (g["env"], cfg["preflight"]["env"]):
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
            raise ConfigError("env tables take string values")
    for key in ("command", "link_dirs", "exclude"):
        _strings(cfg["preflight"][key], f"preflight.{key}")
    for name, cmd in cfg["deps"].items():
        _strings(cmd, f"deps.{name}")
    _strings(cfg["init"]["copy_files"], "init.copy_files")
    if not isinstance(cfg["init"]["install"], list) or not all(isinstance(i, dict) for i in cfg["init"]["install"]):
        raise ConfigError("init.install: a list of {dir, command} tables")
    for item in cfg["init"]["install"]:
        if set(item) - {"dir", "command"} or not isinstance(item.get("dir", "."), str):
            raise ConfigError("init.install: each entry takes dir and command")
        _strings(item.get("command", []), "init.install.command")
    if cfg["cleanup"]["build_output"] is not None:
        try:
            re.compile(cfg["cleanup"]["build_output"])
        except (re.error, TypeError) as err:
            raise ConfigError(f"cleanup.build_output: {err}") from None
    for key in ("deploy_patterns", "allow_args"):
        _strings(cfg["guard"][key], f"guard.{key}")
        for pat in cfg["guard"][key]:
            try:
                re.compile(pat)
            except re.error as err:
                raise ConfigError(f"guard.{key}: {err}") from None
    _strings(cfg["hooks"]["on_live"], "hooks.on_live")
    if not isinstance(cfg["plan_review"]["enabled"], bool):
        raise ConfigError("plan_review.enabled: true or false")
    if not isinstance(cfg["plan_review"]["focus"], str):
        raise ConfigError("plan_review.focus: text for the second reviewer")
    _strings(cfg["deck"]["lease_command"], "deck.lease_command")
    _validate_graph(cfg["graph"])
    return cfg


GLOB_KEYS = ("from", "to")


def _validate_graph(g):
    if not isinstance(g["enabled"], bool):
        raise ConfigError("graph.enabled: true or false")
    _strings(g["command"], "graph.command")
    if not g["command"]:
        raise ConfigError("graph.command: the graphify command, e.g. [\"graphify\"]")
    _strings(g["update_args"], "graph.update_args")
    out = g["out_dir"]
    if not isinstance(out, str) or not out.strip("/") or ".." in Path(out).parts or out.startswith("/"):
        raise ConfigError("graph.out_dir: a directory inside the repository")
    g["out_dir"] = out.strip("/")
    for key in ("timeout_s", "min_avail_gb"):
        if isinstance(g[key], bool) or not isinstance(g[key], (int, float)) or g[key] <= 0:
            raise ConfigError(f"graph.{key}: a positive number")
    if not isinstance(g["rules"], list) or not all(isinstance(r, dict) for r in g["rules"]):
        raise ConfigError("graph.rules: a list of [[graph.rules]] tables")
    names = set()
    for rule in g["rules"]:
        extra = set(rule) - {"name", *GLOB_KEYS}
        if extra:
            raise ConfigError(f"graph.rules: unknown key {sorted(extra)[0]!r}")
        if not isinstance(rule.get("name"), str) or not NAME.match(rule["name"]) or rule["name"] in names:
            raise ConfigError("graph.rules: each rule needs a unique short `name`")
        names.add(rule["name"])
        for key in GLOB_KEYS:
            if not rule.get(key):
                raise ConfigError(f"graph.rules.{rule['name']}.{key}: a list of path globs")
            _strings(rule[key], f"graph.rules.{rule['name']}.{key}")


def load(path):
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path}: {err}") from None
    return validate(raw, str(path))


def find(start, stop=None):
    """The nearest `.sluicy.toml` at or above `start`, never above `stop` (default: $HOME), or None. Cheap:
    hooks call it on every tool use in every repository."""
    here = Path(start).resolve()
    stop = Path(stop).resolve() if stop else Path.home().resolve()
    while True:
        f = here / FILE
        if f.is_file():
            return f
        if here == stop or here.parent == here:
            return None
        here = here.parent
