"""The code map (graphify): reading graph.json, impact, overlap, rules, briefs, and running graphify safely.

    python3 tests/test_graph.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sluicy"))
import graph  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "graph.json"


def node(i, f, community, label=None):
    return {"id": i, "label": label or i, "source_file": f, "community": community, "community_name": f"area{community}",
            "file_type": "code"}


def edge(a, b, f, relation="calls"):
    return {"source": a, "target": b, "relation": relation, "confidence": "EXTRACTED", "source_file": f}


def raw(nodes, links, version=1):
    return {"directed": False, "multigraph": False, "graph": {"schema_version": version, "graphify_version": "0.9.108"},
            "nodes": nodes, "links": links, "hyperedges": []}


BASE_NODES = [node("web_a", "web/a.ts", 0), node("web_b", "web/b.ts", 0), node("api_x", "api/src/x.ts", 1),
              node("api_y", "api/src/y.ts", 1), node("lib_z", "lib/z.ts", 2)]
# web/a.ts already calls into api/src/x.ts on main: an old violation.
BASE_LINKS = [edge("web_a", "api_x", "web/a.ts"), edge("web_a", "web_b", "web/a.ts"), edge("api_x", "api_y", "api/src/x.ts")]
RULES = [{"name": "web-not-api", "from": ["web/**"], "to": ["api/src/**"]}]


def write(tmp, name, data):
    p = Path(tmp) / name
    p.write_text(json.dumps(data))
    return p


class Load(unittest.TestCase):
    def test_real_fixture_loads(self):
        g, why = graph.load(FIXTURE)
        self.assertEqual(why, "")
        self.assertIn("sluicy/config.py", g.file_nodes)
        self.assertTrue(g.communities)
        self.assertTrue(g.god_nodes())

    def test_corrupt_and_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "g.json"
            bad.write_text("{not json")
            self.assertIsNone(graph.load(bad)[0])
            self.assertIn("schema_version 2", graph.load(write(tmp, "v2.json", raw([], [], version=2)))[1])
            self.assertIn("not a graphify graph", graph.load(write(tmp, "x.json", {"nodes": 1}))[1])
            self.assertIn("cannot read", graph.load(Path(tmp) / "missing.json")[1])

    def test_empty_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, _ = graph.load(write(tmp, "e.json", raw([], [])))
            imp = graph.impact(g, ["web/a.ts"], RULES, g)
            self.assertEqual(imp["communities"], [])
            self.assertEqual(imp["mapped_files"], 0)

    def test_dangling_edges_ignored(self):
        g = graph.Graph(raw(BASE_NODES, [*BASE_LINKS, edge("gone", "web_a", "web/a.ts"), "junk"]))
        self.assertIn(("web/a.ts", "api/src/x.ts"), g.deps)


class Impact(unittest.TestCase):
    def test_communities_hubs_and_neighbours(self):
        g = graph.Graph(raw(BASE_NODES, BASE_LINKS))
        imp = graph.impact(g, ["web/a.ts", "README.md"])
        self.assertEqual(imp["files"], ["README.md", "web/a.ts"])
        self.assertEqual(imp["mapped_files"], 1)
        self.assertEqual([c["name"] for c in imp["communities"]], ["area0"])
        self.assertEqual(imp["neighbour_files"], 2)  # web/b.ts and api/src/x.ts
        self.assertIn("web_a", imp["god_nodes"])
        json.dumps(imp)  # stored as a column

    def test_direction_comes_from_the_edge_source_file(self):
        # Stored undirected: target/source swapped, but the reference is written in web/a.ts.
        g = graph.Graph(raw(BASE_NODES, [edge("api_x", "web_a", "web/a.ts")]))
        self.assertEqual(g.deps, {("web/a.ts", "api/src/x.ts")})

    def test_non_dependency_relations_are_not_deps(self):
        g = graph.Graph(raw(BASE_NODES, [edge("web_a", "api_x", "web/a.ts", relation="rationale_for")]))
        self.assertEqual(g.deps, set())


class Rules(unittest.TestCase):
    def test_old_violation_in_a_changed_file_is_not_new(self):
        base = graph.Graph(raw(BASE_NODES, BASE_LINKS))
        merged = graph.Graph(raw(BASE_NODES, BASE_LINKS))
        self.assertEqual(graph.impact(merged, ["web/a.ts"], RULES, base)["rule_hits"], {})

    def test_added_edge_is_a_hit(self):
        base = graph.Graph(raw(BASE_NODES, BASE_LINKS))
        merged = graph.Graph(raw(BASE_NODES, [*BASE_LINKS, edge("web_b", "api_y", "web/b.ts", "imports_from")]))
        hits = graph.impact(merged, ["web/b.ts"], RULES, base)["rule_hits"]
        self.assertEqual(hits, {"web-not-api": [["web/b.ts", "api/src/y.ts"]]})
        self.assertIn("1 new rule hit(s): web-not-api", graph.summary(graph.impact(merged, ["web/b.ts"], RULES, base)))

    def test_reverse_direction_is_allowed(self):
        base = graph.Graph(raw(BASE_NODES, []))
        merged = graph.Graph(raw(BASE_NODES, [edge("api_y", "web_b", "api/src/y.ts")]))
        self.assertEqual(graph.new_rule_hits(base, merged, RULES), {})

    def test_hits_are_capped(self):
        nodes = [node(f"w{i}", f"web/{i}.ts", 0) for i in range(30)] + [node("api_x", "api/src/x.ts", 1)]
        links = [edge(f"w{i}", "api_x", f"web/{i}.ts") for i in range(30)]
        hits = graph.new_rule_hits(graph.Graph(raw(nodes, [])), graph.Graph(raw(nodes, links)), RULES)["web-not-api"]
        self.assertEqual(len(hits), graph.HIT_CAP + 1)
        self.assertEqual(hits[-1], ["…", "10 more"])


class Overlap(unittest.TestCase):
    def test_tickets_meet_by_file(self):
        g = graph.Graph(raw(BASE_NODES, BASE_LINKS))
        a = graph.impact(g, ["web/a.ts"])
        b = graph.impact(g, ["web/b.ts"])  # same community as web/a.ts
        c = graph.impact(g, ["lib/z.ts"])
        self.assertEqual(graph.overlap(a, b), ["web/a.ts", "web/b.ts"])
        self.assertEqual(graph.overlap(a, c), [])


class Text(unittest.TestCase):
    def test_summary_and_brief(self):
        self.assertEqual(graph.summary(None), "map: none yet")
        g = graph.Graph(raw(BASE_NODES, BASE_LINKS))
        imp = graph.impact(g, ["web/a.ts"], RULES, g)
        t = {"id": 7, "branch": "feat", "sha": "a" * 40}
        text = graph.brief(t, imp, "g/graph.json")
        self.assertIn("ticket 7", text)
        self.assertIn("**area0**", text)
        self.assertIn("no new rule hits", text)
        self.assertIn('graphify explain "<symbol>" --graph g/graph.json', text)
        self.assertIn("No code map yet", graph.brief(t, None, "g/graph.json"))


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = (self.dir / "map.log").open("w")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def fake(self, body):
        exe = self.dir / "fake-graphify"
        exe.write_text("#!/bin/bash\n" + body)
        exe.chmod(0o755)
        return {"enabled": True, "command": [str(exe)], "update_args": ["update", "."], "timeout_s": 2}

    def test_available(self):
        self.assertFalse(graph.available({"enabled": False, "command": ["graphify"]})[0])
        ok, why = graph.available({"enabled": True, "command": ["no-such-graphify-binary"]})
        self.assertFalse(ok)
        self.assertIn("not on PATH", why)

    def test_env_has_no_llm_credentials(self):
        cfg = self.fake('env > "$PWD/env.txt"\n')
        base = {"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k",
                "AWS_SECRET_ACCESS_KEY": "k", "GITHUB_AUTH_TOKEN": "k", "HOME": "/h"}
        self.assertIsNone(graph.run(self.dir, cfg, self.log, base))
        env = (self.dir / "env.txt").read_text()
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_AUTH_TOKEN"):
            self.assertNotIn(name, env)
        self.assertIn("GRAPHIFY_BACKEND=none", env)
        self.assertIn("HOME=/h", env)

    def test_failure_is_a_reason(self):
        self.assertEqual(graph.run(self.dir, self.fake("exit 3\n"), self.log, dict(os.environ)), "graphify exited 3")

    def test_hang_kills_the_whole_group(self):
        cfg = self.fake('sleep 300 &\necho $! > "$PWD/child.pid"\nwait\n')
        started = time.monotonic()
        why = graph.run(self.dir, cfg, self.log, dict(os.environ))
        self.assertIn("timed out", why)
        self.assertLess(time.monotonic() - started, 15)
        child = int((self.dir / "child.pid").read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("graphify's child outlived the timeout")


if __name__ == "__main__":
    unittest.main()
