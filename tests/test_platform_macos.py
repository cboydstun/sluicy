"""platform_macos: the macOS side of sluicy's OS layer, tested on any OS with faked command output.

    python3 tests/test_platform_macos.py
"""
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sluicy"))
import platform_macos as mac  # noqa: E402

GB = 1024 ** 3


def runner(outputs):
    """A fake `run(argv) -> stdout`: the first output whose key is a prefix of the command."""
    calls = []

    def run(argv):
        calls.append(argv)
        cmd = " ".join([Path(argv[0]).name, *argv[1:]])
        for key, out in outputs.items():
            if cmd.startswith(key):
                if isinstance(out, Exception):
                    raise out
                return out
        raise AssertionError(f"unexpected command: {cmd}")

    run.calls = calls
    return run


class Memory(unittest.TestCase):
    def test_total_comes_from_hw_memsize(self):
        run = runner({"sysctl -n hw.memsize": f"{32 * GB}\n"})
        self.assertEqual(mac.mem_total_gb(run=run), 32.0)

    def test_gate_cap_is_three_quarters_but_keeps_the_reserve(self):
        run = runner({"sysctl -n hw.memsize": f"{32 * GB}\n"})
        self.assertEqual(mac.gate_mem_max_gb(14, run=run), 18.0)
        self.assertEqual(mac.gate_mem_max_gb(4, run=run), 24.0)

    def test_available_is_the_kernels_own_free_percentage(self):
        run = runner({
            "sysctl -n hw.memsize": f"{32 * GB}\n",
            "sysctl -n kern.memorystatus_level": "81\n",
            "sysctl -n kern.memorystatus_vm_pressure_level": "1\n",
        })
        m = mac.read_memory(run=run)
        self.assertAlmostEqual(m["avail_gb"], 25.92, places=2)
        self.assertEqual((m["some"], m["full"]), (0.0, 0.0))

    def test_pressure_levels_map_onto_the_linux_thresholds(self):
        def at(level):
            return mac.read_memory(run=runner({
                "sysctl -n hw.memsize": f"{32 * GB}\n",
                "sysctl -n kern.memorystatus_level": "50\n",
                "sysctl -n kern.memorystatus_vm_pressure_level": f"{level}\n",
            }))

        warn, critical = at(2), at(4)
        # warn: too busy to start a gate (some > 10) but not a brake (some <= 30, full <= 5)
        self.assertGreater(warn["some"], 10)
        self.assertLessEqual(warn["some"], 30)
        self.assertLessEqual(warn["full"], 5)
        # critical: a brake (full > 5)
        self.assertGreater(critical["full"], 5)

    def test_an_unreadable_pressure_level_counts_as_normal(self):
        run = runner({
            "sysctl -n hw.memsize": f"{16 * GB}\n",
            "sysctl -n kern.memorystatus_level": "40\n",
            "sysctl -n kern.memorystatus_vm_pressure_level": OSError("no such sysctl"),
        })
        m = mac.read_memory(run=run)
        self.assertAlmostEqual(m["avail_gb"], 6.4, places=2)
        self.assertEqual((m["some"], m["full"]), (0.0, 0.0))


@unittest.skipUnless(sys.platform == "darwin", "macOS only: the real commands")
class RealCommands(unittest.TestCase):
    def test_readers_work_without_sbin_on_path(self):
        # sluicy's worker runs with a cleaned env, and /usr/sbin (sysctl, lsof) is not on its PATH.
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import platform_macos as m; "
                "assert m.read_memory()['avail_gb'] > 0; assert m.read_proc(); print('ok')") % str(ROOT / "sluicy")
        res = subprocess.run([sys.executable, "-c", code], env={"PATH": "/nonexistent"}, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)


class Processes(unittest.TestCase):
    PS = (
        "  101   100  4194304 /opt/homebrew/bin/node /x/next build\n"
        "  100   100     2048 /bin/bash scripts/ci.sh\n"
        "  200   200   512000 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
        "  300   300      100 sleep 5\n"
    )
    LSOF = "p100\nfcwd\nn/Users/me/.local/share/sluicy/checkouts/gala\np101\nfcwd\nn/Users/me/.local/share/sluicy/checkouts/gala/apps/web\np200\nfcwd\nn/\n"

    def test_reads_pid_group_rss_command_and_cwd(self):
        run = runner({"ps -axo": self.PS, "lsof": self.LSOF})
        procs = mac.read_proc(run=run, ours=["/Users/me/.local/share/sluicy/checkouts"])
        self.assertEqual(procs[101][0], 100)  # pgrp
        self.assertEqual(procs[101][1], 4194304 * 1024)  # ps rss is KiB
        self.assertEqual(procs[101][3], "/Users/me/.local/share/sluicy/checkouts/gala/apps/web")
        self.assertEqual(procs[101][4], "/opt/homebrew/bin/node /x/next build")
        self.assertEqual(procs[300][3], "")  # lsof had no cwd for it

    def test_processes_under_a_sluicy_checkout_are_tagged_as_the_gate(self):
        run = runner({"ps -axo": self.PS, "lsof": self.LSOF})
        procs = mac.read_proc(run=run, ours=["/Users/me/.local/share/sluicy/checkouts"])
        self.assertIn("sluicy-gate", procs[100][2])
        self.assertIn("sluicy-gate", procs[101][2])
        self.assertEqual(procs[200][2], "")

    def test_a_failing_lsof_still_lists_processes_without_cwd(self):
        run = runner({"ps -axo": self.PS, "lsof": OSError("lsof missing")})
        procs = mac.read_proc(run=run, ours=[])
        self.assertEqual(len(procs), 4)
        self.assertTrue(all(p[3] == "" for p in procs.values()))


class Hogs(unittest.TestCase):
    def test_mac_desktop_apps_are_not_hogs(self):
        import os
        os.environ.setdefault("SLUICY_HOME", tempfile.mkdtemp())
        import sluicy
        big = 10 * GB
        procs = {
            1291: (1291, big, "", "/", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --type=x"),
            1500: (1500, big, "", "/", "/Applications/Cursor.app/Contents/MacOS/Cursor"),
            2000: (2000, big, "", "/Users/me/proj", "/opt/homebrew/bin/node next build"),
        }
        self.assertEqual([h["pid"] for h in sluicy.hogs(procs, [])], [2000])


class Units(unittest.TestCase):
    def test_no_scope_and_no_units(self):
        # No cgroups on macOS: gates run uncapped and the memory governor is the brake.
        self.assertFalse(mac.scope_available())
        self.assertEqual(mac.scoped(["git", "push"], "u", 8.0)[1:], ["-n", "10", "git", "push"])  # niced, as Linux's scope
        self.assertEqual(mac.stop_units("sluicy-gate-*"), (None, ""))
        self.assertFalse(mac.oomd_killed("sluicy-gate-", 0))
        self.assertFalse(mac.start_unit("u", ["true"], {}, "/", "/dev/null"))


class Timer(unittest.TestCase):
    def test_launch_agent_runs_conduct_every_five_minutes(self):
        d = Path(tempfile.mkdtemp())
        run = runner({"launchctl": ""})
        plist = mac.install_timer(["/usr/bin/python3", "/x/sluicy.py", "conduct"], d / "LaunchAgents", d / "logs", run=run, uid=501)
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["Label"], "com.sluicy.conduct")
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "/x/sluicy.py", "conduct"])
        self.assertEqual(data["StartInterval"], 300)
        # A worker the conductor starts inherits the agent's priority and cannot raise it back.
        self.assertNotIn("Nice", data)
        self.assertNotIn("LowPriorityIO", data)
        self.assertEqual(data["ProcessType"], "Standard")
        self.assertTrue(data["StandardOutPath"].startswith(str(d / "logs")))
        cmds = [" ".join([Path(c[0]).name, *c[1:]]) for c in run.calls]
        self.assertIn(f"launchctl bootout gui/501/com.sluicy.conduct", cmds)  # reload when updating
        self.assertIn(f"launchctl bootstrap gui/501 {plist}", cmds)

    def test_bootstrap_retries_once_while_the_old_job_unloads(self):
        d = Path(tempfile.mkdtemp())
        tries = []

        def run(argv):
            if argv[1] == "bootstrap":
                tries.append(argv)
                if len(tries) == 1:
                    raise subprocess.CalledProcessError(5, argv, "", "Bootstrap failed: 5: Input/output error")
            return ""

        mac.time.sleep, sleep = (lambda _s: None), mac.time.sleep
        try:
            mac.install_timer(["x"], d, d, run=run, uid=501)
        finally:
            mac.time.sleep = sleep
        self.assertEqual(len(tries), 2)

    def test_stable_python_follows_the_unversioned_homebrew_link(self):
        d = Path(tempfile.mkdtemp())
        real = d / "python3.14"
        real.write_text("")
        link = d / "python3"
        link.symlink_to(real)
        self.assertEqual(mac.stable_python(str(real), candidates=(str(d / "missing"), str(link))), str(link))
        other = d / "python3.13"
        other.write_text("")
        self.assertEqual(mac.stable_python(str(other), candidates=(str(link),)), str(other))

    def test_remove_timer_unloads_and_deletes(self):
        d = Path(tempfile.mkdtemp())
        run = runner({"launchctl": ""})
        plist = mac.install_timer(["x"], d, d, run=run, uid=501)
        mac.remove_timer(d, run=run, uid=501)
        self.assertFalse(plist.exists())


class Sweep(unittest.TestCase):
    def test_kill_in_stops_what_left_the_gate_group_in_the_checkout(self):
        root = Path(tempfile.mkdtemp())
        # A detached child, as a dev server or test database started with setsid would be.
        child = subprocess.Popen(["sleep", "60"], cwd=root, start_new_session=True)
        try:
            run = runner({
                # 999999: a person's shell `cd`'d into the checkout, on a terminal; never killed.
                "ps -axo pid=,tty=": f"{child.pid} ??\n{os.getpid()} ??\n999999 ttys001\n",
                "ps -axo": f"{child.pid} {child.pid} 100 sleep 60\n{os.getpid()} 1 100 python3\n999999 999999 100 -zsh\n",
                "lsof": f"p{child.pid}\nn{root}\np{os.getpid()}\nn{root}\np999999\nn{root}/apps\n",
            })
            log = io.StringIO()
            hit = mac.kill_in(root, log, exclude={os.getpid()}, run=run, wait=5)
            self.assertEqual(hit, [child.pid])
            self.assertIsNotNone(child.wait(timeout=5))
            self.assertIn("stopping 1 process(es)", log.getvalue())
            self.assertIn("leaving 1 process(es) in", log.getvalue())
        finally:
            if child.poll() is None:
                child.kill()

    def test_kill_in_leaves_other_directories_alone(self):
        run = runner({"ps -axo pid=,tty=": "4242 ??\n", "ps -axo": "4242 4242 100 vim\n",
                      "lsof": "p4242\nn/Users/me/notes\n"})
        self.assertEqual(mac.kill_in("/tmp/checkout", io.StringIO(), run=run), [])


class ReviewFixes(unittest.TestCase):
    """Fixes from the Linux-side review of the port (PR #3)."""

    def test_a_failing_sysctl_does_not_fail_the_gate_cap(self):
        # Only reported on macOS; a TimeoutExpired here used to fail the push as a worker error.
        run = runner({"sysctl": subprocess.TimeoutExpired(["sysctl"], 30)})
        os.environ.pop("SLUICY_GATE_MEM_MAX", None)
        self.assertEqual(mac.gate_mem_max_gb(4, run=run), 0.0)

    def test_lsof_is_this_user_only_and_a_nonzero_exit_keeps_its_output(self):
        lsof = "p100\nfcwd\nn/Users/me/.local/share/sluicy/checkouts/gala\n"
        run = runner({"ps -axo": Processes.PS,
                      "lsof": subprocess.CalledProcessError(1, ["lsof"], output=lsof, stderr="lsof: no pwd entry")})
        procs = mac.read_proc(run=run, ours=["/Users/me/.local/share/sluicy/checkouts"])
        self.assertEqual(procs[100][3], "/Users/me/.local/share/sluicy/checkouts/gala")
        self.assertEqual(procs[100][2], "sluicy-gate")
        argv = next(c for c in run.calls if Path(c[0]).name == "lsof")
        self.assertEqual(argv[argv.index("-u") + 1], str(os.getuid()))

    def test_a_root_reached_through_a_symlink_matches_lsofs_resolved_cwd(self):
        d = Path(tempfile.mkdtemp())
        real = d / "real"
        (real / "co").mkdir(parents=True)
        (d / "link").symlink_to(real)
        resolved = os.path.realpath(real / "co")
        run = runner({"ps -axo": "500 500 100 node\n", "lsof": f"p500\nn{resolved}/apps\n"})
        procs = mac.read_proc(run=run, ours=[str(d / "link" / "co")])
        self.assertEqual(procs[500][2], "sluicy-gate")

    def test_launch_agent_carries_the_installing_shells_path(self):
        d = Path(tempfile.mkdtemp())
        plist = mac.install_timer(["x"], d, d, run=runner({"launchctl": ""}), uid=501,
                                  path="/opt/homebrew/bin:/usr/bin:/bin")
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin:/bin")

    def test_etime(self):
        self.assertEqual(mac._etime("00:05"), 5)
        self.assertEqual(mac._etime("01:02:03"), 3723)
        self.assertEqual(mac._etime("2-00:00:01"), 2 * 86400 + 1)
        self.assertIsNone(mac._etime("??"))

    def test_kill_in_spares_what_started_before_the_gate(self):
        root = Path(tempfile.mkdtemp())
        child = subprocess.Popen(["sleep", "60"], cwd=root, start_new_session=True)
        try:
            # 777777: an editor's language server, up for an hour; 888888: age unknown. Both spared.
            run = runner({
                "ps -axo pid=,tty=": f"{child.pid} ?? 00:01\n777777 ?? 01:00:00\n888888 ??\n",
                "ps -axo": f"{child.pid} {child.pid} 100 sleep 60\n777777 777777 100 tsserver\n888888 888888 100 x\n",
                "lsof": f"p{child.pid}\nn{root}\np777777\nn{root}\np888888\nn{root}\n",
            })
            log = io.StringIO()
            hit = mac.kill_in(root, log, run=run, wait=5, since=__import__("time").time() - 60)
            self.assertEqual(hit, [child.pid])
            self.assertIn("777777 888888", log.getvalue())
            self.assertIsNotNone(child.wait(timeout=5))
        finally:
            if child.poll() is None:
                child.kill()

    def test_kill_in_sends_no_sigkill_to_pids_that_already_exited(self):
        root = Path(tempfile.mkdtemp())
        sent = []
        run = runner({"ps -axo pid=,tty=": "424242 ??\n", "ps -axo": "424242 424242 100 x\n",
                      "lsof": f"p424242\nn{root}\n"})
        kill, alive = mac.os.kill, mac._alive

        def fake_kill(pid, sig):
            sent.append(sig)
            if sig == mac.signal.SIGKILL:
                raise AssertionError("SIGKILL to a pid that had exited")

        mac.os.kill, mac._alive = fake_kill, (lambda _p: False)
        try:
            self.assertEqual(mac.kill_in(root, io.StringIO(), run=run, wait=1), [424242])
        finally:
            mac.os.kill, mac._alive = kill, alive
        self.assertEqual(sent, [mac.signal.SIGTERM])

    def test_clean_env_keeps_path_and_our_roots_list_registered_checkouts(self):
        home = tempfile.mkdtemp()
        code = (
            "import sluicy, os, time\n"
            "conn = sluicy.db()\n"
            "conn.execute(\"INSERT OR IGNORE INTO projects VALUES ('p', '/src/p', '/work/p-co', ?)\", (time.time(),))\n"
            "conn.commit()\n"
            "print('roots', sluicy.our_roots())\n"
            "class C: cfg = {'gate': {'waivers': [], 'strip_env': [], 'env': {'PATH': '/x'}, 'strip_prefixes': []},"
            " 'preflight': {'env': {}}}\n"
            "sluicy.P = C()\n"
            "print('path', 'PATH' in sluicy.clean_env())\n"
        )
        env = {**os.environ, "SLUICY_HOME": home, "PYTHONPATH": str(ROOT / "sluicy")}
        res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("/work/p-co-preflight", res.stdout, res.stderr)
        self.assertIn("/work/p-co-map", res.stdout)
        self.assertIn("path True", res.stdout)
        # Named in strip_env, PATH is still stripped.
        code2 = code.replace("'strip_env': []", "'strip_env': ['PATH']")
        res = subprocess.run([sys.executable, "-c", code2], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("path False", res.stdout, res.stderr)


class MemoryReadFailure(unittest.TestCase):
    def test_a_failing_sysctl_is_an_oserror_for_the_governor(self):
        # The worker's callers treat OSError as "memory unknown"; a raw CalledProcessError killed it.
        code = (
            "import subprocess, sluicy\n"
            "def boom(): raise subprocess.CalledProcessError(1, ['sysctl'])\n"
            "sluicy.plat.read_memory = boom\n"
            "try:\n    sluicy.read_memory()\nexcept OSError as e:\n    print('oserror', e)\n"
        )
        env = {**os.environ, "SLUICY_PLATFORM": "darwin", "PYTHONPATH": str(ROOT / "sluicy")}
        env.pop("SLUICY_MEMINFO", None)
        res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("oserror cannot read memory", res.stdout, res.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
