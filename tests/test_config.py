"""`.sluicy.toml` validation and the Shipyard migration.

    python3 tests/test_config.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sluicy"))
import config  # noqa: E402

TOOL = ROOT / "sluicy" / "sluicy.py"


class Validate(unittest.TestCase):
    def ok(self, text):
        return config.validate(__import__("tomllib").loads(text))

    def bad(self, text, needle):
        with self.assertRaises(config.ConfigError) as cm:
            self.ok(text)
        self.assertIn(needle, str(cm.exception))

    def test_minimal_config_gets_defaults(self):
        cfg = self.ok('project = "shop"')
        self.assertEqual(cfg["deploy"]["kind"], "none")
        self.assertEqual(cfg["repo"], {"remote": "origin", "branch": "main", "ssh_keepalive": True})
        self.assertEqual(cfg["deploy"]["exit_codes"]["rolled_back"], 13)
        self.assertFalse(cfg["plan_review"]["enabled"])

    def test_unknown_keys_are_errors(self):
        self.bad('project = "x"\nprojct = 1', "unknown key 'projct'")
        self.bad('project = "x"\n[gate]\nwaiver = []', "unknown key 'waiver'")
        self.bad('project = "x"\n[components.web]\npath = "web"\nflag = "--web"', "unknown key 'flag'")
        self.bad('project = "x"\n[deploy.exit_codes]\nbusy = 10', "unknown key 'busy'")

    def test_project_name_and_paths(self):
        self.bad("", "`project`")
        self.bad('project = "has space"', "`project`")
        self.bad('project = "x"\n[components.web]\ndeploy_args = []', "components.web.path")

    def test_deploy_command_required_for_command_kind(self):
        self.bad('project = "x"\n[deploy]\nkind = "command"', "deploy.command")
        self.bad('project = "x"\n[deploy]\nkind = "vercel"', "deploy.kind")
        cfg = self.ok('project = "x"\n[deploy]\nkind = "command"\ncommand = ["./d.sh", "{args}"]\n[deploy.exit_codes]\nlease_busy = 75')
        self.assertEqual(cfg["deploy"]["exit_codes"]["lease_busy"], 75)
        self.assertEqual(cfg["deploy"]["exit_codes"]["refused"], 11)

    def test_never_waive_must_be_a_waiver(self):
        self.bad('project = "x"\n[gate]\nnever_waive = ["SKIP_E2E"]', "never_waive")

    def test_regexes_are_checked(self):
        self.bad('project = "x"\n[gate]\nred_line = "("', "gate.red_line")
        self.bad('project = "x"\n[guard]\ndeploy_patterns = ["["]', "guard.deploy_patterns")

    def test_env_values_are_strings(self):
        self.bad('project = "x"\n[gate]\nenv = { A = 1 }', "string values")

    def test_examples_are_valid(self):
        for f in sorted((ROOT / "examples").glob("*.toml")):
            with self.subTest(f.name):
                config.load(f)

    def test_find_stops_at_the_given_root(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "a" / "b").mkdir(parents=True)
        (root / ".sluicy.toml").write_text('project = "x"')
        self.assertEqual(config.find(root / "a" / "b", stop=root), root / ".sluicy.toml")
        self.assertIsNone(config.find(root / "a" / "b", stop=root / "a"))


OLD_SCHEMA = """
CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL, sha TEXT NOT NULL,
  source TEXT NOT NULL, mode TEXT NOT NULL, components TEXT NOT NULL DEFAULT '', recreate INTEGER NOT NULL DEFAULT 0,
  waivers TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL, base_sha TEXT, merged_sha TEXT, detail TEXT NOT NULL DEFAULT '',
  rolled_back TEXT NOT NULL DEFAULT '', log_path TEXT NOT NULL DEFAULT '', push_attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, brake_count INTEGER NOT NULL DEFAULT 0, push_pgid INTEGER,
  submitted_by TEXT NOT NULL DEFAULT 'hand', launch_note TEXT);
CREATE TABLE ticket_commits (sha TEXT NOT NULL, ticket_id INTEGER NOT NULL, PRIMARY KEY (sha, ticket_id));
CREATE TABLE allows (sha TEXT NOT NULL, component TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE holds (component TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL, cleared_at REAL);
CREATE TABLE live (component TEXT PRIMARY KEY, sha TEXT NOT NULL);
CREATE TABLE debts (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, kind TEXT NOT NULL,
  detail TEXT NOT NULL, created_at REAL NOT NULL, done_at REAL);
"""


class MigrateShipyard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.old = self.root / "old"
        (self.old / "logs").mkdir(parents=True)
        (self.old / "logs" / "7.log").write_text("gate output\n")
        (self.old / "guards-on").write_text("")
        (self.old / "auto-ship-off").write_text("")
        c = sqlite3.connect(self.old / "shipyard.db")
        c.executescript(OLD_SCHEMA)
        c.execute("INSERT INTO tickets (id, branch, sha, source, mode, components, state, log_path, created_at, updated_at)"
                  " VALUES (7, 'fix/a', 'abc', '/src', 'deploy', 'web', 'live', ?, 1, 2)", (str(self.old / "logs" / "7.log"),))
        c.execute("INSERT INTO ticket_commits VALUES ('abc', 7)")
        c.execute("INSERT INTO live VALUES ('web', 'abc')")
        c.execute("INSERT INTO holds VALUES ('api', 'rolled back', 1, NULL)")
        c.execute("INSERT INTO allows VALUES ('def', 'api', 'hotfix', 1)")
        c.execute("INSERT INTO debts (ticket_id, kind, detail, created_at) VALUES (7, 'visuals', 'recapture', 1)")
        c.commit()
        c.close()
        self.env = {**os.environ, "HOME": str(self.root), "SLUICY_HOME": str(self.root / "new"), "SLUICY_NO_NOTIFY": "1"}

    def sy(self, *args):
        return subprocess.run(["python3", str(TOOL), *args], env=self.env, capture_output=True, text=True, timeout=60)

    def test_round_trip(self):
        res = self.sy("migrate-shipyard", str(self.old), "--project", "shop")
        self.assertEqual(res.returncode, 0, res.stderr)
        c = sqlite3.connect(self.root / "new" / "sluicy.db")
        c.row_factory = sqlite3.Row
        t = c.execute("SELECT * FROM tickets WHERE id = 7").fetchone()
        self.assertEqual((t["project"], t["state"], t["components"]), ("shop", "live", "web"))
        self.assertEqual(t["log_path"], str(self.root / "new" / "logs" / "7.log"))
        self.assertEqual((self.root / "new" / "logs" / "7.log").read_text(), "gate output\n")
        self.assertEqual(tuple(c.execute("SELECT project, component, sha FROM live").fetchone()), ("shop", "web", "abc"))
        self.assertEqual(c.execute("SELECT project FROM holds").fetchone()[0], "shop")
        self.assertEqual(c.execute("SELECT project FROM allows").fetchone()[0], "shop")
        self.assertEqual(c.execute("SELECT kind FROM debts").fetchone()[0], "visuals")
        # guards-on is copied (older hooks still read the old one); auto-ship-off stays behind
        self.assertTrue((self.old / "guards-on").exists())
        self.assertTrue((self.root / "new" / "guards-on").exists())
        self.assertFalse((self.root / "new" / "auto-ship-off").exists())
        self.assertTrue((self.old / "MIGRATED").exists())
        self.assertFalse(os.access(self.old / "shipyard.db", os.W_OK) and os.geteuid() != 0)
        again = self.sy("migrate-shipyard", str(self.old), "--project", "shop")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already migrated", again.stderr)

    def test_refuses_while_the_old_queue_is_busy(self):
        c = sqlite3.connect(self.old / "shipyard.db")
        c.execute("UPDATE tickets SET state = 'pushing'")
        c.commit()
        c.close()
        res = self.sy("migrate-shipyard", str(self.old), "--project", "shop")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("active tickets", res.stderr)
        self.assertFalse((self.old / "MIGRATED").exists())


class ConfigCheck(unittest.TestCase):
    def test_cli_reports_ok_and_errors(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        env = {**os.environ, "HOME": str(root), "SLUICY_HOME": str(root / "s")}
        (root / ".sluicy.toml").write_text('project = "shop"\n[components.web]\npath = "web"\n')
        res = subprocess.run(["python3", str(TOOL), "config", "check"], cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("the push is the deploy", res.stdout)
        (root / ".sluicy.toml").write_text('project = "shop"\nbogus = 1\n')
        res = subprocess.run(["python3", str(TOOL), "config", "check"], cwd=root, env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("bogus", res.stderr)
        res = subprocess.run(["python3", str(TOOL), "config", "check", "--json"], cwd=root, env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        (root / ".sluicy.toml").write_text('project = "shop"\n')
        res = subprocess.run(["python3", str(TOOL), "config", "check", "--json"], cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(json.loads(res.stdout)["project"], "shop")


if __name__ == "__main__":
    unittest.main(verbosity=1)
