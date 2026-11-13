"""The code map: optional graphify support (https://github.com/Graphify-Labs/graphify).

graphify turns a repository into a graph of its code (files, symbols, calls, imports) grouped into
communities. sluicy uses that graph, when a project turns `[graph] enabled = true` on and `graphify` is on
PATH, to say what a ticket touches:

- impact: the files a ticket changes, the graph nodes in them, the communities (areas) they belong to and
  the most connected nodes ("god nodes") it touches;
- overlap: two queued tickets whose areas meet;
- rules: "files matching `from` must not depend on files matching `to`", reported when a ticket ADDS such
  an edge (in the merged graph, not in main's). Report-only: a rule never fails a ticket;
- a review brief for a person or a reviewer agent.

Standard library only. Nothing here raises into the queue: every failure comes back as a reason.

Privacy: graphify runs with `GRAPHIFY_BACKEND=none` and with LLM credentials removed from its
environment, so only its local tree-sitter pass runs and no code leaves the machine.
"""
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

SCHEMA_MAJOR = 1
# Edge relations that mean "this file depends on that one".
DEP_RELATIONS = {"calls", "references", "imports", "imports_from", "inherits", "indirect_call"}
GOD_NODES = 20
AREA_FILE_CAP = 300
HIT_CAP = 20

# Credentials graphify (or a library it loads) could use to reach a model. Removed from its environment.
LLM_ENV = re.compile(
    r"^(ANTHROPIC|OPENAI|GEMINI|GOOGLE|DEEPSEEK|MOONSHOT|KIMI|MISTRAL|GROQ|AZURE_OPENAI|OPENROUTER|TOGETHER|"
    r"FIREWORKS|COHERE|HF|HUGGINGFACE|XAI|OLLAMA|AWS|CLAUDE)_|(_API_KEY|_AUTH_TOKEN|_ACCESS_KEY|_SECRET_KEY)$"
)


def available(gcfg):
    """(True, "") when the project asks for the map and graphify can run, else (False, why)."""
    if not gcfg.get("enabled"):
        return False, "[graph] is not enabled"
    if not shutil.which(gcfg["command"][0]):
        return False, f"{gcfg['command'][0]} is not on PATH (uv tool install graphifyy)"
    return True, ""


def graph_env(base):
    env = {k: v for k, v in base.items() if not LLM_ENV.search(k)}
    env.update({"GRAPHIFY_BACKEND": "none", "GRAPHIFY_QUERY_LOG_DISABLE": "1"})
    return env


def run(root, gcfg, log, base_env):
    """Run `command + update_args` in `root`, niced, in its own process group, killed whole on timeout.
    Returns None on success, else a short reason. Never raises."""
    prefix = (["nice", "-n", "19"] if shutil.which("nice") else []) + (["ionice", "-c", "3"] if shutil.which("ionice") else [])
    argv = [*prefix, *gcfg["command"], *gcfg["update_args"]]
    log.write(f"$ {' '.join(argv)}\n")
    log.flush()
    try:
        proc = subprocess.Popen(argv, cwd=root, env=graph_env(base_env), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as err:
        return f"could not start graphify: {err}"
    try:
        rc = proc.wait(timeout=gcfg["timeout_s"])
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return f"graphify timed out after {gcfg['timeout_s']:g}s"
    return None if rc == 0 else f"graphify exited {rc}"


def kill_group(proc, grace=5):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def load(path):
    """(graph, "") or (None, why). Accepts only a schema graphify documents (major SCHEMA_MAJOR)."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        return None, f"cannot read {path}: {err}"
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list) or not isinstance(raw.get("links"), list):
        return None, f"{path}: not a graphify graph"
    version = (raw.get("graph") or {}).get("schema_version")
    if not isinstance(version, int) or version != SCHEMA_MAJOR:
        return None, f"{path}: schema_version {version!r}, sluicy reads {SCHEMA_MAJOR}"
    return Graph(raw), ""


class Graph:
    """The parts of graph.json sluicy uses. graphify stores the graph undirected, so an edge's direction comes
    from its own `source_file` (where the reference is written), not from source/target order."""

    def __init__(self, raw):
        self.nodes = {}
        self.file_nodes = {}
        self.communities = {}
        for n in raw["nodes"]:
            if not isinstance(n, dict) or "id" not in n:
                continue
            self.nodes[n["id"]] = n
            f = n.get("source_file")
            if f:
                self.file_nodes.setdefault(f, []).append(n["id"])
            c = n.get("community")
            if c is not None:
                entry = self.communities.setdefault(c, {"id": c, "name": n.get("community_name") or f"community {c}",
                                                        "files": set(), "size": 0})
                entry["size"] += 1
                if f:
                    entry["files"].add(f)
        self.degree = {}
        self.deps = set()
        self.adjacent = {}
        for e in raw["links"]:
            if not isinstance(e, dict):
                continue
            a, b = self.nodes.get(e.get("source")), self.nodes.get(e.get("target"))
            if not a or not b:
                continue
            for n in (a, b):
                self.degree[n["id"]] = self.degree.get(n["id"], 0) + 1
            fa, fb = a.get("source_file"), b.get("source_file")
            if fa and fb and fa != fb:
                self.adjacent.setdefault(fa, set()).add(fb)
                self.adjacent.setdefault(fb, set()).add(fa)
            origin = e.get("source_file")
            if e.get("relation") in DEP_RELATIONS and fa and fb and fa != fb and origin in (fa, fb):
                self.deps.add((origin, fb if origin == fa else fa))

    def god_nodes(self, top=GOD_NODES):
        ranked = sorted(self.degree.items(), key=lambda kv: (-kv[1], kv[0]))
        return [self.nodes[i] for i, _ in ranked[:top]]


def _match(path, globs):
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def rule_hits(g, rules):
    """{rule name: {(from file, to file)}} for every dependency edge a rule forbids."""
    out = {}
    for rule in rules:
        out[rule["name"]] = {(a, b) for a, b in g.deps if _match(a, rule["from"]) and _match(b, rule["to"])}
    return out


def new_rule_hits(base, merged, rules):
    """Rule hits in the merged graph that main's graph does not have: what the ticket adds."""
    before = rule_hits(base, rules) if base else {}
    out = {}
    for name, hits in rule_hits(merged, rules).items():
        added = sorted(hits - before.get(name, set()))
        if added:
            out[name] = [list(h) for h in added[:HIT_CAP]] + ([["…", f"{len(added) - HIT_CAP} more"]] if len(added) > HIT_CAP else [])
    return out


def impact(g, changed, rules=(), base=None):
    """What a ticket touches, as a JSON-able dict, from the merged graph `g` and main's graph `base`."""
    changed = sorted(set(changed))
    in_graph = [f for f in changed if f in g.file_nodes]
    node_ids = [i for f in in_graph for i in g.file_nodes[f]]
    comm_ids = sorted({g.nodes[i].get("community") for i in node_ids} - {None}, key=lambda c: (-g.communities[c]["size"], str(c)))
    gods = {n["id"] for n in g.god_nodes()}
    neighbours = set().union(*(g.adjacent.get(f, set()) for f in in_graph)) - set(changed) if in_graph else set()
    return {
        "files": changed,
        "mapped_files": len(in_graph),
        "nodes": len(node_ids),
        "communities": [
            {"id": c, "name": g.communities[c]["name"], "size": g.communities[c]["size"],
             "files": sorted(g.communities[c]["files"])[:AREA_FILE_CAP]}
            for c in comm_ids
        ],
        "god_nodes": sorted({g.nodes[i].get("label") or i for i in node_ids if i in gods}),
        "neighbour_files": len(neighbours),
        "rule_hits": new_rule_hits(base, g, rules) if rules else {},
        "built_at": time.time(),
    }


def area(imp):
    return {f for c in imp.get("communities", []) for f in c.get("files", [])} | set(imp.get("files", []))


def overlap(a, b):
    """Files where two tickets' areas meet: one changes what lies in the other's communities. Compared by
    file path, because community ids are numbered afresh in every graph."""
    return sorted((set(a.get("files", [])) & area(b)) | (set(b.get("files", [])) & area(a)))


def summary(imp):
    """One line for `status` and `wait`."""
    if not imp:
        return "map: none yet"
    if imp.get("skipped"):
        return f"map: skipped ({imp['skipped']})"
    names = [c["name"] for c in imp.get("communities", [])]
    parts = [f"{len(imp.get('files', []))} file(s)", f"{len(names)} area(s)" + (f" ({', '.join(names[:3])}{', …' if len(names) > 3 else ''})" if names else "")]
    if imp.get("god_nodes"):
        parts.append(f"hubs: {', '.join(imp['god_nodes'][:3])}")
    hits = sum(len(v) for v in imp.get("rule_hits", {}).values())
    if hits:
        parts.append(f"{hits} new rule hit(s): {', '.join(sorted(imp['rule_hits']))}")
    return "map: " + "; ".join(parts)


def brief(t, imp, graph_path):
    """Markdown for a person or a reviewer agent: what the ticket touches and where to look. `graph_path` is
    the graph the follow-up commands read (main's, at the commit the ticket was mapped against)."""
    head = f"# Review brief: ticket {t['id']} ({t['branch']} at {t['sha'][:10]})\n"
    if not imp:
        return head + "\nNo code map yet. It is built after preflight, when graphify is installed and `[graph] enabled = true`.\n"
    if imp.get("skipped"):
        return head + f"\nNo code map: {imp['skipped']}.\n"
    lines = [head, summary(imp), "", "## Files changed"]
    lines += [f"- `{f}`" for f in imp["files"][:60]] + ([f"- … {len(imp['files']) - 60} more"] if len(imp["files"]) > 60 else [])
    lines += ["", f"{imp['mapped_files']} of them are in the graph ({imp['nodes']} symbols); "
              f"{imp['neighbour_files']} other file(s) are one edge away.", "", "## Areas touched"]
    lines += [f"- **{c['name']}** ({c['size']} symbols, {len(c['files'])} files)" for c in imp["communities"][:20]] or ["- none"]
    if imp.get("god_nodes"):
        lines += ["", "## Hubs touched (most connected symbols: a change here reaches far)"]
        lines += [f"- `{n}` — `graphify affected \"{n}\" --graph {graph_path}`" for n in imp["god_nodes"]]
    lines += ["", "## Rules"]
    if imp.get("rule_hits"):
        for name, hits in sorted(imp["rule_hits"].items()):
            lines.append(f"- **{name}**: new dependencies")
            lines += [f"  - `{a}` → `{b}`" for a, b in hits]
    else:
        lines.append("- no new rule hits")
    lines += ["", f"## Ask the graph (main at {str(imp.get('base_sha') or '?')[:10]})", f"- `graphify explain \"<symbol>\" --graph {graph_path}`",
              f"- `graphify path \"<A>\" \"<B>\" --graph {graph_path}`",
              f"- `graphify query \"<question>\" --graph {graph_path}`",
              "", "Built by graphify's local code pass; edges are heuristic, so confirm in the code before acting."]
    return "\n".join(lines) + "\n"
