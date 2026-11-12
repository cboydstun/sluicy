#!/usr/bin/env python3
"""sluicy: one gate at a time. Merge, gate and deploy for many concurrent coding sessions, across every
repository on the machine.

Standard library only. There is no daemon: submit starts a detached worker when none is running, and it exits
when the queue is empty. Reality (git, the deployed stamps) always outranks the database after a crash, and the
worker never redeploys on its own. Each repository describes itself in `.sluicy.toml` (see README).

Exit codes for wait: 0 merged or live; 1 failed, conflict or rolled back; 2 needs a person; 4 superseded or
cancelled; 75 still running, call again; 3 no such ticket.
"""
import argparse
import contextlib
import fcntl
import fnmatch
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as sconfig  # noqa: E402
import platform_linux as plat  # noqa: E402

KEEPALIVE = "ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=60"
MAX_PUSH_ATTEMPTS = 3
MAX_BRAKES = 3
GOOD = {"live", "merged"}
BAD = {"failed", "conflict", "rolled_back"}
NEEDS_PERSON = {"blocked", "unknown", "held"}
# Ended without a verdict on the code: a newer ticket replaced it, or someone cancelled it. `wait` exits 4,
# and autoship's fail cap does not count them.
ENDED = {"superseded", "cancelled"}
MAX_REQUEUES = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch TEXT NOT NULL, sha TEXT NOT NULL, source TEXT NOT NULL,
  mode TEXT NOT NULL, components TEXT NOT NULL DEFAULT '',
  recreate INTEGER NOT NULL DEFAULT 0, waivers TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL, base_sha TEXT, merged_sha TEXT, detail TEXT NOT NULL DEFAULT '',
  rolled_back TEXT NOT NULL DEFAULT '', log_path TEXT NOT NULL DEFAULT '',
  push_attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  brake_count INTEGER NOT NULL DEFAULT 0, project TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ticket_commits (sha TEXT NOT NULL, ticket_id INTEGER NOT NULL, PRIMARY KEY (sha, ticket_id));
CREATE TABLE IF NOT EXISTS allows (project TEXT NOT NULL, sha TEXT NOT NULL, component TEXT NOT NULL,
  reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS holds (project TEXT NOT NULL, component TEXT NOT NULL, reason TEXT NOT NULL,
  created_at REAL NOT NULL, cleared_at REAL);
CREATE TABLE IF NOT EXISTS live (project TEXT NOT NULL, component TEXT NOT NULL, sha TEXT NOT NULL,
  PRIMARY KEY (project, component));
CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
  kind TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL, done_at REAL);
CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, source TEXT NOT NULL, checkout TEXT NOT NULL,
  created_at REAL NOT NULL);
"""


def home():
    return Path(os.environ.get("SLUICY_HOME") or Path.home() / ".local/state/sluicy")


def checkouts_root():
    return Path(os.environ.get("SLUICY_CHECKOUTS") or Path.home() / ".local/share/sluicy/checkouts")


# ---- the project a command or ticket is about
#
# The worker serves every registered repository from one queue, so "which project" is set per ticket
# (`use_project`) before anything touches git, config or the component tables. Commands run inside a
# repository take it from that repository's `.sluicy.toml`.


class Project:
    def __init__(self, name, checkout, cfg):
        self.name, self.checkout, self.cfg = name, Path(checkout), cfg

    @property
    def components(self):
        return self.cfg["components"]

    @property
    def waivers(self):
        return set(self.cfg["gate"]["waivers"])

    @property
    def remote(self):
        return self.cfg["repo"]["remote"]

    @property
    def branch(self):
        return self.cfg["repo"]["branch"]

    @property
    def main_ref(self):
        return f"{self.remote}/{self.branch}"


P = None  # the current Project


def project_row(conn, name):
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def load_project_config(checkout, source=None):
    """The config as it is on main in the permanent checkout (what passed the gate), else the source's."""
    for root in (checkout, source):
        if root and (Path(root) / sconfig.FILE).is_file():
            return sconfig.load(Path(root) / sconfig.FILE)
    raise sconfig.ConfigError(f"no {sconfig.FILE} in {checkout}")


def use_project(conn, name):
    global P
    if P is not None and P.name == name:
        return P
    row = project_row(conn, name)
    if not row:
        raise RuntimeError(f"project {name!r} is not registered; run `sluicy init` in it")
    P = Project(name, row["checkout"], load_project_config(row["checkout"], row["source"]))
    return P


def here_project(conn=None, required=True):
    """The project of the repository the command runs in, from its `.sluicy.toml`. The registered checkout's
    copy (main) wins over the working tree's, so a branch cannot widen its own rules."""
    global P
    f = sconfig.find(Path.cwd(), stop="/")
    if not f:
        if required:
            die(f"no {sconfig.FILE} here or above; this repository does not use sluicy")
        return None
    cfg = sconfig.load(f)
    row = project_row(conn or db(), cfg["project"])
    checkout = row["checkout"] if row else checkouts_root() / cfg["project"]
    if row and (Path(checkout) / sconfig.FILE).is_file():
        cfg = load_project_config(checkout)
    P = Project(cfg["project"], checkout, cfg)
    return P


def checkout():
    return P.checkout


def db():
    home().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(os.environ.get("SLUICY_DB") or home() / "sluicy.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
    for col, decl in (("push_pgid", "INTEGER"), ("submitted_by", "TEXT NOT NULL DEFAULT 'hand'"),
                      ("preflight_sha", "TEXT"), ("preflight_at", "REAL"), ("waived_by", "TEXT NOT NULL DEFAULT '{}'"),
                      ("failure", "TEXT NOT NULL DEFAULT ''"), ("notes", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {decl}")
            conn.commit()
    return conn


def die(message, code=1):
    print(f"sluicy: {message}", file=sys.stderr)
    sys.exit(code)


def git(*args, cwd=None, check=True, log=None, timeout=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(extra_env or {})
    res = subprocess.run(["git", *args], cwd=cwd or checkout(), env=env, capture_output=True, text=True, timeout=timeout)
    if log:
        log.write(f"$ git {' '.join(args)}\n{res.stdout}{res.stderr}")
        log.flush()
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip() if check else res


def alert(message):
    home().mkdir(parents=True, exist_ok=True)
    with (home() / "alerts.log").open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    plat.notify("sluicy", message)


def set_ticket(conn, tid, **fields):
    fields["updated_at"] = time.time()
    names = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tickets SET {names} WHERE id = ?", (*fields.values(), tid))
    conn.commit()


def get_ticket(conn, tid):
    return conn.execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()


def components_of(t):
    return [c for c in t["components"].split(",") if c]


# Variables that change what a gate checks or how hard it runs. A worker must never take them from
# whichever shell or session happened to start it: waivers come only from the ticket.
BASE_GATE_VARS = {"CI", "CI_BASE", "SLUICY_BYPASS", "SLUICY_TICKET", "SLUICY_BRAKED", "SLUICY_WORKER_PID",
                  "SLUICY_GATE_SCOPED", "GATE_LOGS"}


def clean_env(extra=None):
    names, prefixes = set(BASE_GATE_VARS), ("GATE_",)
    if P is not None:
        g = P.cfg["gate"]
        names |= set(g["waivers"]) | set(g["strip_env"]) | set(g["env"]) | set(P.cfg["preflight"]["env"])
        prefixes = tuple(g["strip_prefixes"])
    env = {k: v for k, v in os.environ.items() if k not in names and not (prefixes and k.startswith(prefixes))}
    env.update(extra or {})
    return env


WORKER_UNIT = "sluicy-worker"
PREFLIGHT_UNIT = "sluicy-preflight"


def worker_script():
    return Path(__file__).resolve()


def spawn_worker():
    spawn("run", WORKER_UNIT, "worker.out")


def spawn_preflight():
    spawn("preflight", PREFLIGHT_UNIT, "preflight.out")


def spawn(cmd, unit, outname):
    """Start a worker (or the preflight) unless one runs. Under a systemd user session it is its own unit,
    so closing the terminal or session that submitted cannot kill it (or its gate), and the unit name
    keeps it single."""
    if os.environ.get("SLUICY_NO_SPAWN"):
        return
    home().mkdir(parents=True, exist_ok=True)
    env = clean_env()
    argv = [sys.executable, str(worker_script()), cmd]
    if not os.environ.get("SLUICY_HOME") and plat.start_unit(unit, argv, env, home(), home() / outname):
        return
    with (home() / outname).open("a") as out:
        subprocess.Popen(argv, cwd=home(), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)


# ---- reading reality


def read_stamps(comps):
    """{comp: deployed commit, or None for an unstamped build}. The project's stamps command gets the
    component names as arguments and prints `name=sha` lines. Raises when it cannot answer, which is not the
    same as a build with no stamp. Without a stamps command nothing is known about production: {}."""
    comps = list(dict.fromkeys(comps))
    cmd = P.cfg["stamps"]["command"]
    if not comps or not cmd:
        return {}
    res = subprocess.run([*cmd, *comps], cwd=checkout(), capture_output=True, text=True, timeout=60,
                         stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        raise RuntimeError(f"could not read the {','.join(comps)} stamps")
    out = {}
    for line in res.stdout.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k in comps:
            out[k] = v.strip() or None
    missing = [c for c in comps if c not in out]
    if missing:
        raise RuntimeError(f"the stamps command gave no answer for {','.join(missing)}")
    return out


def is_ancestor(a, b):
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def commits_touching(old, new, comp):
    out = git("log", "--no-merges", "--format=%H", f"{old}..{new}", "--", P.components[comp]["path"])
    return out.split()


def hold_for(conn, comp, project=None):
    return conn.execute(
        "SELECT * FROM holds WHERE project = ? AND component = ? AND cleared_at IS NULL ORDER BY created_at",
        (project or P.name, comp),
    ).fetchone()


def add_hold(conn, comp, reason):
    if not hold_for(conn, comp):
        conn.execute("INSERT INTO holds VALUES (?, ?, ?, ?, NULL)", (P.name, comp, reason, time.time()))
        conn.commit()


def clear_holds(conn, comp):
    conn.execute("UPDATE holds SET cleared_at = ? WHERE project = ? AND component = ? AND cleared_at IS NULL",
                 (time.time(), P.name, comp))
    conn.commit()


def live_sha(conn, comp):
    row = conn.execute("SELECT sha FROM live WHERE project = ? AND component = ?", (P.name, comp)).fetchone()
    return row["sha"] if row else None


def notice_outside_rollback(conn, comp, stamp):
    """A live build older than the one the queue last shipped means someone rolled back by hand."""
    last = live_sha(conn, comp)
    if not last or not stamp or last == stamp or not is_ancestor(stamp, last):
        return
    undone = set(commits_touching(stamp, last, comp))
    for t in conn.execute("SELECT * FROM tickets WHERE state = 'live' AND project = ?", (P.name,)).fetchall():
        own = {r["sha"] for r in conn.execute("SELECT sha FROM ticket_commits WHERE ticket_id = ?", (t["id"],))}
        if own & undone or comp in components_of(t) and is_ancestor(stamp, t["merged_sha"]) and t["merged_sha"] != stamp:
            set_ticket(conn, t["id"], rolled_back=",".join(filter(None, [t["rolled_back"], comp])))
    add_hold(conn, comp, f"{comp} was rolled back to {stamp[:8]} outside the queue")
    conn.execute("UPDATE live SET sha = ? WHERE project = ? AND component = ?", (stamp, P.name, comp))
    conn.commit()
    alert(f"{comp} was rolled back to {stamp[:8]} outside the queue; holding it until a new commit or allow")


def authorisation_problems(conn, t, comp, stamp):
    """Why this ticket may not deploy this component, given its live stamp. Empty means it may.
    The caller has fetched origin/main."""
    allowed = {
        r["sha"] for r in conn.execute("SELECT sha FROM allows WHERE project = ? AND component = ?", (P.name, comp))
    }
    if not stamp:
        return [] if "unstamped" in allowed else [f"{comp}: live build has no stamp, range unknown"]
    if "unstamped" in allowed:
        # The allow covered one unstamped build. Now there is a stamp, so it must not outlive it.
        conn.execute("DELETE FROM allows WHERE sha = 'unstamped' AND project = ? AND component = ?", (P.name, comp))
        conn.commit()
    notice_outside_rollback(conn, comp, stamp)
    if git("cat-file", "-e", f"{stamp}^{{commit}}", check=False).returncode != 0 or not is_ancestor(stamp, t["merged_sha"]):
        return [f"{comp}: live build {stamp[:8]} is not behind {t['merged_sha'][:8]}"]

    own = {r["sha"] for r in conn.execute("SELECT sha FROM ticket_commits WHERE ticket_id = ?", (t["id"],))}
    in_range = commits_touching(stamp, t["merged_sha"], comp)
    problems = []
    hold = hold_for(conn, comp)
    # A new commit in the component is the fix a hold waits for.
    if hold and not (own & set(in_range)):
        problems.append(f"{comp}: hold: {hold['reason']}")
    for sha in in_range:
        if sha in allowed:
            continue
        owners = conn.execute(
            "SELECT t.* FROM ticket_commits c JOIN tickets t ON t.id = c.ticket_id WHERE c.sha = ? AND t.project = ?",
            (sha, P.name),
        ).fetchall()
        if any(o["mode"] == "ride" or (o["mode"] == "deploy" and comp in components_of(o)) for o in owners):
            continue
        who = f"ticket {owners[0]['id']} ({owners[0]['mode']})" if owners else "no ticket"
        problems.append(f"{comp}: {sha[:8]} belongs to {who}")
    return problems


# ---- memory brakes
#
# The gate is the biggest thing this laptop runs, and nothing else knows it is coming. Before a gate
# starts, the worker waits for room; while it runs, a governor watches memory pressure and, when the
# box starts to stall, stops the gate's scope and requeues the ticket, so the desktop never gets near
# systemd-oomd's line (which can kill the whole user session). Every threshold can be set in
# the environment; the /proc paths too, for the tests.


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def read_memory():
    """{'avail_gb', 'some', 'full'}: MemAvailable and the avg10 memory pressure percentages."""
    avail_kb = None
    with open(os.environ.get("SLUICY_MEMINFO", "/proc/meminfo")) as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
    psi = {"some": 0.0, "full": 0.0}
    try:
        with open(os.environ.get("SLUICY_PSI", "/proc/pressure/memory")) as f:
            for line in f:
                kind, *fields = line.split()
                for field in fields:
                    if field.startswith("avg10="):
                        psi[kind] = float(field[6:])
    except OSError:
        pass  # a kernel without PSI: MemAvailable alone decides
    return {"avail_gb": (avail_kb or 0) / 1048576, **psi}


def describe(m):
    return f"{m['avail_gb']:.1f}G free, pressure {m['some']:.0f}% (full {m['full']:.0f}%)"


def room_to_start(m):
    """Enough memory to start a gate: plenty free and nothing stalling."""
    return m["avail_gb"] >= _env_float("SLUICY_MIN_AVAIL_GB", 14) and m["some"] <= _env_float(
        "SLUICY_MAX_PSI_SOME", 10
    )


def stalling(m):
    """The box is starting to stall: time to stop the gate before oomd picks a victim itself."""
    return (
        m["full"] > _env_float("SLUICY_BRAKE_FULL", 5)
        or m["some"] > _env_float("SLUICY_BRAKE_SOME", 30)
        or m["avail_gb"] < _env_float("SLUICY_BRAKE_MIN_AVAIL_GB", 3)
    )


def wait_for_memory(conn, t, log):
    """Block until there is room for a gate, held for SLUICY_MEM_SETTLE seconds. No timeout: the
    ticket's detail says what it waits for, and a person can free memory."""
    settle = _env_float("SLUICY_MEM_SETTLE", 30)
    poll = _env_float("SLUICY_MEM_POLL", 15)
    good_since = None
    waited = False
    while True:
        m = read_memory()
        if room_to_start(m):
            good_since = good_since or time.monotonic()
            if time.monotonic() - good_since >= settle:
                if waited:
                    set_ticket(conn, t["id"], detail="")
                    log.write(f"memory ok: {describe(m)}\n")
                    log.flush()
                return
            time.sleep(min(poll, settle))
            continue
        good_since = None
        detail = f"waiting for memory: {describe(m)}"
        if not waited:
            log.write(detail + "\n")
            log.flush()
            alert(f"ticket {t['id']} ({t['branch']}): {detail}")
        waited = True
        set_ticket(conn, t["id"], detail=detail)
        time.sleep(poll)


def gate_unit_prefix(tid, project=None):
    return f"sluicy-gate-{project or P.name}-{tid}-"


def stop_gate(tid, log, project=None):
    """Stop the gate's scope: systemd kills the whole tree (test runners, dev servers, browsers)."""
    pattern = gate_unit_prefix(tid, project) + "*"
    rc, err = plat.stop_units(pattern)
    if rc is None:
        return
    log.write(f"$ systemctl --user stop {pattern}  rc={rc} {err}\n")
    log.flush()


def oomd_killed_gate(tid, since):
    """True when systemd-oomd killed this ticket's gate scope since `since` (epoch seconds)."""
    return plat.oomd_killed(gate_unit_prefix(tid), since)


def gate_mem_max_gb():
    return plat.gate_mem_max_gb(float(P.cfg["gate"]["mem_reserve_gb"]), os.environ.get("SLUICY_MEMINFO", "/proc/meminfo"))


def run_gated_push(conn, tid, env, log):
    """Push with the governor watching. Returns the brake reason, or None when it ran its course."""
    poll = _env_float("SLUICY_BRAKE_POLL", 2)
    need = int(_env_float("SLUICY_BRAKE_POLLS", 3))
    started = time.time()
    argv = plat.scoped(["git", "push", P.remote, P.branch], f"{gate_unit_prefix(tid)}{os.getpid()}", gate_mem_max_gb())
    if argv[0] == "systemd-run":
        log.write(f"gate scope: {gate_unit_prefix(tid)}{os.getpid()}, MemoryMax {gate_mem_max_gb():.1f}G, no swap\n")
        log.flush()
    proc = subprocess.Popen(
        argv,
        cwd=checkout(),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    # Its own session, so a dead worker leaves it running: reconcile waits for this group.
    set_ticket(conn, tid, push_pgid=proc.pid)
    bad = 0
    while proc.poll() is None:
        try:
            m = read_memory()
        except OSError:
            m = None
        bad = bad + 1 if m and stalling(m) else 0
        if bad >= need:
            reason = f"memory pressure: {describe(m)}"
            log.write(f"\nsluicy brake: {reason}; stopping the gate\n")
            log.flush()
            stop_gate(tid, log)
            try:
                os.killpg(proc.pid, 15)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait()
            return reason
        time.sleep(poll)
    if proc.returncode != 0 and oomd_killed_gate(tid, started):
        log.write("\nsystemd-oomd killed the gate's scope (memory pressure)\n")
        return "systemd-oomd killed the gate (memory pressure)"
    return None


def braked(conn, t, reason):
    count = t["brake_count"] + 1
    if count >= MAX_BRAKES:
        detail = f"memory brakes x{count} ({reason}); free memory, or submit again with fewer sessions running"
        set_ticket(conn, t["id"], state="failed", brake_count=count, detail=detail)
        alert(f"ticket {t['id']} ({t['branch']}): {detail}")
        return
    detail = f"braked x{count} ({reason}); retrying with fewer workers when memory recovers"
    set_ticket(conn, t["id"], state="queued", brake_count=count, merged_sha=None, base_sha=None, detail=detail)
    alert(f"ticket {t['id']} ({t['branch']}): {detail}")


# ---- the worker


FAILURE_CAP = 2000


def pattern(key):
    """A failure-capture regex from the project's [gate] config; None when the project sets none.
    red_line: a stage's red line in the gate's own output (its next lines say what to fix).
    case_head: a failing test case in the gate's output. suite_head: a failing suite in a stage log."""
    raw = P.cfg["gate"][key] if P is not None else ""
    return re.compile(raw) if raw else None


def gate_logs_dir(root):
    """Where a gate may keep per-stage output for a checkout. sluicy exports it to the gate as GATE_LOGS and
    copies what is there into the ticket's logs when the gate fails; the next gate overwrites it."""
    return Path(os.environ.get("TMPDIR", "/tmp")) / "sluicy-gate-logs" / str(root).replace("/", "_")


def excerpt_lines(lines, head, after=2, limit=12):
    """Each line matching `head`, plus the next `after` non-blank lines. A `FAIL <file>` line keeps no
    followers, Jest's `● Console` blocks are skipped whole, and a gate summary line (✗) ends a run."""
    out, keep, console = [], 0, False
    for line in lines:
        if re.match(r"\s+● Console", line):
            console, keep = True, 0
            continue
        if head.match(line):
            console = False
            out.append(line.rstrip())
            keep = 0 if line.startswith("FAIL ") else after
        elif keep and line.strip() and not console:
            if "✗" in line:
                keep = 0
                continue
            out.append(line.rstrip())
            keep -= 1
        if len(out) >= limit:
            break
    return out


def capture_failure(conn, tid, gate_log, root=None, conflict_files=None):
    """Keep what a red ticket's session needs to fix it, before the next gate overwrites the stage logs:
    a copy of each stage log, and an excerpt in `failure` that `wait` and `status` print."""
    try:
        parts = []
        if conflict_files:
            parts.append("conflicting files: " + " ".join(conflict_files[:20]))
        text = ANSI.sub("", Path(gate_log).read_text(errors="replace")) if gate_log and Path(gate_log).is_file() else ""
        lines = text.splitlines()
        red, case, suite = pattern("red_line"), pattern("case_head"), pattern("suite_head")
        if red:
            parts += excerpt_lines(lines, red, after=3, limit=16)
        if case:
            parts += excerpt_lines(lines, case, after=1, limit=10)
        if root is not None:
            src = gate_logs_dir(root)
            for log in sorted(src.glob("*.log")) if src.is_dir() else []:
                if log.stat().st_mtime < time.time() - 6 * 3600:
                    continue  # an older gate's output, not this ticket's
                dst = home() / "logs" / f"{tid}.{log.stem}.log"
                shutil.copy2(log, dst)
                jest = excerpt_lines(ANSI.sub("", log.read_text(errors="replace")).splitlines(), suite) if suite else []
                if jest:
                    parts.append(f"[{log.stem}, full log {dst}]")
                    parts += list(dict.fromkeys(jest))  # Jest prints each failure twice
        failure = "\n".join(dict.fromkeys(parts))[:FAILURE_CAP]
        conn.execute("UPDATE tickets SET failure = ? WHERE id = ?", (failure, tid))
        conn.commit()
    except Exception as err:  # noqa: BLE001 - a failed capture must never change the ticket's outcome
        conn.execute("UPDATE tickets SET failure = ? WHERE id = ?", (f"(could not capture: {err})", tid))
        conn.commit()


def conflicted_files(root=None):
    out = git("diff", "--name-only", "--diff-filter=U", cwd=root, check=False)
    return out.stdout.split() if out.returncode == 0 else []


def quarantine_strays(t, log, root=None):
    """Move untracked, non-ignored paths out of the checkout before a merge.

    `reset --hard` keeps untracked files, and a gate's formatter or linter scans them. A failed
    ticket that added a directory (say a 200 MB build output) would fail every later ticket;
    target/) left it behind, and every later ticket failed "Some files need formatting".
    Ignored paths (node_modules, .env files) are never listed, so deps and env survive.
    Moved, not deleted: whole directories go, ignored contents included, to
    <state>/stray/<ticket>-<time>/, where a person can look or put them back."""
    root = root or checkout()
    strays = [p for p in git("ls-files", "-z", "--others", "--exclude-standard", "--directory", cwd=root).split("\0") if p]
    if not strays:
        return
    dest = home() / "stray" / f"{t['id']}-{int(time.time())}"
    for rel in strays:
        src = root / rel.rstrip("/")
        target = dest / rel.rstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
    log.write(f"moved {len(strays)} untracked path(s) out of {root} to {dest}: {' '.join(strays[:10])}\n")
    log.flush()
    alert(f"ticket {t['id']}: moved untracked {' '.join(strays[:5])} from {root.name} to {dest}")


def merge(conn, t, log):
    """Merge the submitted sha onto a fresh origin/main. False when the ticket has ended."""
    # Guarded: a supersede, cancel or preflight red may have ended it while it waited for memory.
    taken = conn.execute("UPDATE tickets SET state = 'merging', updated_at = ? WHERE id = ? AND state = 'queued'",
                         (time.time(), t["id"]))
    conn.commit()
    if not taken.rowcount:
        log.write("ticket left the queue before its merge; skipped\n")
        return False
    git("fetch", "-q", P.remote, P.branch, log=log)
    git("checkout", "-q", "-B", P.branch, P.main_ref, log=log)
    git("reset", "-q", "--hard", P.main_ref, log=log)  # never git clean: it holds deps and env files
    quarantine_strays(t, log)
    base = git("rev-parse", "HEAD")
    fetched = git("fetch", "-q", t["source"], f"refs/heads/{t['branch']}", check=False, log=log)
    if fetched.returncode != 0 or git("merge-base", "--is-ancestor", t["sha"], "FETCH_HEAD", check=False).returncode != 0:
        set_ticket(conn, t["id"], state="failed", detail=f"could not fetch {t['sha'][:8]} from {t['branch']}")
        return False
    merged = git("merge", "--no-ff", "--no-edit", "-m", f"Merge branch '{t['branch']}'", t["sha"], check=False, log=log)
    if merged.returncode != 0:
        files = conflicted_files()
        git("merge", "--abort", check=False, log=log)
        capture_failure(conn, t["id"], None, conflict_files=files)
        set_ticket(conn, t["id"], state="conflict", detail=f"does not merge onto {base[:8]}; rebase or merge main and submit again")
        alert(f"ticket {t['id']} ({t['branch']}) has a merge conflict")
        return False
    head = git("rev-parse", "HEAD")
    if not install_deps(conn, t, git("diff", "--name-only", base, head).split(), log):
        return False
    set_ticket(conn, t["id"], base_sha=base, merged_sha=head)
    return True


def install_deps(conn, t, changed, log):
    """The project's [deps] rules: a changed file matching a key (a glob on its name or path) runs that
    command in the file's directory, so the gate sees the merged dependencies. False when one fails."""
    rules = P.cfg["deps"]
    for name in changed:
        for glob, cmd in rules.items():
            if not (fnmatch.fnmatch(Path(name).name, glob) or fnmatch.fnmatch(name, glob)):
                continue
            where = checkout() / Path(name).parent
            log.write(f"$ {' '.join(cmd)}  (in {where})\n")
            log.flush()
            if subprocess.run(cmd, cwd=where, stdout=log, stderr=log, stdin=subprocess.DEVNULL).returncode != 0:
                detail = f"deps step failed: {' '.join(cmd)} in {Path(name).parent}"
                set_ticket(conn, t["id"], state="failed", detail=detail)
                alert(f"ticket {t['id']} ({t['branch']}): {detail}")
                return False
    return True


def after_push(conn, t):
    """The merge is on origin. Record whose commits these are and move on."""
    for sha in git("rev-list", "--no-merges", f"{t['base_sha']}..{t['merged_sha']}").split():
        conn.execute("INSERT OR IGNORE INTO ticket_commits VALUES (?, ?)", (sha, t["id"]))
    conn.commit()
    if t["mode"] == "deploy" and P.cfg["deploy"]["kind"] == "command":
        set_ticket(conn, t["id"], state="pushed", detail="")
    elif t["mode"] == "hold":
        set_ticket(conn, t["id"], state="held", detail="merged; waiting for `sluicy release`")
    else:
        set_ticket(conn, t["id"], state="merged", detail="")


def push(conn, t, log):
    set_ticket(conn, t["id"], state="pushing")
    env = clean_env({
        "SLUICY_TICKET": str(t["id"]),
        "SLUICY_PROJECT": P.name,
        "SLUICY_WORKER_PID": str(os.getpid()),
        # The gate runs in sluicy's capped scope: a gate script that caps itself can skip that.
        "SLUICY_GATE_SCOPED": "1",
        "GATE_LOGS": str(gate_logs_dir(checkout())),
        **P.cfg["gate"]["env"],
    })
    if P.cfg["repo"]["ssh_keepalive"]:
        env["GIT_SSH_COMMAND"] = KEEPALIVE  # remotes drop an idle connection while a long gate runs
    for name in json.loads(t["waivers"]):
        env[name] = "1"
    if t["brake_count"]:
        env["SLUICY_BRAKED"] = str(t["brake_count"])
    log.write(f"$ git push {P.remote} {P.branch}  (ticket {t['id']})\n")
    log.flush()
    brake = run_gated_push(conn, t["id"], env, log)
    # The exit code is not the truth: an idle connection dies with 141 after the gate.
    if not fetch_main(log):
        # The push may have landed. Failing it here would orphan its commits; reconcile decides.
        detail = "could not reach origin after the push; the next worker decides from git"
        set_ticket(conn, t["id"], state="pushing", detail=detail)
        alert(f"ticket {t['id']} ({t['branch']}): {detail}")
        return
    t = get_ticket(conn, t["id"])
    if is_ancestor(t["merged_sha"], P.main_ref):
        after_push(conn, t)
        return
    if brake:
        braked(conn, t, brake)
        return
    attempts = t["push_attempts"] + 1
    moved = git("rev-parse", P.main_ref) != t["base_sha"]
    if moved and attempts < MAX_PUSH_ATTEMPTS:
        set_ticket(conn, t["id"], state="queued", push_attempts=attempts, merged_sha=None, base_sha=None, detail="main moved during the gate; merging again")
        return
    detail = f"gate or push failed; log {t['log_path']}"
    log.flush()
    capture_failure(conn, t["id"], t["log_path"], root=checkout())
    set_ticket(conn, t["id"], state="failed", detail=detail)
    alert(f"ticket {t['id']} ({t['branch']}): {detail}")


def fetch_main(log=None):
    """Fetch origin/main, retrying a flaky network. False when it never answered."""
    backoff = _env_float("SLUICY_FETCH_BACKOFF", 5)
    for attempt in range(3):
        if git("fetch", "-q", P.remote, P.branch, check=False, log=log).returncode == 0:
            return True
        if attempt < 2:
            time.sleep(backoff * (attempt + 1))
    return False


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def settle_orphan_push(t):
    """A dead worker's push runs in its own session and may still be gating, or about to land. Let it
    finish (bounded), then stop whatever is left, so the next merge never resets the checkout under it."""
    with open(t["log_path"], "a") as log:
        pgid = t["push_pgid"]
        if pgid and group_alive(pgid):
            log.write(f"\nsluicy: waiting for the previous worker's push (pgid {pgid})\n")
            log.flush()
            deadline = time.monotonic() + _env_float("SLUICY_ORPHAN_WAIT", 1200)
            while group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(_env_float("SLUICY_ORPHAN_POLL", 5))
            if group_alive(pgid):
                log.write("sluicy: it did not finish in time; stopping it\n")
                stop_gate(t["id"], log)
                try:
                    os.killpg(pgid, 15)
                    time.sleep(5)
                    os.killpg(pgid, 9)
                except ProcessLookupError:
                    pass
        # A gate scope can outlive its push: systemd keeps the scope when the push itself is killed.
        stop_gate(t["id"], log)


def mark_live(conn, t, old_stamps=None):
    """`old_stamps`: what each component ran before this deploy, read before it. Without them (reconcile) the
    `live` rows are the fallback, read here before they are replaced."""
    comps = components_of(t)
    old = {c: (old_stamps or {}).get(c) or live_sha(conn, c) for c in comps}
    for comp in comps:
        conn.execute("INSERT OR REPLACE INTO live VALUES (?, ?, ?)", (P.name, comp, t["merged_sha"]))
        clear_holds(conn, comp)
    conn.commit()
    set_ticket(conn, t["id"], state="live", detail="")
    run_on_live(t, old)


def run_on_live(t, old):
    """The project's [hooks] on_live command, after a deploy went live. Its output goes to the ticket's log;
    it never changes the ticket's outcome."""
    cmd = P.cfg["hooks"]["on_live"]
    if not cmd:
        return
    env = clean_env({
        "SLUICY_HOME": str(home()), "SLUICY_TICKET": str(t["id"]), "SLUICY_PROJECT": P.name,
        "SLUICY_MERGED_SHA": t["merged_sha"], "SLUICY_COMPONENTS": t["components"],
        **{f"SLUICY_OLD_STAMP_{re.sub(r'[^A-Za-z0-9]', '_', c).upper()}": (sha or "") for c, sha in old.items()},
    })
    try:
        with open(t["log_path"], "a") as log:
            log.write(f"$ {' '.join(cmd)}  (on_live)\n")
            log.flush()
            rc = subprocess.run(cmd, cwd=checkout(), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                timeout=600).returncode
        if rc != 0:
            alert(f"on_live for ticket {t['id']} exited {rc}; see {t['log_path']}")
    except Exception as exc:  # noqa: BLE001 - a hook must never touch the deploy's outcome
        alert(f"on_live for ticket {t['id']} failed: {exc}")


def mark_unknown(conn, t, detail):
    for comp in components_of(t):
        add_hold(conn, comp, f"ticket {t['id']} left {comp} in an unknown state")
    set_ticket(conn, t["id"], state="unknown", detail=detail)
    alert(f"ticket {t['id']} ({t['branch']}): {detail}; components {t['components']} held. Look at production.")


def already_live(t, stamps):
    """A later ticket's deploy may have carried this one."""
    stamps = [stamps[c] for c in components_of(t)]
    return all(s and git("cat-file", "-e", f"{s}^{{commit}}", check=False).returncode == 0 and is_ancestor(t["merged_sha"], s) for s in stamps)


def current_stamps(conn, comps):
    """What each component runs now: the project's stamps command, or, without one, the last commit sluicy
    itself deployed (None before its first deploy, which then needs `allow unstamped`)."""
    if P.cfg["stamps"]["command"]:
        return read_stamps(comps)
    return {c: live_sha(conn, c) for c in comps}


def deploy_command(t):
    d = P.cfg["deploy"]
    args = [a for c in components_of(t) for a in P.components[c]["deploy_args"]]
    cmd = []
    for part in d["command"]:
        if part == "{args}":
            cmd += args
        else:
            cmd.append(part.replace("{sha}", t["merged_sha"]).replace("{components}", t["components"]))
    if t["recreate"] and d["recreate_arg"]:
        cmd.append(d["recreate_arg"])
    return cmd


def deploy(conn, t, log):
    comps = components_of(t)
    git("fetch", "-q", P.remote, P.branch)
    try:
        stamps = current_stamps(conn, comps)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        stamps, problems = None, [str(exc)]
    if stamps is not None:
        if already_live(t, stamps):
            set_ticket(conn, t["id"], state="live", detail="carried by a later deploy")
            return
        problems = [p for c in comps for p in authorisation_problems(conn, t, c, stamps[c])]
    if problems:
        detail = "; ".join(problems)
        fresh = get_ticket(conn, t["id"])
        if fresh["state"] != "blocked" or fresh["detail"] != detail:
            alert(f"ticket {t['id']} ({t['branch']}) blocked: {detail}")
        set_ticket(conn, t["id"], state="blocked", detail=detail)
        return

    cmd = deploy_command(t)
    codes = P.cfg["deploy"]["exit_codes"]
    busy = codes["lease_busy"]
    env = clean_env({"SLUICY_TICKET": str(t["id"]), "SLUICY_PROJECT": P.name, "SLUICY_WORKER_PID": str(os.getpid())})
    wait = int(os.environ.get("SLUICY_LEASE_WAIT", "60"))
    tries = int(os.environ.get("SLUICY_LEASE_TRIES", "30"))
    git("checkout", "-q", "-B", P.branch, P.main_ref, log=log)

    for attempt in range(1, tries + 1):
        set_ticket(conn, t["id"], state="deploying")
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        rc = subprocess.run(cmd, cwd=checkout(), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
        if rc != busy:
            break
        if attempt < tries:
            set_ticket(conn, t["id"], state="pushed", detail=f"lease busy, try {attempt} of {tries}")
            time.sleep(wait)

    if rc == 0:
        mark_live(conn, t, old_stamps=stamps)
    elif rc == busy:
        detail = f"deploy lease busy after {tries} tries"
        alert(f"ticket {t['id']} ({t['branch']}): {detail}")
        set_ticket(conn, t["id"], state="blocked", detail=detail)
    elif rc == codes["build_failed"]:
        detail = f"build failed, old build still live; log {t['log_path']}"
        alert(f"ticket {t['id']} ({t['branch']}): {detail}")
        set_ticket(conn, t["id"], state="failed", detail=detail)
    elif rc == codes["refused"]:
        detail = f"deploy preflight refused; log {t['log_path']}"
        alert(f"ticket {t['id']} ({t['branch']}): {detail}")
        set_ticket(conn, t["id"], state="failed", detail=detail)
    elif rc == codes["rolled_back"]:
        for c in comps:
            add_hold(conn, c, f"ticket {t['id']} failed after the swap and was rolled back; {t['merged_sha'][:8]} is still on main")
        set_ticket(conn, t["id"], state="rolled_back", rolled_back=t["components"], detail=f"rolled back healthy; log {t['log_path']}")
        alert(f"ticket {t['id']} ({t['branch']}) was rolled back; holding {t['components']} until a new commit or allow")
    else:
        mark_unknown(conn, t, f"deploy exit {rc}; log {t['log_path']}")


def reconcile(conn):
    """Ticket mid-step from a dead worker: decide from git and the stamps, never redeploy."""
    for t in conn.execute("SELECT * FROM tickets WHERE state IN ('merging', 'pushing', 'deploying')").fetchall():
        use_project(conn, t["project"])
        if t["state"] == "pushing":
            settle_orphan_push(t)
        git("fetch", "-q", P.remote, P.branch)
        if t["state"] != "deploying":
            if t["merged_sha"] and is_ancestor(t["merged_sha"], P.main_ref):
                after_push(conn, t)
            else:
                set_ticket(conn, t["id"], state="queued", merged_sha=None, base_sha=None, detail="worker died before the push landed")
            continue
        try:
            stamps = read_stamps(components_of(t)) if P.cfg["stamps"]["command"] else {}
            stamps_match = bool(stamps) and all(s == t["merged_sha"] for s in stamps.values())
        except (RuntimeError, subprocess.TimeoutExpired):
            stamps_match = False
        if not stamps_match:
            mark_unknown(conn, t, "worker died during the deploy and the stamps do not match (or there are none)")
            continue
        smoke = P.cfg["deploy"]["smoke_command"]
        rc = 0
        if smoke:
            with open(t["log_path"], "a") as log:
                env = {**os.environ, "SLUICY_TICKET": str(t["id"]), "SLUICY_PROJECT": P.name}
                rc = subprocess.run(smoke, cwd=checkout(), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
        if rc == 0:
            mark_live(conn, t)
        else:
            mark_unknown(conn, t, f"worker died after the deploy and the smoke command returned {rc}")


def cmd_run(_args):
    home().mkdir(parents=True, exist_ok=True)
    lock = (home() / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    conn = db()
    try:
        reconcile(conn)
    except Exception as exc:  # noqa: BLE001 - deciding nothing is safer than guessing; a later wait retries
        alert(f"worker could not reconcile ({type(exc).__name__}: {exc}); nothing ran")
        return 1
    tried_deploy = set()
    while True:
        rows = conn.execute("SELECT * FROM tickets WHERE state IN ('queued', 'pushed') ORDER BY id").fetchall()
        rows = [r for r in rows if r["id"] not in tried_deploy]
        if not rows:
            run_conduct_quietly()
            return 0
        t = rows[0]
        with open(t["log_path"], "a") as log:
            try:
                use_project(conn, t["project"])
                if not checkout().is_dir():
                    raise RuntimeError(f"no checkout at {checkout()}; run `sluicy init` in {t['project']}")
                if t["state"] == "queued":
                    wait_for_memory(conn, t, log)
                    if merge(conn, t, log):
                        push(conn, get_ticket(conn, t["id"]), log)
                else:
                    tried_deploy.add(t["id"])
                    deploy(conn, t, log)
                    if get_ticket(conn, t["id"])["state"] == "live":
                        # Something shipped: a hold may have lifted, so waiting tickets look again.
                        conn.execute("UPDATE tickets SET state = 'pushed' WHERE state = 'blocked' AND project = ?", (P.name,))
                        conn.commit()
                        tried_deploy.clear()
            except Exception as err:  # noqa: BLE001 - one ticket's error must not strand the queue
                exc = err if isinstance(err, RuntimeError) else f"{type(err).__name__}: {err}"
                log.write(f"sluicy error: {exc}\n")
                fresh = get_ticket(conn, t["id"])
                if fresh["state"] == "deploying":
                    mark_unknown(conn, fresh, f"worker error during the deploy: {exc}")
                else:
                    set_ticket(conn, t["id"], state="failed", detail=f"worker error: {exc}")
                    alert(f"ticket {t['id']} ({t['branch']}): worker error: {exc}")


# ---- preflight: the quick checks, minutes after submit instead of after the queue


def preflight_dir():
    return Path(os.environ.get("SLUICY_PREFLIGHT_DIR") or f"{checkout()}-preflight")


def ensure_preflight_tree():
    """A detached worktree of the project's checkout, never the tree the worker merges in. The project's
    [preflight] link_dirs (e.g. node_modules) are symlinks to the checkout's, so the quick checks need no
    second install; [preflight] exclude names build caches the checks create, kept out of the stray mover."""
    root = preflight_dir()
    if not root.is_dir():
        git("worktree", "prune")
        git("worktree", "add", "-q", "--detach", str(root), P.main_ref)
    # `node_modules/` in .gitignore matches directories only, and these are symlinks.
    exclude = Path(git("rev-parse", "--git-common-dir")) / "info" / "exclude"
    if not exclude.is_absolute():
        exclude = checkout() / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    pf = P.cfg["preflight"]
    links = pf["link_dirs"]
    have = exclude.read_text().split() if exclude.exists() else []
    missing = [p for p in [*{Path(d).name for d in links}, *pf["exclude"]] if p not in have]
    if missing:
        with exclude.open("a") as f:
            f.write("\n" + "\n".join(missing) + "\n")
    for rel in links:
        src, dst = checkout() / rel, root / rel
        if src.is_dir() and dst.parent.is_dir() and not dst.exists() and not dst.is_symlink():
            dst.symlink_to(src)
    return root


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def end_queued(conn, t, state, detail, preflight_sha=None):
    """Move a ticket out of `queued`, only if the worker has not taken it meanwhile."""
    cur = conn.execute(
        "UPDATE tickets SET state = ?, detail = ?, preflight_sha = ?, preflight_at = ?, updated_at = ?"
        " WHERE id = ? AND state = 'queued'",
        (state, detail, preflight_sha, time.time(), time.time(), t["id"]),
    )
    conn.commit()
    if cur.rowcount:
        alert(f"ticket {t['id']} ({t['branch']}): {detail}")


def mark_preflighted(conn, t):
    conn.execute("UPDATE tickets SET preflight_sha = ?, preflight_at = ? WHERE id = ?", (t["sha"], time.time(), t["id"]))
    conn.commit()


def preflight_one(conn, t, root):
    logp = home() / "logs" / f"{t['id']}.preflight.log"
    logp.parent.mkdir(parents=True, exist_ok=True)
    with logp.open("w") as log:
        git("fetch", "-q", P.remote, P.branch, cwd=root, log=log)
        git("checkout", "-q", "--detach", P.main_ref, cwd=root, log=log)
        git("reset", "-q", "--hard", P.main_ref, cwd=root, log=log)
        quarantine_strays(t, log, root=root)
        base = git("rev-parse", "HEAD", cwd=root)
        fetched = git("fetch", "-q", t["source"], f"refs/heads/{t['branch']}", cwd=root, check=False, log=log)
        if fetched.returncode != 0 or git("merge-base", "--is-ancestor", t["sha"], "FETCH_HEAD", cwd=root, check=False).returncode != 0:
            log.write("could not fetch the sha; the worker decides\n")
            return mark_preflighted(conn, t)
        merged = git("merge", "--no-ff", "--no-commit", t["sha"], cwd=root, check=False, log=log)
        try:
            if merged.returncode != 0:
                capture_failure(conn, t["id"], None, conflict_files=conflicted_files(root))
                return end_queued(conn, t, "conflict", f"does not merge onto {base[:8]}; rebase or merge main and submit again")
            cmd = P.cfg["preflight"]["command"]
            if not cmd:
                log.write("the project has no [preflight] command; the push gate decides\n")
                return mark_preflighted(conn, t)
            env = clean_env({"CI_BASE": P.main_ref, "SLUICY_PROJECT": P.name, **P.cfg["preflight"]["env"]})
            for name in json.loads(t["waivers"]):
                env[name] = "1"
            prefix = (["nice", "-n", "19"] if shutil.which("nice") else []) + (["ionice", "-c", "3"] if shutil.which("ionice") else [])
            log.write(f"$ {' '.join(f'{k}={v}' for k, v in P.cfg['preflight']['env'].items())} {' '.join(cmd)}\n".replace("$  ", "$ "))
            log.flush()
            rc = subprocess.run([*prefix, *cmd], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
        finally:
            git("merge", "--abort", cwd=root, check=False)
            git("reset", "-q", "--hard", P.main_ref, cwd=root, check=False)
    if rc == 0:
        return mark_preflighted(conn, t)
    lines = [ANSI.sub("", line) for line in logp.read_text().splitlines()]
    red = pattern("red_line")
    reds = [line.strip() for line in lines if red and red.match(line)]
    tail = [line.strip() for line in lines if line.strip()]
    first = reds[0] if reds else tail[-1] if tail else f"exit {rc}"
    capture_failure(conn, t["id"], logp, root=root)
    end_queued(conn, t, "failed", f"preflight: {first[:160]}; log {logp}", t["sha"])


def cmd_preflight(_args):
    """Run the gate's quick checks on every queued ticket not yet checked at its sha. A red ends the
    ticket now, so its session's `wait` returns 1 while other tickets hold the gate. Green is not a
    pass: the push gate still runs everything."""
    home().mkdir(parents=True, exist_ok=True)
    lock = (home() / "preflight.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    conn = db()
    trees = {}
    while True:
        t = conn.execute(
            "SELECT * FROM tickets WHERE state = 'queued' AND (preflight_sha IS NULL OR preflight_sha != sha) ORDER BY id"
        ).fetchone()
        if not t:
            run_conduct_quietly()
            return 0
        try:
            use_project(conn, t["project"])
            if not checkout().is_dir():
                raise RuntimeError(f"no checkout at {checkout()}")
            if P.name not in trees:
                trees[P.name] = ensure_preflight_tree()
            preflight_one(conn, t, trees[P.name])
        except Exception as err:  # noqa: BLE001 - a broken preflight must never stop a ticket; the gate decides
            with (home() / "logs" / f"{t['id']}.preflight.log").open("a") as log:
                log.write(f"preflight error: {type(err).__name__}: {err}\n")
            mark_preflighted(conn, t)


# ---- requeue: a person or the conductor sends a failed ticket round again, on the record

def failed_at_quick_check(t):
    """A red the project's quick checks gave: the preflight, or a gate whose log carries one of the
    project's [gate] quick_stop lines (the gate stopped before its slow stages). Only those take a waiver."""
    if t["detail"].startswith("preflight:"):
        return True
    stops = P.cfg["gate"]["quick_stop"] if P is not None else []
    if not stops:
        return False
    log = Path(t["log_path"])
    text = ANSI.sub("", log.read_text(errors="replace")) if log.is_file() else ""
    return any(s in text for s in stops)


def requeue_chain(conn, t):
    """How many requeues led to this ticket."""
    chain, cur = 0, t
    while cur and cur["submitted_by"].startswith("requeue:"):
        chain += 1
        cur = get_ticket(conn, int(cur["submitted_by"].split(":")[1]))
    return chain


def cmd_requeue(args):
    conn = db()
    t = get_ticket(conn, args.ticket)
    if not t:
        die(f"no ticket {args.ticket}", 3)
    if t["state"] != "failed":
        die(f"ticket {t['id']} is {t['state']}; only a failed ticket can be requeued")
    use_project(conn, t["project"])
    never = set(P.cfg["gate"]["never_waive"])
    waivers = json.loads(t["waivers"])
    waived_by = json.loads(t["waived_by"] or "{}")
    new = {}
    for item in args.waive or []:
        name, _, reason = item.partition("=")
        if name in never:
            die(f"requeue never waives {name}")
        if name not in P.waivers or not reason.strip():
            die(f"--waive takes NAME=\"reason\" with NAME one of {', '.join(sorted(P.waivers - never)) or '(none configured)'}")
        new[name] = reason.strip()
    if (new or args.sha) and not failed_at_quick_check(t):
        die(f"ticket {t['id']} failed past the quick checks; fix it on the branch and submit")
    chain = requeue_chain(conn, t)
    if chain >= MAX_REQUEUES:
        die(f"ticket {t['id']} has been requeued {chain} times already; find the cause and submit a fix")
    # The ticket stays its owner's: same branch and source, so autoship and the owner's `wait` still see it.
    sha, branch, source = t["sha"], t["branch"], t["source"]
    if args.sha:
        res = git("rev-parse", "--verify", f"{args.sha}^{{commit}}", cwd=source, check=False)
        if res.returncode != 0:
            die(f"no commit {args.sha} in {source}")
        sha = res.stdout.strip()
        if git("merge-base", "--is-ancestor", t["sha"], sha, cwd=source, check=False).returncode != 0:
            die(f"{sha[:8]} does not contain the ticket's commit {t['sha'][:8]}")
        if git("merge-base", "--is-ancestor", sha, f"refs/heads/{branch}", cwd=source, check=False).returncode != 0:
            die(f"{sha[:8]} is not on {branch}: the owner commits fixes to their own branch")
    # Who: what the caller says, and where it ran from, so a waiver cannot borrow a name.
    here = git("rev-parse", "--show-toplevel", cwd=Path.cwd(), check=False)
    by = f"{args.by}@{here.stdout.strip() if here.returncode == 0 else Path.cwd()}"
    waivers.update(new)
    waived_by.update({k: by for k in new})
    tid = submit_ticket(conn, branch, sha, source, t["mode"], components_of(t), t["recreate"], waivers, f"requeue:{t['id']}:{by}")
    set_ticket(conn, tid, waived_by=json.dumps(waived_by))
    for item in args.debt or []:
        kind, _, detail = item.partition("=")
        conn.execute("INSERT INTO debts (ticket_id, kind, detail, created_at) VALUES (?, ?, ?, ?)", (tid, kind.strip(), detail.strip(), time.time()))
    conn.commit()
    given = ", ".join(f"{k} by {by}: {v}" for k, v in new.items())
    print(f"ticket {tid} requeues {t['id']} at {sha[:8]}" + (f"; {given}" if given else ""))
    if new:
        alert(f"ticket {tid} (requeue of {t['id']}): {given}")
    spawn_worker()
    return 0


def cmd_debts(args):
    """Work a waiver promised: e.g. screenshots to recapture once the ticket is live."""
    conn = db()
    if args.done:
        cur = conn.execute("UPDATE debts SET done_at = ? WHERE id = ? AND done_at IS NULL", (time.time(), args.done))
        conn.commit()
        if not cur.rowcount:
            die(f"no open debt {args.done}")
        print(f"debt {args.done} done")
        return 0
    for d in conn.execute("SELECT d.*, t.state, t.branch FROM debts d JOIN tickets t ON t.id = d.ticket_id WHERE d.done_at IS NULL ORDER BY d.id"):
        print(f"{d['id']:>3} ticket {d['ticket_id']} {d['state']:<10} {d['kind']}: {d['detail']}  ({d['branch']})")
    return 0


# ---- conduct: what a person would do by hand, done by code every few minutes

INFRA_SIGNS = ("worker error:", "memory brakes x", "deps step failed")
HOG_BYTES = 3 * 1024 ** 3
OURS_CGROUP = ("sluicy-gate", "sluicy-preflight", "sluicy-worker", "sluicy-conduct")
# A person's own apps are not workloads a session can cap (the first live run flagged Chrome showing the Deck).
DESKTOP_APP = re.compile(r"(^|/)(chrome|chromium|firefox|code|gnome-shell|Xwayland|slack|spotify)( |$)")


def conduct_state():
    p = home() / "conduct.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"done": [], "cleanup": []}


def save_conduct_state(state):
    p = home() / "conduct.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(p)


def agents():
    """Live Claude sessions (`claude agents --json --all`), [] when the CLI cannot answer."""
    exe = os.environ.get("SLUICY_CLAUDE", "claude")
    try:
        res = subprocess.run([exe, "agents", "--json", "--all"], capture_output=True, text=True, timeout=20)
        data = json.loads(res.stdout) if res.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return data if isinstance(data, list) else data.get("agents", [])


def job_sessions():
    """{worktreePath: {id, name, state}} from Claude's background-job records (`~/.claude/jobs/<id>/state.json`).
    The only place that ties a stopped session to its worktree: `claude agents` reports a stopped session's cwd
    as the checkout it started from, and its name is a title, not the worktree's name (without this, six
    "no session" cleanup rows belonged to resumable sessions)."""
    root = Path(os.environ.get("SLUICY_JOBS", str(Path.home() / ".claude" / "jobs")))
    out = {}
    for f in root.glob("*/state.json") if root.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        wt = str(d.get("worktreePath") or "").rstrip("/")
        if wt:
            out[wt] = {"id": f.parent.name, "name": d.get("name"), "state": d.get("state")}
    return out


def owner_of(path, sessions, jobs=None):
    """The session working in this worktree. A background job record naming the worktree wins; then a session
    whose cwd is the path or under it. The main checkout has no single owner (many sessions start there), so it
    gets None. A job with no row in `sessions` still owns it: the agents list is not complete."""
    path = str(path).rstrip("/")
    if "/.claude/worktrees/" not in path:
        return None
    job = (jobs or {}).get(path)
    if job:
        row = next((a for a in sessions if a.get("id") == job["id"]), None)
        if row:
            return row
        return {"id": job["id"], "name": job["name"], "state": job["state"], "kind": "background", "unlisted": True}
    best = None
    for a in sessions:
        cwd = str(a.get("cwd") or "").rstrip("/")
        # A stopped `claude --bg -w <name>` session reports the checkout it started from, not its worktree;
        # its worktree is <that>/.claude/worktrees/<name> (the Deck reads it the same way).
        own = f"{cwd}/.claude/worktrees/{a.get('name')}" if a.get("name") and not a.get("pid") else None
        if cwd == path or cwd.startswith(path + "/") or own == path:
            best = a
    return best


def add_note(conn, t, text):
    note = (t["notes"] + "\n\n" if t["notes"] else "") + f"[{time.strftime('%H:%M')}] {text}"
    conn.execute("UPDATE tickets SET notes = ? WHERE id = ?", (note[-4000:], t["id"]))
    conn.commit()


def classify(t):
    """What kind of red a finished ticket is, or None when there is nothing to do."""
    detail = t["detail"] or ""
    if t["state"] == "conflict":
        return "conflict"
    if t["state"] != "failed" or detail.startswith(("cancelled", "superseded")):
        return None
    if detail.startswith(INFRA_SIGNS):
        return "infra"
    if detail.startswith("preflight:") or failed_at_quick_check(t):
        return "quick"
    if detail.startswith("gate or push failed"):
        return "real"
    return None


def conduct_tickets(conn, state, sessions, now):
    done = set(state["done"])
    acted = []
    rows = conn.execute("SELECT * FROM tickets WHERE state IN ('failed', 'conflict', 'held', 'unknown') AND updated_at > ?",
                        (now - 2 * 86400,)).fetchall()
    for t in rows:
        if t["state"] in ("held", "unknown"):
            key = f"{t['id']}:{t['state']}"
            if key not in done:
                alert(f"ticket {t['id']} ({t['branch']}) is {t['state']} and needs a person: {t['detail']}")
                done.add(key)
                acted.append(key)
            continue
        try:
            use_project(conn, t["project"])
        except Exception as err:  # noqa: BLE001 - a project that cannot load must not stop the others
            key = f"{t['id']}:noproject"
            if key not in done:
                alert(f"ticket {t['id']}: project {t['project']!r} cannot load ({err}); the conductor skips it")
                done.add(key)
            continue
        kind = classify(t)
        key = f"{t['id']}:{kind}"
        if not kind or key in done:
            continue
        newer = conn.execute("SELECT 1 FROM tickets WHERE project = ? AND branch = ? AND id > ? LIMIT 1",
                             (t["project"], t["branch"], t["id"])).fetchone()
        if newer:
            done.add(key)  # the owner already moved on
            continue
        owner = owner_of(t["source"], sessions)
        if kind == "infra":
            if requeue_chain(conn, t) < MAX_REQUEUES and memory_ok():
                waived_by = t["waived_by"]
                tid = submit_ticket(conn, t["branch"], t["sha"], t["source"], t["mode"], components_of(t), t["recreate"],
                                    json.loads(t["waivers"]), f"requeue:{t['id']}:conductor")
                set_ticket(conn, tid, waived_by=waived_by)
                add_note(conn, t, f"Not your code ({t['detail'][:120]}). Requeued unchanged as ticket {tid}; wait on {tid}.")
                spawn_worker()
            else:
                add_note(conn, t, f"Infrastructure failure ({t['detail'][:120]}); not requeued again (cap or memory). A person decides.")
                alert(f"ticket {t['id']} ({t['branch']}): infrastructure failure, requeue cap reached")
        elif kind == "quick":
            add_note(conn, t, P.cfg["gate"]["quick_note"] + "\n" + (t["failure"] or t["detail"]))
        elif kind == "real":
            add_note(conn, t, "The gate failed past the quick checks; fix the failures below on your branch and commit:\n"
                     + (t["failure"] or f"see {t['log_path']}"))
        elif kind == "conflict":
            add_note(conn, t, "Does not merge onto main. Merge origin/main into your branch, keep both sides, rerun "
                     "the affected checks and submit again.\n" + (t["failure"] or ""))
        if kind != "infra" and owner is None:
            alert(f"ticket {t['id']} ({t['branch']}) failed ({kind}) and no session owns it; see `sluicy status {t['id']}`")
        done.add(key)
        acted.append(key)
    state["done"] = sorted(done)[-2000:]
    return acted


def memory_ok():
    """Requeue a braked ticket only when the worker's own start rule would let it run now."""
    try:
        return room_to_start(read_memory())
    except (OSError, ValueError, KeyError):
        return False


def read_proc(root):
    """{pid: (pgrp, rss_bytes, cgroup, cwd, cmd)} from a /proc-shaped directory."""
    procs = {}
    for d in Path(root).iterdir() if Path(root).is_dir() else []:
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            pgrp = int(stat.rsplit(")", 1)[1].split()[2])
            rss = 0
            for line in (d / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
            cgroup = (d / "cgroup").read_text()
            try:
                cwd = os.readlink(d / "cwd")
            except OSError:
                cwd = ""
            cmd = (d / "cmdline").read_text().replace("\0", " ").strip()
        except (OSError, ValueError, IndexError):
            continue
        procs[int(d.name)] = (pgrp, rss, cgroup, cwd, cmd)
    return procs


def hogs(procs, sessions):
    """Process groups using ≥ 3 GB outside sluicy's own gate and preflight, with the session that owns them."""
    groups = {}
    for pid, (pgrp, rss, cgroup, cwd, cmd) in procs.items():
        g = groups.setdefault(pgrp, {"rss": 0, "ours": False, "pid": pid, "cwd": cwd, "cmd": cmd})
        g["rss"] += rss
        g["ours"] |= any(s in cgroup for s in OURS_CGROUP)
        if pid == pgrp or (pid < g["pid"] and g["pid"] != pgrp):
            g.update(pid=pid, cwd=cwd, cmd=cmd)
    out = []
    for pgrp, g in groups.items():
        if g["ours"] or g["rss"] < HOG_BYTES or DESKTOP_APP.search(g["cmd"].split(" ")[0]):
            continue
        who = next((a.get("name") for a in sessions if a.get("cwd") and g["cwd"].startswith(str(a["cwd"]).rstrip("/"))), None)
        out.append({"pgrp": pgrp, "pid": g["pid"], "gb": round(g["rss"] / 1024 ** 3, 1), "cwd": g["cwd"],
                    "cmd": g["cmd"][:120], "session": who})
    return sorted(out, key=lambda h: -h["gb"])


def conduct_hogs(conn, state, sessions):
    if not conn.execute("SELECT 1 FROM tickets WHERE state = 'pushing' LIMIT 1").fetchone():
        return []
    done = set(state["done"])
    acted = []
    for h in hogs(read_proc(os.environ.get("SLUICY_PROC", "/proc")), sessions):
        key = f"hog:{h['pgrp']}"
        if key in done:
            continue
        who = f"session {h['session']}" if h["session"] else "no Claude session"
        alert(f"{h['gb']} GB outside the gate while it runs: pid {h['pid']} ({who}) in {h['cwd']}: {h['cmd']}. "
              "Ask it to cap workers; sluicy never kills it.")
        done.add(key)
        acted.append(key)
    state["done"] = sorted(done)[-2000:]
    return acted


def worktree_head(path):
    res = git("rev-parse", "HEAD", cwd=path, check=False)
    return res.stdout.strip() if res.returncode == 0 else None


def worktree_clean(path):
    res = git("status", "--porcelain", "--untracked-files=no", cwd=path, check=False)
    return res.returncode == 0 and not res.stdout.strip()


def cleanup_candidates(conn, sessions, now, jobs=None):
    """Things that are finished and only take space or attention. Listed here; `auto_clean` removes the
    finished sessions and worktrees among them that are provably safe to remove."""
    items = []
    # A worktree whose HEAD shipped (merged or live ticket at that exact sha) and is clean: done.
    seen = set()
    for t in conn.execute("SELECT * FROM tickets WHERE state IN ('merged', 'live') ORDER BY id DESC LIMIT 300"):
        path = t["source"]
        if path in seen or "/.claude/worktrees/" not in path or not Path(path).is_dir():
            continue
        seen.add(path)
        if worktree_head(path) != t["sha"] or not worktree_clean(path):
            continue
        s = owner_of(path, sessions, jobs)
        # `state: blocked` is a finished turn that asked a person something; `working` is mid-turn.
        # Neither is done.
        if s and (s.get("status") in ("busy", "waiting") or s.get("waitingFor") or s.get("state") in ("blocked", "working")):
            continue
        # Only a background session can be removed from the Deck (`claude rm`); a terminal one is left alone.
        if s and s.get("kind") != "background":
            continue
        if s:
            items.append({"kind": "session", "id": f"session:{s.get('name')}", "name": s.get("name"), "path": path,
                          "session": s.get("id"), "ticket": t["id"],
                          "label": f"Session {s.get('name')}: its work shipped in ticket {t['id']} ({t['state']}); "
                                   "remove the session and its worktree"})
        else:
            items.append({"kind": "worktree", "id": f"worktree:{path}", "path": path, "ticket": t["id"],
                          "label": f"Worktree {Path(path).name}: shipped in ticket {t['id']}, clean, no session; remove it"})
    # Tickets that ended red 1–7 days ago and nobody is coming back for: one item, so the list stays short.
    # Never auto tickets: a cancelled ticket is ENDED, which autoship's two-failure stop does not count.
    stale = []
    for t in conn.execute("SELECT * FROM tickets WHERE state IN ('failed', 'conflict') AND submitted_by != 'auto'"
                          " AND updated_at BETWEEN ? AND ?", (now - 7 * 86400, now - 86400)):
        newer = conn.execute("SELECT 1 FROM tickets WHERE project = ? AND branch = ? AND id > ? LIMIT 1",
                             (t["project"], t["branch"], t["id"])).fetchone()
        if newer or owner_of(t["source"], sessions, jobs) is None:
            stale.append(t["id"])
    if stale:
        items.append({"kind": "tickets", "id": "tickets:" + ",".join(map(str, stale)), "tickets": stale,
                      "label": f"{len(stale)} red ticket(s) nobody is coming back for (a newer ticket exists, or no "
                               f"session owns it): {', '.join(map(str, stale[:12]))}; cancel them"})
    # Strays older than a week, by path and size: they may hold real untracked source.
    for d in sorted((home() / "stray").glob("*")) if (home() / "stray").is_dir() else []:
        if d.stat().st_mtime > now - 7 * 86400:
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        paths = [str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()][:8]
        items.append({"kind": "stray", "id": f"stray:{d.name}", "path": str(d), "mb": round(size / 1e6, 1),
                      "label": f"Stray {d.name} ({round(size / 1e6, 1)} MB): {', '.join(paths)}; delete it"})
    for d in conn.execute("SELECT * FROM debts WHERE done_at IS NULL AND created_at < ?", (now - 3 * 86400,)):
        items.append({"kind": "debt", "id": f"debt:{d['id']}", "debt": d["id"],
                      "label": f"Debt {d['id']} (ticket {d['ticket_id']}): {d['kind']} {d['detail']}"})
    return items


# ---- auto-clean: finished, stopped sessions (and truly ownerless worktrees) are removed without asking.
# Removing what is done should not need a person. Every check
# below guards something a review found would be lost (session, process, ignored file, unmerged commit).

AUTO_CLEAN_MIN_AGE = 3600
# Ignored paths that are build output: removing a worktree may delete these. Anything else ignored (videos
# not yet published, screenshots, e2e auth state, a .env that differs from the main checkout's) keeps it.
BUILD_OUTPUT = re.compile(
    r"(^|/)(node_modules|\.next[^/]*|dist[^/]*|\.swc|coverage|logs|\.turbo|test-results|playwright-report|__pycache__|target|\.pytest_cache)(/|$)"
    r"|\.tsbuildinfo$"
)


def auto_clean_off():
    return (home() / "auto-clean-off").exists()


def main_checkout_of(path):
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path, check=False)
    if common.returncode != 0:
        return None
    return Path(common.stdout.strip()).parent


def locked_worktrees(main):
    res = git("worktree", "list", "--porcelain", cwd=main, check=False)
    locked, current = set(), None
    for line in (res.stdout if res.returncode == 0 else "").splitlines():
        if line.startswith("worktree "):
            current = line[9:].rstrip("/")
        elif line.startswith("locked") and current:
            locked.add(current)
    return locked


def build_output():
    custom = P.cfg["cleanup"]["build_output"] if P is not None else None
    return re.compile(custom) if custom else BUILD_OUTPUT


def leftovers(path, main):
    """What `git worktree remove` would delete that is not build output: untracked or ignored, one path each.
    A `.env*` file counts unless the main checkout has the same bytes."""
    res = git("--no-optional-locks", "status", "--porcelain", "--ignored", "--untracked-files=all", cwd=path, check=False)
    if res.returncode != 0:
        return ["git status failed"]
    out = []
    for line in res.stdout.splitlines():
        rel = line[3:].strip().strip('"')
        if line.startswith("!! ") and build_output().search(rel):
            continue
        if line.startswith("!! ") and re.search(r"(^|/)\.env[^/]*$", rel):
            mine, theirs = Path(path) / rel, Path(main) / rel
            try:
                if theirs.is_file() and mine.read_bytes() == theirs.read_bytes():
                    continue
            except OSError:
                pass
        out.append(rel)
    return out


def process_inside(path):
    procs = read_proc(os.environ.get("SLUICY_PROC", "/proc"))
    path = str(path).rstrip("/")
    return next((pid for pid, p in procs.items() if p[3] == path or p[3].startswith(path + "/")), None)


def why_not_removable(conn, item, sessions, now, locked):
    """None when the item is safe to remove, else the reason it stays."""
    path = item["path"]
    if "/.claude/worktrees/" not in path or Path(path).name.startswith("agent-"):
        return "not a session worktree"
    if item["kind"] == "session":
        s = next((a for a in sessions if a.get("id") == item.get("session")), None)
        if not s:
            return "session not in the agents list"
        if s.get("pid") or s.get("status") or s.get("waitingFor") or s.get("state") != "done" or s.get("kind") != "background":
            return "session is running or not finished"
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (item.get("ticket"),)).fetchone()
    if t:
        try:
            use_project(conn, t["project"])
        except Exception:  # noqa: BLE001 - an unloadable project keeps its worktrees
            return f"project {t['project']} cannot load"
    if not t or now - t["updated_at"] < AUTO_CLEAN_MIN_AGE:
        return "shipped under an hour ago"
    if any(hold_for(conn, c, t["project"]) for c in components_of(t)):
        return "a component it deployed is on hold"
    if worktree_head(path) != t["sha"]:
        return "HEAD moved since it shipped"
    main = main_checkout_of(path)
    if not main:
        return "no main checkout"
    if git("merge-base", "--is-ancestor", t["sha"], P.main_ref, cwd=main, check=False).returncode != 0:
        return "not on origin/main yet"
    if path.rstrip("/") in locked:
        return "worktree is locked"
    pid = process_inside(path)
    if pid:
        return f"process {pid} is working in it"
    left = leftovers(path, main)
    if left:
        return "would delete: " + ", ".join(left[:3]) + (f" (+{len(left) - 3})" if len(left) > 3 else "")
    return None


def auto_clean(conn, state, items, sessions, now):
    """Remove the session and worktree items that are provably finished; return the items that stay listed.
    Skipped entirely when the agents list did not answer: no answer is not the same as no sessions."""
    if auto_clean_off() or not sessions:
        return items
    reasons = state.setdefault("autoclean", {})
    kept, removed = [], []
    locked_by_main = {}
    exe = os.environ.get("SLUICY_CLAUDE", "claude")
    for item in items:
        if item["kind"] not in ("session", "worktree"):
            kept.append(item)
            continue
        main = main_checkout_of(item["path"]) if Path(item["path"]).is_dir() else None
        if main and str(main) not in locked_by_main:
            locked_by_main[str(main)] = locked_worktrees(main)
        why = why_not_removable(conn, item, sessions, now, locked_by_main.get(str(main), set()))
        if not why:
            # Never a force flag: `claude rm` and `git worktree remove` refuse unpushed or dirty work themselves.
            if item["kind"] == "session":
                cmd, label = [exe, "rm", item["session"]], "claude rm"
            else:
                cmd, label = ["git", "worktree", "remove", item["path"]], "git worktree remove"
            try:
                res = subprocess.run(cmd, cwd=main, capture_output=True, text=True, timeout=120)
                why = None if res.returncode == 0 else f"{label} failed: {(res.stderr or res.stdout).strip()[:200]}"
            except (OSError, subprocess.TimeoutExpired) as err:
                why = f"{label} failed: {err}"
        if why:
            if reasons.get(item["path"]) != why and " failed: " in why:
                alert(f"auto-clean kept {Path(item['path']).name}: {why}")
            reasons[item["path"]] = why
            kept.append({**item, "autoClean": why})
        else:
            reasons.pop(item["path"], None)
            removed.append(item.get("name") or Path(item["path"]).name)
    if removed:
        alert(f"removed {len(removed)} finished session(s)/worktree(s): {', '.join(removed)}")
    # Forget reasons for paths that are no longer listed.
    listed = {i["path"] for i in kept if "path" in i}
    state["autoclean"] = {p: r for p, r in reasons.items() if p in listed}
    return kept


def cmd_conduct(_args):
    """One pass: act on finished reds once, warn about memory hogs while a gate runs, list cleanup. Idempotent."""
    home().mkdir(parents=True, exist_ok=True)
    lock = (home() / "conduct.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    conn = db()
    now = time.time()
    state = conduct_state()
    sessions = agents()
    jobs = job_sessions()
    acted = conduct_tickets(conn, state, sessions, now) + conduct_hogs(conn, state, sessions)
    items = cleanup_candidates(conn, sessions, now, jobs)
    items = auto_clean(conn, state, items, sessions, now)
    out = home() / "cleanup.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generated": now, "items": items}, indent=1))
    tmp.replace(out)  # the Deck reads it while we write: never half a file
    fresh = [i for i in items if i["id"] not in set(state.get("cleanup", []))]
    if fresh:
        alert(f"{len(items)} thing(s) to clean up ({', '.join(i['kind'] for i in fresh[:4])} new); open the Session Deck")
    state["cleanup"] = [i["id"] for i in items]
    save_conduct_state(state)
    for key in acted:
        print(key)
    return 0


def run_conduct_quietly():
    """The worker and preflight call this as they finish. It must never fail them."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_conduct(None)
    except Exception as err:  # noqa: BLE001
        alert(f"conduct failed: {type(err).__name__}: {err}")


# ---- commands


def cmd_init(args):
    """Register the repository this runs in and create its permanent checkout. Reuses what exists and
    overwrites no file."""
    conn = db()
    proj = here_project(conn)
    source = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    target = Path(args.checkout) if args.checkout else checkouts_root() / proj.name
    row = project_row(conn, proj.name)
    if row and Path(row["checkout"]) != target and not args.checkout:
        target = Path(row["checkout"])
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        url = git("remote", "get-url", proj.remote, cwd=source)
        subprocess.run(["git", "clone", "-q", url, str(target)], check=True)
    conn.execute("INSERT OR REPLACE INTO projects VALUES (?, ?, ?, ?)",
                 (proj.name, str(source), str(target), row["created_at"] if row else time.time()))
    conn.commit()
    proj.checkout = target
    hooks = target / ".githooks"
    if hooks.is_dir():
        git("config", "core.hooksPath", ".githooks", cwd=target)
    if not args.no_install:
        for rel in proj.cfg["init"]["copy_files"]:
            src, dst = source / rel, target / rel
            if src.is_file() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"copied {rel}")
            elif not src.is_file():
                print(f"not in {source}: {rel}")
        for item in proj.cfg["init"]["install"]:
            where = target / item.get("dir", ".")
            if where.is_dir() and item.get("command"):
                print(f"{' '.join(item['command'])} in {item.get('dir', '.')}")
                subprocess.run(item["command"], cwd=where, check=True)
    install_conduct_timer()
    print(f"project {proj.name}: checkout ready at {target}")
    return 0


def install_conduct_timer():
    """`sluicy conduct` every 5 minutes as a systemd user timer. Not under tests (SLUICY_HOME) or without a
    systemd user session."""
    if os.environ.get("SLUICY_HOME") or not shutil.which("systemctl"):
        return
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "sluicy-conduct.service").write_text(
        "[Unit]\nDescription=sluicy conductor pass\n\n[Service]\nType=oneshot\n"
        f"ExecStart={sys.executable} {worker_script()} conduct\nNice=19\nIOSchedulingClass=idle\n"
    )
    (unit_dir / "sluicy-conduct.timer").write_text(
        "[Unit]\nDescription=sluicy conductor every 5 minutes\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    res = subprocess.run(["systemctl", "--user", "enable", "--now", "sluicy-conduct.timer"], check=False,
                         capture_output=True, text=True)
    print("conduct timer: " + ("on, every 5 min" if res.returncode == 0 else f"not enabled ({res.stderr.strip()[:120]})"))


def require_registered(conn):
    if not project_row(conn, P.name):
        die(f"project {P.name} is not registered here; run `sluicy init` in it first")


def cmd_submit(args):
    conn = db()
    here_project(conn)
    require_registered(conn)
    source = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    sha = git("rev-parse", f"refs/heads/{args.branch}^{{commit}}", cwd=source, check=False)
    if sha.returncode != 0:
        die(f"no local branch {args.branch}")
    if args.branch == P.branch:
        die(f"submit a branch, not {P.branch}")
    if args.deploy is not None:
        if P.cfg["deploy"]["kind"] != "command":
            die("this project has no deploy command (deploy.kind = \"none\"): its push is the deploy; use --ride")
        mode, comps = "deploy", [c.strip() for c in args.deploy.split(",") if c.strip()]
        bad = [c for c in comps if c not in P.components]
        if bad or not comps:
            die(f"--deploy takes {', '.join(P.components)}; got {args.deploy}")
    elif args.ride:
        mode, comps = "ride", []
    else:
        mode, comps = "hold", []
    if args.recreate and mode != "deploy":
        die("--recreate needs --deploy")
    waivers = {}
    for item in args.waive or []:
        name, _, reason = item.partition("=")
        if name not in P.waivers or not reason.strip():
            die(f"--waive takes NAME=\"reason\" with NAME one of {', '.join(sorted(P.waivers)) or '(none configured)'}")
        waivers[name] = reason.strip()
    marks = ",".join("?" * len(ACTIVE))
    dup = conn.execute(f"SELECT id FROM tickets WHERE project = ? AND sha = ? AND mode = ? AND components = ? AND state IN ({marks})",
                       (P.name, sha.stdout.strip(), mode, ",".join(comps), *ACTIVE)).fetchone()
    if dup:
        die(f"{sha.stdout.strip()[:8]} is already ticket {dup['id']} with the same request; wait on that one")
    tid = submit_ticket(conn, args.branch, sha.stdout.strip(), source, mode, comps, int(args.recreate), waivers, "hand")
    print(f"ticket {tid} {args.branch} {sha.stdout.strip()[:8]} {mode} {','.join(comps)}".rstrip())
    spawn_worker()
    return 0


def submit_ticket(conn, branch, sha, source, mode, comps, recreate, waivers, by):
    """Queue one ticket for exactly this sha. The caller starts the worker."""
    now = time.time()
    cur = conn.execute(
        "INSERT INTO tickets (project, branch, sha, source, mode, components, recreate, waivers, state, created_at,"
        " updated_at, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
        (P.name, branch, sha, str(source), mode, ",".join(comps), recreate, json.dumps(waivers), now, now, by),
    )
    tid = cur.lastrowid
    (home() / "logs").mkdir(exist_ok=True)
    set_ticket(conn, tid, log_path=str(home() / "logs" / f"{tid}.log"))
    if not by.startswith("requeue:"):
        supersede_older(conn, tid, branch, sha, source, mode, comps, waivers)
    spawn_preflight()
    return tid


def supersede_older(conn, tid, branch, sha, source, mode, comps, waivers):
    """A newer commit on the same branch, from the same worktree, asking for the same thing replaces a
    still-queued older ticket: running both spends a gate on code that is about to be replaced. Never a
    hand ticket asking for something else (another mode, other apps, other waivers), never one the worker
    has taken, and never in favour of a requeue."""
    olds = conn.execute(
        "SELECT * FROM tickets WHERE project = ? AND branch = ? AND source = ? AND state = 'queued' AND id < ?",
        (P.name, branch, str(source), tid),
    ).fetchall()
    for old in olds:
        same_ask = (old["mode"], old["components"], json.loads(old["waivers"])) == (mode, ",".join(comps), waivers)
        if not (same_ask or old["submitted_by"] == "auto"):
            continue
        if git("merge-base", "--is-ancestor", old["sha"], sha, cwd=source, check=False).returncode != 0:
            continue
        cur = conn.execute(
            "UPDATE tickets SET state = 'superseded', detail = ?, updated_at = ? WHERE id = ? AND state = 'queued'",
            (f"superseded by {tid} ({sha[:8]})", time.time(), old["id"]),
        )
        conn.commit()
        if cur.rowcount:
            print(f"ticket {old['id']} superseded by {tid}")


def cmd_cancel(args):
    conn = db()
    t = get_ticket(conn, args.ticket)
    if not t:
        die(f"no ticket {args.ticket}", 3)
    here = git("rev-parse", "--show-toplevel", cwd=Path.cwd(), check=False)
    who = here.stdout.strip() if here.returncode == 0 else str(Path.cwd())
    cur = conn.execute(
        "UPDATE tickets SET state = 'cancelled', detail = ?, updated_at = ? WHERE id = ? AND state IN ('queued', 'conflict', 'failed')",
        (f"cancelled from {who}: {args.reason}", time.time(), t["id"]),
    )
    conn.commit()
    if not cur.rowcount:
        die(f"ticket {t['id']} is {t['state']}; only a queued, conflict or failed ticket can be cancelled")
    print(f"ticket {t['id']} cancelled")
    return 0


def as_dict(t):
    return {k: t[k] for k in t.keys()}


def cmd_status(args):
    conn = db()
    if args.ticket:
        t = get_ticket(conn, args.ticket)
        if not t:
            die(f"no ticket {args.ticket}", 3)
        if args.json:
            print(json.dumps(as_dict(t)))
        else:
            for k, v in as_dict(t).items():
                print(f"{k}: {v}")
        return 0
    where, params = ("WHERE project = ?", (args.project,)) if args.project else ("", ())
    for t in conn.execute(f"SELECT * FROM tickets {where} ORDER BY id DESC LIMIT 30", params):
        pf = "pf✓ " if t["state"] == "queued" and t["preflight_sha"] == t["sha"] else ""
        print(f"{t['id']:>4} {t['project'][:14]:<14} {t['state']:<12} {t['mode']:<7} {t['components']:<18} "
              f"{pf}{t['branch']}  {t['detail'][:90]}")
    for h in conn.execute(f"SELECT * FROM holds {where.replace('WHERE', 'WHERE cleared_at IS NULL AND') or 'WHERE cleared_at IS NULL'}", params):
        print(f"HOLD {h['project']}/{h['component']}: {h['reason']}")
    return 0


ACTIVE = ("queued", "pushed", "merging", "pushing", "deploying")


def cmd_wait(args):
    conn = db()
    deadline = time.time() + args.timeout
    # A worker that died leaves work behind; nothing else would start one until the next submit.
    marks = ",".join("?" * len(ACTIVE))
    if conn.execute(f"SELECT 1 FROM tickets WHERE state IN ({marks}) LIMIT 1", ACTIVE).fetchone():
        spawn_preflight()
        spawn_worker()
    while True:
        t = get_ticket(conn, args.ticket)
        if not t:
            die(f"no ticket {args.ticket}", 3)
        if t["state"] in GOOD | BAD | NEEDS_PERSON | ENDED:
            print(f"ticket {t['id']}: {t['state']} {t['detail']}".rstrip())
            if t["failure"]:
                print(f"failure:\n{t['failure']}")
            if t["notes"]:
                print(f"note from the sluicy conductor (not the user):\n{t['notes']}")
            if t["state"] in ENDED:
                return 4
            return 0 if t["state"] in GOOD else 1 if t["state"] in BAD else 2
        if time.time() >= deadline:
            print(f"ticket {t['id']}: still {t['state']}")
            return 75
        time.sleep(min(2, max(0.1, deadline - time.time())))


def unblock(conn):
    conn.execute("UPDATE tickets SET state = 'pushed', detail = '' WHERE state = 'blocked' AND project = ?", (P.name,))
    conn.commit()
    spawn_worker()


def cmd_allow(args):
    if not (args.reason or "").strip():
        die("allow needs --reason")
    conn = db()
    if args.project:
        use_project(conn, args.project)
    else:
        here_project(conn)
    if args.component not in P.components:
        die(f"--component takes {', '.join(P.components)}")
    if args.target.isdigit():
        shas = [r["sha"] for r in conn.execute("SELECT sha FROM ticket_commits WHERE ticket_id = ?", (int(args.target),))]
    elif args.target == "hold":
        shas = []  # clearing the current holds is the whole allow; a later hold must still block
    elif args.target == "unstamped":
        shas = [args.target]
    else:
        shas = [git("rev-parse", f"{args.target}^{{commit}}")]
    now = time.time()
    for sha in shas:
        conn.execute("INSERT INTO allows VALUES (?, ?, ?, ?, ?)", (P.name, sha, args.component, args.reason.strip(), now))
    if args.target == "hold":
        clear_holds(conn, args.component)
    conn.commit()
    print(f"cleared the holds on {args.component}" if args.target == "hold" else f"allowed {len(shas)} for {args.component}")
    unblock(conn)
    return 0


def cmd_release(args):
    conn = db()
    t = get_ticket(conn, args.ticket)
    if not t or t["state"] != "held":
        die(f"ticket {args.ticket} is not held")
    use_project(conn, t["project"])
    set_ticket(conn, t["id"], mode="ride", state="merged", detail="released")
    unblock(conn)
    return 0


# ---- guards: all inert until the guards-on file exists


def guards_on():
    return (home() / "guards-on").exists()


def log_bypass(what, reason):
    with (home() / "bypass.log").open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what} {reason}\n")


def bypass(what):
    raw = os.environ.get("SLUICY_BYPASS")
    if raw is None or not raw.strip():
        return False
    log_bypass(what, raw.strip())
    return True


QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
READ_ONLY = r"\b(cat|less|more|grep|rg|sed|head|tail|wc|diff|git\s+(log|show|diff|blame))\b"


def refusal(command, cfg):
    """Why a session's Bash command must go through the queue instead, or None. Quoted text is blanked first:
    a `|` inside a grep pattern is not a pipe, and a branch name inside a commit message is not a push."""
    bare = QUOTED.sub("''", command)
    if "SLUICY_BYPASS" in command:
        return "that override is for a person, not a session"
    for segment in re.split(r"[|;&]+", bare):
        if re.search(r"\bgit\s+push\b", segment):
            if "--no-verify" in segment:
                return "pushes go through the gate"
            if re.search(rf"(^|[\s:+]){re.escape(cfg['repo']['branch'])}\b", segment.split("push", 1)[1]):
                return f"{cfg['repo']['branch']} moves through the queue"
        for pat in cfg["guard"]["deploy_patterns"]:
            if re.search(pat, segment) and not re.search(READ_ONLY, segment):
                if not any(re.search(a, segment) for a in cfg["guard"]["allow_args"]):
                    return "deploys go through the queue"
    return None


def guard_session_hook():
    """PreToolUse(Bash) hook. Runs on every Bash call in every repository, so it returns at once when guards
    are off or the session's repository has no `.sluicy.toml`."""
    if not guards_on():
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (data.get("tool_input") or {}).get("command", "") if isinstance(data, dict) else ""
    f = sconfig.find(data.get("cwd") or os.getcwd()) if command else None
    if not f:
        return 0
    try:
        cfg = sconfig.load(f)
    except sconfig.ConfigError:
        return 0
    why = refusal(command, cfg)
    if why:
        print(f"Refused: {why}. Use: sluicy submit --branch <branch> (--deploy <components> | --ride)", file=sys.stderr)
        return 2
    return 0


# ---- auto-ship: the Stop hook that submits a session's finished work


AUTO_OFF = "auto-ship-off"  # in home(): switches auto-ship off for every session
HOLD_FILE = "sluicy-hold"  # in a worktree's git dir: switches it off for that worktree
WIP_SUBJECT = re.compile(r"(?i)^\s*(wip\b|fixup!|squash!|amend!)")
AUTO_FAIL_CAP = 2  # consecutive failed auto tickets on a branch before it must be submitted by hand
FETCH_TIMEOUT = 20


def ship_trailer(top):
    # Any line, any paragraph: `-m "Ship: yes"` followed by a Co-Authored-By paragraph is no git trailer.
    return bool(re.search(r"(?mi)^ship:\s*yes\s*$", git("log", "-1", "--format=%B", "HEAD", cwd=top)))


def entered_by_session(data, top):
    """None when this session entered `top` with EnterWorktree, else why not. The proof is the transcript's
    last `worktree-state` record: EnterWorktree writes the worktree path and the session id there, and
    ExitWorktree writes one without a worktree. A cwd alone proves nothing (a Bash `cd` moves it)."""
    tp, sid = data.get("transcript_path"), data.get("session_id")
    if not tp or not sid:
        return "the hook had no transcript or session id"
    last = None
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"worktree-state"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "worktree-state":
                    last = rec
    except OSError:
        return "transcript unreadable"
    ws = last.get("worktreeSession") if last else None
    if not isinstance(ws, dict) or not ws.get("worktreePath"):
        return "this session did not enter it with EnterWorktree"
    if ws.get("sessionId") != sid:
        return "entered by another session"
    if Path(ws["worktreePath"]).resolve() != Path(top).resolve():
        return f"this session's worktree is {ws['worktreePath']}"
    return None


def skip_alert(branch, sha, reason):
    """A `Ship: yes` commit the hook will not submit: say so once, never silently."""
    line = f"auto-ship skipped {branch} at {sha[:8]}: {reason}"
    try:
        if line in (home() / "alerts.log").read_text(errors="replace"):
            return
    except OSError:
        pass
    alert(line)


def auto_ship(data, submitted=None):
    """Submit the session's own worktree branch when its HEAD says `Ship: yes`. Returns the note for the
    session, or None. `submitted` (a list) receives the new ticket id, so the caller can start the worker
    after the note is out.

    Every check here keeps a human decision in place: only the session's own linked worktree, only a
    committed HEAD marked for shipping, never inside a continuation this hook forced, never beside a
    ticket that is running or waits on a person, never after a failed hand-made ticket (a person chose
    its --deploy and waivers), never over a component hold, and never after AUTO_FAIL_CAP failures in a row."""
    if (home() / AUTO_OFF).exists():
        return None
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project:
        return None
    cwd = Path(data.get("cwd") or project)
    if not sconfig.find(cwd, stop="/"):
        return None  # not a sluicy repository: cheap exit before any git
    res = git("rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir", cwd=cwd, check=False)
    mine = git("rev-parse", "--show-toplevel", "--git-common-dir", cwd=Path(project), check=False)
    if res.returncode != 0 or mine.returncode != 0:
        return None
    top, gdir, common = res.stdout.strip().splitlines()
    top, gdir, common = Path(top), Path(gdir), (cwd / common).resolve()
    my_top, my_common = mine.stdout.strip().splitlines()
    # A session started in the main checkout that isolated itself with EnterWorktree keeps the main
    # checkout as CLAUDE_PROJECT_DIR; its worktree is proven its own by the transcript further down.
    entered = top.resolve() != Path(my_top).resolve()
    if entered and common != (Path(project) / my_common).resolve():
        return None  # another repo: not this session's to ship
    if gdir.resolve() == common:
        return None  # the main checkout: work there is the user's own
    if (gdir / HOLD_FILE).exists():
        return None
    ref = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=top, check=False)
    branch = ref.stdout.strip()
    if ref.returncode != 0 or branch in ("main", "master"):
        return None
    if git("status", "--porcelain", "--untracked-files=no", cwd=top):
        return None  # mid-edit: ship only what is committed and settled
    sha = git("rev-parse", "HEAD", cwd=top)
    if not ship_trailer(top) or WIP_SUBJECT.match(git("log", "-1", "--format=%s", "HEAD", cwd=top)):
        return None
    cfg_file = sconfig.find(top, stop="/")
    if not cfg_file:
        return None
    home().mkdir(parents=True, exist_ok=True)
    with (home() / "autoship.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conn = db()
        name = sconfig.load(cfg_file)["project"]
        if not project_row(conn, name):
            skip_alert(branch, sha, f"project {name} is not registered (run `sluicy init`)")
            return None
        use_project(conn, name)
        if conn.execute("SELECT 1 FROM ticket_commits WHERE sha = ? UNION SELECT 1 FROM tickets WHERE sha = ?", (sha, sha)).fetchone():
            return None  # already submitted (or failed: the session was told, and a fix is a new commit)
        if entered:
            refused = entered_by_session(data, top)
            if refused:
                skip_alert(branch, sha, f"not this session's worktree ({refused})")
                return None  # another session's worktree, or one nobody entered: not this session's to ship
        quiet = bool(data.get("stop_hook_active"))
        recent = conn.execute(
            "SELECT * FROM tickets WHERE project = ? AND branch = ? AND source = ? AND state NOT IN ('superseded', 'cancelled')"
            " ORDER BY id DESC LIMIT ?",
            (P.name, branch, str(top), AUTO_FAIL_CAP),
        ).fetchall()
        if recent:
            last = recent[0]
            if last["state"] in set(ACTIVE) | NEEDS_PERSON | {"rolled_back"}:
                return None if quiet else (
                    f"sluicy auto-ship: ticket {last['id']} for {branch} is {last['state']}, so {sha[:8]} was not "
                    f"submitted. Check it with `sluicy status {last['id']}`; a blocked, "
                    "held or unknown ticket needs a person. Once it is done, the next stop submits the newer commit.")
            if last["submitted_by"] != "auto" and last["state"] in BAD:
                skip_alert(branch, sha, f"hand ticket {last['id']} on this branch is {last['state']}; submit by hand")
                return None  # a hand ticket failed with apps and waivers a person chose: they resubmit
            if len(recent) == AUTO_FAIL_CAP and all(t["submitted_by"] == "auto" and t["state"] in BAD for t in recent):
                return None if quiet else (
                    f"sluicy auto-ship: the last {AUTO_FAIL_CAP} auto tickets for {branch} failed, so {sha[:8]} "
                    "was not submitted. Tell the user, and submit by hand once the cause is understood.")
        if quiet:
            return None  # inside a continuation this hook forced: the next real turn end submits
        fetched = git("fetch", "-q", P.remote, P.branch, cwd=top, check=False, timeout=FETCH_TIMEOUT,
                      extra_env={"GIT_TERMINAL_PROMPT": "0"})
        if fetched.returncode != 0:
            return None  # a stale main would widen the deploy set
        if not git("rev-list", "--no-merges", f"{P.main_ref}..HEAD", cwd=top):
            return None  # nothing but merges of main: nothing to ship
        files = git("diff", "--name-only", f"{P.main_ref}...HEAD", cwd=top).splitlines()
        comps = [c for c, v in P.components.items() if any(f.startswith(v["path"] + "/") for f in files)]
        if P.cfg["deploy"]["kind"] != "command":
            comps = []  # the push is the deploy
        held = [(c, h["reason"]) for c in comps for h in [hold_for(conn, c)] if h]
        if held:
            what = "; ".join(f"{c}: {reason}" for c, reason in held)
            return (f"sluicy auto-ship: {sha[:8]} was not submitted because a component it touches is held ({what}). "
                    "A hold waits for a person: tell the user. Do not work around it.")
        mode = "deploy" if comps else "ride"
        tid = submit_ticket(conn, branch, sha, top, mode, comps, 0, {}, "auto")
    if submitted is not None:
        submitted.append(tid)
    what = (f"deploys {', '.join(comps)}" if comps else "merges without a deploy (no component changed)"
            if P.cfg["deploy"]["kind"] == "command" else "merges; the push is the deploy")
    return (
        f"sluicy auto-ship: submitted ticket {tid}, {branch} at {sha[:8]}; it {what} once the full pre-push gate "
        f"passes. Run `sluicy wait {tid} --timeout 540` in the background (exit 75 = "
        "still running, call again; 0 = live, 1 = failed, 2 = needs a person) and report the outcome to the user. "
        f"On a failure, read the log named by `sluicy status {tid}`, fix it on this branch "
        "and commit with a `Ship: yes` trailer once the fix is done. Never push the main branch or deploy by hand."
    )


def log_auto_error(e):
    try:
        home().mkdir(parents=True, exist_ok=True)
        with (home() / "alerts.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} auto-ship error: {e!r}\n")
    except OSError:
        pass


def cmd_autoship(_args):
    """Stop hook. Never fails the session: errors are logged to alerts.log and the session stops as usual."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    submitted = []
    try:
        note = auto_ship(data if isinstance(data, dict) else {}, submitted)
    except Exception as e:  # noqa: BLE001 — a broken hook must not wedge every session
        log_auto_error(e)
        return 0
    if note:
        print(json.dumps({"decision": "block", "reason": note}), flush=True)
    if submitted:
        try:
            spawn_worker()
        except Exception as e:  # noqa: BLE001 — the ticket is queued; the session's `wait` starts a worker
            log_auto_error(e)
    return 0


def cmd_guard(args):
    if args.kind == "hook":
        return guard_session_hook()
    if not guards_on():
        return 0
    if args.kind == "rollback":
        if bypass("rollback"):
            return 0
        print("sluicy: state a reason for this rollback (SLUICY_BYPASS=<reason>)", file=sys.stderr)
        return 1
    if bypass(f"{args.kind} {args.sha or ''}".strip()):
        return 0
    tid = os.environ.get("SLUICY_TICKET", "")
    t = get_ticket(db(), int(tid)) if tid.isdigit() else None
    if args.kind == "push" and t and t["state"] == "pushing" and t["merged_sha"] == args.sha:
        return 0
    if args.kind == "deploy" and t and t["state"] == "deploying":
        return 0
    print(f"sluicy: refused, this {args.kind} has no ticket. Use: sluicy submit", file=sys.stderr)
    return 1


def cmd_config(args):
    """`sluicy config check`: validate the repository's `.sluicy.toml` and print what sluicy will do."""
    f = sconfig.find(Path.cwd(), stop="/")
    if not f:
        die(f"no {sconfig.FILE} here or above")
    try:
        cfg = sconfig.load(f)
    except sconfig.ConfigError as err:
        die(str(err))
    if args.json:
        print(json.dumps(cfg, indent=1))
        return 0
    d = cfg["deploy"]
    print(f"{f}: ok")
    print(f"project {cfg['project']}, main {cfg['repo']['remote']}/{cfg['repo']['branch']}")
    print("components: " + (", ".join(f"{n} ({c['path']}/)" for n, c in cfg["components"].items()) or "none"))
    print("deploy: " + ("the push is the deploy" if d["kind"] == "none" else " ".join(d["command"])))
    print("stamps: " + (" ".join(cfg["stamps"]["command"]) or "none (sluicy remembers what it deployed)"))
    print("waivers: " + (", ".join(cfg["gate"]["waivers"]) or "none"))
    print("preflight: " + (" ".join(cfg["preflight"]["command"]) or "none"))
    print(f"plan review: {'on' if cfg['plan_review']['enabled'] else 'off'}")
    return 0


def cmd_paths(args):
    """Where sluicy keeps things, for the Deck and for scripts."""
    conn = db()
    projects = [{"name": r["name"], "source": r["source"], "checkout": r["checkout"]}
                for r in conn.execute("SELECT * FROM projects ORDER BY name")]
    out = {"home": str(home()), "db": str(os.environ.get("SLUICY_DB") or home() / "sluicy.db"),
           "alerts": str(home() / "alerts.log"), "cleanup": str(home() / "cleanup.json"),
           "gate_logs": str(Path(os.environ.get("TMPDIR", "/tmp")) / "sluicy-gate-logs"),
           "projects": projects}
    if args.json:
        print(json.dumps(out))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    return 0


def cmd_stamps(args):
    """What production runs per component (the project's stamps command) and, when the project names one,
    who holds its deploy lease ([deck] lease_command, one line per fact). For the Deck."""
    here_project()
    out = {"project": P.name, "ok": None, "stamps": {}, "lease": [], "components": list(P.components),
           "waivers": sorted(P.waivers), "configured": bool(P.cfg["stamps"]["command"])}
    if P.cfg["stamps"]["command"] and P.components:
        try:
            P.checkout = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
            out["stamps"] = {k: (v or "none") for k, v in read_stamps(list(P.components)).items()}
            out["ok"] = True
        except (RuntimeError, subprocess.TimeoutExpired):
            out["ok"] = False
    lease = P.cfg["deck"]["lease_command"]
    if lease:
        try:
            res = subprocess.run(lease, cwd=Path.cwd(), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            out["lease"] = [line for line in res.stdout.splitlines() if line.strip()][:10]
        except (OSError, subprocess.TimeoutExpired):
            out["lease"] = ["unknown"]
    print(json.dumps(out) if args.json else "\n".join(f"{k}={v}" for k, v in out["stamps"].items()))
    return 0


def cmd_scope(args):
    """`sluicy scope -- <command>`: run a command in the same memory-capped, swapless scope the worker gives a
    gate, for a gate run by hand. Without a systemd user session it runs uncapped, and says so."""
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        die("usage: sluicy scope -- <command> [args]")
    reserve = float(os.environ.get("SLUICY_GATE_RESERVE_GB", "4"))
    f = sconfig.find(Path.cwd(), stop="/")
    if f:
        with contextlib.suppress(sconfig.ConfigError):
            reserve = float(sconfig.load(f)["gate"]["mem_reserve_gb"])
    mem = plat.gate_mem_max_gb(reserve)
    argv = plat.scoped(cmd, f"sluicy-gate-manual-{os.getpid()}", mem)
    if argv is cmd:
        print("sluicy: no systemd user scope available; running uncapped", file=sys.stderr)
    env = {**os.environ, "SLUICY_GATE_SCOPED": "1"}
    os.execvpe(argv[0], argv, env)


def cmd_migrate_shipyard(args):
    """Copy an older Shipyard state directory (one project, one queue) into sluicy as `--project`. The old
    directory gets a MIGRATED marker and a read-only database, and is otherwise left as it was, so going back
    is removing the marker. Refuses while the old queue has active work, or when sluicy already has tickets."""
    src = Path(args.source).expanduser()
    old_db = src / "shipyard.db"
    if not old_db.is_file():
        die(f"no shipyard.db in {src}")
    if (src / "MIGRATED").exists():
        die(f"{src} was already migrated ({(src / 'MIGRATED').read_text().strip()})")
    old = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    marks = ",".join("?" * len(ACTIVE))
    busy = old.execute(f"SELECT id, state FROM tickets WHERE state IN ({marks})", ACTIVE).fetchall()
    if busy:
        die("the old queue still has active tickets: " + ", ".join(f"{r['id']} {r['state']}" for r in busy))
    conn = db()
    if conn.execute("SELECT 1 FROM tickets LIMIT 1").fetchone():
        die("sluicy already has tickets; migrate into a fresh state directory")
    name = args.project
    if not sconfig.NAME.match(name):
        die("--project: a short name (letters, digits, . _ -)")
    (home() / "logs").mkdir(parents=True, exist_ok=True)
    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
    n = 0
    for t in old.execute("SELECT * FROM tickets ORDER BY id"):
        row = {k: t[k] for k in t.keys() if k in new_cols}
        row["project"] = name
        if row.get("log_path"):
            row["log_path"] = str(home() / "logs" / Path(row["log_path"]).name)
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO tickets ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        n += 1
    conn.executemany("INSERT OR IGNORE INTO ticket_commits VALUES (?, ?)",
                     [(r["sha"], r["ticket_id"]) for r in old.execute("SELECT * FROM ticket_commits")])
    conn.executemany("INSERT INTO allows VALUES (?, ?, ?, ?, ?)",
                     [(name, r["sha"], r["component"], r["reason"], r["created_at"]) for r in old.execute("SELECT * FROM allows")])
    conn.executemany("INSERT INTO holds VALUES (?, ?, ?, ?, ?)",
                     [(name, r["component"], r["reason"], r["created_at"], r["cleared_at"]) for r in old.execute("SELECT * FROM holds")])
    conn.executemany("INSERT OR REPLACE INTO live VALUES (?, ?, ?)",
                     [(name, r["component"], r["sha"]) for r in old.execute("SELECT * FROM live")])
    conn.executemany("INSERT INTO debts (id, ticket_id, kind, detail, created_at, done_at) VALUES (?, ?, ?, ?, ?, ?)",
                     [tuple(r) for r in old.execute("SELECT id, ticket_id, kind, detail, created_at, done_at FROM debts")])
    conn.commit()
    old.close()
    copied = 0
    for f in sorted((src / "logs").glob("*")) if (src / "logs").is_dir() else []:
        dst = home() / "logs" / f.name
        if f.is_file() and not dst.exists():
            shutil.copy2(f, dst)
            copied += 1
    for name_ in ("alerts.log", "bypass.log", "conduct.json", "guards-on"):
        if (src / name_).is_file() and not (home() / name_).exists():
            shutil.copy2(src / name_, home() / name_)  # copied, never moved: older hooks still read the old dir
    (src / "MIGRATED").write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} to {home()} as project {name}\n")
    old_db.chmod(0o444)
    print(f"migrated {n} ticket(s) and {copied} log(s) into {home()} as project {name}; {src} marked MIGRATED")
    return 0


def main():
    p = argparse.ArgumentParser(prog="sluicy", description="One gate at a time: a release queue for many concurrent sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="queue a branch")
    s.add_argument("--branch", required=True)
    mode = s.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deploy", help="comma-separated components to deploy after the merge")
    mode.add_argument("--ride", action="store_true", help="merge only")
    mode.add_argument("--no-deploy-until-released", action="store_true")
    s.add_argument("--recreate", action="store_true")
    s.add_argument("--waive", action="append")
    s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("status", help="tickets and holds")
    s.add_argument("ticket", nargs="?", type=int)
    s.add_argument("--json", action="store_true")
    s.add_argument("--project")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("wait", help="wait for a ticket")
    s.add_argument("ticket", type=int)
    s.add_argument("--timeout", type=int, default=540)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("allow", help="let a commit, an unstamped build or a held component deploy")
    s.add_argument("target")
    s.add_argument("--component", required=True)
    s.add_argument("--reason")
    s.add_argument("--project")
    s.set_defaults(fn=cmd_allow)
    s = sub.add_parser("release", help="deploy a held ticket's merge with the next deploy")
    s.add_argument("ticket", type=int)
    s.set_defaults(fn=cmd_release)
    s = sub.add_parser("run", help="the worker (started for you)")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("init", help="register this repository and create its checkout")
    s.add_argument("--no-install", action="store_true")
    s.add_argument("--checkout", help="where the permanent checkout lives (default under ~/.local/share/sluicy)")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("guard", help="hooks: refuse pushes and deploys that bypass the queue")
    s.add_argument("kind", choices=["push", "deploy", "rollback", "hook"])
    s.add_argument("sha", nargs="?")
    s.set_defaults(fn=cmd_guard)
    s = sub.add_parser("autoship", help="Stop hook: submit a finished `Ship: yes` commit")
    s.set_defaults(fn=cmd_autoship)
    s = sub.add_parser("preflight", help="quick checks on queued tickets (started for you)")
    s.set_defaults(fn=cmd_preflight)
    s = sub.add_parser("requeue", help="send a failed ticket round again, on the record")
    s.add_argument("ticket", type=int)
    s.add_argument("--waive", action="append")
    s.add_argument("--sha", help="a fix commit on the ticket's own branch that contains its commit")
    s.add_argument("--by", required=True)
    s.add_argument("--debt", action="append", help='KIND="what is owed", e.g. visuals="recapture the screenshots"')
    s.set_defaults(fn=cmd_requeue)
    s = sub.add_parser("conduct", help="one conductor pass (a timer runs it)")
    s.set_defaults(fn=cmd_conduct)
    s = sub.add_parser("cancel")
    s.add_argument("ticket", type=int)
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("debts")
    s.add_argument("--done", type=int)
    s.set_defaults(fn=cmd_debts)
    s = sub.add_parser("config", help="check this repository's .sluicy.toml")
    s.add_argument("action", choices=["check"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("paths", help="where sluicy keeps things")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_paths)
    s = sub.add_parser("timer", help="install the conductor's systemd user timer (install.sh runs it)")
    s.set_defaults(fn=lambda _a: install_conduct_timer() or 0)
    s = sub.add_parser("stamps", help="what production runs, per component (for the Deck)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stamps)
    s = sub.add_parser("scope", help="run a command in the gate's memory-capped scope")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_scope)
    s = sub.add_parser("migrate-shipyard", help="import an older Shipyard state directory")
    s.add_argument("source")
    s.add_argument("--project", required=True)
    s.set_defaults(fn=cmd_migrate_shipyard)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
