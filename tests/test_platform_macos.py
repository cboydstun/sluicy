"""platform_macos: the macOS side of sluicy's OS layer, tested on any OS with faked command output.

    python3 tests/test_platform_macos.py
"""
import plistlib
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


class Units(unittest.TestCase):
    def test_no_scope_and_no_units(self):
        # No cgroups on macOS: gates run uncapped and the memory governor is the brake.
        self.assertFalse(mac.scope_available())
        self.assertEqual(mac.scoped(["git", "push"], "u", 8.0), ["git", "push"])
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
        self.assertEqual(data["Nice"], 19)
        self.assertTrue(data["StandardOutPath"].startswith(str(d / "logs")))
        cmds = [" ".join([Path(c[0]).name, *c[1:]]) for c in run.calls]
        self.assertIn(f"launchctl bootout gui/501/com.sluicy.conduct", cmds)  # reload when updating
        self.assertIn(f"launchctl bootstrap gui/501 {plist}", cmds)

    def test_remove_timer_unloads_and_deletes(self):
        d = Path(tempfile.mkdtemp())
        run = runner({"launchctl": ""})
        plist = mac.install_timer(["x"], d, d, run=run, uid=501)
        mac.remove_timer(d, run=run, uid=501)
        self.assertFalse(plist.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
