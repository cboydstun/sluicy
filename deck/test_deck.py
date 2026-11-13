"""Session Deck tests: the deck binary over HTTP, with a fake claude and sluicy on PATH and a temporary
queue database.

    cargo build --release --manifest-path deck/Cargo.toml
    DECK_BIN=deck/target/release/deck python3 deck/test_deck.py
"""
import base64
import http.client
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Claude Code's Bash tool runs commands with SIGTERM and SIGHUP blocked, and children inherit the mask, so
# the deck's SIGTERM could not stop the test's `sleep` and the kill test timed out. Unblock for our children.
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGHUP})
BIN = str(Path(os.environ.get("DECK_BIN") or HERE / "target" / "release" / "deck").resolve())
if not Path(BIN).is_file():
    sys.exit(f"no deck binary at {BIN}: cargo build --release, or set DECK_BIN")
sys.path.insert(0, str(HERE.parent / "sluicy"))

FAKE_CLAUDE = """\
#!/bin/bash
echo "$*" >> "$DECK_CTL/claude.log"
case "$1" in
  agents) sleep "$(cat "$DECK_CTL/agents-delay" 2>/dev/null || echo 0)"; cat "$DECK_CTL/agents.json" ;;
  attach) exec cat ;;
  --bg) echo "started bg1" ;;
  logs) printf '\\033[1mlog line\\033[0m for %s\\n' "$2" ;;
  stop|rm) echo "ok $1 $2" ;;
esac
"""

FAKE_SLUICY = """\
import json, sys, os
args = sys.argv[1:]
if args[:3] == ["config", "check", "--json"]:
    print(json.dumps({"components": {"api": {}, "web": {}, "admin": {}},
                      "gate": {"waivers": ["VISUALS_OK", "CHANGELOG_OK", "DEMO_OK", "MARKETING_OK", "SKIP_E2E"]}}))
    sys.exit(0)
if args[:2] == ["stamps", "--json"]:
    print(json.dumps({"ok": True, "configured": True, "stamps": {"api": "abc123", "web": "def456", "admin": "none"},
                      "lease": ["free"]}))
    sys.exit(0)
open(os.path.join(os.environ["DECK_CTL"], "sluicy.log"), "a").write(" ".join(args) + "\\n")
print("ticket 9 ok")
"""


def write_exec(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class DeckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        r = cls.root
        cls.ctl = r / "ctl"
        cls.ctl.mkdir()
        (r / "bin").mkdir()
        write_exec(r / "bin" / "claude", FAKE_CLAUDE)
        (r / "sluicy_fake.py").write_text(FAKE_SLUICY)
        # A repo with origin, one merged clean worktree and one dirty one.
        os.environ.update(
            {
                "GIT_CONFIG_GLOBAL": str(r / "gitconfig"),
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        (r / "gitconfig").write_text("[user]\n\temail = t@example.com\n\tname = t\n[init]\n\tdefaultBranch = main\n")
        sh(r, "git", "init", "-q", "--bare", "origin.git")
        sh(r, "git", "clone", "-q", "origin.git", "repo")
        cls.repo = r / "repo"
        (cls.repo / "a.txt").write_text("a\n")
        sh(cls.repo, "git", "add", "-A")
        sh(cls.repo, "git", "commit", "-qm", "base")
        sh(cls.repo, "git", "push", "-q", "origin", "main")
        sh(cls.repo, "git", "branch", "fix/merged")
        sh(cls.repo, "git", "worktree", "add", "-q", str(r / "wt-merged"), "fix/merged")
        sh(cls.repo, "git", "worktree", "add", "-q", "-b", "fix/dirty", str(r / "wt-dirty"))
        (r / "wt-dirty" / "b.txt").write_text("dirty\n")
        # A stopped background session keeps its worktree where `claude --bg -w` put it.
        sh(cls.repo, "git", "worktree", "add", "-q", "-b", "worktree-old-task", str(cls.repo / ".claude" / "worktrees" / "old-task"))
        sh(cls.repo, "git", "fetch", "-q", "origin")
        # Sessions: one interactive (this test process stands in for it), one running background
        # and one stopped background session (no pid; `claude agents --json --all` lists it).
        cls.sleeper = subprocess.Popen(["sleep", "300"])
        agents = [
            {"pid": os.getpid(), "cwd": str(r / "wt-dirty"), "kind": "interactive", "sessionId": "s-int", "name": "my-feature", "status": "busy", "startedAt": 1000},
            {"pid": cls.sleeper.pid, "cwd": str(cls.repo), "kind": "background", "sessionId": "s-bg", "id": "bg1", "name": "bg-task", "status": "idle", "startedAt": 2000},
            {"cwd": str(cls.repo), "kind": "background", "sessionId": "s-done", "id": "done1", "name": "old-task", "state": "done", "startedAt": 1500},
        ]
        (cls.ctl / "agents.json").write_text(json.dumps(agents))
        (r / "sessions").mkdir()
        (r / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "statusUpdatedAt": 5000}))
        proj = r / "projects" / str(r / "wt-dirty").replace("/", "-").replace(".", "-").replace("_", "-")
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"content": "please fix it"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed the login bug.\nDetails follow."}]}},
            {"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}},
        ]
        (proj / "s-int.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        bg = r / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cls.repo))
        bg.mkdir(parents=True)
        bg_lines = [
            {"type": "user", "message": {"content": "ship it"}, "timestamp": "2026-11-04T13:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git   status", "description": "Show status"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "clean"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Shipped."}]}},
        ]
        (bg / "s-bg.jsonl").write_text("\n".join(json.dumps(x) for x in bg_lines) + "\n")
        old = r / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cls.repo / ".claude" / "worktrees" / "old-task"))
        old.mkdir(parents=True)
        (old / "s-done.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done with the old task."}]}}) + "\n")
        # Queue database through the real sluicy schema.
        cls.sy_home = r / "sluicy"
        os.environ["SLUICY_HOME"] = str(cls.sy_home)
        import sluicy

        conn = sluicy.db()
        conn.execute(
            "INSERT INTO tickets (project, branch, sha, source, mode, components, state, created_at, updated_at) VALUES ('proj','fix/dirty','x','y','deploy','web','blocked',1,1)"
        )
        conn.execute(
            "UPDATE tickets SET impact = ?, impact_sha = 'x' WHERE id = 1",
            (json.dumps({"files": ["web/a.ts"], "communities": [{"name": "web", "files": ["web/a.ts"] * 50}], "god_nodes": [], "rule_hits": {}}),),
        )
        conn.commit()
        (cls.sy_home / "logs").mkdir()
        (cls.sy_home / "logs" / "1.log").write_text("\x1b[0;32mgate ok\x1b[0m\n")
        (cls.sy_home / "alerts.log").write_text("ticket 1 blocked\n")
        (r / "push.lock").write_text("pid 1, x, since 10:00\n")
        os.environ.update(
            {
                "PATH": f"{r / 'bin'}:{os.environ['PATH']}",
                "DECK_CTL": str(cls.ctl),
                "DECK_REPO": str(cls.repo),
                "DECK_SLUICY": str(r / "sluicy_fake.py"),
                "DECK_SESSIONS_DIR": str(r / "sessions"),
                "DECK_PROJECTS_DIR": str(r / "projects"),
                "DECK_LOCKS": f"push gate={r / 'push.lock'};e2e={r / 'e2e.lock'}",
                "DECK_GATE_LOGS": str(r / "gatelogs"),
                "DECK_MEM_WARN_PCT": "101",
                # Most tests change the fake sessions list between refreshes; the cache test turns it on.
                "DECK_AGENTS_TTL": "0",
            }
        )
        cls.token = "tok-" + "x" * 20
        cls.backends = []
        cls.port = cls.start_backend(18765 + os.getpid() % 1000)

    @classmethod
    def start_backend(cls, port, **env):
        """A deck binary on `port` with `env` added to the environment."""
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            proc = subprocess.Popen([BIN, "--port", str(port), "--no-open", "--token", cls.token], stdout=subprocess.DEVNULL)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    probe = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    probe.connect()
                    probe.close()
                    break
                except OSError:
                    time.sleep(0.05)
            cls.backends.append(("bin", proc))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return port

    @classmethod
    def tearDownClass(cls):
        for kind, b in cls.backends:
            if kind == "bin":
                b.terminate()  # SIGTERM hangs up its terminals
                b.wait(10)
            else:
                d, server = b
                server.shutdown()
                server.server_close()
                for t in d.terms.values():
                    t.close()
        cls.sleeper.kill()
        cls.sleeper.wait()
        shutil.rmtree(cls.root, True)

    def req(self, method, path, body=None, token=True, origin=True, host=None, port=None):
        port = port or self.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if token:
            headers["X-Deck-Token"] = self.token
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        raw = res.read()
        try:
            return res.status, json.loads(raw)
        except ValueError:
            return res.status, raw

    def act(self, name, body, port=None):
        return self.req("POST", f"/api/action/{name}", body, port=port)

    def state(self, port=None):
        code, s = self.req("GET", "/api/state", port=port)
        self.assertEqual(code, 200)
        return s

    def log(self, name):
        p = self.ctl / name
        return p.read_text() if p.exists() else ""

    # ---- access
    def test_needs_token(self):
        self.assertEqual(self.req("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/", token=False)[0], 403)
        self.assertEqual(self.req("GET", f"/?t={self.token}", token=False)[0], 200)

    def test_post_needs_matching_origin_and_host(self):
        self.assertEqual(self.req("POST", "/api/action/release", {"ticket": 1}, origin=False)[0], 403)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/action/release", body=b"{}", headers={"Host": f"127.0.0.1:{self.port}", "X-Deck-Token": self.token, "Origin": "http://evil.example"})
        self.assertEqual(conn.getresponse().status, 403)
        self.assertEqual(self.req("GET", "/api/state", host="evil.example")[0], 403)

    # ---- collectors
    def test_sessions_joined(self):
        s = {x["name"]: x for x in self.state()["sessions"]}
        me = s["my-feature"]
        self.assertEqual(me["worktree"]["branch"], "fix/dirty")
        self.assertEqual(me["worktree"]["dirty"], 1)
        self.assertEqual((me["worktree"]["ahead"], me["worktree"]["behind"]), ("0", "0"))
        self.assertEqual(me["ticket"]["state"], "blocked")
        self.assertEqual(me["last"], "Fixed the login bug.")
        self.assertGreater(me["rss"], 0)
        self.assertEqual(me["statusSince"], 5)
        self.assertFalse(me["attachable"])
        self.assertTrue(s["bg-task"]["attachable"])
        self.assertFalse(s["bg-task"]["stopped"])
        self.assertIsNone(s["bg-task"]["ticket"])

    def test_stopped_background_session_stays_listed(self):
        # Stop used to drop the row, leaving its worktree with no Remove button.
        s = {x["name"]: x for x in self.state()["sessions"]}["old-task"]
        self.assertTrue(s["stopped"])
        self.assertTrue(s["attachable"])  # attach wakes it
        self.assertEqual(s["status"], "stopped")
        self.assertEqual(s["worktree"]["branch"], "worktree-old-task")
        self.assertEqual(s["rss"], 0)
        self.assertEqual(s["last"], "Done with the old task.")  # read from the worktree's folder
        self.assertIn("agents --json --all", self.log("claude.log"))
        self.assertEqual(self.act("session_rm", {"id": "done1"})[0], 200)
        self.assertIn("rm done1", self.log("claude.log"))

    def test_queue_system_worktrees(self):
        st = self.state()
        self.assertEqual(st["queue"]["tickets"][0]["branch"], "fix/dirty")
        self.assertEqual(st["queue"]["tickets"][0]["map"], "1 file · 1 area")
        self.assertNotIn("impact", st["queue"]["tickets"][0])  # the full map stays out of the page
        self.assertIn("ticket 1 blocked", st["queue"]["alerts"][0])
        sys_ = st["system"]
        self.assertIn("Only", sys_["warnings"][0])
        self.assertFalse(sys_["locks"]["push gate"]["held"])
        self.assertTrue(sys_["top"])
        trees = {Path(t["path"]).name: t for t in st["worktrees"]}
        self.assertTrue(trees["wt-merged"]["merged"])
        self.assertEqual(trees["wt-dirty"]["dirty"], 1)
        self.assertEqual(trees["wt-dirty"]["sessions"], ["my-feature"])
        self.assertIn("fix/dirty", st["branches"])
        for _ in range(40):
            if self.state()["queue"]["vps"]["stamps"]:
                break
            time.sleep(0.1)
        self.assertEqual(self.state()["queue"]["vps"]["stamps"]["web"], "def456")

    def test_held_lock_is_shown(self):
        import fcntl

        with open(self.root / "push.lock") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lock = self.state()["system"]["locks"]["push gate"]
        self.assertTrue(lock["held"])
        self.assertIn("since 10:00", lock["holder"])

    # ---- queue actions
    def test_submit_builds_argv(self):
        self.state()
        code, out = self.act("submit", {"branch": "fix/dirty", "mode": "deploy", "components": ["web", "api", "bogus"], "waivers": {"VISUALS_OK": "copy only"}})
        self.assertEqual(code, 200, out)
        self.assertIn("submit --branch fix/dirty --deploy web,api --waive VISUALS_OK=copy only", self.log("sluicy.log"))

    def test_submit_refusals(self):
        self.state()
        self.assertEqual(self.act("submit", {"branch": "not-a-branch", "mode": "ride"})[0], 400)
        self.assertEqual(self.act("submit", {"branch": "fix/dirty", "mode": "deploy", "components": []})[0], 400)
        self.assertEqual(self.act("submit", {"branch": "fix/dirty", "mode": "ride", "waivers": {"PATH": "x"}})[0], 400)
        self.assertEqual(self.act("submit", {"branch": "fix/dirty", "mode": "ride", "waivers": {"DEMO_OK": " "}})[0], 400)

    def test_allow_and_release(self):
        self.assertEqual(self.act("allow", {"target": "unstamped", "component": "api"})[0], 400)
        self.assertEqual(self.act("allow", {"target": "; rm -rf", "component": "api", "reason": "x"})[0], 400)
        self.assertEqual(self.act("allow", {"target": "abc1234", "component": "api", "reason": "reviewed"})[0], 200)
        self.assertEqual(self.act("release", {"ticket": "3"})[0], 200)
        self.assertEqual(self.act("release", {"ticket": "3; x"})[0], 400)
        log = self.log("sluicy.log")
        self.assertIn("allow abc1234 --component api --reason reviewed", log)
        self.assertIn("release 3", log)

    def test_ticket_log_strips_colour(self):
        code, out = self.act("ticket_log", {"ticket": 1})
        self.assertEqual(out["output"], "gate ok\n")

    # ---- sessions
    def test_start_session(self):
        def start(body):
            before = len(self.log("claude.log"))
            code, out = self.act("session_start", body)
            return code, out, self.log("claude.log")[before:]

        # Default: "Ask questions first" appends the brainstorming preamble to the system prompt;
        # the task itself is passed untouched.
        code, out, tail = start({"name": "new-thing", "prompt": "do the thing"})
        self.assertEqual(code, 200, out)
        self.assertIn("--bg --permission-mode plan --append-system-prompt ", tail)
        self.assertIn("Ask questions first", tail)
        self.assertIn(" -n new-thing -w new-thing do the thing", tail)
        # Switched off, the argv is exactly the plain one.
        code, out, tail = start({"name": "plain", "prompt": "do it", "brainstorm": False})
        self.assertEqual(code, 200, out)
        self.assertIn("--bg --permission-mode plan -n plain -w plain do it", tail)
        self.assertNotIn("--append-system-prompt", tail)
        # Spaces and other characters become dashes: the name is the worktree and branch.
        code, _, tail = start({"name": " Fix  Login_now! ", "prompt": "go", "brainstorm": False})
        self.assertEqual(code, 200)
        self.assertIn("--bg --permission-mode plan -n fix-login-now -w fix-login-now go", tail)
        # Without a worktree the session still starts in plan mode.
        code, _, tail = start({"name": "here", "prompt": "go", "worktree": False, "brainstorm": False})
        self.assertEqual(code, 200)
        self.assertIn("--bg --permission-mode plan -n here go", tail)
        self.assertEqual(self.act("session_start", {"name": "!!!", "prompt": "x"})[0], 400)
        self.assertEqual(self.act("session_start", {"name": "ok", "prompt": " "})[0], 400)

    def test_swap_holders_listed(self):
        swap = self.state()["system"]["swap"]
        self.assertIn("holders", swap)
        self.assertIn("hot", swap)
        for p in swap["holders"]:
            self.assertGreater(p["swap"], 0)

    def test_queue_status_terminal(self):
        self.state()
        code, out = self.act("term_queue", {})
        self.assertEqual(code, 200, out)
        term = out["term"]
        self.assertEqual(self.act("term_queue", {})[1]["term"], term)  # reused while alive
        self.assertEqual(self.act("term_input", {"term": term, "data": "\x03"})[0], 400)  # read-only
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/api/term/{term}/stream?t={self.token}", headers={"Host": f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        seen = b""
        deadline = time.time() + 8
        while b"ticket 9 ok" not in seen and time.time() < deadline:
            line = res.fp.readline()
            if line.startswith(b"data: ") and len(line) > 7:
                seen += base64.b64decode(line[6:].strip())
        self.assertIn(b"ticket 9 ok", seen)
        self.assertIn("status", self.log("sluicy.log"))
        conn.close()
        self.assertEqual(self.act("term_close", {"term": term})[0], 200)

    def test_stop_rm_logs_only_listed_background_sessions(self):
        self.state()
        self.assertEqual(self.act("session_stop", {"id": "bg1"})[0], 200)
        self.assertEqual(self.act("session_rm", {"id": "bg1"})[0], 200)
        code, out = self.act("session_logs", {"id": "bg1"})
        # The transcript as text, one line per tool call; tool results are left out.
        self.assertEqual(code, 200, out)
        self.assertTrue(out["found"])
        self.assertEqual(
            [(m["role"], m["text"]) for m in out["messages"]],
            [("user", "ship it"), ("tool", "Bash: Show status"), ("assistant", "Shipped.")],
        )
        self.assertEqual(self.act("session_stop", {"id": "nope"})[0], 400)
        self.assertEqual(self.act("session_stop", {"id": "s-int"})[0], 400)  # interactive
        self.assertIn("stop bg1", self.log("claude.log"))

    def test_transcript_dropdown_removed(self):
        self.state()
        self.assertEqual(self.act("session_transcript", {"id": "s-int"})[0], 404)

    def test_terminal_round_trip(self):
        self.state()
        code, out = self.act("term_open", {"id": "bg1"})
        self.assertEqual(code, 200, out)
        term = out["term"]
        self.assertEqual(self.act("term_open", {"id": "bg1"})[1]["term"], term)
        self.assertEqual(self.act("term_resize", {"term": term, "cols": 100, "rows": 30})[0], 200)
        self.assertEqual(self.act("term_input", {"term": term, "data": "hello deck\r"})[0], 200)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/api/term/{term}/stream?t={self.token}", headers={"Host": f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        seen = b""
        deadline = time.time() + 8
        while b"hello deck" not in seen and time.time() < deadline:
            line = res.fp.readline()
            if line.startswith(b"data: ") and len(line) > 7:
                seen += base64.b64decode(line[6:].strip())
        self.assertIn(b"hello deck", seen)
        self.assertEqual(self.act("term_close", {"term": term})[0], 200)
        self.assertEqual(self.act("term_input", {"term": term, "data": "x"})[0], 400)
        self.assertEqual(self.act("term_open", {"id": "s-int"})[0], 400)

    # ---- audit fixes: refresh cost, terminal leaks
    def agent_calls(self):
        return self.log("claude.log").count("agents --json --all")

    def test_sessions_list_is_cached_and_forgotten_after_an_action(self):
        port = self.start_backend(self.port + 1, DECK_AGENTS_TTL="60")
        before = self.agent_calls()
        self.state(port)
        self.state(port)
        self.assertEqual(self.agent_calls(), before + 1)
        self.assertEqual(self.act("session_stop", {"id": "bg1"}, port)[0], 200)
        self.state(port)
        self.assertEqual(self.agent_calls(), before + 2)

    def test_sessions_list_reruns_only_when_the_registry_moves(self):
        jobs = self.root / "jobs"
        (jobs / "j1").mkdir(parents=True, exist_ok=True)
        (jobs / "j1" / "state.json").write_text("{}")
        port = self.start_backend(self.port + 3, DECK_AGENTS_TTL="0.3", DECK_JOBS_DIR=str(jobs))
        self.state(port)
        before = self.agent_calls()
        time.sleep(0.5)  # past the TTL, registry unchanged: the cached list stands
        self.state(port)
        self.assertEqual(self.agent_calls(), before)
        (jobs / "j1" / "state.json").write_text('{"state": "done"}')
        self.state(port)
        self.assertEqual(self.agent_calls(), before + 1)
        time.sleep(0.5)
        new = self.root / "sessions" / "999999.json"  # a new registry file
        new.write_text("{}")
        self.addCleanup(new.unlink)
        self.state(port)
        self.assertEqual(self.agent_calls(), before + 2)

    def test_status_reruns_when_a_session_in_the_worktree_writes(self):
        port = self.start_backend(self.port + 4, DECK_AGENTS_TTL="0.3")
        me = lambda: {x["name"]: x for x in self.state(port)["sessions"]}["my-feature"]["worktree"]["dirty"]
        self.assertEqual(me(), 1)
        extra = self.root / "wt-dirty" / "c.txt"
        extra.write_text("new\n")
        self.addCleanup(extra.unlink)
        time.sleep(0.5)
        self.assertEqual(me(), 1)  # nothing a session did: the cached status stands
        transcript = next((self.root / "projects").glob("*wt-dirty/s-int.jsonl"))
        with transcript.open("a") as f:  # the session in that worktree wrote
            f.write(json.dumps({"type": "user", "message": {"content": "more"}}) + "\n")
        time.sleep(0.5)
        self.assertEqual(me(), 2)

    def test_concurrent_refreshes_share_one_build(self):
        (self.ctl / "agents-delay").write_text("1")
        self.addCleanup((self.ctl / "agents-delay").unlink)
        before = self.agent_calls()
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(self.req("GET", "/api/state")[0])) for _ in range(3)]
        for t in threads:
            t.start()
            time.sleep(0.1)
        for t in threads:
            t.join(15)
        self.assertEqual(codes, [200, 200, 200])
        self.assertEqual(self.agent_calls(), before + 1)

    def test_idle_terminal_is_reaped_and_a_streamed_one_is_not(self):
        # A deck whose reaper runs every second and closes a terminal unread for 0.3 s.
        port = self.start_backend(self.port + 2, DECK_TERM_IDLE="0.3")
        self.state(port)
        term = self.act("term_open", {"id": "bg1"}, port)[1]["term"]
        listed = lambda: term in [t["id"] for t in self.state(port)["terminals"]]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/term/{term}/stream?t={self.token}", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        time.sleep(2.5)  # two reaper passes
        self.assertTrue(listed())  # a page is streaming it
        res.close()
        conn.close()
        deadline = time.time() + 20  # the stream notices the closed socket at its next ping
        while listed() and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(listed())
        self.assertEqual(self.act("term_input", {"term": term, "data": "x"}, port)[0], 400)

    def _swap(self, path, text):
        """Write `text` to `path` for one test; the old content (or absence) comes back after."""
        old = path.read_text() if path.exists() else None
        self.addCleanup(lambda: path.write_text(old) if old is not None else path.unlink(missing_ok=True))
        path.write_text(text)

    def _bg_waiting(self, status, transcript, waiting_for=None, state=None):
        agents = json.loads((self.ctl / "agents.json").read_text())
        for a in agents:
            if a.get("id") == "bg1" and state:
                a["state"] = state
        self._swap(self.ctl / "agents.json", json.dumps(agents))
        reg = {"pid": self.sleeper.pid, "status": status, "statusUpdatedAt": int(time.time() * 1000)}
        if waiting_for:
            reg["waitingFor"] = waiting_for
        self._swap(self.root / "sessions" / f"{self.sleeper.pid}.json", json.dumps(reg))
        bg = self.root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.repo)) / "s-bg.jsonl"
        self._swap(bg, "\n".join(json.dumps(x) for x in transcript) + "\n")
        return {x["name"]: x for x in self.state()["sessions"]}["bg-task"]

    def test_question_is_surfaced_with_its_choices(self):
        ask = {
            "type": "tool_use",
            "id": "tu1",
            "name": "AskUserQuestion",
            "input": {"questions": [{"question": "Which port?", "header": "Port", "options": [{"label": "8080", "description": "default"}, {"label": "9090"}]}]},
        }
        s = self._bg_waiting("waiting", [{"type": "assistant", "message": {"content": [ask]}}], "permission prompt")
        self.assertEqual(s["status"], "waiting")  # the registry beats the agents list's "idle"
        n = s["needs"]
        self.assertEqual(n["kind"], "question")
        q = n["tool"]["questions"][0]
        self.assertEqual(q["question"], "Which port?")
        self.assertEqual([o["label"] for o in q["options"]], ["8080", "9090"])

    def test_permission_prompt_shows_the_command(self):
        call = {"type": "tool_use", "id": "tu2", "name": "Bash", "input": {"command": "rm -rf build", "description": "Clean"}}
        s = self._bg_waiting("waiting", [{"type": "assistant", "message": {"content": [call]}}], "permission prompt")
        self.assertEqual(s["needs"]["kind"], "permission")
        self.assertEqual(s["needs"]["label"], "permission prompt")
        self.assertEqual(s["needs"]["tool"]["command"], "rm -rf build")
        self.assertEqual(s["waitingFor"], "permission prompt")

    def test_answered_call_is_not_pending(self):
        call = {"type": "tool_use", "id": "tu3", "name": "Bash", "input": {"command": "ls"}}
        res = {"type": "tool_result", "tool_use_id": "tu3", "content": "x"}
        s = self._bg_waiting("busy", [{"type": "assistant", "message": {"content": [call]}}, {"type": "user", "message": {"content": [res]}}])
        self.assertIsNone(s["needs"])
        # Still waiting, but the call's result is in: no pending tool to show.
        s = self._bg_waiting("waiting", [{"type": "assistant", "message": {"content": [call]}}, {"type": "user", "message": {"content": [res]}}])
        self.assertEqual(s["needs"]["kind"], "permission")
        self.assertIsNone(s["needs"]["tool"])

    def test_finished_turn_that_asks_is_a_reply_need(self):
        msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Should I deploy the API too?"}]}}
        s = self._bg_waiting("idle", [msg], state="blocked")
        self.assertEqual(s["needs"]["kind"], "reply")
        self.assertIn("deploy the API", s["needs"]["message"])

    def test_done_and_stopped_sessions_need_nothing(self):
        msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done?"}]}}
        self.assertIsNone(self._bg_waiting("idle", [msg], state="done")["needs"])
        self.assertIsNone(self._bg_waiting("busy", [msg], state="blocked")["needs"])  # working again after an answer
        self.assertIsNone({x["name"]: x for x in self.state()["sessions"]}["old-task"]["needs"])  # stopped, blocked or not
        self.assertEqual(self._bg_waiting("waiting", [msg])["needs"]["kind"], "permission")

    def test_status_change_refetches_cached_agents(self):
        port = self.start_backend(self.port + 5, DECK_AGENTS_TTL="60")
        self.state(port)
        before = self.agent_calls()
        self.state(port)
        self.assertEqual(self.agent_calls(), before)  # cached
        time.sleep(0.05)
        reg = {"pid": self.sleeper.pid, "status": "waiting", "waitingFor": "permission prompt", "statusUpdatedAt": int(time.time() * 1000) + 1}
        self._swap(self.root / "sessions" / f"{self.sleeper.pid}.json", json.dumps(reg))
        self.state(port)
        self.assertEqual(self.agent_calls(), before + 1)  # a session changed status: read again now
        self.state(port)
        self.assertEqual(self.agent_calls(), before + 1)

    def test_page_has_needs_panel(self):
        body = self.req("GET", "/")[1]
        self.assertIn(b'id="needs-wrap"', body)
        self.assertIn(b"renderNeeds", body)

    # ---- processes and worktrees
    def test_kill_checks_start_time_and_owner(self):
        victim = subprocess.Popen(["sleep", "300"])
        try:
            code, info = self.act("proc_info", {"pid": victim.pid})
            self.assertEqual(code, 200)
            self.assertIn("sleep", info["cmd"])
            self.assertEqual(self.act("kill", {"pid": victim.pid, "start": info["start"] + 1})[0], 409)
            self.assertEqual(self.act("kill", {"pid": 1, "start": self.act("proc_info", {"pid": 1})[1]["start"]})[0], 403)
            self.assertEqual(self.act("kill", {"pid": victim.pid, "start": info["start"]})[0], 200)
            self.assertEqual(victim.wait(5), -15)
        finally:
            victim.kill()

    def test_worktree_remove_only_clean_and_merged(self):
        st = self.state()
        paths = {Path(t["path"]).name: t["path"] for t in st["worktrees"]}
        self.assertEqual(self.act("worktree_remove", {"path": paths["wt-dirty"]})[0], 409)
        self.assertEqual(self.act("worktree_remove", {"path": paths["repo"]})[0], 400)
        self.assertEqual(self.act("worktree_remove", {"path": "/etc"})[0], 400)
        code, out = self.act("worktree_remove", {"path": paths["wt-merged"]})
        self.assertEqual(code, 200, out)
        self.assertFalse(Path(paths["wt-merged"]).exists())

    # ---- Clean up panel (the sluicy conductor's cleanup.json)
    def write_cleanup(self, items, generated=1000.0):
        (self.sy_home / "cleanup.json").write_text(json.dumps({"generated": generated, "items": items}))

    def cleanup_tickets(self):
        import sluicy
        conn = sluicy.db()
        ids = {}
        for name, state, by in (("hand", "failed", "hand"), ("auto", "failed", "auto"), ("queued", "queued", "hand")):
            cur = conn.execute(
                "INSERT INTO tickets (branch, sha, source, mode, components, state, created_at, updated_at, submitted_by)"
                " VALUES (?, 'x', 'y', 'ride', '', ?, 1, 1, ?)", (f"fix/cleanup-{name}", state, by))
            ids[name] = cur.lastrowid
        conn.commit()

        def drop():  # the fixture database is shared by every test in the class
            c = sluicy.db()
            c.execute(f"DELETE FROM tickets WHERE id IN ({','.join('?' * len(ids))})", list(ids.values()))
            c.commit()
        self.addCleanup(drop)
        return ids

    def test_cleanup_state_resolves_sessions_and_hides_gone_rows(self):
        old = str(self.repo / ".claude" / "worktrees" / "old-task")
        self.write_cleanup([
            {"kind": "session", "id": "session:old-task", "name": "old-task", "path": old, "label": "Session old-task"},
            {"kind": "session", "id": "session:my-feature", "name": "my-feature", "path": str(self.root / "wt-dirty"), "label": "x"},
            {"kind": "worktree", "id": "worktree:/gone", "path": str(self.root / "no-such-dir"), "label": "gone"},
        ])
        items = {i["id"]: i for i in self.state()["cleanup"]["items"]}
        self.assertEqual(items["session:old-task"]["sessionId"], "done1")
        self.assertIn("stale", items["session:my-feature"])  # a terminal session is never offered
        self.assertNotIn("worktree:/gone", items)
        (self.sy_home / "cleanup.json").write_text('{"generated": 1, "items": [')  # torn
        self.assertEqual(self.state()["cleanup"]["items"], [])

    def test_cleanup_ticket_cancel_only_listed_failed_hand_tickets(self):
        ids = self.cleanup_tickets()
        all_ids = [ids["hand"], ids["auto"], ids["queued"]]
        self.write_cleanup([{"kind": "tickets", "id": "tickets:x", "tickets": all_ids, "label": "red"}])
        self.assertEqual(self.act("ticket_cancel", {"id": "tickets:nope", "tickets": all_ids})[0], 400)
        code, out = self.act("ticket_cancel", {"id": "tickets:x", "tickets": all_ids + [999]})
        self.assertEqual(code, 500)
        ok = {r["ticket"]: r["ok"] for r in out["results"]}
        self.assertEqual(ok, {ids["hand"]: True, ids["auto"]: False, ids["queued"]: False, 999: False})
        self.assertIn(f"cancel {ids['hand']} --reason Deck clean up", self.log("sluicy.log"))
        self.assertNotIn(f"cancel {ids['auto']} ", self.log("sluicy.log"))

    def test_cleanup_stray_rm_is_held_to_the_stray_folder(self):
        stray = self.sy_home / "stray"
        stray.mkdir(exist_ok=True)
        old, young = stray / "57-1", stray / "58-2"
        for d in (old, young):
            (d / "target").mkdir(parents=True)
            (d / "target" / "x.json").write_text("{}")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        outside = self.root / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (stray / "59-3").symlink_to(outside)
        items = [{"kind": "stray", "id": f"stray:{n}", "path": str(stray / n), "label": n} for n in ("57-1", "58-2", "59-3")]
        items.append({"kind": "stray", "id": "stray:../precious", "path": str(outside), "label": "trick"})
        self.write_cleanup(items)
        self.assertEqual(self.act("stray_rm", {"name": "../precious"})[0], 400)
        self.assertEqual(self.act("stray_rm", {"name": "59-3"})[0], 400)  # symlink
        self.assertEqual(self.act("stray_rm", {"name": "58-2"})[0], 409)  # under 7 days
        self.assertEqual(self.act("stray_rm", {"name": "60-4"})[0], 400)  # not listed
        self.assertTrue((outside / "keep.txt").exists())
        code, out = self.act("stray_rm", {"name": "57-1"})
        self.assertEqual(code, 200, out)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertNotIn("stray:57-1", {i["id"] for i in self.state()["cleanup"]["items"]})

    def test_cleanup_debt_done_needs_a_listed_debt(self):
        self.write_cleanup([{"kind": "debt", "id": "debt:5", "debt": 5, "label": "recapture"}], generated=2000.0)
        self.assertEqual(self.act("debt_done", {"debt": 6})[0], 400)
        self.assertEqual(self.act("debt_done", {"debt": "5"})[0], 400)
        code, out = self.act("debt_done", {"debt": 5})
        self.assertEqual(code, 200, out)
        self.assertIn("debts --done 5", self.log("sluicy.log"))
        self.assertEqual(self.state()["cleanup"]["items"], [])  # hidden until the conductor rewrites
        self.write_cleanup([{"kind": "debt", "id": "debt:5", "debt": 5, "label": "recapture"}], generated=3000.0)
        self.assertEqual(len(self.state()["cleanup"]["items"]), 1)

    def test_unknown_action_and_bad_json(self):
        self.assertEqual(self.act("nope", {})[0], 404)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/action/release", body=b"{bad", headers={"Host": f"127.0.0.1:{self.port}", "X-Deck-Token": self.token, "Origin": f"http://127.0.0.1:{self.port}", "Content-Length": "4"})
        self.assertEqual(conn.getresponse().status, 400)

    def test_page_served(self):
        code, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Session Deck", body)

    # ---- the terminal deck (`deck tui`, Rust only)
    def _tui(self):
        """`deck tui` in a 120x40 pty with the fake environment; returns (pid, fd)."""
        import fcntl
        import pty
        import struct
        import termios

        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                os.environ["TERM"] = "xterm-256color"
                os.environ["DECK_NOTIFY"] = "off"
                os.execv(BIN, [BIN, "tui"])
            finally:
                os._exit(127)
        return pid, fd

    def _read_until(self, fd, needles, timeout=30):
        import select

        seen = b""
        deadline = time.time() + timeout
        while time.time() < deadline and not all(n in seen for n in needles):
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                seen += chunk
        return seen

    def _finish(self, pid, fd):
        """Read to EOF and reap; returns (output, exit status)."""
        out = self._read_until(fd, [b"\x00never"], timeout=15)
        os.close(fd)
        _, status = os.waitpid(pid, 0)
        return out, status

    def test_tui_shows_sessions_and_q_restores_the_terminal(self):
        pid, fd = self._tui()
        screen = self._read_until(fd, [b"Sessions", b"bg-task", b"Queue"])
        self.assertIn(b"bg-task", screen)
        self.assertIn(b"Queue", screen)
        os.write(fd, b"q")
        out, status = self._finish(pid, fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn(b"\x1b[?1049l", screen + out)  # left the alternate screen

    def test_tui_sigterm_restores_the_terminal(self):
        import signal

        pid, fd = self._tui()
        screen = self._read_until(fd, [b"Sessions"])
        self.assertIn(b"Sessions", screen)
        os.kill(pid, signal.SIGTERM)
        out, status = self._finish(pid, fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn(b"\x1b[?1049l", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
