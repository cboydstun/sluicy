"""install.sh / uninstall.sh in a throwaway $HOME, and the settings.json merge.

    python3 tests/test_install.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sluicy"))
import claude_hooks  # noqa: E402

RTK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}


class Merge(unittest.TestCase):
    def test_keeps_other_hooks_and_is_idempotent(self):
        before = {"model": "x", "hooks": {"PreToolUse": [RTK]}}
        once = claude_hooks.install(json.loads(json.dumps(before)), "/opt/sluicy")
        twice = claude_hooks.install(json.loads(json.dumps(once)), "/opt/sluicy")
        self.assertEqual(once, twice)
        self.assertEqual(once["model"], "x")
        self.assertIn(RTK, once["hooks"]["PreToolUse"])
        cmds = [h["command"] for g in once["hooks"]["PreToolUse"] + once["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(sum("guard hook" in c for c in cmds), 1)
        self.assertEqual(sum("autoship" in c for c in cmds), 1)
        self.assertEqual(sum("plan_review.py" in c for c in cmds), 1)
        self.assertEqual(claude_hooks.strip(json.loads(json.dumps(once))), before)

    def test_moving_the_checkout_replaces_the_old_paths(self):
        a = claude_hooks.install({}, "/old/sluicy")
        b = claude_hooks.install(a, "/new/sluicy")
        text = json.dumps(b)
        self.assertNotIn("/old/sluicy", text)
        self.assertIn("/new/sluicy", text)


class InstallScript(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        fake = self.home / "fakebin"
        fake.mkdir()
        # No systemd user session here: the script must say so and carry on.
        (fake / "systemctl").write_text("#!/bin/bash\nexit 1\n")
        (fake / "systemctl").chmod(0o755)
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [RTK]}}))
        self.env = {"HOME": str(self.home), "PATH": f"{fake}:{self.home / '.local/bin'}:/usr/bin:/bin",
                    "SLUICY_HOME": str(self.home / "state")}

    def run_sh(self, name, *args):
        return subprocess.run(["bash", str(ROOT / name), *args], env=self.env, capture_output=True, text=True, timeout=120)

    def test_round_trip(self):
        res = self.run_sh("install.sh", "--no-deck")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("no systemd user session", res.stdout)
        for name in ("sluicy", "plan-review", "deck"):
            self.assertTrue((self.home / ".local/bin" / name).is_symlink(), name)
        s = json.loads(self.settings.read_text())
        self.assertIn(RTK, s["hooks"]["PreToolUse"])
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 3)
        self.assertEqual(len(list((self.home / ".claude").glob("settings.json.before-sluicy-*"))), 1)
        # the installed command works through its symlink
        out = subprocess.run([str(self.home / ".local/bin/sluicy"), "paths", "--json"], env=self.env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(out.stdout)["home"], str(self.home / "state"))
        again = self.run_sh("install.sh", "--no-deck")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already set", again.stdout)
        res = self.run_sh("uninstall.sh")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": {"PreToolUse": [RTK]}})
        self.assertFalse((self.home / ".local/bin/sluicy").exists())

    def test_hooks_are_inert_outside_a_sluicy_repo(self):
        self.assertEqual(self.run_sh("install.sh", "--no-deck").returncode, 0)
        (self.home / "state").mkdir(exist_ok=True)
        (self.home / "state" / "guards-on").write_text("")
        plain = self.home / "plain"
        plain.mkdir()
        s = json.loads(self.settings.read_text())
        guard = next(h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"] if "guard hook" in h["command"])
        payload = json.dumps({"cwd": str(plain), "tool_input": {"command": "git push origin main"}})
        start = time.monotonic()
        res = subprocess.run(["bash", "-c", guard], input=payload, env=self.env, capture_output=True, text=True, cwd=plain)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
