"""The macOS side of sluicy's OS layer (platform_linux is the other). macOS has no systemd, cgroups or /proc,
so:

- workers are plain detached processes (start_unit says no and sluicy falls back to its own spawn);
- a gate runs in its own process group, uncapped. The memory governor (read_memory + `stalling`) is the
  brake: it stops the whole group when the machine runs low, as it does on Linux before oomd would;
- memory comes from the kernel's own figures: `kern.memorystatus_level` (the free percentage Activity
  Monitor and `memory_pressure` report) and `kern.memorystatus_vm_pressure_level` (1 normal, 2 warn,
  4 critical), mapped onto the PSI percentages the Linux thresholds use;
- processes come from `ps`, their working directories from one `lsof` call;
- the conductor timer is a launchd agent.

Every reader takes `run` (argv -> stdout) so the tests can feed it canned output.
"""
import os
import plistlib
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

GB = 1024 ** 3
LABEL = "com.sluicy.conduct"
# kern.memorystatus_vm_pressure_level -> PSI-shaped percentages. Warn stops new gates from starting
# (some > SLUICY_MAX_PSI_SOME, 10) without braking a running one; critical brakes (full > 5).
PRESSURE = {1: (0.0, 0.0), 2: (20.0, 0.0), 4: (60.0, 20.0)}


# Absolute paths: sluicy's worker runs with a cleaned env whose PATH need not have /usr/sbin.
TOOLS = {"sysctl": "/usr/sbin/sysctl", "lsof": "/usr/sbin/lsof", "ps": "/bin/ps", "launchctl": "/bin/launchctl",
         "nice": "/usr/bin/nice"}


def _tool(name):
    path = TOOLS.get(name, name)
    return path if os.path.exists(path) else (shutil.which(name) or name)


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True).stdout


def _sysctl(name, run):
    return int(run([_tool("sysctl"), "-n", name]).strip())


def notify(title, message):
    if shutil.which("osascript") and not os.environ.get("SLUICY_NO_NOTIFY"):
        script = f"display notification {_applescript(message)} with title {_applescript(title)}"
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _applescript(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def start_unit(unit, argv, env, cwd, out):
    """No service manager to hand a transient job to: sluicy starts it as a detached process itself."""
    return False


def mem_total_gb(meminfo=None, run=_run):
    return _sysctl("hw.memsize", run) / GB


def gate_mem_max_gb(reserve_gb, meminfo=None, run=_run):
    """Same rule as Linux (three quarters of RAM, never closer than `reserve_gb` to all of it). Nothing
    enforces it on macOS; it is reported, and SLUICY_GATE_MEM_MAX still wins."""
    forced = os.environ.get("SLUICY_GATE_MEM_MAX")
    if forced:
        return float(forced.rstrip("Gg"))
    try:
        total = mem_total_gb(run=run)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Only reported on macOS (nothing enforces it), so a failing sysctl must not fail the push.
        return 0.0
    return max(1.0, min(total * 0.75, total - reserve_gb))


def scope_available():
    return False


def scoped(argv, unit, mem_max_gb):
    """No scope to cap it: the gate runs niced, as the Linux scope runs it (--nice=10)."""
    return [_tool("nice"), "-n", "10", *argv]


def stop_units(pattern):
    """No units: sluicy stops a gate by killing its process group."""
    return None, ""


def oomd_killed(prefix, since):
    return False


def read_memory(run=_run):
    """{'avail_gb', 'some', 'full'} in the shape of the Linux reader."""
    total = _sysctl("hw.memsize", run)
    free_pct = _sysctl("kern.memorystatus_level", run)
    try:
        level = _sysctl("kern.memorystatus_vm_pressure_level", run)
    except (OSError, ValueError, subprocess.CalledProcessError):
        level = 1
    some, full = PRESSURE.get(level, PRESSURE[4] if level > 4 else PRESSURE[1])
    return {"avail_gb": total * free_pct / 100 / GB, "some": some, "full": full}


def read_proc(run=_run, ours=()):
    """{pid: (pgrp, rss_bytes, tag, cwd, cmd)}, the shape of the Linux /proc reader. There are no cgroups,
    so `tag` is "sluicy-gate" for a process working under one of `ours` (sluicy's checkouts), else ""."""
    procs = {}
    for line in run([_tool("ps"), "-axo", "pid=,pgid=,rss=,command="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, pgrp, rss = int(parts[0]), int(parts[1]), int(parts[2]) * 1024
        except ValueError:
            continue
        procs[pid] = [pgrp, rss, "", "", parts[3].strip() if len(parts) > 3 else ""]
    # This user's processes only (the only ones sluicy may signal). lsof exits 1 when it could not inspect
    # some process even though the rest of its output is complete, so a non-zero exit keeps what it printed.
    cwds, _err, _note = _read_cwds(run)
    # lsof reports resolved paths (/private/tmp, a symlinked ~/coding resolved), so compare resolved roots.
    roots = [os.path.realpath(str(r)).rstrip("/") for r in ours if r]
    for pid, p in procs.items():
        p[3] = cwds.get(pid, "")
        if p[3] and any(p[3] == r or p[3].startswith(r + "/") for r in roots):
            p[2] = "sluicy-gate"
    return {pid: tuple(p) for pid, p in procs.items()}


def _read_cwds(run):
    """({pid: cwd}, error or None) for this user's processes (the only ones sluicy may signal). lsof exits 1
    when it could not inspect some process even though the rest of its output is complete, so a non-zero
    exit keeps what it printed; a non-zero exit that printed nothing is an error."""
    argv = [_tool("lsof"), "-nP", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"]
    try:
        return _lsof_cwds(run(argv)), None, None
    except subprocess.CalledProcessError as e:
        cwds = _lsof_cwds(e.stdout or "")
        if not cwds:
            return {}, f"lsof exited {e.returncode} with no output", None
        return cwds, None, f"lsof exited {e.returncode}: some processes could not be inspected"
    except (OSError, subprocess.SubprocessError) as e:
        return {}, f"lsof: {e}", None


def _lsof_cwds(out):
    cwds, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def install_timer(argv, agents_dir, logs_dir, run=_run, uid=None, path=None):
    """`argv` every 5 minutes as a launchd user agent. Returns the plist path. `path` (default: this shell's
    PATH) is written into the agent: launchd's own is /usr/bin:/bin:/usr/sbin:/sbin, and the conductor and
    every worker it starts would otherwise miss Homebrew's git, node and npm and ~/.local/bin."""
    path = os.environ.get("PATH", "") if path is None else path
    uid = os.getuid() if uid is None else uid
    agents_dir, logs_dir = Path(agents_dir), Path(logs_dir)
    agents_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    plist = agents_dir / f"{LABEL}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(a) for a in argv],
        "StartInterval": 300,
        "RunAtLoad": False,
        # No Nice, LowPriorityIO or Background ProcessType: a worker the conductor starts is a plain
        # child (no transient unit, as on Linux), inherits them and cannot drop them, so the whole gate
        # would run at nice 19 on throttled IO. The conduct pass itself is a few cheap reads.
        "ProcessType": "Standard",
        **({"EnvironmentVariables": {"PATH": path}} if path else {}),
        "StandardOutPath": str(logs_dir / "conduct.out"),
        "StandardErrorPath": str(logs_dir / "conduct.out"),
    }))
    _quiet(run, [_tool("launchctl"), "bootout", f"gui/{uid}/{LABEL}"])  # not loaded yet is fine
    try:
        run([_tool("launchctl"), "bootstrap", f"gui/{uid}", str(plist)])
    except subprocess.SubprocessError:
        # launchd can refuse (5: Input/output error) while the job it just booted out is still unloading.
        time.sleep(2)
        run([_tool("launchctl"), "bootstrap", f"gui/{uid}", str(plist)])
    return plist


def stable_python(executable, candidates=("/opt/homebrew/bin/python3", "/usr/local/bin/python3")):
    """The interpreter for the launch agent: Homebrew's unversioned `python3` link when it is the same
    interpreter as `executable`, so a Homebrew Python upgrade (which removes python@3.N) does not
    leave the agent pointing at a deleted binary. Otherwise `executable` itself."""
    try:
        real = os.path.realpath(executable)
        for cand in candidates:
            if os.path.exists(cand) and os.path.realpath(cand) == real:
                return cand
    except OSError:
        pass
    return executable


def _etime(text):
    """Seconds from ps's etime, `[[dd-]hh:]mm:ss`; None when it does not parse."""
    try:
        days, _, rest = text.rpartition("-")
        parts = [int(x) for x in rest.split(":")]
        if not 1 <= len(parts) <= 3:
            return None
        secs = 0
        for x in parts:
            secs = secs * 60 + x
        return secs + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def _snapshot(run):
    """{pid: (ppid, tty, start_epoch or None, command)} from one ps read; "??" is no controlling terminal."""
    now, out = time.time(), {}
    for line in run([_tool("ps"), "-axo", "pid=,ppid=,tty=,etime=,command="]).splitlines():
        parts = line.split(None, 4)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        age = _etime(parts[3]) if len(parts) > 3 else None
        out[int(parts[0])] = (int(parts[1]), parts[2], None if age is None else now - age,
                              parts[4].strip() if len(parts) > 4 else "")
    return out


# Never a gate's: an app bundle (an editor, its helpers) or a Claude Code session, and what they start.
_PERSON = re.compile(r"^/(System/)?Applications/[^\n]*?\.app/Contents/|^(\S*/)?claude( |$)")


def _verify(run, starts):
    """The pids of `starts` ({pid: start_epoch}) still running with the same start (±2 s; ps's etime is in
    whole seconds), read again just before signalling, so a pid reused since the snapshot is left alone.
    Raises OSError/SubprocessError when ps cannot be read at all."""
    if not starts:
        return []
    argv = [_tool("ps"), "-o", "pid=,etime=", "-p", ",".join(map(str, sorted(starts)))]
    try:
        out = run(argv)
    except subprocess.CalledProcessError as e:
        out = e.stdout or ""  # ps exits 1 when none of the pids is left
    now, same = time.time(), []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid, age = int(parts[0]), _etime(parts[1])
        if pid in starts and age is not None and abs((now - age) - starts[pid]) <= 2:
            same.append(pid)
    return sorted(same)


def kill_in(root, log, exclude=(), run=_run, wait=10.0, since=None):
    """Stop what a gate left running under `root` (the ticket's checkout): there is no scope to stop on
    macOS, and a gate's dev servers, test databases or emulators can leave its process group. Run it
    after the gate's own group is gone.

    A process working under `root` is stopped only when it, and every ancestor up to launchd, is new: off
    a terminal, of known age, and (given `since`, epoch seconds the gate started) started after the gate.
    An app bundle's process (an editor and its helpers) or a Claude Code session counts as old whatever its
    age. So a person's shell, an editor's language server and anything an editor or Claude session started
    mid-push are spared and logged, while this gate's leftovers, which launchd adopts once the gate is gone,
    still go. Leftovers older than `since` (an earlier gate's) are spared too: the safe direction.
    Still not told apart: a process with no terminal, started during the push by launchd itself, that is
    neither an app bundle nor `claude`, working in sluicy's own checkout. The ancestor walk stops at pid 1, an excluded pid, a pid the
    snapshot does not hold, or a cycle; an ancestor's working directory does not matter.

    One ps snapshot (with lsof for working directories) decides, and each pid is read again just before SIGTERM and before SIGKILL and skipped
    unless its start time still matches. What is left: the milliseconds between that read and the signal,
    and a pid reused within 2 s of the original's start. When ps or lsof cannot be read, nothing is stopped
    and the log says so. Returns the pids it signalled."""
    root = os.path.realpath(str(root)).rstrip("/")
    exclude = set(exclude)
    try:
        snap = _snapshot(run)
    except (OSError, subprocess.SubprocessError) as e:
        snap, err = None, f"ps: {e}"
    note = None
    if snap is not None:
        cwds, err, note = _read_cwds(run)
    if err:
        log.write(f"sluicy: cannot check processes in {root} ({err}); nothing stopped\n")
        log.flush()
        return []
    if note:
        log.write(f"sluicy: {note}; checking the rest in {root}\n")
    inside = sorted(pid for pid, cwd in cwds.items()
                    if (cwd == root or cwd.startswith(root + "/")) and pid > 1 and pid not in exclude)
    cutoff = None if since is None else since - 2  # 2 s: ps rounds etime down to the second

    def old(pid):
        _ppid, tty, start, cmd = snap.get(pid, (0, "??", None, ""))
        if tty not in ("??", "?", "-") or _PERSON.search(cmd):
            return True
        return start is None or (cutoff is not None and start < cutoff)

    def spared(pid):
        if old(pid):
            return True
        seen, p = {pid}, snap[pid][0]
        while p > 1 and p not in exclude and p in snap and p not in seen:
            if old(p):
                return True
            seen.add(p)
            p = snap[p][0]
        return False

    kept = [pid for pid in inside if spared(pid)]
    left = [pid for pid in inside if pid not in kept]
    if kept:
        log.write(f"sluicy: leaving {len(kept)} process(es) in {root} (on a terminal, older than the gate, "
                  f"or started by one): {' '.join(map(str, kept))}\n")
    if not left:
        log.flush()
        return []
    starts = {pid: snap[pid][2] for pid in left}
    log.write(f"sluicy: stopping {len(left)} process(es) left in {root}: {' '.join(map(str, left))}\n")
    log.flush()
    signalled = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            targets = _verify(run, {p: starts[p] for p in left})
        except (OSError, subprocess.SubprocessError) as e:
            log.write(f"sluicy: cannot re-check processes in {root} (ps: {e}); not signalling them\n")
            log.flush()
            break
        alive = []
        for pid in targets:
            try:
                os.kill(pid, sig)
                alive.append(pid)
                signalled.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and any(_alive(p) for p in alive):
                time.sleep(0.2)
            # Only what we signalled and is still there: never SIGKILL a pid that exited (and may be reused).
            left = [p for p in alive if _alive(p)]
            if not left:
                break
    return sorted(set(signalled))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def remove_timer(agents_dir, run=_run, uid=None):
    uid = os.getuid() if uid is None else uid
    _quiet(run, [_tool("launchctl"), "bootout", f"gui/{uid}/{LABEL}"])
    (Path(agents_dir) / f"{LABEL}.plist").unlink(missing_ok=True)


def _quiet(run, argv):
    try:
        run(argv)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
