"""sluicy tests: real git and SQLite, with a fake gate, deploy script and ssh.

    python3 tests/test_sluicy.py

The fakes live in a temp dir, and ssh on PATH is a stub, so nothing here can reach a server or a remote.
"""
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "sluicy" / "sluicy.py"

# Claude Code's Bash tool runs commands with SIGTERM and SIGHUP blocked, and a blocked mask is inherited:
# the fake gate then ignores the governor's SIGTERM and every brake waits 30 s for SIGKILL, so the
# three-brake test overran its 60 s. A systemd-run worker has nothing blocked; tests must not depend on it.
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGHUP})

FAKE_HOOK = """\
#!/bin/bash
# Fake gate: records the push and the waivers it could see.
cat > /dev/null
ctl="$SY_CTL"
echo "$(git rev-parse HEAD) ticket=${SLUICY_TICKET:-} visuals=${VISUALS_OK:-} demo=${DEMO_OK:-} braked=${SLUICY_BRAKED:-} failfast=${GATE_FAIL_FAST:-} headroom=${GATE_HEADROOM_GB:-} pw=${PLAYWRIGHT_WORKERS:-} scoped=${SLUICY_GATE_SCOPED:-}" >> "$ctl/gates.log"
# The push already resolved its URL; break origin for the fetch that follows it.
[ -f "$ctl/break-fetch" ] && { rm -f "$ctl/break-fetch"; git config remote.origin.url "$ctl/no-such-origin"; }
if [ -f "$ctl/gate-spike-once" ] || [ -f "$ctl/gate-spike-always" ]; then
  rm -f "$ctl/gate-spike-once"
  printf 'some avg10=60.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=40.00 avg60=0.00 avg300=0.00 total=1\n' > "$ctl/psi"
  sleep 30
  exit 0
fi
if [ -f "$ctl/gate-oomd-once" ]; then
  rm -f "$ctl/gate-oomd-once"
  echo "Killed /user.slice/user-1.slice/user@1.service/sluicy-gate.slice/sluicy-gate-proj-${SLUICY_TICKET}-42.scope due to memory pressure" >> "$ctl/oomd.log"
  exit 1
fi
[ -f "$ctl/kill-at-gate" ] && { rm -f "$ctl/kill-at-gate"; kill -9 "$SLUICY_WORKER_PID"; exit 1; }
[ -f "$ctl/kill-at-gate-pass" ] && { rm -f "$ctl/kill-at-gate-pass"; kill -9 "$SLUICY_WORKER_PID"; exit 0; }
if [ -f "$ctl/gate-red-jest" ]; then
  d="$GATE_LOGS"; mkdir -p "$d"
  printf 'PASS tests/a.test.ts\\nFAIL tests/unit/x.test.ts\\n  ● Console\\n    console.log noise\\n  ● x › breaks\\n\\n    expect(received).toEqual(expected)\\n    - Array []\\nFAIL tests/unit/x.test.ts\\n  ● x › breaks\\n' > "$d/api-jest.log"
  printf '  1) [web] › tests/web/screens.spec.ts:42:3 › screens › payroll \\n\\n    Error: /payroll shows no data\\n\\033[0;31m✗ Some tests failed\\033[0m\\n'
  exit 1
fi
[ -f "$ctl/gate-red" ] && exit 1
if [ -f "$ctl/race-push" ]; then
  rm -f "$ctl/race-push"
  bash "$ctl/race-push.sh"
  exit 1
fi
exit 0
"""

FAKE_DEPLOY = """\
#!/bin/bash
ctl="$SY_CTL"
echo "$* ticket=${SLUICY_TICKET:-}" >> "$ctl/deploys.log"
case "$*" in *--smoke-only*) exit "$(cat "$ctl/smoke-rc" 2>/dev/null || echo 0)" ;; esac
[ -f "$ctl/kill-before-deploy" ] && { rm -f "$ctl/kill-before-deploy"; kill -9 "$SLUICY_WORKER_PID"; sleep 1; exit 0; }
sha=""
comps=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) sha="$2"; shift ;;
    --api) comps="$comps api" ;;
    --web) comps="$comps web" ;;
    --admin) comps="$comps admin" ;;
  esac
  shift
done
rc=$(cat "$ctl/deploy-rc" 2>/dev/null || echo 0)
if [ -f "$ctl/deploy-rc-once" ]; then rc=$(cat "$ctl/deploy-rc-once"); rm -f "$ctl/deploy-rc-once"; fi
if [ "$rc" = 0 ]; then
  for c in $comps; do echo "$sha" > "$ctl/stamp-$c"; done
fi
[ -f "$ctl/kill-after-deploy" ] && { rm -f "$ctl/kill-after-deploy"; kill -9 "$SLUICY_WORKER_PID"; sleep 1; exit 0; }
exit "$rc"
"""

FAKE_PREFLIGHT_GATE = """\
#!/bin/bash
# Fake quick checks (the project's preflight command): red when a RED file is in the tree, unless waived.
echo "preflight sha=$(git rev-parse HEAD) visuals=${VISUALS_OK:-} demo=${DEMO_OK:-}" >> "$SY_CTL/preflight.log"
[ -f "$SY_CTL/preflight-steal" ] && python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\\"UPDATE tickets SET state='merging'\\"); c.commit()" "$SLUICY_DB"
if [ -n "$(find . -name RED -not -path './.git/*')" ] && [ -z "${VISUALS_OK:-}" ]; then
  echo -e "\\033[0;31m✗ A screen changed but what documents or advertises it did not:\\033[0m"
  echo -e "\\033[0;31m✗ Preflight failed with 1 error(s) and 0 warning(s)\\033[0m"
  exit 1
fi
exit 0
"""

FAKE_SSH = """\
#!/bin/bash
# Answers only the stamp read, one line per component asked for, like a real batched read.
ctl="$SY_CTL"
echo "$*" >> "$ctl/ssh.log"
case "$*" in *deployed-sha*) ;; *) echo "stub ssh refused: $*" >&2; exit 255 ;; esac
case "$*" in */srv/api/.deployed-sha*) echo "api=$(cat "$ctl/stamp-api" 2>/dev/null)" ;; esac
case "$*" in */srv/web/.deployed-sha*) echo "web=$(cat "$ctl/stamp-web" 2>/dev/null)" ;; esac
case "$*" in */srv/admin/.deployed-sha*) echo "admin=$(cat "$ctl/stamp-admin" 2>/dev/null)" ;; esac
exit 0
"""


FAKE_SYSTEMCTL = """\
#!/bin/bash
# Records the stop and, like a real stop, relieves the pressure the gate caused.
echo "$*" >> "$SY_CTL/systemctl.log"
printf 'some avg10=0.00 avg60=0.00 avg300=0.00 total=1\\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\\n' > "$SY_CTL/psi"
exit 0
"""

FAKE_JOURNALCTL = """\
#!/bin/bash
cat "$SY_CTL/oomd.log" 2>/dev/null
exit 0
"""


FAKE_STAMPS = """\
#!/bin/bash
# The project's stamps command: one batched ssh read, `name=sha` per component asked for.
cmd=""
for c in "$@"; do cmd="$cmd echo $c=\\$(cat /srv/$c/.deployed-sha 2>/dev/null);"; done
exec ssh -o ConnectTimeout=15 prod "$cmd true"
"""

SLUICY_TOML = """\
project = "proj"

[components.api]
path = "api"
deploy_args = ["--api"]

[components.web]
path = "web"
deploy_args = ["--web"]

[components.admin]
path = "admin"
deploy_args = ["--admin"]

[deploy]
kind = "command"
command = ["./deploy.sh", "{args}", "--sha", "{sha}", "--yes"]
recreate_arg = "--recreate"
smoke_command = ["./deploy.sh", "--smoke-only"]

[stamps]
command = ["bash", "scripts/stamps.sh"]

[gate]
waivers = ["VISUALS_OK", "CHANGELOG_OK", "DEMO_OK", "MARKETING_OK", "SKIP_E2E"]
never_waive = ["SKIP_E2E"]
strip_env = ["PLAYWRIGHT_WORKERS", "PRETTIER_FULL"]
env = { GATE_HEADROOM_GB = "8", GATE_FAIL_FAST = "1" }
red_line = '^\\s*✗ (?!(Validation|Preflight|Pre-push validation) failed)'
case_head = '^\\s+\\d+\\) \\[[\\w-]+\\] › '
suite_head = '^(FAIL |\\s+● (?!Console))'
quick_stop = ["Stopped early (a pre-E2E check", "Stopped early (formatting"]

[guard]
deploy_patterns = ['(^|[\\s;&|/])deploy\\.sh\\b']
allow_args = ['--smoke-only']

[preflight]
command = ["bash", "scripts/quick-checks.sh"]
env = { GATE_PREFLIGHT = "1" }
link_dirs = ["node_modules"]
exclude = [".swc/"]

[deps]
"package-lock.json" = ["npm", "ci", "--silent"]
"""


def write_exec(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Base(unittest.TestCase):
    TOML = None  # the repository's .sluicy.toml; None = SLUICY_TOML
    EXTRA_TOML = ""
    EXTRA_FILES = {}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ctl = self.root / "ctl"
        self.ctl.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        write_exec(bin_dir / "ssh", FAKE_SSH)
        write_exec(bin_dir / "systemctl", FAKE_SYSTEMCTL)
        write_exec(bin_dir / "journalctl", FAKE_JOURNALCTL)
        self.set_memory(64)
        # npm must not run for real; record calls.
        write_exec(bin_dir / "npm", '#!/bin/bash\necho "$PWD $*" >> "$SY_CTL/npm.log"\nexit 0\n')
        home = self.root / "home"
        home.mkdir()
        (home / "gitconfig").write_text(
            "[user]\n\temail = test@example.com\n\tname = test\n[init]\n\tdefaultBranch = main\n"
        )
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "SY_CTL": str(self.ctl),
            "SLUICY_HOME": str(self.root / "state"),
            "SLUICY_CHECKOUTS": str(self.root / "checkouts"),
            "SLUICY_NO_SCOPE": "1",
            "SLUICY_NO_NOTIFY": "1",
            "SLUICY_NO_SPAWN": "1",
            "SLUICY_LEASE_WAIT": "0",
            "SLUICY_LEASE_TRIES": "2",
            "SLUICY_MEMINFO": str(self.ctl / "meminfo"),
            "SLUICY_PSI": str(self.ctl / "psi"),
            "SLUICY_MEM_SETTLE": "0",
            "SLUICY_MEM_POLL": "0.1",
            "SLUICY_BRAKE_POLL": "0.05",
            "SLUICY_DB": str(self.root / "state" / "sluicy.db"),
            "TMPDIR": str(self.root / "tmp"),
        }
        (self.root / "tmp").mkdir()
        origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", str(origin))
        self.dev = self.root / "dev"
        self.git(self.root, "clone", "-q", str(origin), str(self.dev))
        for d in ("api", "web", "admin", "docs"):
            (self.dev / d).mkdir()
            (self.dev / d / "file.txt").write_text("base\n")
        (self.dev / ".githooks").mkdir()
        write_exec(self.dev / ".githooks" / "pre-push", FAKE_HOOK)
        write_exec(self.dev / "deploy.sh", FAKE_DEPLOY)
        (self.dev / "scripts").mkdir()
        write_exec(self.dev / "scripts" / "quick-checks.sh", FAKE_PREFLIGHT_GATE)
        write_exec(self.dev / "scripts" / "stamps.sh", FAKE_STAMPS)
        (self.dev / ".sluicy.toml").write_text((self.TOML or SLUICY_TOML) + self.EXTRA_TOML)
        for rel, body in self.EXTRA_FILES.items():
            write_exec(self.dev / rel, body)
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "base")
        self.git(self.dev, "push", "-q", "origin", "main")
        self.base = self.git(self.dev, "rev-parse", "HEAD")
        for c in ("api", "web", "admin"):
            (self.ctl / f"stamp-{c}").write_text(self.base + "\n")
        self.sy("init", "--no-install", "--checkout", str(self.root / "checkout"), check=True)

    # ---- helpers
    def set_memory(self, avail_gb, some=0.0, full=0.0):
        (self.ctl / "meminfo").write_text(f"MemTotal: 33554432 kB\nMemAvailable: {int(avail_gb * 1048576)} kB\n")
        (self.ctl / "psi").write_text(
            f"some avg10={some:.2f} avg60=0.00 avg300=0.00 total=1\nfull avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\n"
        )

    def git(self, cwd, *args):
        out = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    def sy(self, *args, check=False, env=None, cwd=None):
        full = {**self.env, **(env or {})}
        return subprocess.run(
            ["python3", str(TOOL), *args],
            cwd=cwd or self.dev,
            env=full,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    def branch(self, name, path="web/file.txt", base="origin/main"):
        self.git(self.dev, "fetch", "-q", "origin", "main")
        self.git(self.dev, "checkout", "-q", "-B", name, base)
        target = self.dev / path
        target.parent.mkdir(exist_ok=True)
        with target.open("a") as f:
            f.write(f"{name}\n")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", f"change {name}")
        return self.git(self.dev, "rev-parse", "HEAD")

    def direct_push(self, path):
        """A commit that reached main without a ticket."""
        self.git(self.dev, "fetch", "-q", "origin", "main")
        self.git(self.dev, "checkout", "-q", "-B", "direct", "origin/main")
        with (self.dev / path).open("a") as f:
            f.write("direct\n")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "direct push")
        self.git(self.dev, "push", "-q", "--no-verify", "origin", "direct:main")
        return self.git(self.dev, "rev-parse", "HEAD")

    def submit(self, name, *mode, path="web/file.txt"):
        self.branch(name, path)
        res = self.sy("submit", "--branch", name, *mode, check=True)
        return res.stdout.strip().split()[1]

    def run_worker(self):
        return self.sy("run")

    def ticket(self, tid):
        return json.loads(self.sy("status", tid, "--json", check=True).stdout)

    def state(self, tid):
        return self.ticket(tid)["state"]

    def count(self, name, needle=""):
        p = self.ctl / name
        if not p.exists():
            return 0
        return sum(1 for line in p.read_text().splitlines() if needle in line)

    def origin_main(self):
        return self.git(self.root / "origin.git", "rev-parse", "main")

    def stamp(self, comp):
        return (self.ctl / f"stamp-{comp}").read_text().strip()

    def alerts(self):
        p = self.root / "state" / "alerts.log"
        return p.read_text() if p.exists() else ""


class CleanPath(Base):
    def test_deploy_ticket_merges_gates_once_and_ships_that_sha(self):
        tid = self.submit("fix/a", "--deploy", "web")
        self.assertEqual(self.state(tid), "queued")
        self.assertEqual(self.run_worker().returncode, 0)
        t = self.ticket(tid)
        self.assertEqual(t["state"], "live")
        self.assertEqual(self.count("gates.log"), 1)
        self.assertEqual(self.origin_main(), t["merged_sha"])
        self.assertEqual(self.stamp("web"), t["merged_sha"])
        self.assertEqual(self.stamp("api"), self.base)
        self.assertEqual(self.count("deploys.log", f"--web --sha {t['merged_sha']} --yes"), 1)
        self.assertEqual(self.count("deploys.log", f"ticket={tid}"), 1)

    def test_untracked_leftovers_are_moved_out_before_the_merge(self):
        co = self.root / "checkout"
        (co / ".gitignore").write_text(".env\n")  # stands in for the real ignore file
        (co / ".git" / "info" / "exclude").write_text(".gitignore\n")
        (co / ".env").write_text("SECRET=1\n")
        stray = co / "docs" / "deck-rs"
        (stray / "src").mkdir(parents=True)
        (stray / "src" / "main.rs").write_text("fn main(){}\n")
        (stray / ".gitignore").write_text("target/\n")
        (stray / "target").mkdir()
        (stray / "target" / "x.json").write_text("{bad\n")
        tid = self.submit("fix/stray", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(tid), "live")
        self.assertFalse((co / "docs" / "deck-rs").exists())
        self.assertEqual((co / ".env").read_text(), "SECRET=1\n")
        moved = list((self.root / "state" / "stray").glob(f"{tid}-*/docs/deck-rs/target/x.json"))
        self.assertEqual(len(moved), 1)
        self.assertIn("moved untracked docs/deck-rs/", self.alerts())

    def test_merge_is_no_ff_and_keeps_only_the_submitted_sha(self):
        sha = self.branch("fix/moved")
        tid = self.sy("submit", "--branch", "fix/moved", "--deploy", "web", check=True).stdout.split()[1]
        (self.dev / "web" / "later.txt").write_text("not submitted\n")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "after submit")
        self.run_worker()
        self.git(self.dev, "fetch", "-q", "origin", "main")
        parents = self.git(self.dev, "log", "-1", "--format=%P", "origin/main").split()
        self.assertEqual(len(parents), 2)
        self.assertEqual(parents[1], sha)
        self.assertEqual(self.ticket(tid)["state"], "live")

    def test_no_deploy_modes(self):
        ride = self.submit("fix/ride", "--ride")
        hold = self.submit("fix/hold", "--no-deploy-until-released", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(ride), "merged")
        self.assertEqual(self.state(hold), "held")
        self.assertEqual(self.count("deploys.log"), 0)

    def test_submit_needs_exactly_one_mode(self):
        self.branch("fix/none")
        self.assertNotEqual(self.sy("submit", "--branch", "fix/none").returncode, 0)
        self.assertNotEqual(
            self.sy("submit", "--branch", "fix/none", "--ride", "--deploy", "api").returncode, 0
        )
        self.assertNotEqual(self.sy("submit", "--branch", "fix/none", "--deploy", "mobile").returncode, 0)

    def test_order_is_submission_order(self):
        a = self.submit("fix/1", "--deploy", "web")
        b = self.submit("fix/2", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        self.assertEqual(self.state(b), "live")
        lines = (self.ctl / "deploys.log").read_text().splitlines()
        self.assertIn(f"ticket={a}", lines[0])
        self.assertIn(f"ticket={b}", lines[1])

    def test_recreate_is_passed(self):
        tid = self.submit("fix/r", "--deploy", "api", "--recreate", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(tid), "live")
        self.assertEqual(self.count("deploys.log", "--recreate"), 1)


class Failures(Base):
    def test_conflict_returns_to_owner_and_queue_moves_on(self):
        a = self.submit("fix/a", "--ride", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(a), "merged")
        self.git(self.dev, "checkout", "-q", "-B", "fix/conflict", self.base)
        (self.dev / "docs" / "file.txt").write_text("conflict\n")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "conflict")
        c = self.sy("submit", "--branch", "fix/conflict", "--ride", check=True).stdout.split()[1]
        d = self.submit("fix/d", "--ride", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(c), "conflict")
        self.assertEqual(self.state(d), "merged")

    def test_red_gate_fails_without_retry_and_queue_moves_on(self):
        before = self.origin_main()
        (self.ctl / "gate-red").touch()
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "failed")
        self.assertEqual(self.count("gates.log"), 1)
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(self.count("deploys.log"), 0)
        self.assertTrue(Path(self.ticket(a)["log_path"]).exists())
        self.assertIn(a, self.alerts())
        (self.ctl / "gate-red").unlink()
        b = self.submit("fix/b", "--ride", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(b), "merged")

    def test_push_race_remerges_once_main_moved(self):
        clone = self.root / "racer"
        self.git(self.root, "clone", "-q", str(self.root / "origin.git"), str(clone))
        (self.ctl / "race-push.sh").write_text(
            f'cd {clone} && git pull -q origin main && echo r >> docs/file.txt && '
            'git add -A && git commit -qm race && git push -q --no-verify origin main\n'
        )
        (self.ctl / "race-push").touch()
        a = self.submit("fix/a", "--ride")
        self.run_worker()
        self.assertEqual(self.state(a), "merged")
        self.assertEqual(self.count("gates.log"), 2)

    def test_waiver_reaches_only_its_own_ticket(self):
        self.branch("fix/w", "docs/file.txt")
        a = self.sy(
            "submit", "--branch", "fix/w", "--ride", "--waive", "VISUALS_OK=no visible change", check=True
        ).stdout.split()[1]
        b = self.submit("fix/x", "--ride", path="api/file.txt")
        self.run_worker()
        lines = (self.ctl / "gates.log").read_text().splitlines()
        self.assertIn(f"ticket={a} visuals=1", lines[0])
        self.assertIn(f"ticket={b} visuals= ", lines[1])
        self.assertIn("no visible change", self.ticket(a)["waivers"])

    def test_unknown_waiver_refused(self):
        self.branch("fix/w")
        res = self.sy("submit", "--branch", "fix/w", "--ride", "--waive", "PATH=/evil")
        self.assertNotEqual(res.returncode, 0)
        res = self.sy("submit", "--branch", "fix/w", "--ride", "--waive", "VISUALS_OK=")
        self.assertNotEqual(res.returncode, 0)

    def test_build_failure_fails_ticket_and_holds_nothing(self):
        (self.ctl / "deploy-rc-once").write_text("12")
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "failed")
        self.assertEqual(self.stamp("web"), self.base)
        self.assertIn(a, self.alerts())
        b = self.submit("fix/b", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(b), "live")

    def test_lease_busy_retries_then_blocks(self):
        (self.ctl / "deploy-rc").write_text("10")
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "blocked")
        self.assertEqual(self.count("deploys.log"), 2)
        self.assertEqual(self.count("gates.log"), 1)
        (self.ctl / "deploy-rc").unlink()
        self.sy("allow", a, "--component", "web", "--reason", "lease cleared", check=True)
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        self.assertEqual(self.count("gates.log"), 1)

    def test_unknown_outcome_is_never_redeployed(self):
        (self.ctl / "deploy-rc").write_text("14")
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "unknown")
        self.run_worker()
        self.assertEqual(self.count("deploys.log"), 1)
        self.assertIn("web", self.alerts())


class Authorisation(Base):
    def test_unclaimed_commit_blocks_only_its_component(self):
        unclaimed = self.direct_push("web/file.txt")
        t = self.submit("fix/t", "--deploy", "web")
        a = self.submit("fix/a", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(t), "blocked")
        self.assertIn(unclaimed[:8], self.ticket(t)["detail"])
        self.assertEqual(self.state(a), "live")
        self.assertEqual(self.stamp("web"), self.base)
        self.assertEqual(self.count("deploys.log", "--web"), 0)
        self.assertIn(unclaimed[:8], self.alerts())

    def test_allow_clears_a_commit_for_one_component(self):
        unclaimed = self.direct_push("web/file.txt")
        t = self.submit("fix/t", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(t), "blocked")
        self.assertNotEqual(self.sy("allow", unclaimed, "--component", "web").returncode, 0)
        self.sy("allow", unclaimed, "--component", "api", "--reason", "wrong one", check=True)
        self.run_worker()
        self.assertEqual(self.state(t), "blocked")
        self.sy("allow", unclaimed, "--component", "web", "--reason", "reviewed", check=True)
        self.run_worker()
        self.assertEqual(self.state(t), "live")

    def test_ride_commit_may_ship_with_anyone(self):
        self.submit("fix/ride", "--ride")
        self.run_worker()
        t = self.submit("fix/t", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(t), "live")

    def test_other_tickets_deploy_commit_needs_that_component(self):
        # Touches web files, but its owner asked to deploy only the API.
        a = self.submit("fix/a", "--deploy", "api")
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        t = self.submit("fix/t", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(t), "blocked")

    def test_held_ticket_blocks_until_released(self):
        h = self.submit("fix/h", "--no-deploy-until-released")
        self.run_worker()
        self.assertEqual(self.state(h), "held")
        t = self.submit("fix/t", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(t), "blocked")
        self.sy("release", h, check=True)
        self.run_worker()
        self.assertEqual(self.state(t), "live")

    def test_unstamped_component_blocks_until_allowed(self):
        (self.ctl / "stamp-api").unlink()
        a = self.submit("fix/a", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(a), "blocked")
        self.assertIn("no stamp", self.ticket(a)["detail"])
        self.sy("allow", "unstamped", "--component", "api", "--reason", "first deploy", check=True)
        self.run_worker()
        self.assertEqual(self.state(a), "live")

    def test_vps_unreachable_blocks_without_deploying(self):
        (self.root / "bin" / "ssh").write_text("#!/bin/bash\nexit 255\n")
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "blocked")
        self.assertEqual(self.count("deploys.log"), 0)


class Rollback(Base):
    def test_auto_rollback_is_sticky_until_a_new_commit(self):
        (self.ctl / "deploy-rc-once").write_text("13")
        a = self.submit("fix/bad", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "rolled_back")
        self.assertIn("web", self.ticket(a)["rolled_back"])
        self.assertIn("rolled", self.alerts())
        # Unrelated change to the same component cannot ship the bad commit along.
        other = self.submit("fix/docs", "--deploy", "web", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(other), "blocked")
        self.assertIn("hold", self.ticket(other)["detail"])
        # A new commit in the component is the fix, and clears the hold when it ships.
        fix = self.submit("fix/fix", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(fix), "live")
        # The waiting ticket retries on its own, and the fix's deploy already carried its commit.
        self.assertEqual(self.state(other), "live")
        self.assertEqual(self.count("deploys.log", f"ticket={other}"), 0)
        self.assertEqual(self.stamp("web"), self.ticket(fix)["merged_sha"])

    def test_hold_cleared_by_allow(self):
        (self.ctl / "deploy-rc-once").write_text("13")
        a = self.submit("fix/bad", "--deploy", "web")
        self.run_worker()
        other = self.submit("fix/docs", "--deploy", "web", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(other), "blocked")
        self.sy("allow", "hold", "--component", "web", "--reason", "bad commit was not bad", check=True)
        self.run_worker()
        self.assertEqual(self.state(other), "live")
        self.assertEqual(self.state(a), "rolled_back")

    def test_manual_rollback_is_noticed_and_held(self):
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        (self.ctl / "stamp-web").write_text(self.base + "\n")
        other = self.submit("fix/docs", "--deploy", "web", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(other), "blocked")
        self.assertIn("web", self.ticket(a)["rolled_back"])
        self.assertIn("rolled", self.alerts())

    def test_rollback_hold_does_not_touch_other_components(self):
        (self.ctl / "deploy-rc-once").write_text("13")
        self.submit("fix/bad", "--deploy", "web")
        self.run_worker()
        b = self.submit("fix/api", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(b), "live")


class CrashRecovery(Base):
    """The worker dies with SIGKILL at each step. Nothing may be pushed or deployed twice."""

    def test_killed_during_gate_requeues_and_pushes_once(self):
        (self.ctl / "kill-at-gate").touch()
        a = self.submit("fix/a", "--deploy", "web")
        self.assertNotEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.state(a), "pushing")
        self.assertEqual(self.run_worker().returncode, 0)
        t = self.ticket(a)
        self.assertEqual(t["state"], "live")
        self.assertEqual(self.count("deploys.log", "--sha"), 1)
        # One merge of the branch on main.
        log = self.git(self.root / "origin.git", "log", "--oneline", "--merges", "main")
        self.assertEqual(len(log.splitlines()), 1)

    def test_killed_while_its_push_lands_is_not_merged_again(self):
        (self.ctl / "kill-at-gate-pass").touch()
        a = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "pushing")
        landed = self.origin_main()
        self.run_worker()
        t = self.ticket(a)
        self.assertEqual(t["merged_sha"], landed)
        self.assertEqual(t["state"], "live")
        self.assertEqual(self.count("gates.log"), 1)

    def test_killed_after_push_landed_does_not_push_again(self):
        a = self.submit("fix/a", "--deploy", "web")
        (self.ctl / "kill-before-deploy").touch()
        self.assertNotEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.state(a), "deploying")
        self.assertEqual(self.count("gates.log"), 1)
        before = self.origin_main()
        self.run_worker()
        self.assertEqual(self.count("gates.log"), 1)
        self.assertEqual(self.origin_main(), before)
        # Stamps did not move, so the outcome is unknown: alert, and never redeploy.
        self.assertEqual(self.state(a), "unknown")
        self.assertEqual(self.count("deploys.log", "--sha"), 1)
        self.assertIn(a, self.alerts())

    def test_killed_after_deploy_finished_verifies_with_smoke_only(self):
        a = self.submit("fix/a", "--deploy", "web")
        (self.ctl / "kill-after-deploy").touch()
        self.assertNotEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.state(a), "deploying")
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        self.assertEqual(self.count("deploys.log", "--sha"), 1)
        self.assertEqual(self.count("deploys.log", "--smoke-only"), 1)

    def test_smoke_failure_after_crash_is_unknown(self):
        a = self.submit("fix/a", "--deploy", "web")
        (self.ctl / "kill-after-deploy").touch()
        self.run_worker()
        (self.ctl / "smoke-rc").write_text("1")
        self.run_worker()
        self.assertEqual(self.state(a), "unknown")
        self.assertEqual(self.count("deploys.log", "--sha"), 1)

    def test_second_worker_exits_while_one_runs(self):
        import fcntl

        lock = self.root / "state" / "worker.lock"
        lock.parent.mkdir(exist_ok=True)
        with lock.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            a = self.submit("fix/a", "--ride")
            self.assertEqual(self.run_worker().returncode, 0)
            self.assertEqual(self.state(a), "queued")


class Cli(Base):
    def test_wait_exit_codes(self):
        a = self.submit("fix/a", "--ride")
        self.assertEqual(self.sy("wait", a, "--timeout", "0").returncode, 75)
        self.run_worker()
        self.assertEqual(self.sy("wait", a, "--timeout", "0").returncode, 0)
        (self.ctl / "gate-red").touch()
        b = self.submit("fix/b", "--ride", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.sy("wait", b, "--timeout", "0").returncode, 1)
        (self.ctl / "gate-red").unlink()
        self.direct_push("web/file.txt")
        c = self.submit("fix/c", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.sy("wait", c, "--timeout", "0").returncode, 2)
        self.assertNotEqual(self.sy("wait", "999", "--timeout", "0").returncode, 75)

    def test_submit_starts_a_detached_worker(self):
        env = {"SLUICY_NO_SPAWN": ""}
        self.branch("fix/a")
        a = self.sy("submit", "--branch", "fix/a", "--deploy", "web", env=env, check=True).stdout.split()[1]
        res = self.sy("wait", a, "--timeout", "30")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.state(a), "live")

    def test_status_lists_tickets(self):
        a = self.submit("fix/a", "--ride")
        out = self.sy("status", check=True).stdout
        self.assertIn(a, out)
        self.assertIn("fix/a", out)

    def test_npm_ci_runs_only_where_the_lockfile_changed(self):
        a = self.submit("fix/lock", "--ride", path="web/package-lock.json")
        self.run_worker()
        self.assertEqual(self.state(a), "merged")
        self.assertEqual(self.count("npm.log", "web ci"), 1)
        self.assertEqual(self.count("npm.log"), 1)


class Guards(Base):
    def on(self):
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "guards-on").touch()

    def test_off_by_default(self):
        self.assertEqual(self.sy("guard", "push", "abc").returncode, 0)
        self.assertEqual(self.sy("guard", "deploy").returncode, 0)
        self.assertEqual(self.sy("guard", "rollback").returncode, 0)

    def test_push_needs_a_pushing_ticket_with_this_sha(self):
        self.on()
        self.assertNotEqual(self.sy("guard", "push", "abc").returncode, 0)
        self.assertNotEqual(self.sy("guard", "push", "abc", env={"SLUICY_TICKET": "1"}).returncode, 0)

    def test_worker_passes_its_own_guards(self):
        self.on()
        guard = (
            'python3 "$SY_TOOL" guard push "$(git rev-parse HEAD)" || { '
            'echo refused >> "$SY_CTL/refused.log"; exit 1; }\n'
        )
        hook = self.dev / ".githooks" / "pre-push"
        self.git(self.dev, "checkout", "-q", "-B", "hookmod", "origin/main")
        hook.write_text(FAKE_HOOK + guard)
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "guarded hook")
        self.git(self.dev, "push", "-q", "--no-verify", "origin", "hookmod:main")
        self.env["SY_TOOL"] = str(TOOL)
        a = self.submit("fix/a", "--ride", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(a), "merged")
        self.assertEqual(self.count("refused.log"), 0)

    def test_bypass_needs_a_reason_and_is_logged(self):
        self.on()
        self.assertEqual(self.sy("guard", "push", "abc", env={"SLUICY_BYPASS": "outage"}).returncode, 0)
        self.assertNotEqual(self.sy("guard", "push", "abc", env={"SLUICY_BYPASS": " "}).returncode, 0)
        self.assertIn("outage", (self.root / "state" / "bypass.log").read_text())

    def test_rollback_never_needs_the_queue_only_a_reason(self):
        self.on()
        self.assertNotEqual(self.sy("guard", "rollback").returncode, 0)
        self.assertEqual(self.sy("guard", "rollback", env={"SLUICY_BYPASS": "site down"}).returncode, 0)

    def test_refusal_does_not_print_the_bypass(self):
        self.on()
        res = self.sy("guard", "deploy")
        self.assertNotIn("SLUICY_BYPASS", res.stdout + res.stderr)

    def _hook(self, command):
        payload = json.dumps({"tool_name": "Bash", "cwd": str(self.dev), "tool_input": {"command": command}})
        full = {**self.env}
        return subprocess.run(
            ["python3", str(TOOL), "guard", "hook"],
            input=payload,
            env=full,
            capture_output=True,
            text=True,
        )

    def test_session_hook(self):
        self.assertEqual(self._hook("./deploy.sh --web").returncode, 0)  # guards off
        self.on()
        for bad in (
            "./deploy.sh --web",
            "cd x && ./deploy.sh",
            "git push origin main",
            "GIT_SSH_COMMAND='ssh' git push origin main",
            "git push --no-verify origin fix/x",
            "SLUICY_BYPASS=x git status",
        ):
            res = self._hook(bad)
            self.assertEqual(res.returncode, 2, bad)
            self.assertIn("sluicy submit", res.stderr)
            self.assertNotIn("SLUICY_BYPASS=", res.stderr)
        for good in (
            "./deploy.sh --smoke-only",
            "git push origin fix/x",
            "git log origin/main",
            "sluicy status",
            "grep \"a|b\" deploy.sh",
            "cat deploy.sh",
        ):
            self.assertEqual(self._hook(good).returncode, 0, good)


class MemoryBrakes(Base):
    def test_waits_for_memory_before_the_gate_starts(self):
        self.set_memory(4, some=20)
        a = self.submit("fix/a", "--ride")
        worker = subprocess.Popen(["python3", str(TOOL), "run"], cwd=self.dev, env=self.env)
        self.addCleanup(worker.kill)
        for _ in range(100):
            if "waiting for memory" in self.ticket(a)["detail"]:
                break
            time.sleep(0.05)
        t = self.ticket(a)
        self.assertEqual(t["state"], "queued")
        self.assertIn("waiting for memory: 4.0G free", t["detail"])
        self.assertEqual(self.count("gates.log"), 0)
        self.set_memory(64)
        self.assertEqual(worker.wait(timeout=30), 0)
        self.assertEqual(self.state(a), "merged")
        self.assertEqual(self.ticket(a)["detail"], "")
        self.assertEqual(self.count("gates.log"), 1)

    def test_pressure_mid_gate_stops_the_scope_and_requeues(self):
        (self.ctl / "gate-spike-once").touch()
        a = self.submit("fix/a", "--ride")
        self.assertEqual(self.run_worker().returncode, 0)
        t = self.ticket(a)
        self.assertEqual(t["state"], "merged")
        self.assertEqual(t["brake_count"], 1)
        self.assertEqual(self.count("systemctl.log", f"--user stop sluicy-gate-proj-{a}-*"), 1)
        gates = (self.ctl / "gates.log").read_text().splitlines()
        self.assertEqual(len(gates), 2)
        self.assertIn("braked= ", gates[0])
        self.assertIn("braked=1 ", gates[1])
        self.assertIn("braked x1", self.alerts())
        self.assertIn("sluicy brake: memory pressure", Path(t["log_path"]).read_text())

    def test_third_brake_fails_the_ticket(self):
        (self.ctl / "gate-spike-always").touch()
        before = self.origin_main()
        a = self.submit("fix/a", "--ride")
        self.run_worker()
        t = self.ticket(a)
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["brake_count"], 3)
        self.assertIn("memory brakes x3", t["detail"])
        self.assertEqual(self.count("gates.log"), 3)
        self.assertEqual(self.origin_main(), before)

    def test_brief_spike_does_not_brake(self):
        self.set_memory(64, some=50)
        env = {"SLUICY_MIN_AVAIL_GB": "1", "SLUICY_MAX_PSI_SOME": "100", "SLUICY_BRAKE_POLLS": "1000"}
        a = self.submit("fix/a", "--ride")
        self.assertEqual(self.sy("run", env=env).returncode, 0)
        self.assertEqual(self.state(a), "merged")
        self.assertEqual(self.ticket(a)["brake_count"], 0)
        self.assertEqual(self.count("systemctl.log"), 0)

    def test_oomd_killing_the_gate_counts_as_a_brake(self):
        (self.ctl / "gate-oomd-once").touch()
        a = self.submit("fix/a", "--ride")
        self.run_worker()
        t = self.ticket(a)
        self.assertEqual(t["state"], "merged")
        self.assertEqual(t["brake_count"], 1)
        self.assertEqual(self.count("gates.log"), 2)

    def test_red_gate_is_not_a_brake(self):
        (self.ctl / "gate-red").touch()
        a = self.submit("fix/a", "--ride")
        self.run_worker()
        t = self.ticket(a)
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["brake_count"], 0)

class AuditFixes(Base):
    """Fixes from an audit of the queue and the deck."""

    def rollback_hold(self):
        (self.ctl / "deploy-rc-once").write_text("13")
        a = self.submit(f"fix/bad{time.time_ns()}", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(a), "rolled_back")

    def allows(self):
        conn = sqlite3.connect(self.root / "state" / "sluicy.db")
        try:
            return conn.execute("SELECT sha, component FROM allows").fetchall()
        finally:
            conn.close()

    def test_allow_hold_clears_only_the_current_hold(self):
        self.rollback_hold()
        self.sy("allow", "hold", "--component", "web", "--reason", "looked, fine", check=True)
        self.assertEqual(self.allows(), [])
        # A later rollback holds the app again; the earlier allow does not cover it.
        self.rollback_hold()
        other = self.submit("fix/docs", "--deploy", "web", path="docs/file.txt")
        self.run_worker()
        self.assertEqual(self.state(other), "blocked")
        self.assertIn("hold", self.ticket(other)["detail"])

    def test_unstamped_allow_is_spent_once_a_stamp_exists(self):
        (self.ctl / "stamp-api").unlink()
        self.sy("allow", "unstamped", "--component", "api", "--reason", "first deploy", check=True)
        a = self.submit("fix/a", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        self.assertEqual(self.allows(), [("unstamped", "api")])
        b = self.submit("fix/b", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(b), "live")
        self.assertEqual(self.allows(), [])
        # The stamp goes missing again: blocked, not waved through by the old allow.
        (self.ctl / "stamp-api").unlink()
        c = self.submit("fix/c", "--deploy", "api", path="api/file.txt")
        self.run_worker()
        self.assertEqual(self.state(c), "blocked")

    def test_one_ssh_call_per_deploy_decision(self):
        a = self.submit("fix/a", "--deploy", "api,web,admin")
        self.run_worker()
        self.assertEqual(self.state(a), "live")
        self.assertEqual(self.count("ssh.log"), 1)

    def test_gate_gets_project_settings_not_the_callers(self):
        a = self.submit("fix/a", "--ride")
        self.sy("run", env={"VISUALS_OK": "1", "PLAYWRIGHT_WORKERS": "9", "GATE_FAIL_FAST": "0", "GATE_HEADROOM_GB": "7"})
        self.assertEqual(self.state(a), "merged")
        line = (self.ctl / "gates.log").read_text()
        self.assertIn("visuals= ", line)
        self.assertNotIn("pw=9", line)
        self.assertIn("failfast=1", line)
        self.assertIn("headroom=8", line)  # the project's [gate] env, never the caller's
        self.assertIn("scoped=1", line)

    def test_failed_fetch_after_push_leaves_it_for_reconcile(self):
        (self.ctl / "break-fetch").touch()
        a = self.submit("fix/a", "--deploy", "web")
        self.sy("run", env={"SLUICY_FETCH_BACKOFF": "0"})
        t = self.ticket(a)
        self.assertEqual(t["state"], "pushing")
        landed = self.origin_main()
        self.assertEqual(t["merged_sha"], landed)
        self.git(self.root / "checkout", "remote", "set-url", "origin", str(self.root / "origin.git"))
        self.run_worker()
        t = self.ticket(a)
        self.assertEqual(t["state"], "live")
        self.assertEqual(self.count("gates.log"), 1)
        conn = sqlite3.connect(self.root / "state" / "sluicy.db")
        owned = conn.execute("SELECT COUNT(*) FROM ticket_commits WHERE ticket_id = ?", (int(a),)).fetchone()[0]
        conn.close()
        self.assertEqual(owned, 1)

    def orphan(self, seconds):
        """A ticket left in `pushing` by a dead worker whose push group is still running."""
        a = self.submit("fix/a", "--ride")
        proc = subprocess.Popen(["sleep", str(seconds)], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        # A real orphan's parent is dead, so systemd reaps it; here the test is its parent.
        threading.Thread(target=proc.wait, daemon=True).start()
        conn = sqlite3.connect(self.root / "state" / "sluicy.db")
        conn.execute("UPDATE tickets SET state = 'pushing', push_pgid = ? WHERE id = ?", (proc.pid, int(a)))
        conn.commit()
        conn.close()
        return a, proc

    def test_reconcile_waits_for_an_orphaned_push(self):
        a, proc = self.orphan(2)
        started = time.monotonic()
        self.sy("run", env={"SLUICY_ORPHAN_POLL": "0.2"})
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 0)  # finished on its own, not killed
        self.assertEqual(self.state(a), "merged")
        self.assertEqual(self.count("systemctl.log", f"sluicy-gate-proj-{a}-"), 1)

    def test_reconcile_stops_an_orphan_that_never_ends(self):
        a, proc = self.orphan(120)
        self.sy("run", env={"SLUICY_ORPHAN_POLL": "0.2", "SLUICY_ORPHAN_WAIT": "1"})
        proc.wait(timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.state(a), "merged")
        self.assertGreaterEqual(self.count("systemctl.log", f"sluicy-gate-proj-{a}-"), 1)

    def test_wait_restarts_a_dead_worker(self):
        a = self.submit("fix/a", "--ride")
        res = self.sy("wait", a, "--timeout", "30", env={"SLUICY_NO_SPAWN": ""})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.state(a), "merged")

    def test_unexpected_error_fails_one_ticket_and_the_queue_moves_on(self):
        a = self.submit("fix/a", "--ride")
        b = self.submit("fix/b", "--ride", path="docs/file.txt")
        conn = sqlite3.connect(self.root / "state" / "sluicy.db")
        conn.execute("UPDATE tickets SET waivers = 'not json' WHERE id = ?", (int(a),))
        conn.commit()
        conn.close()
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(self.state(a), "failed")
        self.assertIn("JSONDecodeError", self.ticket(a)["detail"])
        self.assertEqual(self.state(b), "merged")


class AutoShip(Base):
    """The Stop hook: a session's own worktree branch, marked `Ship: yes`, enters the queue on its own."""

    def worktree(self, name, path="web/file.txt", subject=None, ship=True):
        self.git(self.dev, "fetch", "-q", "origin", "main")
        wt = self.root / "wt" / name.replace("/", "-")
        self.git(self.dev, "worktree", "add", "-q", "-b", name, str(wt), "origin/main")
        if path:
            self.commit_in(wt, path, subject or f"change {name}", ship=ship)
        return wt

    def commit_in(self, wt, path, subject, ship=True):
        target = wt / path
        target.parent.mkdir(exist_ok=True)
        with target.open("a") as f:
            f.write(f"{subject}\n")
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-qm", subject, *(["-m", "Ship: yes"] if ship else []))
        return self.git(wt, "rev-parse", "HEAD")

    def stop(self, cwd, active=False, project=None, session_id=None, transcript=None):
        payload = {"cwd": str(cwd), "hook_event_name": "Stop", "stop_hook_active": active}
        if session_id:
            payload["session_id"] = session_id
        if transcript:
            payload["transcript_path"] = str(transcript)
        res = subprocess.run(
            ["python3", str(TOOL), "autoship"],
            input=json.dumps(payload),
            cwd=self.root, env={**self.env, "CLAUDE_PROJECT_DIR": str(project or cwd)},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["reason"] if res.stdout.strip() else None

    def tickets(self):
        return self.sy("status", check=True).stdout.splitlines()

    def sql(self, query, *params):
        conn = sqlite3.connect(self.root / "state" / "sluicy.db")
        conn.execute(query, params)
        conn.commit()
        conn.close()

    def test_submits_with_the_components_the_branch_touches_and_ships(self):
        wt = self.worktree("feat/a", path="api/file.txt", ship=False)
        self.commit_in(wt, "web/file.txt", "web too")
        note = self.stop(wt / "web")  # a subdirectory cwd is the same worktree
        self.assertIn("submitted ticket 1", note)
        t = self.ticket("1")
        self.assertEqual((t["mode"], t["components"], t["branch"], t["submitted_by"]), ("deploy", "api,web", "feat/a", "auto"))
        self.assertEqual(t["sha"], self.git(wt, "rev-parse", "HEAD"))
        self.run_worker()
        self.assertEqual(self.state("1"), "live")
        self.assertEqual(self.stamp("api"), self.origin_main())

    def test_hand_submit_is_marked_hand(self):
        self.submit("fix/a", "--ride", path="docs/file.txt")
        self.assertEqual(self.ticket("1")["submitted_by"], "hand")

    def test_no_app_change_rides(self):
        wt = self.worktree("docs/a", path="docs/file.txt")
        self.assertIn("without a deploy", self.stop(wt))
        self.assertEqual(self.ticket("1")["mode"], "ride")

    def test_needs_the_ship_trailer(self):
        wt = self.worktree("feat/a", ship=False)
        self.assertIsNone(self.stop(wt))
        self.assertEqual(self.tickets(), [])

    def test_ship_line_counts_before_a_co_author_paragraph(self):
        wt = self.worktree("feat/a", ship=False)
        self.git(wt, "commit", "-q", "--allow-empty", "-m", "done", "-m", "Ship: yes", "-m", "Co-Authored-By: x <x@y>")
        self.assertIsNotNone(self.stop(wt))
        wt = self.worktree("feat/b", ship=False)
        self.git(wt, "commit", "-q", "--allow-empty", "-m", "done", "-m", "we will ship: yes later")
        self.assertIsNone(self.stop(wt))

    def test_only_the_sessions_own_worktree(self):
        mine = self.worktree("feat/mine")
        theirs = self.worktree("feat/theirs")
        self.assertIsNone(self.stop(theirs, project=mine))
        self.assertIsNone(self.stop(theirs, project=self.dev))
        self.assertEqual(self.tickets(), [])

    # ---- sessions that started in the main checkout and isolated themselves with EnterWorktree

    def entered(self, name):
        """A worktree where EnterWorktree puts one: <main checkout>/.claude/worktrees/<name>."""
        self.git(self.dev, "fetch", "-q", "origin", "main")
        wt = self.dev / ".claude" / "worktrees" / name
        self.git(self.dev, "worktree", "add", "-q", "-b", f"worktree-{name.replace('/', '+')}", str(wt), "origin/main")
        self.commit_in(wt, "web/file.txt", f"change {name}")
        return wt

    def transcript(self, *records):
        p = self.root / f"transcript-{len(list(self.root.glob('transcript-*')))}.jsonl"
        lines = [json.dumps({"type": "user", "message": "worktree-state is mentioned in prose"})]
        for path, sid in records:
            ws = {"worktreePath": str(path), "sessionId": sid, "worktreeBranch": "x"} if path else None
            lines.append(json.dumps({"type": "worktree-state", "sessionId": sid, "worktreeSession": ws}))
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_a_worktree_entered_with_enterworktree_ships(self):
        wt = self.entered("launch")
        note = self.stop(wt, project=self.dev, session_id="s1", transcript=self.transcript((wt, "s1")))
        self.assertIn("submitted ticket 1", note)
        self.assertEqual(self.ticket("1")["branch"], "worktree-launch")

    def test_a_slashed_worktree_name_ships(self):
        wt = self.entered("team/launch")
        self.assertIn("submitted", self.stop(wt, project=self.dev, session_id="s1", transcript=self.transcript((wt, "s1"))))

    def test_another_sessions_entered_worktree_is_refused_and_said_once(self):
        wt = self.entered("theirs")
        tr = self.transcript((wt, "someone-else"))
        self.assertIsNone(self.stop(wt, project=self.dev, session_id="s1", transcript=tr))
        self.assertIsNone(self.stop(wt, project=self.dev, session_id="s1", transcript=tr))
        self.assertEqual(self.tickets(), [])
        self.assertEqual(self.alerts().count("auto-ship skipped worktree-theirs"), 1)
        self.assertIn("entered by another session", self.alerts())

    def test_a_cd_into_a_worktree_is_not_ownership(self):
        wt = self.entered("orphan")
        self.assertIsNone(self.stop(wt, project=self.dev, session_id="s1", transcript=self.transcript()))
        self.assertIsNone(self.stop(wt, project=self.dev, session_id="s1"))  # no transcript at all
        self.assertEqual(self.tickets(), [])
        self.assertIn("did not enter it with EnterWorktree", self.alerts())

    def test_exitworktree_after_enter_is_refused(self):
        wt = self.entered("left")
        tr = self.transcript((wt, "s1"), (None, "s1"))
        self.assertIsNone(self.stop(wt, project=self.dev, session_id="s1", transcript=tr))
        self.assertEqual(self.tickets(), [])

    def test_the_session_entered_a_different_worktree(self):
        mine, theirs = self.entered("mine"), self.entered("theirs")
        tr = self.transcript((mine, "s1"))
        self.assertIsNone(self.stop(theirs, project=self.dev, session_id="s1", transcript=tr))
        self.assertIn("submitted", self.stop(mine, project=self.dev, session_id="s1", transcript=tr))

    def test_an_already_shipped_sha_is_not_reported_as_skipped(self):
        wt = self.entered("done")
        self.stop(wt, project=self.dev, session_id="s1", transcript=self.transcript((wt, "s1")))
        self.assertIsNone(self.stop(wt, project=self.dev, session_id="s2", transcript=self.transcript((wt, "s9"))))
        self.assertNotIn("auto-ship skipped", self.alerts())

    def test_a_failed_hand_ticket_skip_is_said(self):
        wt = self.worktree("feat/hand", ship=False)
        self.sy("submit", "--branch", "feat/hand", "--ride", cwd=wt, check=True)
        self.sql("UPDATE tickets SET state = 'failed'")
        self.commit_in(wt, "web/file.txt", "fix")
        self.assertIsNone(self.stop(wt))
        self.assertIn("auto-ship skipped feat/hand", self.alerts())
        self.assertIn("submit by hand", self.alerts())

    def test_second_stop_on_the_same_commit_submits_nothing(self):
        wt = self.worktree("feat/a")
        self.assertIsNotNone(self.stop(wt))
        self.assertIsNone(self.stop(wt, active=True))
        self.assertIsNone(self.stop(wt))
        self.assertEqual(len(self.tickets()), 1)

    def test_never_submits_inside_a_continuation(self):
        wt = self.worktree("feat/a")
        self.assertIsNone(self.stop(wt, active=True))
        self.assertEqual(self.tickets(), [])
        self.assertIsNotNone(self.stop(wt))

    def test_failed_commit_is_not_resubmitted_but_a_fix_is_and_two_failures_stop_it(self):
        wt = self.worktree("feat/a")
        self.stop(wt)
        (self.ctl / "gate-red").touch()
        self.run_worker()
        self.assertEqual(self.state("1"), "failed")
        self.assertIsNone(self.stop(wt))
        self.commit_in(wt, "web/file.txt", "fix")
        self.assertIn("submitted ticket 2", self.stop(wt))
        self.run_worker()
        self.assertEqual(self.state("2"), "failed")
        self.commit_in(wt, "web/file.txt", "fix again")
        self.assertIn("last 2 auto tickets", self.stop(wt))
        self.assertEqual(len(self.tickets()), 2)

    def test_a_failed_hand_ticket_is_left_to_its_person(self):
        wt = self.worktree("feat/a", ship=False)
        self.sy("submit", "--branch", "feat/a", "--deploy", "web", check=True, cwd=wt)
        (self.ctl / "gate-red").touch()
        self.run_worker()
        self.commit_in(wt, "web/file.txt", "more")
        self.assertIsNone(self.stop(wt))
        self.assertEqual(len(self.tickets()), 1)

    def test_running_or_person_ticket_holds_the_newer_commit_and_says_so_once(self):
        wt = self.worktree("feat/a")
        self.stop(wt)
        self.commit_in(wt, "web/file.txt", "more")
        self.assertIn("ticket 1 for feat/a is queued", self.stop(wt))
        self.assertIsNone(self.stop(wt, active=True))
        self.sql("UPDATE tickets SET state = 'blocked' WHERE id = 1")
        self.assertIn("is blocked", self.stop(wt))
        self.assertEqual(len(self.tickets()), 1)

    def test_never_over_a_component_hold(self):
        self.stop(self.worktree("feat/x", path="docs/file.txt"))  # creates the db
        self.sql("INSERT INTO holds VALUES ('proj', 'web', 'web was rolled back', 0, NULL)")
        note = self.stop(self.worktree("feat/a"))
        self.assertIn("web was rolled back", note)
        self.assertEqual(len(self.tickets()), 2)  # the hold line plus ticket 1
        self.assertIsNotNone(self.stop(self.worktree("feat/api", path="api/file.txt")))

    def test_fetch_failure_submits_nothing(self):
        wt = self.worktree("feat/a")
        self.git(wt, "config", "remote.origin.url", str(self.root / "no-such-origin"))
        self.assertIsNone(self.stop(wt))
        self.assertEqual(self.tickets(), [])

    def test_merge_of_main_alone_is_not_shipped(self):
        wt = self.worktree("feat/a", path=None)
        self.direct_push("docs/file.txt")
        self.git(wt, "fetch", "-q", "origin", "main")
        self.git(wt, "merge", "-q", "--no-ff", "origin/main", "-m", "merge main", "-m", "Ship: yes")
        self.assertIsNone(self.stop(wt))
        self.assertEqual(self.tickets(), [])

    def test_skips_what_is_not_finished_work(self):
        self.assertIsNone(self.stop(self.dev))  # the main checkout
        wt = self.worktree("feat/clean", path=None)
        self.assertIsNone(self.stop(wt))  # nothing main lacks
        wt = self.worktree("feat/dirty")
        (wt / "web" / "file.txt").write_text("edited\n")
        self.assertIsNone(self.stop(wt))  # uncommitted tracked change
        wt = self.worktree("feat/wip", subject="WIP: half done")
        self.assertIsNone(self.stop(wt))
        wt = self.worktree("feat/held")
        (Path(self.git(wt, "rev-parse", "--absolute-git-dir")) / "sluicy-hold").touch()
        self.assertIsNone(self.stop(wt))
        self.assertEqual(self.tickets(), [])

    def test_untracked_files_do_not_hold_it(self):
        wt = self.worktree("feat/a")
        (wt / "scratch.txt").write_text("x\n")
        self.assertIsNotNone(self.stop(wt))

    def test_global_off_switch(self):
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "auto-ship-off").touch()
        self.assertIsNone(self.stop(self.worktree("feat/a")))

    def test_bad_input_never_fails_the_session(self):
        for raw in ("", "not json", "[]", json.dumps({"cwd": str(self.root / "nowhere")})):
            res = subprocess.run(["python3", str(TOOL), "autoship"], input=raw, cwd=self.root,
                                 env={**self.env, "CLAUDE_PROJECT_DIR": str(self.dev)},
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual((res.returncode, res.stdout), (0, ""), raw)
        res = subprocess.run(["python3", str(TOOL), "autoship"], input="{}", cwd=self.root, env=self.env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((res.returncode, res.stdout), (0, ""))  # no project dir: nothing


class Preflight(Base):
    def preflight(self):
        res = self.sy("preflight")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_red_preflight_fails_the_ticket_before_its_turn(self):
        tid = self.submit("fix/red", "--deploy", "web", path="docs/RED")
        self.preflight()
        t = self.ticket(tid)
        self.assertEqual(t["state"], "failed")
        self.assertTrue(t["detail"].startswith("preflight: ✗ A screen changed"), t["detail"])
        self.assertEqual(self.sy("wait", tid, "--timeout", "1").returncode, 1)
        self.assertEqual(self.count("gates.log"), 0)  # never reached the push gate
        self.assertIn(f"ticket {tid}", self.alerts())

    def test_green_preflight_records_the_sha_and_the_gate_still_runs(self):
        tid = self.submit("fix/green", "--deploy", "web")
        self.preflight()
        t = self.ticket(tid)
        self.assertEqual((t["state"], t["preflight_sha"]), ("queued", t["sha"]))
        self.preflight()
        self.assertEqual(self.count("preflight.log"), 1)  # checked once per sha
        self.run_worker()
        self.assertEqual(self.state(tid), "live")
        self.assertEqual(self.count("gates.log"), 1)

    def test_ticket_waivers_reach_the_preflight(self):
        self.branch("fix/waived", "docs/RED")
        tid = self.sy("submit", "--branch", "fix/waived", "--ride", "--waive", "VISUALS_OK=same pixels", check=True).stdout.split()[1]
        self.preflight()
        self.assertEqual(self.state(tid), "queued")
        self.assertEqual(self.count("preflight.log", "visuals=1"), 1)

    def test_conflict_is_reported_by_the_preflight(self):
        tid = self.submit("fix/clash", "--deploy", "web")
        self.direct_push("web/file.txt")
        self.preflight()
        self.assertEqual(self.state(tid), "conflict")

    def test_a_ticket_the_worker_took_meanwhile_is_left_alone(self):
        (self.ctl / "preflight-steal").write_text("")
        tid = self.submit("fix/taken", "--deploy", "web", path="docs/RED")
        self.preflight()
        self.assertEqual(self.state(tid), "merging")

    def test_strays_in_the_preflight_tree_are_moved_out(self):
        tid = self.submit("fix/one", "--ride")
        self.preflight()
        pre = Path(f"{self.root / 'checkout'}-preflight")
        (pre / "docs" / "junk").mkdir()
        (pre / "docs" / "junk" / "RED").write_text("x")
        tid2 = self.submit("fix/two", "--ride")
        self.preflight()
        self.assertEqual(self.state(tid2), "queued")
        self.assertFalse((pre / "docs" / "junk").exists())
        self.assertEqual(self.state(tid), "queued")

    def test_project_without_a_preflight_command_is_skipped(self):
        self.git(self.dev, "checkout", "-q", "main")
        toml = (self.dev / ".sluicy.toml").read_text()
        (self.dev / ".sluicy.toml").write_text(toml.replace('command = ["bash", "scripts/quick-checks.sh"]', "command = []"))
        self.git(self.dev, "commit", "-qam", "no quick checks")
        self.git(self.dev, "push", "-q", "--no-verify", "origin", "main")
        self.git(self.root / "checkout", "pull", "-q", "origin", "main")
        tid = self.submit("fix/oldgate", "--ride", path="docs/RED")
        self.preflight()
        self.assertEqual(self.state(tid), "queued")
        self.assertEqual(self.count("preflight.log"), 0)

    def test_new_commit_after_a_red_is_checked_again(self):
        tid = self.submit("fix/again", "--ride", path="docs/RED")
        self.preflight()
        self.assertEqual(self.state(tid), "failed")
        self.git(self.dev, "rm", "-q", "docs/RED")
        self.git(self.dev, "commit", "-qm", "drop red")
        tid2 = self.sy("submit", "--branch", "fix/again", "--ride", check=True).stdout.split()[1]
        self.preflight()
        self.assertEqual(self.state(tid2), "queued")


class Requeue(Base):
    def red(self, name="fix/red", **kw):
        tid = self.submit(name, "--deploy", "web", path="docs/RED")
        self.assertEqual(self.sy("preflight").returncode, 0)
        self.assertEqual(self.state(tid), "failed")
        return tid

    def test_waiver_requeue_is_recorded_with_who_gave_it(self):
        tid = self.red()
        res = self.sy("requeue", tid, "--waive", "VISUALS_OK=only a dialog changed", "--by", "conductor",
                      "--debt", "visuals=capture-email-assets.ts booking-page")
        self.assertEqual(res.returncode, 0, res.stderr)
        new = res.stdout.split()[1]
        t = self.ticket(new)
        self.assertEqual(json.loads(t["waivers"]), {"VISUALS_OK": "only a dialog changed"})
        self.assertEqual(json.loads(t["waived_by"]), {"VISUALS_OK": f"conductor@{self.dev}"})
        self.assertEqual((t["mode"], t["components"], t["sha"]), ("deploy", "web", self.ticket(tid)["sha"]))
        self.assertTrue(t["submitted_by"].startswith(f"requeue:{tid}:"))
        self.assertIn("VISUALS_OK by conductor", self.alerts())
        self.assertIn("capture-email-assets.ts booking-page", self.sy("debts", check=True).stdout)
        self.sy("preflight")
        self.run_worker()
        self.assertEqual(self.state(new), "live")
        self.assertEqual(self.count("gates.log", "visuals=1"), 1)

    def test_skip_e2e_is_never_waived(self):
        tid = self.red()
        res = self.sy("requeue", tid, "--waive", "SKIP_E2E=please", "--by", "conductor")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("never waives SKIP_E2E", res.stderr)

    def test_waiver_refused_after_a_failure_past_the_quick_checks(self):
        (self.ctl / "gate-red").write_text("")
        tid = self.submit("fix/e2e", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(tid), "failed")
        res = self.sy("requeue", tid, "--waive", "VISUALS_OK=x", "--by", "conductor")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("past the quick checks", res.stderr)
        # A plain retry (same sha, same waivers) is not a bypass, e.g. after a poisoned checkout.
        self.assertEqual(self.sy("requeue", tid, "--by", "conductor").returncode, 0)

    def test_fix_commit_must_contain_the_ticket_commit(self):
        tid = self.red()
        other = self.branch("fix/unrelated")
        res = self.sy("requeue", tid, "--sha", other, "--by", "conductor")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not contain", res.stderr)
        self.git(self.dev, "checkout", "-q", "-B", "elsewhere", "fix/red")
        self.git(self.dev, "rm", "-q", "docs/RED")
        self.git(self.dev, "commit", "-qm", "fix on another branch")
        res = self.sy("requeue", tid, "--sha", self.git(self.dev, "rev-parse", "HEAD"), "--by", "conductor")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("is not on fix/red", res.stderr)
        self.git(self.dev, "checkout", "-q", "fix/red")
        self.git(self.dev, "rm", "-q", "docs/RED")
        self.git(self.dev, "commit", "-qm", "owner fix")
        fix = self.git(self.dev, "rev-parse", "HEAD")
        res = self.sy("requeue", tid, "--sha", fix, "--by", "conductor")
        self.assertEqual(res.returncode, 0, res.stderr)
        new = res.stdout.split()[1]
        self.assertEqual((self.ticket(new)["sha"], self.ticket(new)["branch"]), (fix, "fix/red"))
        self.sy("preflight")
        self.run_worker()
        self.assertEqual(self.state(new), "live")

    def test_only_failed_tickets_requeue(self):
        tid = self.submit("fix/q", "--ride")
        res = self.sy("requeue", tid, "--by", "conductor")
        self.assertNotEqual(res.returncode, 0)

    def test_requeue_chain_is_capped(self):
        (self.ctl / "gate-red").write_text("")
        tid = self.submit("fix/loop", "--ride")
        for _ in range(2):
            self.run_worker()
            self.assertEqual(self.state(tid), "failed")
            res = self.sy("requeue", tid, "--by", "conductor")
            self.assertEqual(res.returncode, 0, res.stderr)
            tid = res.stdout.split()[1]
        self.run_worker()
        res = self.sy("requeue", tid, "--by", "conductor")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("requeued 2 times", res.stderr)


class FailureCapture(Base):
    def test_gate_red_keeps_jest_and_playwright_excerpts_and_the_log(self):
        (self.ctl / "gate-red-jest").write_text("")
        tid = self.submit("fix/jest", "--deploy", "web")
        self.run_worker()
        t = self.ticket(tid)
        self.assertEqual(t["state"], "failed")
        f = t["failure"]
        self.assertIn("✗ Some tests failed", f)
        self.assertIn("1) [web] › tests/web/screens.spec.ts:42:3", f)
        self.assertIn("Error: /payroll shows no data", f)
        self.assertIn("FAIL tests/unit/x.test.ts", f)
        self.assertIn("● x › breaks", f)
        self.assertNotIn("Console", f)
        self.assertEqual(f.count("● x › breaks"), 1)  # Jest's summary repeat is dropped
        kept = self.root / "state" / "logs" / f"{tid}.api-jest.log"
        self.assertTrue(kept.is_file())
        res = self.sy("wait", tid, "--timeout", "1")
        self.assertEqual(res.returncode, 1)
        self.assertIn("failure:", res.stdout)
        self.assertIn("● x › breaks", res.stdout)

    def test_conflict_names_the_conflicting_files(self):
        tid = self.submit("fix/clash", "--deploy", "web")
        self.direct_push("web/file.txt")
        self.run_worker()
        t = self.ticket(tid)
        self.assertEqual(t["state"], "conflict")
        self.assertIn("conflicting files: web/file.txt", t["failure"])

    def test_preflight_red_is_captured_too(self):
        tid = self.submit("fix/pre", "--ride", path="docs/RED")
        self.sy("preflight")
        t = self.ticket(tid)
        self.assertEqual(t["state"], "failed")
        self.assertIn("✗ A screen changed", t["failure"])

    def test_notes_are_printed_as_the_conductors_not_the_users(self):
        tid = self.submit("fix/n", "--ride", path="docs/RED")
        self.sy("preflight")
        c = sqlite3.connect(self.root / "state" / "sluicy.db")
        c.execute("UPDATE tickets SET notes = 'recapture booking-page' WHERE id = ?", (tid,))
        c.commit()
        out = self.sy("wait", tid, "--timeout", "1").stdout
        self.assertIn("note from the sluicy conductor (not the user):\nrecapture booking-page", out)


def load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sluicy_mod", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Conduct(Base):
    def setUp(self):
        super().setUp()
        write_exec(self.root / "bin" / "claude", '#!/bin/bash\ncat "$SY_CTL/agents.json" 2>/dev/null || echo "[]"\n')
        self.env["SLUICY_CLAUDE"] = str(self.root / "bin" / "claude")
        self.env["SLUICY_PROC"] = str(self.root / "proc")
        (self.root / "proc").mkdir()

    def db(self):
        c = sqlite3.connect(self.root / "state" / "sluicy.db")
        c.row_factory = sqlite3.Row
        return c

    def set_state(self, tid, state, detail, age=0):
        c = self.db()
        c.execute("UPDATE tickets SET state = ?, detail = ?, updated_at = ? WHERE id = ?",
                  (state, detail, time.time() - age, tid))
        c.commit()

    def conduct(self):
        res = self.sy("conduct")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.split()

    def cleanup(self):
        return json.loads((self.root / "state" / "cleanup.json").read_text())["items"]

    def test_infra_red_is_requeued_once_with_a_note(self):
        tid = self.submit("fix/infra", "--deploy", "web")
        self.set_state(tid, "failed", "worker error: boom")
        self.assertIn(f"{tid}:infra", self.conduct())
        t = self.ticket(tid)
        self.assertIn("Requeued unchanged", t["notes"])
        new = self.db().execute("SELECT * FROM tickets WHERE id > ?", (int(tid),)).fetchone()
        self.assertEqual((new["sha"], new["state"], new["submitted_by"]), (t["sha"], "queued", f"requeue:{tid}:conductor"))
        self.assertEqual(self.conduct(), [])  # idempotent
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 2)

    def test_infra_red_is_not_requeued_when_memory_is_short(self):
        self.set_memory(4)
        tid = self.submit("fix/mem", "--ride")
        self.set_state(tid, "failed", "memory brakes x3 (pressure); free memory")
        self.conduct()
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        self.assertIn("not requeued again", self.ticket(tid)["notes"])

    def test_quick_red_gets_the_fix_rule_and_an_alert_with_no_owner(self):
        tid = self.submit("fix/q", "--ride", path="docs/RED")
        self.sy("preflight")
        self.conduct()
        t = self.ticket(tid)
        self.assertIn("only when nothing a user can see changed", t["notes"])
        self.assertIn("✗ A screen changed", t["notes"])
        self.assertIn("no session owns it", self.alerts())
        self.assertIn("note from the sluicy conductor", self.sy("wait", tid, "--timeout", "1").stdout)

    def test_a_red_with_a_newer_ticket_is_left_alone(self):
        tid = self.submit("fix/old", "--ride")
        self.set_state(tid, "failed", "gate or push failed; log x")
        self.submit("fix/old", "--ride")
        self.conduct()
        self.assertEqual(self.ticket(tid)["notes"], "")

    def test_held_ticket_alerts_once(self):
        tid = self.submit("fix/h", "--ride")
        self.set_state(tid, "held", "rolled back outside the queue")
        self.conduct()
        self.conduct()
        self.assertEqual(self.alerts().count(f"ticket {tid} (fix/h) is held"), 1)

    def test_owner_is_the_session_in_the_worktree_never_the_main_checkout(self):
        mod = load_tool()
        sessions = [{"name": "a", "cwd": "/r/.claude/worktrees/a/api"}, {"name": "m", "cwd": "/r"}]
        self.assertEqual(mod.owner_of("/r/.claude/worktrees/a", sessions)["name"], "a")
        self.assertIsNone(mod.owner_of("/r/.claude/worktrees/ab", sessions))
        self.assertIsNone(mod.owner_of("/r", sessions))
        stopped = [{"name": "old", "cwd": "/r", "kind": "background"}]  # no pid: reports where it started
        self.assertEqual(mod.owner_of("/r/.claude/worktrees/old", stopped)["name"], "old")
        live = [{"name": "old", "cwd": "/r", "pid": 7}]  # a live one in the main checkout owns nothing
        self.assertIsNone(mod.owner_of("/r/.claude/worktrees/old", live))

    def fake_proc(self, pid, pgrp, rss_kb, cgroup, cwd, cmd):
        d = self.root / "proc" / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (node) S 1 {pgrp} {pgrp} 0 -1\n")
        (d / "status").write_text(f"Name:\tnode\nVmRSS:\t{rss_kb} kB\n")
        (d / "cgroup").write_text(f"0::{cgroup}\n")
        (d / "cmdline").write_text(cmd.replace(" ", "\0"))
        os.symlink(cwd, d / "cwd")

    def test_hog_outside_the_gate_alerts_once_and_spares_the_gate_and_preflight(self):
        gb = 1024 * 1024
        self.fake_proc(100, 100, 2 * gb, "/user.slice/app.slice/term.scope", "/w/shop", "node jest")
        self.fake_proc(101, 100, 2 * gb, "/user.slice/app.slice/term.scope", "/w/shop", "node jest-worker")
        self.fake_proc(200, 200, 8 * gb, "/user.slice/sluicy-gate.slice/sluicy-gate-proj-5-1.scope", "/c", "next-server")
        self.fake_proc(300, 300, 5 * gb, "/user.slice/app.slice/sluicy-preflight.service", "/p", "node")
        (self.ctl / "agents.json").write_text(json.dumps([{"name": "shopper", "cwd": "/w/shop"}]))
        tid = self.submit("fix/g", "--ride")
        self.conduct()
        self.assertNotIn("GB outside the gate", self.alerts())  # no gate running: nothing to protect
        self.set_state(tid, "pushing", "")
        self.conduct()
        self.conduct()
        a = self.alerts()
        self.assertEqual(a.count("GB outside the gate"), 1)
        self.assertIn("4.0 GB outside the gate while it runs: pid 100 (session shopper)", a)
        self.assertIn("never kills", a)

    def test_cleanup_lists_shipped_worktrees_stale_tickets_and_old_strays(self):
        wt = self.root / "repo" / ".claude" / "worktrees" / "done"
        wt.parent.mkdir(parents=True)
        self.git(self.dev, "worktree", "add", "-q", "-b", "fix/wt", str(wt), "origin/main")
        (wt / "docs" / "w.txt").write_text("w\n")
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-qm", "wt")
        tid = self.sy("submit", "--branch", "fix/wt", "--ride", cwd=wt, check=True).stdout.split()[1]
        self.run_worker()
        self.assertEqual(self.state(tid), "merged")
        old = self.submit("fix/stale", "--ride")
        self.set_state(old, "failed", "gate or push failed; log x", age=2 * 86400)
        stray = self.root / "state" / "stray" / "9-1"
        (stray / "tools").mkdir(parents=True)
        (stray / "tools" / "x.json").write_text("{}")
        os.utime(stray, (time.time() - 8 * 86400,) * 2)
        self.conduct()
        kinds = {i["kind"]: i for i in self.cleanup()}
        self.assertEqual(kinds["worktree"]["path"], str(wt))
        self.assertEqual(kinds["tickets"]["tickets"], [int(old)])
        self.assertIn("tools/x.json", kinds["stray"]["label"])
        self.assertIn("thing(s) to clean up", self.alerts())
        # A session still busy in the worktree keeps it off the list; an idle one makes it a session item.
        (self.ctl / "agents.json").write_text(json.dumps([{"name": "done", "cwd": str(wt), "status": "busy"}]))
        self.conduct()
        self.assertNotIn("worktree", {i["kind"] for i in self.cleanup()})
        self.assertNotIn("session", {i["kind"] for i in self.cleanup()})
        for waiting in ({"state": "blocked"}, {"state": "working"}, {"waitingFor": "permission"}):
            (self.ctl / "agents.json").write_text(json.dumps([{"name": "done", "cwd": str(wt), **waiting}]))
            self.conduct()
            self.assertNotIn("session", {i["kind"] for i in self.cleanup()}, waiting)
        # A terminal session cannot be removed from the Deck: not offered, and its worktree is not either.
        (self.ctl / "agents.json").write_text(json.dumps([{"name": "done", "cwd": str(wt), "state": "done", "kind": "interactive"}]))
        self.conduct()
        self.assertFalse({"session", "worktree"} & {i["kind"] for i in self.cleanup()})
        (self.ctl / "agents.json").write_text(json.dumps([{"name": "done", "cwd": str(wt), "state": "done", "kind": "background"}]))
        self.conduct()
        self.assertEqual({i["kind"]: i for i in self.cleanup()}["session"]["name"], "done")
        self.assertFalse((self.root / "state" / "cleanup.tmp").exists())  # written through a tmp file, then renamed

    def test_auto_tickets_are_never_offered_for_cancelling(self):
        hand = self.submit("fix/hand", "--ride")
        self.set_state(hand, "failed", "gate or push failed; log x", age=2 * 86400)
        auto = self.submit("fix/auto", "--ride")
        c = self.db()
        c.execute("UPDATE tickets SET submitted_by = 'auto' WHERE id = ?", (int(auto),))
        c.commit()
        self.set_state(auto, "failed", "gate or push failed; log x", age=2 * 86400)
        self.conduct()
        groups = [i for i in self.cleanup() if i["kind"] == "tickets"]
        self.assertEqual(groups[0]["tickets"], [int(hand)])

    def test_debts_done_refuses_an_unknown_debt(self):
        res = self.sy("debts", "--done", "999")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no open debt 999", res.stderr)

    def test_a_browser_is_never_a_hog(self):
        gb = 1024 * 1024
        self.fake_proc(400, 400, 5 * gb, "/user.slice/app.slice/chrome.scope", "/r", "/opt/google/chrome/chrome http://x")
        tid = self.submit("fix/b", "--ride")
        self.set_state(tid, "pushing", "")
        self.conduct()
        self.assertNotIn("GB outside the gate", self.alerts())

    def test_old_reds_are_one_item_and_a_week_is_the_limit(self):
        a = self.submit("fix/a1", "--ride")
        b = self.submit("fix/b1", "--ride")
        c = self.submit("fix/c1", "--ride")
        self.set_state(a, "failed", "gate or push failed; log x", age=2 * 86400)
        self.set_state(b, "conflict", "does not merge", age=3 * 86400)
        self.set_state(c, "failed", "gate or push failed; log x", age=9 * 86400)
        self.conduct()
        groups = [i for i in self.cleanup() if i["kind"] == "tickets"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["tickets"]), sorted([int(a), int(b)]))

    def test_swc_cache_in_the_preflight_tree_is_not_a_stray(self):
        self.submit("fix/p1", "--ride")
        self.sy("preflight")
        pre = Path(f"{self.root / 'checkout'}-preflight")
        (pre / "web").mkdir(exist_ok=True)
        (pre / "web" / ".swc").mkdir()
        (pre / "web" / ".swc" / "cache").write_text("x")
        self.submit("fix/p2", "--ride")
        self.sy("preflight")
        self.assertTrue((pre / "web" / ".swc" / "cache").exists())
        self.assertNotIn("moved untracked", self.alerts())

    def test_cleanup_skips_a_worktree_that_moved_on_or_is_dirty(self):
        wt = self.root / "repo" / ".claude" / "worktrees" / "busy"
        wt.parent.mkdir(parents=True)
        self.git(self.dev, "worktree", "add", "-q", "-b", "fix/busy", str(wt), "origin/main")
        (wt / "docs" / "b.txt").write_text("b\n")
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-qm", "b")
        self.sy("submit", "--branch", "fix/busy", "--ride", cwd=wt, check=True)
        self.run_worker()
        (wt / "docs" / "b.txt").write_text("edited\n")  # dirty
        self.conduct()
        self.assertEqual([i for i in self.cleanup() if i["kind"] == "worktree"], [])

    def test_worker_runs_conduct_when_the_queue_empties(self):
        tid = self.submit("fix/w", "--ride")
        self.run_worker()
        self.assertEqual(self.state(tid), "merged")
        self.assertTrue((self.root / "state" / "cleanup.json").is_file())


FAKE_CLAUDE_RM = """#!/bin/bash
if [ "$1" = rm ]; then
  echo "$*" >> "$SY_CTL/rm.log"
  [ -f "$SY_CTL/rm-fails" ] && { echo "refused: unpushed" >&2; exit 1; }
  exit 0
fi
cat "$SY_CTL/agents.json" 2>/dev/null || echo "[]"
"""


class AutoClean(Conduct):
    """A finished, stopped background session whose work shipped is removed by the conductor; anything that
    could still be in use, or would lose a file, keeps it."""

    def setUp(self):
        super().setUp()
        write_exec(self.root / "bin" / "claude", FAKE_CLAUDE_RM)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.env["SLUICY_JOBS"] = str(self.jobs)

    def shipped(self, name="early-adopter-proof", title="marketing-outreach", job="j1", age=7200, fetch=True):
        """A worktree under .claude/worktrees whose HEAD shipped, owned by a stopped session named `title`."""
        wt = self.root / "repo" / ".claude" / "worktrees" / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.dev, "worktree", "add", "-q", "-b", f"fix/{name}", str(wt), "origin/main")
        (wt / "docs" / f"{name}.txt").write_text("x\n")
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "-qm", name)
        tid = self.sy("submit", "--branch", f"fix/{name}", "--ride", cwd=wt, check=True).stdout.split()[1]
        self.run_worker()
        self.assertEqual(self.state(tid), "merged")
        c = self.db()
        c.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (time.time() - age, int(tid)))
        c.commit()
        if fetch:
            self.git(self.dev, "fetch", "-q", "origin")
        (self.jobs / job).mkdir()
        (self.jobs / job / "state.json").write_text(json.dumps({"worktreePath": str(wt), "name": title, "state": "done"}))
        self.agents([{"id": job, "name": title, "cwd": str(self.dev), "kind": "background", "state": "done"}])
        return wt, tid

    def agents(self, rows):
        (self.ctl / "agents.json").write_text(json.dumps(rows))

    def rm_calls(self):
        p = self.ctl / "rm.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_owner_found_through_the_job_record_when_names_differ(self):
        mod = load_tool()
        jobs = {"/r/.claude/worktrees/early-adopter-proof": {"id": "j1", "name": "marketing-outreach", "state": "done"}}
        rows = [{"id": "j1", "name": "marketing-outreach", "cwd": "/r", "kind": "background"}]
        self.assertEqual(mod.owner_of("/r/.claude/worktrees/early-adopter-proof", rows, jobs)["id"], "j1")
        # Not in the agents list at all: still owned, never treated as sessionless.
        unlisted = mod.owner_of("/r/.claude/worktrees/early-adopter-proof", [], jobs)
        self.assertTrue(unlisted["unlisted"])

    def test_a_finished_stopped_session_is_removed_with_plain_claude_rm(self):
        self.shipped()
        self.conduct()
        self.assertEqual(self.rm_calls(), ["rm j1"])  # no --discard-unpushed, no --force-remove-worktree
        self.assertEqual([i for i in self.cleanup() if i["kind"] in ("session", "worktree")], [])
        self.assertIn("removed 1 finished session(s)/worktree(s): marketing-outreach", self.alerts())

    def test_an_identical_env_file_does_not_keep_it(self):
        (self.dev / ".git" / "info").mkdir(exist_ok=True)
        (self.dev / ".git" / "info" / "exclude").write_text(".env\n")
        (self.dev / ".env").write_text("A=1\n")
        wt, _ = self.shipped()
        (wt / ".env").write_text("A=1\n")
        self.conduct()
        self.assertEqual(self.rm_calls(), ["rm j1"])

    def test_anything_still_in_use_or_holding_a_file_keeps_it(self):
        def keep_running_session(wt):
            self.agents([{"id": "j1", "name": "t", "cwd": str(self.dev), "kind": "background", "state": "done", "pid": 99}])

        def keep_working(wt):
            self.agents([{"id": "j1", "name": "t", "cwd": str(self.dev), "kind": "background", "state": "working"}])

        def keep_untracked(wt):
            (wt / "docs" / "new.txt").write_text("source\n")

        def keep_ignored(wt):
            (self.dev / ".git" / "info").mkdir(exist_ok=True)
            (self.dev / ".git" / "info" / "exclude").write_text("out/\n.env\n")
            (wt / "out").mkdir()
            (wt / "out" / "video.mp4").write_text("v")

        def keep_differing_env(wt):
            (self.dev / ".git" / "info").mkdir(exist_ok=True)
            (self.dev / ".git" / "info" / "exclude").write_text(".env\n")
            (self.dev / ".env").write_text("A=1\n")
            (wt / ".env").write_text("A=2\n")

        def keep_process(wt):
            self.fake_proc(500, 500, 1000, "/user.slice/app.slice/term.scope", str(wt / "e2e"), "npm run dev")

        def keep_locked(wt):
            self.git(self.dev, "worktree", "lock", str(wt))

        def keep_off(wt):
            (self.root / "state" / "auto-clean-off").write_text("")

        def keep_no_agents(wt):
            self.agents([])

        cases = [keep_running_session, keep_working, keep_untracked, keep_ignored, keep_differing_env,
                 keep_process, keep_locked, keep_off, keep_no_agents]
        for i, case in enumerate(cases):
            with self.subTest(case.__name__):
                for p in (self.ctl / "rm.log", self.root / "state" / "auto-clean-off"):
                    p.unlink(missing_ok=True)
                wt, _ = self.shipped(name=f"wt{i}", job=f"j{i}")
                for d in (self.root / "proc").iterdir():
                    shutil.rmtree(d)
                case(wt)
                self.conduct()
                self.assertEqual(self.rm_calls(), [], case.__name__)
                self.assertTrue(wt.is_dir(), case.__name__)
                if case is keep_locked:
                    self.git(self.dev, "worktree", "unlock", str(wt))
                shutil.rmtree(self.jobs / f"j{i}")

    def test_a_worktree_with_no_session_at_all_is_removed_by_git_and_agent_trees_are_not(self):
        wt, _ = self.shipped(name="orphan", job="j1")
        shutil.rmtree(self.jobs / "j1")
        sub, _ = self.shipped(name="agent-a35c", job="j2")
        shutil.rmtree(self.jobs / "j2")
        self.agents([{"id": "other", "name": "x", "cwd": str(self.dev), "kind": "background", "state": "done"}])
        self.conduct()
        self.assertFalse(wt.exists())
        self.assertTrue(sub.is_dir())
        self.assertEqual(self.rm_calls(), [])
        self.assertIn("fix/orphan", self.git(self.dev, "branch", "--list", "fix/orphan"))  # branch kept

    def test_too_recent_or_not_on_origin_main_keeps_it(self):
        self.shipped(name="young", job="j1", age=60)
        self.conduct()
        self.assertEqual(self.rm_calls(), [])
        item = next(i for i in self.cleanup() if i["kind"] == "session")
        self.assertEqual(item["autoClean"], "shipped under an hour ago")

    def test_not_fetched_yet_keeps_it(self):
        self.shipped(name="stale-ref", fetch=False)
        self.conduct()
        self.assertEqual(self.rm_calls(), [])
        item = next(i for i in self.cleanup() if i["kind"] == "session")
        self.assertEqual(item["autoClean"], "not on origin/main yet")

    def test_a_refused_rm_keeps_the_item_and_alerts_once(self):
        self.shipped()
        (self.ctl / "rm-fails").write_text("")
        self.conduct()
        self.conduct()
        item = next(i for i in self.cleanup() if i["kind"] == "session")
        self.assertTrue(item["autoClean"].startswith("claude rm failed: refused: unpushed"))
        self.assertEqual(self.alerts().count("auto-clean kept early-adopter-proof"), 1)


class Supersede(Base):
    def test_newer_commit_supersedes_the_queued_one_and_wait_exits_4(self):
        old = self.submit("fix/s", "--deploy", "web")
        (self.dev / "web" / "more.txt").write_text("more\n")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "on top")
        new = self.sy("submit", "--branch", "fix/s", "--deploy", "web", check=True).stdout.splitlines()[-1].split()[1]
        self.assertEqual(self.state(old), "superseded")
        res = self.sy("wait", old, "--timeout", "1")
        self.assertEqual(res.returncode, 4)
        self.assertIn(f"superseded by {new}", res.stdout)
        self.run_worker()
        self.assertEqual(self.state(new), "live")
        self.assertEqual(self.count("gates.log"), 1)

    def test_a_different_request_is_not_superseded(self):
        old = self.submit("fix/d", "--deploy", "web")
        self.submit("fix/d", "--deploy", "api,web")
        self.assertEqual(self.state(old), "queued")
        hold = self.submit("fix/h", "--no-deploy-until-released")
        self.submit("fix/h", "--ride")
        self.assertEqual(self.state(hold), "queued")

    def test_an_unrelated_sha_is_not_superseded(self):
        old = self.submit("fix/u", "--ride")
        self.git(self.dev, "checkout", "-q", "-B", "fix/u", "origin/main")  # rewritten branch
        (self.dev / "docs" / "other.txt").write_text("x\n")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-qm", "rewrite")
        self.sy("submit", "--branch", "fix/u", "--ride", check=True)
        self.assertEqual(self.state(old), "queued")

    def test_a_requeue_never_supersedes_newer_work(self):
        (self.ctl / "gate-red").write_text("")
        old = self.submit("fix/r", "--ride")
        self.run_worker()
        (self.ctl / "gate-red").unlink()
        newer = self.submit("fix/r", "--ride")
        self.assertEqual(self.sy("requeue", old, "--by", "x").returncode, 0)
        self.assertEqual(self.state(newer), "queued")

    def test_the_same_sha_twice_is_refused(self):
        self.submit("fix/twice", "--deploy", "web")
        res = self.sy("submit", "--branch", "fix/twice", "--deploy", "web")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("already ticket", res.stderr)

    def test_worker_skips_a_ticket_that_ended_while_queued(self):
        tid = self.submit("fix/c", "--deploy", "web")
        self.assertEqual(self.sy("cancel", tid, "--reason", "not needed").returncode, 0)
        self.run_worker()
        self.assertEqual(self.state(tid), "cancelled")
        self.assertEqual(self.count("gates.log"), 0)
        self.assertEqual(self.sy("wait", tid, "--timeout", "1").returncode, 4)

    def test_cancel_refuses_a_running_or_finished_ticket(self):
        tid = self.submit("fix/l", "--ride")
        self.run_worker()
        res = self.sy("cancel", tid, "--reason", "late")
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.state(tid), "merged")


FAKE_ON_LIVE = """\\
#!/bin/bash
echo "ticket=$SLUICY_TICKET project=$SLUICY_PROJECT sha=$SLUICY_MERGED_SHA comps=$SLUICY_COMPONENTS old_web=$SLUICY_OLD_STAMP_WEB pwd=$PWD" >> "$SY_CTL/on_live.log"
exit "$(cat "$SY_CTL/on-live-rc" 2>/dev/null || echo 0)"
"""


class OnLive(Base):
    EXTRA_TOML = '\n[hooks]\non_live = ["bash", "scripts/on-live.sh"]\n'
    EXTRA_FILES = {"scripts/on-live.sh": FAKE_ON_LIVE}

    def test_runs_after_a_deploy_goes_live_with_the_old_stamp(self):
        tid = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(tid), "live")
        line = (self.ctl / "on_live.log").read_text()
        t = self.ticket(tid)
        self.assertIn(f"ticket={tid} project=proj sha={t['merged_sha']} comps=web old_web={self.base}", line)
        self.assertIn(f"pwd={self.root / 'checkout'}", line)

    def test_a_failing_hook_never_changes_the_outcome(self):
        (self.ctl / "on-live-rc").write_text("3")
        tid = self.submit("fix/a", "--deploy", "web")
        self.run_worker()
        self.assertEqual(self.state(tid), "live")
        self.assertIn(f"on_live for ticket {tid} exited 3", (self.root / "state" / "alerts.log").read_text())

    def test_not_run_for_a_merge_without_a_deploy(self):
        tid = self.submit("fix/a", "--ride")
        self.run_worker()
        self.assertEqual(self.state(tid), "merged")
        self.assertFalse((self.ctl / "on_live.log").exists())


class PushIsTheDeploy(Base):
    """A project whose host builds every push to main (deploy.kind = "none")."""
    TOML = 'project = "proj"\n[components.web]\npath = "web"\n'

    def test_deploy_is_refused_and_ride_ends_merged(self):
        self.branch("fix/a")
        res = self.sy("submit", "--branch", "fix/a", "--deploy", "web")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("push is the deploy", res.stderr)
        tid = self.submit("fix/b", "--ride")
        self.run_worker()
        self.assertEqual(self.state(tid), "merged")
        self.assertEqual(self.count("deploys.log"), 0)
        self.assertEqual(self.count("ssh.log"), 0)


class TwoProjects(Base):
    """One queue, one worker, two repositories with a component of the same name."""

    def setUp(self):
        super().setUp()
        origin = self.root / "origin2.git"
        self.git(self.root, "init", "-q", "--bare", str(origin))
        self.dev2 = self.root / "dev2"
        self.git(self.root, "clone", "-q", str(origin), str(self.dev2))
        (self.dev2 / "web").mkdir()
        (self.dev2 / "web" / "file.txt").write_text("base\n")
        (self.dev2 / ".githooks").mkdir()
        write_exec(self.dev2 / ".githooks" / "pre-push", FAKE_HOOK)
        (self.dev2 / ".sluicy.toml").write_text('project = "other"\n[components.web]\npath = "web"\n')
        self.git(self.dev2, "add", "-A")
        self.git(self.dev2, "commit", "-qm", "base")
        self.git(self.dev2, "push", "-q", "origin", "main")
        self.sy("init", "--no-install", "--checkout", str(self.root / "checkout2"), check=True, cwd=self.dev2)

    def submit2(self, name):
        self.git(self.dev2, "checkout", "-q", "-B", name, "origin/main")
        with (self.dev2 / "web" / "file.txt").open("a") as f:
            f.write(name + "\n")
        self.git(self.dev2, "commit", "-qam", name)
        res = self.sy("submit", "--branch", name, "--ride", check=True, cwd=self.dev2)
        return res.stdout.split()[1]

    def test_fifo_across_projects_and_holds_stay_in_their_project(self):
        a = self.submit("fix/a", "--deploy", "web")
        b = self.submit2("fix/b")
        c = sqlite3.connect(self.root / "state" / "sluicy.db")
        c.execute("INSERT INTO holds VALUES ('other', 'web', 'held elsewhere', 0, NULL)")
        c.commit()
        self.run_worker()
        self.assertEqual(self.state(a), "live")  # the other project's hold on its own `web` does not apply
        self.assertEqual(self.state(b), "merged")
        self.assertEqual([self.ticket(a)["project"], self.ticket(b)["project"]], ["proj", "other"])
        gates = (self.ctl / "gates.log").read_text().splitlines()
        self.assertEqual(len(gates), 2)  # one gate each, one after the other
        status = self.sy("status", check=True).stdout
        self.assertIn("HOLD other/web", status)

    def test_status_filters_by_project(self):
        self.submit("fix/a", "--ride")
        self.submit2("fix/b")
        out = self.sy("status", "--project", "other", check=True).stdout
        self.assertIn("fix/b", out)
        self.assertNotIn("fix/a", out)


class NoConfig(unittest.TestCase):
    """A repository without .sluicy.toml: every hook returns at once and does nothing."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "state").mkdir()
        (self.root / "state" / "guards-on").write_text("")
        self.repo = self.root / "plain"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = {**os.environ, "HOME": str(self.root), "SLUICY_HOME": str(self.root / "state"),
                    "CLAUDE_PROJECT_DIR": str(self.repo), "SLUICY_NO_SPAWN": "1"}

    def hook(self, *args, data):
        return subprocess.run(["python3", str(TOOL), *args], input=json.dumps(data), env=self.env,
                              capture_output=True, text=True, cwd=self.repo, timeout=30)

    def test_guard_allows_everything(self):
        res = self.hook("guard", "hook", data={"cwd": str(self.repo), "tool_input": {"command": "git push origin main --no-verify"}})
        self.assertEqual(res.returncode, 0)

    def test_autoship_does_nothing(self):
        res = self.hook("autoship", data={"cwd": str(self.repo), "session_id": "s"})
        self.assertEqual((res.returncode, res.stdout), (0, ""))
        self.assertFalse((self.root / "state" / "sluicy.db").exists())

    def test_guard_hook_is_fast(self):
        start = time.monotonic()
        for _ in range(5):
            self.hook("guard", "hook", data={"cwd": str(self.repo), "tool_input": {"command": "ls"}})
        self.assertLess((time.monotonic() - start) / 5, 0.25)


class GuardRegex(unittest.TestCase):
    def setUp(self):
        self.mod = load_tool()
        self.cfg = {"repo": {"branch": "main"}, "guard": {"deploy_patterns": [r"(^|[\s/])deploy\.sh\b"], "allow_args": [r"--smoke-only"]}}

    def test_quoted_pipe_is_not_a_pipe(self):
        self.assertIsNone(self.mod.refusal('grep "a|b" deploy.sh', self.cfg))
        self.assertIsNone(self.mod.refusal("git commit -m 'push to main later'", self.cfg))

    def test_still_refuses(self):
        self.assertIsNotNone(self.mod.refusal("./deploy.sh --web", self.cfg))
        self.assertIsNotNone(self.mod.refusal("ls | ./deploy.sh", self.cfg))
        self.assertIsNotNone(self.mod.refusal("git push origin HEAD:main", self.cfg))
        self.assertIsNone(self.mod.refusal("git push origin worktree-main-fix", self.cfg))
        self.assertIsNone(self.mod.refusal("./deploy.sh --smoke-only", self.cfg))


if __name__ == "__main__":
    unittest.main(verbosity=1)
