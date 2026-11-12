"""Everything sluicy needs from the operating system, in one place: systemd user units and scopes, memory,
desktop notifications. Linux (and WSL2 with systemd) only; a port to another OS replaces this module.
"""
import os
import re
import shutil
import subprocess

GATE_SLICE = "sluicy-gate.slice"


def notify(title, message):
    if shutil.which("notify-send") and not os.environ.get("SLUICY_NO_NOTIFY"):
        subprocess.run(["notify-send", title, message], check=False, capture_output=True)


def start_unit(unit, argv, env, cwd, out):
    """Start `argv` as its own transient systemd user unit, so it outlives the shell that started it. False
    when systemd cannot (no user session); True when it started or one by that name already runs."""
    if not shutil.which("systemd-run"):
        return False
    setenv = [f"--setenv={k}={v}" for k, v in env.items() if "\n" not in v and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)]
    res = subprocess.run(
        ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}", f"--working-directory={cwd}",
         f"--property=StandardOutput=append:{out}", f"--property=StandardError=append:{out}", *setenv, *argv],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode == 0:
        return True
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=30).returncode == 0


def mem_total_gb(meminfo="/proc/meminfo"):
    with open(meminfo) as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1048576
    return 0.0


def gate_mem_max_gb(reserve_gb, meminfo="/proc/meminfo"):
    """The gate's MemoryMax: three quarters of RAM, and never closer than `reserve_gb` to all of it, so an
    overrun is OOM-killed inside the gate rather than freezing the desktop. SLUICY_GATE_MEM_MAX (in GB) wins."""
    forced = os.environ.get("SLUICY_GATE_MEM_MAX")
    if forced:
        return float(forced.rstrip("Gg"))
    total = mem_total_gb(meminfo)
    return max(1.0, min(total * 0.75, total - reserve_gb))


def scope_available():
    if os.environ.get("SLUICY_NO_SCOPE") or not shutil.which("systemd-run"):
        return False
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scoped(argv, unit, mem_max_gb):
    """`argv` wrapped in a memory-capped, swapless systemd scope in sluicy-gate.slice (when that slice is
    installed), or `argv` unchanged when no scope is possible. The scope execs `argv`, so its pid is argv's."""
    if not scope_available():
        return argv
    slice_args = []
    if subprocess.run(["systemctl", "--user", "cat", GATE_SLICE], capture_output=True).returncode == 0:
        slice_args = [f"--slice={GATE_SLICE}"]
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *slice_args, f"--unit={unit}",
            "-p", f"MemoryMax={int(mem_max_gb * 1024)}M", "-p", "MemorySwapMax=0", "--nice=10", *argv]


def stop_units(pattern):
    """Stop every user unit matching `pattern` (a glob). Returns (rc, stderr)."""
    if not shutil.which("systemctl"):
        return None, ""
    res = subprocess.run(["systemctl", "--user", "stop", pattern], capture_output=True, text=True, timeout=60)
    return res.returncode, res.stderr.strip()


def oomd_killed(prefix, since):
    """True when systemd-oomd killed a unit whose name starts with `prefix` since `since` (epoch seconds)."""
    if not shutil.which("journalctl"):
        return False
    res = subprocess.run(["journalctl", "-u", "systemd-oomd", "--since", f"@{int(since)}", "-o", "cat", "--no-pager"],
                         capture_output=True, text=True, timeout=30)
    return any("Killed" in line and prefix in line for line in res.stdout.splitlines())
