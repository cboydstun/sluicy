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
import shutil
import subprocess
from pathlib import Path

GB = 1024 ** 3
LABEL = "com.sluicy.conduct"
# kern.memorystatus_vm_pressure_level -> PSI-shaped percentages. Warn stops new gates from starting
# (some > SLUICY_MAX_PSI_SOME, 10) without braking a running one; critical brakes (full > 5).
PRESSURE = {1: (0.0, 0.0), 2: (20.0, 0.0), 4: (60.0, 20.0)}


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True).stdout


def _sysctl(name, run):
    return int(run(["sysctl", "-n", name]).strip())


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
    total = mem_total_gb(run=run)
    return max(1.0, min(total * 0.75, total - reserve_gb))


def scope_available():
    return False


def scoped(argv, unit, mem_max_gb):
    return argv


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
    for line in run(["ps", "-axo", "pid=,pgid=,rss=,command="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, pgrp, rss = int(parts[0]), int(parts[1]), int(parts[2]) * 1024
        except ValueError:
            continue
        procs[pid] = [pgrp, rss, "", "", parts[3].strip() if len(parts) > 3 else ""]
    try:
        cwds = _lsof_cwds(run(["lsof", "-nP", "-a", "-d", "cwd", "-Fpn"]))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        cwds = {}
    roots = [str(r).rstrip("/") for r in ours if r]
    for pid, p in procs.items():
        p[3] = cwds.get(pid, "")
        if p[3] and any(p[3] == r or p[3].startswith(r + "/") for r in roots):
            p[2] = "sluicy-gate"
    return {pid: tuple(p) for pid, p in procs.items()}


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


def install_timer(argv, agents_dir, logs_dir, run=_run, uid=None):
    """`argv` every 5 minutes as a launchd user agent. Returns the plist path."""
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
        "Nice": 19,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "StandardOutPath": str(logs_dir / "conduct.out"),
        "StandardErrorPath": str(logs_dir / "conduct.out"),
    }))
    _quiet(run, ["launchctl", "bootout", f"gui/{uid}/{LABEL}"])  # not loaded yet is fine
    run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
    return plist


def remove_timer(agents_dir, run=_run, uid=None):
    uid = os.getuid() if uid is None else uid
    _quiet(run, ["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    (Path(agents_dir) / f"{LABEL}.plist").unlink(missing_ok=True)


def _quiet(run, argv):
    try:
        run(argv)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
