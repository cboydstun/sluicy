//! Collectors: configuration, sessions, the release queue, the laptop, worktrees. Each refresh
//! reads the CLI, git, /proc and the queue's database; nothing here keeps state beyond caches.

use crate::gitfiles;
use crate::procs::{self, Procs};
use crate::util::{self, git, head, lines, mtime_secs, read_tail, round1, GitMemo};
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Read;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone)]
pub struct Cfg {
    pub claude: String,
    pub python: String,
    pub repo: PathBuf,
    /// How to run sluicy: `sluicy` on PATH, or DECK_SLUICY (a `.py` file runs under `python`).
    pub sluicy: Vec<String>,
    pub sluicy_home: PathBuf,
    /// The repo's components and waivers, from its `.sluicy.toml` (`sluicy config check --json`).
    pub components: Vec<String>,
    pub waivers: Vec<String>,
    pub sessions_dir: PathBuf,
    pub projects_dir: PathBuf,
    pub locks: Vec<(String, PathBuf)>,
    pub gate_logs: PathBuf,
    pub mem_warn_pct: f64,
    pub swap_warn_pct: f64,
    /// `claude agents` is a node start-up (~0.25 s CPU): most of what a refresh costs.
    pub agents_ttl: f64,
    /// The longest a sessions list is reused while the registry files stand still.
    pub agents_max_age: f64,
    /// The longest a worktree's `git status` is reused while nothing that changes it moved.
    pub status_max_age: f64,
    pub jobs_dir: PathBuf,
    /// A terminal nobody has streamed for this long is closed.
    pub term_idle: f64,
}

fn env_or(name: &str, default: impl Into<String>) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.into())
}

fn env_path(name: &str, default: impl Into<PathBuf>) -> PathBuf {
    std::env::var_os(name)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| default.into())
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// The main worktree of the repository the deck is started in, even from a linked worktree.
fn main_checkout() -> PathBuf {
    let here = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let (rc, out, _) = util::run(
        &util::argv(&["git", "worktree", "list", "--porcelain"]),
        Some(&here),
        Duration::from_secs(30),
    );
    let first = if rc == 0 {
        out.lines().next().unwrap_or("").to_string()
    } else {
        String::new()
    };
    match first.strip_prefix("worktree ") {
        Some(p) => PathBuf::from(p),
        None => std::env::current_dir().unwrap_or_default(),
    }
}

/// `DECK_LOCKS="push gate=/tmp/x.lock;e2e=/tmp/y.lock"`: flock files the Laptop panel shows.
fn locks_from_env() -> Vec<(String, PathBuf)> {
    env_or("DECK_LOCKS", "")
        .split(';')
        .filter_map(|item| item.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), PathBuf::from(v.trim())))
        .filter(|(k, v)| !k.is_empty() && !v.as_os_str().is_empty())
        .collect()
}

fn sluicy_argv(python: &str) -> Vec<String> {
    let s = env_or("DECK_SLUICY", "sluicy");
    if s.ends_with(".py") {
        vec![python.to_string(), s]
    } else {
        vec![s]
    }
}

/// (components, waivers) from the repo's `.sluicy.toml`, through sluicy itself; empty when it has none.
fn repo_config(sluicy: &[String], repo: &Path) -> (Vec<String>, Vec<String>) {
    let mut argv = sluicy.to_vec();
    argv.extend(util::argv(&["config", "check", "--json"]));
    let (rc, out, _) = util::run(&argv, Some(repo), Duration::from_secs(30));
    if rc != 0 {
        return (vec![], vec![]);
    }
    let v: Value = serde_json::from_str(&out).unwrap_or(Value::Null);
    let comps = v["components"]
        .as_object()
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default();
    let waivers = v["gate"]["waivers"]
        .as_array()
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();
    (comps, waivers)
}

impl Cfg {
    pub fn from_env() -> Cfg {
        let home = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
        let repo = std::env::var_os("DECK_REPO")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(main_checkout);
        let python = env_or("DECK_PYTHON", "python3");
        let sluicy = sluicy_argv(&python);
        let (components, waivers) = repo_config(&sluicy, &repo);
        Cfg {
            claude: env_or("DECK_CLAUDE", "claude"),
            python,
            sluicy,
            components,
            waivers,
            sluicy_home: env_path("SLUICY_HOME", home.join(".local/state/sluicy")),
            sessions_dir: env_path("DECK_SESSIONS_DIR", home.join(".claude/sessions")),
            projects_dir: env_path("DECK_PROJECTS_DIR", home.join(".claude/projects")),
            locks: locks_from_env(),
            gate_logs: env_path("DECK_GATE_LOGS", std::env::temp_dir().join("sluicy-gate-logs")),
            mem_warn_pct: env_f64("DECK_MEM_WARN_PCT", 15.0),
            swap_warn_pct: env_f64("DECK_SWAP_WARN_PCT", 50.0),
            agents_ttl: env_f64("DECK_AGENTS_TTL", 10.0),
            agents_max_age: env_f64("DECK_AGENTS_MAX_AGE", 60.0),
            status_max_age: env_f64("DECK_STATUS_MAX_AGE", 60.0),
            jobs_dir: env_path("DECK_JOBS_DIR", home.join(".claude/jobs")),
            term_idle: env_f64("DECK_TERM_IDLE", 120.0),
            repo,
        }
    }
}

// ---- transcripts

#[derive(Clone)]
struct Tail {
    mtime: f64,
    /// First line of the last assistant message, for the table.
    last: String,
    /// That message in full (capped), for the Needs you panel.
    last_full: String,
    /// The newest tool call with no result yet, or null.
    pending: Value,
}

/// transcript path -> ((size, mtime ns), tail)
type TailCache = HashMap<PathBuf, ((u64, i64), Tail)>;
static TAIL_CACHE: Mutex<Option<TailCache>> = Mutex::new(None);

fn message_text(content: Option<&Value>) -> String {
    match content {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(blocks)) => blocks
            .iter()
            .filter(|b| b.get("type").and_then(Value::as_str) == Some("text"))
            .map(|b| b.get("text").and_then(Value::as_str).unwrap_or(""))
            .collect::<Vec<_>>()
            .join(" "),
        _ => String::new(),
    }
}

/// The last assistant line of a session, re-read only when its transcript changed (size or mtime).
fn transcript_tail(c: &Cfg, cwd: &str, session_id: &str) -> Option<Tail> {
    use std::os::unix::fs::MetadataExt;
    let path = c
        .projects_dir
        .join(util::project_dir_name(cwd))
        .join(format!("{session_id}.jsonl"));
    let md = fs::metadata(&path).ok()?;
    let key = (md.size(), md.mtime() * 1_000_000_000 + md.mtime_nsec());
    {
        let g = TAIL_CACHE.lock().unwrap();
        if let Some(Some((k, v))) = g.as_ref().map(|m| m.get(&path)) {
            if *k == key {
                return Some(v.clone());
            }
        }
    }
    let text = read_tail(&path, 256 * 1024)?;
    let parsed: Vec<Value> = lines(&text)
        .into_iter()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .collect();
    let mut last = String::new();
    for d in &parsed {
        if d.get("type").and_then(Value::as_str) != Some("assistant") {
            continue;
        }
        let t = message_text(d.get("message").and_then(|m| m.get("content")));
        let t = t.trim();
        if !t.is_empty() && !t.starts_with('<') {
            last = t.to_string();
        }
    }
    let tail = Tail {
        mtime: mtime_secs(&md),
        last: head(last.lines().next().unwrap_or(""), 200),
        last_full: head(&last, 1200),
        pending: pending_tool(&parsed),
    };
    TAIL_CACHE
        .lock()
        .unwrap()
        .get_or_insert_with(HashMap::new)
        .insert(path, (key, tail.clone()));
    Some(tail)
}

/// The newest tool call with no result after it: what a session parked on a permission prompt or
/// a question is asking about. Null once its result is in.
fn pending_tool(entries: &[Value]) -> Value {
    let mut pending: Option<&Value> = None;
    for d in entries {
        let Some(blocks) = d
            .get("message")
            .and_then(|m| m.get("content"))
            .and_then(Value::as_array)
        else {
            continue;
        };
        for b in blocks.iter().filter(|b| b.is_object()) {
            match b.get("type").and_then(Value::as_str) {
                Some("tool_use") => pending = Some(b),
                Some("tool_result") => {
                    if let Some(p) = pending {
                        if b.get("tool_use_id").is_some() && b.get("tool_use_id") == p.get("id") {
                            pending = None;
                        }
                    }
                }
                _ => {}
            }
        }
    }
    let Some(p) = pending else { return Value::Null };
    let empty = Map::new();
    let inp = p.get("input").and_then(Value::as_object).unwrap_or(&empty);
    let name = p.get("name").and_then(Value::as_str).unwrap_or("tool");
    let mut out = json!({"name": name, "line": tool_line(p)});
    if let Some(qs) = inp.get("questions").and_then(Value::as_array) {
        // AskUserQuestion: the questions and their choices.
        let questions: Vec<Value> = qs
            .iter()
            .filter(|q| q.is_object())
            .take(4)
            .map(|q| {
                let options: Vec<Value> = q
                    .get("options")
                    .and_then(Value::as_array)
                    .map(|o| {
                        o.iter()
                            .filter(|o| o.is_object())
                            .take(6)
                            .map(|o| {
                                json!({
                                    "label": head(&util::as_str(o.get("label")), 120),
                                    "description": head(&util::as_str(o.get("description")), 300),
                                })
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                json!({
                    "question": head(&util::as_str(q.get("question")), 500),
                    "header": head(&util::as_str(q.get("header")), 40),
                    "multiSelect": util::truthy(q.get("multiSelect")),
                    "options": options,
                })
            })
            .collect();
        out["questions"] = json!(questions);
    } else if util::truthy(inp.get("plan")) {
        out["plan"] = json!(head(&util::as_str(inp.get("plan")), 1500)); // ExitPlanMode
    } else if util::truthy(inp.get("command")) {
        out["command"] = json!(head(&util::as_str(inp.get("command")), 1500));
    }
    out
}

/// What a session is waiting on the person for, or null. `waiting` is Claude Code's own flag for
/// a permission prompt or a question on screen; `blocked` after a finished turn means its last
/// message asked something. A stopped session asks nothing until it is woken.
fn session_needs(
    status: &str,
    waiting_for: &str,
    state: &str,
    stopped: bool,
    tail: Option<&Tail>,
) -> Value {
    if stopped {
        return Value::Null;
    }
    let tool = tail.map(|t| t.pending.clone()).unwrap_or(Value::Null);
    let message = tail.map(|t| t.last_full.clone()).unwrap_or_default();
    if status == "waiting" {
        let (kind, label) = match tool.get("name").and_then(Value::as_str) {
            Some("AskUserQuestion") => ("question", "question".to_string()),
            Some("ExitPlanMode") => ("plan", "plan approval".to_string()),
            _ => (
                "permission",
                if waiting_for.is_empty() {
                    "permission prompt".to_string()
                } else {
                    waiting_for.to_string()
                },
            ),
        };
        return json!({"kind": kind, "label": label, "tool": tool, "message": message});
    }
    if state == "blocked" && status != "busy" && status != "waiting" {
        return json!({"kind": "reply", "label": "waiting for your reply", "tool": null, "message": message});
    }
    Value::Null
}

/// One line for a tool call: its name and the input a person would recognise.
fn tool_line(block: &Value) -> String {
    let empty = Map::new();
    let inp = block
        .get("input")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let detail = [
        "description",
        "command",
        "file_path",
        "path",
        "pattern",
        "url",
        "query",
        "action",
    ]
    .iter()
    .find_map(|k| {
        inp.get(*k)
            .filter(|v| util::truthy(Some(v)))
            .map(|v| util::as_str(Some(v)))
    })
    .unwrap_or_default();
    let name = block.get("name").and_then(Value::as_str).unwrap_or("tool");
    head(
        &format!(
            "{name}: {}",
            detail.split_whitespace().collect::<Vec<_>>().join(" ")
        ),
        240,
    )
}

/// What a session has been doing, as text: its messages and one line per tool call.
pub fn transcript_log(
    c: &Cfg,
    cwds: &[&str],
    session_id: &str,
    limit: usize,
) -> Option<Vec<Value>> {
    let mut seen = Vec::new();
    for cwd in cwds.iter().filter(|d| !d.is_empty()) {
        if seen.contains(cwd) {
            continue;
        }
        seen.push(*cwd);
        let path = c
            .projects_dir
            .join(util::project_dir_name(cwd))
            .join(format!("{session_id}.jsonl"));
        if fs::metadata(&path).is_err() {
            continue;
        }
        let text = read_tail(&path, 1024 * 1024)?;
        let mut out = Vec::new();
        for line in lines(&text) {
            let Ok(d) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let role = d.get("type").and_then(Value::as_str).unwrap_or("");
            if role != "user" && role != "assistant" {
                continue;
            }
            let at = d.get("timestamp").cloned().unwrap_or(json!(""));
            let content = d.get("message").and_then(|m| m.get("content"));
            let blocks: Vec<Value> = match content {
                Some(Value::String(s)) => vec![json!({"type": "text", "text": s})],
                Some(Value::Array(a)) => a.clone(),
                _ => vec![],
            };
            for b in blocks.iter().filter(|b| b.is_object()) {
                match b.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        let t = b.get("text").and_then(Value::as_str).unwrap_or("").trim();
                        if !t.is_empty() && !t.starts_with('<') {
                            out.push(json!({"role": role, "text": head(t, 3000), "at": at}));
                        }
                    }
                    Some("tool_use") => {
                        out.push(json!({"role": "tool", "text": tool_line(b), "at": at}))
                    }
                    _ => {}
                }
            }
        }
        let skip = out.len().saturating_sub(limit);
        return Some(out.into_iter().skip(skip).collect());
    }
    None
}

// ---- queue database

pub fn sluicy_db(c: &Cfg) -> Option<Connection> {
    let path = std::env::var_os("SLUICY_DB")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| c.sluicy_home.join("sluicy.db"));
    if !path.is_file() {
        return None;
    }
    let conn = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .ok()?;
    let _ = conn.busy_timeout(Duration::from_secs(5));
    Some(conn)
}

fn rows(conn: &Connection, sql: &str) -> Vec<Map<String, Value>> {
    let Ok(mut st) = conn.prepare(sql) else {
        return vec![];
    };
    let names: Vec<String> = st.column_names().iter().map(|s| s.to_string()).collect();
    let Ok(mut rs) = st.query([]) else {
        return vec![];
    };
    let mut out = Vec::new();
    while let Ok(Some(r)) = rs.next() {
        let mut m = Map::new();
        for (i, n) in names.iter().enumerate() {
            let v = match r.get_ref(i) {
                Ok(ValueRef::Null) | Err(_) => Value::Null,
                Ok(ValueRef::Integer(x)) => json!(x),
                Ok(ValueRef::Real(x)) => json!(x),
                Ok(ValueRef::Text(t)) => json!(String::from_utf8_lossy(t)),
                Ok(ValueRef::Blob(b)) => json!(String::from_utf8_lossy(b)),
            };
            m.insert(n.clone(), v);
        }
        out.push(m);
    }
    out
}

// ---- sessions

/// Answers that depend only on commit ids, kept until those commits move: (kind, sha, sha) -> answer.
type ByCommit = HashMap<(&'static str, String, String), Option<String>>;
static BY_COMMIT: Mutex<Option<ByCommit>> = Mutex::new(None);

/// `compute` runs only when this pair of commits has not been asked about before.
fn by_commit(
    kind: &'static str,
    a: &str,
    b: &str,
    compute: impl FnOnce() -> Option<String>,
) -> Option<String> {
    let key = (kind, a.to_string(), b.to_string());
    if let Some(v) = BY_COMMIT.lock().unwrap().as_ref().and_then(|m| m.get(&key)) {
        return v.clone();
    }
    let v = compute();
    if v.is_none() && kind == "ahead" {
        return v; // a failed git run is not an answer to keep
    }
    let mut g = BY_COMMIT.lock().unwrap();
    let m = g.get_or_insert_with(HashMap::new);
    if m.len() > 2000 {
        m.clear();
    }
    m.insert(key, v.clone());
    v
}

const ORIGIN_MAIN: &str = "refs/remotes/origin/main";

/// Newest transcript write of any session working in `path` (or below it).
fn activity_in(path: &str, activity: &[(String, f64)]) -> f64 {
    let prefix = format!("{path}/");
    activity
        .iter()
        .filter(|(cwd, _)| cwd == path || cwd.starts_with(&prefix))
        .map(|(_, m)| *m)
        .fold(0.0, f64::max)
}

/// path -> (when run, fingerprint after the run, `git status --porcelain` output)
type StatusCache = HashMap<PathBuf, (Instant, u64, Option<String>)>;
static STATUS: Mutex<Option<StatusCache>> = Mutex::new(None);

/// What can change a worktree's status, cheaply: its index (size, mtime), its HEAD commit, and
/// the newest transcript write of a session working in it, since sessions make nearly every edit.
fn status_fingerprint(path: &Path, dirs: Option<&gitfiles::Dirs>, activity: f64) -> u64 {
    use std::hash::{Hash, Hasher};
    use std::os::unix::fs::MetadataExt;
    let mut h = std::collections::hash_map::DefaultHasher::new();
    path.hash(&mut h);
    if let Some(d) = dirs {
        if let Ok(md) = fs::metadata(d.git_dir.join("index")) {
            (md.size(), md.mtime(), md.mtime_nsec()).hash(&mut h);
        }
        gitfiles::head_sha(d).hash(&mut h);
    }
    activity.to_bits().hash(&mut h);
    h.finish()
}

/// `git status --porcelain`, re-run only when the fingerprint moved (at most once per TTL) or the
/// answer is older than DECK_STATUS_MAX_AGE, so a hand edit with no session still shows within it.
/// TTL 0 runs it every time.
fn worktree_status(
    c: &Cfg,
    path: &Path,
    dirs: Option<&gitfiles::Dirs>,
    activity: f64,
    memo: &mut GitMemo,
) -> Option<String> {
    let key = (
        path.to_string_lossy().into_owned(),
        vec!["status".to_string(), "--porcelain".into()],
    );
    if let Some(v) = memo.get(&key) {
        return v.clone();
    }
    let fp = status_fingerprint(path, dirs, activity);
    if c.agents_ttl > 0.0 {
        if let Some((at, seen, out)) = STATUS.lock().unwrap().as_ref().and_then(|m| m.get(path)) {
            let age = at.elapsed().as_secs_f64();
            if age < c.agents_ttl || (*seen == fp && age < c.status_max_age) {
                return out.clone();
            }
        }
    }
    let out = git(path, &["status", "--porcelain"], Some(memo));
    // Fingerprint after the run: status may itself refresh the index.
    let after = status_fingerprint(path, dirs, activity);
    STATUS
        .lock()
        .unwrap()
        .get_or_insert_with(HashMap::new)
        .insert(path.to_path_buf(), (Instant::now(), after, out.clone()));
    out
}

/// After an action the next refresh must see its effect at once.
pub fn forget_status() {
    *STATUS.lock().unwrap() = None;
}

fn worktree_info(c: &Cfg, path: &Path, activity: f64, memo: &mut GitMemo) -> Value {
    let dirs = gitfiles::dirs(path);
    let branch = dirs
        .as_ref()
        .and_then(gitfiles::branch)
        .or_else(|| git(path, &["rev-parse", "--abbrev-ref", "HEAD"], Some(memo)));
    let status = worktree_status(c, path, dirs.as_ref(), activity, memo);
    let shas = dirs
        .as_ref()
        .and_then(|d| Some((gitfiles::resolve(d, ORIGIN_MAIN)?, gitfiles::head_sha(d)?)));
    let counts = match shas {
        Some((main, head)) => by_commit("ahead", &main, &head, || {
            git(
                path,
                &["rev-list", "--left-right", "--count", "origin/main...HEAD"],
                None,
            )
        }),
        None => git(
            path,
            &["rev-list", "--left-right", "--count", "origin/main...HEAD"],
            Some(memo),
        ),
    };
    let (behind, ahead) = match counts
        .as_deref()
        .map(|c| c.split_whitespace().collect::<Vec<_>>())
    {
        Some(v) if v.len() == 2 => (v[0].to_string(), v[1].to_string()),
        _ => ("?".into(), "?".into()),
    };
    json!({
        "branch": branch,
        "dirty": status.map(|s| lines(&s).len()),
        "ahead": ahead,
        "behind": behind,
    })
}

fn within(path: &str, roots: &[String]) -> bool {
    roots
        .iter()
        .any(|r| path == r || path.starts_with(&format!("{r}/")))
}

/// What `claude agents` reads, reduced to a number: every `sessions/<pid>.json` and
/// `jobs/<id>/state.json` with its size and mtime, and whether each registered pid is alive
/// (a session that crashes leaves its file behind). Costs a few stat calls.
pub fn registry_fingerprint(c: &Cfg) -> u64 {
    use std::hash::{Hash, Hasher};
    use std::os::unix::fs::MetadataExt;
    let mut entries: Vec<(String, u64, i64, i64, bool)> = Vec::new();
    let mut add = |p: &Path, alive: bool| {
        if let Ok(md) = fs::metadata(p) {
            entries.push((
                p.to_string_lossy().into_owned(),
                md.size(),
                md.mtime(),
                md.mtime_nsec(),
                alive,
            ));
        }
    };
    if let Ok(rd) = fs::read_dir(&c.sessions_dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) != Some("json") {
                continue;
            }
            let alive = p
                .file_stem()
                .and_then(|s| s.to_str())
                .and_then(|s| s.parse::<i32>().ok())
                .map(|pid| Path::new(&format!("/proc/{pid}")).exists())
                .unwrap_or(false);
            add(&p, alive);
        }
    }
    if let Ok(rd) = fs::read_dir(&c.jobs_dir) {
        for e in rd.flatten() {
            add(&e.path().join("state.json"), false);
        }
    }
    entries.sort();
    let mut h = std::collections::hash_map::DefaultHasher::new();
    entries.hash(&mut h);
    h.finish()
}

/// (agents, error text). --all keeps stopped background sessions listed.
pub fn list_agents(c: &Cfg) -> (Vec<Value>, String) {
    let (rc, out, err) = util::run(
        &[
            c.claude.clone(),
            "agents".into(),
            "--json".into(),
            "--all".into(),
        ],
        Some(&c.repo),
        Duration::from_secs(20),
    );
    let agents = if rc == 0 {
        serde_json::from_str::<Value>(&out)
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default()
    } else {
        vec![]
    };
    (
        agents,
        if rc != 0 {
            head(err.trim(), 300)
        } else {
            String::new()
        },
    )
}

fn s_or<'a>(a: &'a Value, b: &'a Value, k: &str) -> String {
    for v in [a.get(k), b.get(k)] {
        if util::truthy(v) {
            return util::as_str(v);
        }
    }
    String::new()
}

/// Each live session's own file, {pid: record}. A session rewrites it the moment its status
/// changes (busy, idle, waiting + waitingFor), so it is fresher than the cached `claude agents`.
pub fn read_registry(c: &Cfg) -> HashMap<i64, Value> {
    let mut registry: HashMap<i64, Value> = HashMap::new();
    if let Ok(rd) = fs::read_dir(&c.sessions_dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) != Some("json") {
                continue;
            }
            if let Some(d) = fs::read_to_string(&p)
                .ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            {
                if let Some(pid) = d.get("pid").and_then(Value::as_i64) {
                    registry.insert(pid, d);
                }
            }
        }
    }
    registry
}

pub fn collect_sessions(
    c: &Cfg,
    procs: &Procs,
    roots: &[String],
    agents: &[Value],
    memo: &mut GitMemo,
    registry: &HashMap<i64, Value>,
) -> Vec<Value> {
    let mut tickets: HashMap<String, Value> = HashMap::new();
    if let Some(conn) = sluicy_db(c) {
        for t in rows(
            &conn,
            "SELECT id, branch, state, mode, components, detail FROM tickets ORDER BY id",
        ) {
            let b = util::as_str(t.get("branch"));
            tickets.insert(b, Value::Object(t));
        }
    }
    let null = Value::Null;
    // Pass 1: where each session works and when it last wrote its transcript.
    struct Pre<'a> {
        a: &'a Value,
        reg: &'a Value,
        pid: Option<i64>,
        kind: String,
        stopped: bool,
        cwd: String,
        use_cwd: String,
        name: String,
        tail: Option<Tail>,
    }
    let mut pres = Vec::new();
    for a in agents {
        let pid = a.get("pid").and_then(Value::as_i64).filter(|p| *p != 0);
        let reg = pid.and_then(|p| registry.get(&p)).unwrap_or(&null);
        let kind = s_or(a, reg, "kind");
        // A stopped background session has no pid; its `state` still reads "done" after attach.
        let stopped = kind == "background" && pid.is_none();
        let cwd = s_or(a, reg, "cwd");
        let mut live_cwd = pid.map(|p| procs::cwd(p as i32)).unwrap_or_default();
        if !within(&live_cwd, roots) {
            live_cwd.clear();
        }
        let name = util::as_str(a.get("name"));
        if stopped && !name.is_empty() {
            let base = if cwd.is_empty() {
                c.repo.clone()
            } else {
                PathBuf::from(&cwd)
            };
            let own = base.join(".claude").join("worktrees").join(&name);
            if own.is_dir() {
                live_cwd = own.to_string_lossy().into_owned();
            }
        }
        let use_cwd = if live_cwd.is_empty() {
            cwd.clone()
        } else {
            live_cwd.clone()
        };
        let session_id = util::as_str(a.get("sessionId"));
        let mut tail = None;
        for d in [&cwd, &live_cwd] {
            if tail.is_none() && !d.is_empty() {
                tail = transcript_tail(c, d, &session_id);
            }
        }
        pres.push(Pre {
            a,
            reg,
            pid,
            kind,
            stopped,
            cwd,
            use_cwd,
            name,
            tail,
        });
    }
    let activity: Vec<(String, f64)> = pres
        .iter()
        .filter_map(|p| Some((p.use_cwd.clone(), p.tail.as_ref()?.mtime)))
        .collect();
    // Pass 2: the rows.
    let mut out = Vec::new();
    for Pre {
        a,
        reg,
        pid,
        kind,
        stopped,
        cwd,
        use_cwd,
        name,
        tail,
    } in pres
    {
        let tree = pid.map(|p| procs.tree(p as i32)).unwrap_or_default();
        let wt = if use_cwd.is_empty() {
            json!({})
        } else {
            worktree_info(
                c,
                Path::new(&use_cwd),
                activity_in(&use_cwd, &activity),
                memo,
            )
        };
        let branch = wt.get("branch").and_then(Value::as_str);
        let ticket = match branch {
            Some(b) if b != "main" && b != "HEAD" => tickets.get(b).cloned().unwrap_or(Value::Null),
            _ => Value::Null,
        };
        let id = ["id", "shortId", "sessionId"]
            .iter()
            .map(|k| a.get(*k))
            .find(|v| util::truthy(*v))
            .map(|v| v.cloned().unwrap())
            .unwrap_or(Value::Null);
        // The registry is rewritten on every status change; the agents list may be a TTL old.
        let reg_status = util::truthy(reg.get("status"));
        let status = if stopped {
            "stopped".to_string()
        } else {
            s_or(reg, a, "status")
        };
        let waiting_for = util::as_str(if reg_status {
            reg.get("waitingFor")
        } else {
            a.get("waitingFor")
        });
        let state = util::as_str(a.get("state"));
        let needs = session_needs(&status, &waiting_for, &state, stopped, tail.as_ref());
        let display_name = if name.is_empty() {
            util::as_str(reg.get("name"))
        } else {
            name
        };
        let num = |v: Option<&Value>| v.and_then(Value::as_f64).unwrap_or(0.0) / 1000.0;
        out.push(json!({
            "id": id,
            "sessionId": a.get("sessionId").cloned().unwrap_or(Value::Null),
            "name": display_name,
            "kind": kind,
            "waitingFor": if status == "waiting" { waiting_for } else { String::new() },
            "state": state,
            "needs": needs,
            "status": status,
            "stopped": stopped,
            "statusSince": num(reg.get("statusUpdatedAt")),
            "startedAt": num(a.get("startedAt")),
            "pid": pid,
            "cwd": use_cwd,
            "registeredCwd": cwd,
            "worktree": wt,
            "ticket": ticket,
            "cpu": round1(tree.iter().map(|p| p.cpu).sum()),
            "rss": tree.iter().map(|p| p.rss).sum::<u64>(),
            "procs": tree.len(),
            "lastActivity": tail.as_ref().map(|t| t.mtime),
            "last": tail.as_ref().map(|t| t.last.clone()).unwrap_or_default(),
            "attachable": kind == "background",
        }));
    }
    out
}

// ---- production stamps and lease, read in the background and cached 60 s

struct Vps {
    at: Option<Instant>,
    data: Value,
    running: bool,
}

static VPS: Mutex<Option<Vps>> = Mutex::new(None);

fn vps_state(c: &Cfg) -> Value {
    let mut g = VPS.lock().unwrap();
    let v = g.get_or_insert_with(|| Vps {
        at: None,
        data: json!({"ok": null, "stamps": {}, "lease": []}),
        running: false,
    });
    let stale =
        v.at.map(|a| a.elapsed() > Duration::from_secs(60))
            .unwrap_or(true);
    if stale && !v.running {
        v.running = true;
        let mut argv = c.sluicy.clone();
        argv.extend(util::argv(&["stamps", "--json"]));
        let repo = c.repo.clone();
        std::thread::spawn(move || {
            // `sluicy stamps` runs the project's own stamps and lease commands (often one ssh read).
            let (rc, out, _) = util::run(&argv, Some(&repo), Duration::from_secs(45));
            let got: Value = serde_json::from_str(&out).unwrap_or(Value::Null);
            let mut stamps = Map::new();
            if let Some(m) = got["stamps"].as_object() {
                for (k, val) in m {
                    stamps.insert(k.clone(), json!(head(val.as_str().unwrap_or(""), 12)));
                }
            }
            let lease = got["lease"].as_array().cloned().unwrap_or_default();
            let ok = if rc != 0 { json!(false) } else { got["ok"].clone() };
            let mut g = VPS.lock().unwrap();
            if let Some(v) = g.as_mut() {
                let configured = got.get("configured").cloned().unwrap_or(Value::Null);
                v.data = json!({"ok": ok, "stamps": stamps, "lease": lease, "configured": configured});
                v.at = Some(Instant::now());
                v.running = false;
            }
        });
    }
    let mut d = v.data.clone();
    d["age"] =
        v.at.map(|a| json!(a.elapsed().as_secs()))
            .unwrap_or(Value::Null);
    d
}

/// The code map's impact (sluicy's `impact` column, JSON) as a few words for the queue's Map column, or "".
/// The full impact (every area's files) stays out of the page: `sluicy review <id>` prints it.
pub fn map_summary(impact: &Value, current: bool) -> String {
    let Some(text) = impact.as_str() else {
        return String::new();
    };
    let Ok(imp) = serde_json::from_str::<Value>(text) else {
        return String::new();
    };
    let count = |k: &str| imp[k].as_array().map(|a| a.len()).unwrap_or(0);
    let plural = |n: usize, one: &str| format!("{n} {one}{}", if n == 1 { "" } else { "s" });
    let mut parts = vec![plural(count("files"), "file"), plural(count("communities"), "area")];
    if count("god_nodes") > 0 {
        parts.push(plural(count("god_nodes"), "hub"));
    }
    let hits: usize = imp["rule_hits"]
        .as_object()
        .map(|o| o.values().map(|v| v.as_array().map(|a| a.len()).unwrap_or(0)).sum())
        .unwrap_or(0);
    if hits > 0 {
        parts.push(plural(hits, "rule hit"));
    }
    let mut out = parts.join(" · ");
    if !current {
        out.push_str(" (older commit)");
    }
    out
}

pub fn collect_queue(c: &Cfg) -> Value {
    let mut tickets = vec![];
    let mut holds = vec![];
    if let Some(conn) = sluicy_db(c) {
        tickets = rows(&conn, "SELECT * FROM tickets ORDER BY id DESC LIMIT 40");
        holds = rows(&conn, "SELECT * FROM holds WHERE cleared_at IS NULL");
    }
    for t in tickets.iter_mut() {
        let current = t.get("impact_sha").is_some() && t.get("impact_sha") == t.get("sha");
        let map = t.remove("impact").map(|v| map_summary(&v, current)).unwrap_or_default();
        t.insert("map".into(), json!(map));
    }
    let alerts_path = c.sluicy_home.join("alerts.log");
    let alerts: Vec<String> = if alerts_path.is_file() {
        let t = String::from_utf8_lossy(&fs::read(&alerts_path).unwrap_or_default()).into_owned();
        let l = lines(&t);
        l[l.len().saturating_sub(15)..]
            .iter()
            .map(|s| s.to_string())
            .collect()
    } else {
        vec![]
    };
    json!({
        "tickets": tickets,
        "holds": holds,
        "alerts": alerts,
        "guards": c.sluicy_home.join("guards-on").exists(),
        "components": c.components,
        "waivers": c.waivers,
        "vps": vps_state(c),
    })
}

// ---- the laptop

fn lock_state(path: &Path) -> Value {
    if !path.exists() {
        return json!({"held": false, "holder": ""});
    }
    let Ok(mut f) = File::open(path) else {
        return json!({"held": null, "holder": ""});
    };
    let held = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) } != 0;
    let mut buf = vec![0u8; 300];
    let n = f.read(&mut buf).unwrap_or(0);
    let holder = String::from_utf8_lossy(&buf[..n]).trim().to_string();
    let since = fs::metadata(path).map(|m| mtime_secs(&m)).unwrap_or(0.0);
    json!({"held": held, "holder": if held { holder } else { String::new() }, "since": since})
}

fn disk_usage(path: &str) -> (u64, u64) {
    let c = std::ffi::CString::new(path).unwrap();
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return (0, 0);
    }
    (
        st.f_blocks as u64 * st.f_frsize as u64,
        st.f_bavail as u64 * st.f_frsize as u64,
    )
}

fn latest_gate_log(dir: &Path) -> Option<PathBuf> {
    let mut best: Option<(f64, PathBuf)> = None;
    for sub in fs::read_dir(dir).ok()?.flatten() {
        let Ok(rd) = fs::read_dir(sub.path()) else {
            continue;
        };
        for f in rd.flatten() {
            let p = f.path();
            if p.extension().and_then(|x| x.to_str()) != Some("log") {
                continue;
            }
            let Ok(md) = f.metadata() else { continue };
            if !md.is_file() {
                continue;
            }
            let m = mtime_secs(&md);
            if best.as_ref().map(|b| m > b.0).unwrap_or(true) {
                best = Some((m, p));
            }
        }
    }
    best.map(|b| b.1)
}

pub fn collect_system(c: &Cfg, procs: &Procs) -> Value {
    let m = procs::meminfo();
    let g = |k: &str| *m.get(k).unwrap_or(&0);
    let avail_pct = 100.0 * g("MemAvailable") as f64 / g("MemTotal").max(1) as f64;
    let swap_total = g("SwapTotal");
    let swap_used = swap_total.saturating_sub(g("SwapFree"));
    let swap_used_pct = if swap_total > 0 {
        100.0 * swap_used as f64 / swap_total as f64
    } else {
        0.0
    };
    let (disk_total, disk_free) = disk_usage("/");
    let uid = procs::my_uid();
    let mut mine: Vec<&procs::Proc> = procs.procs.values().filter(|p| p.uid == uid).collect();
    mine.sort_by(|a, b| b.rss.cmp(&a.rss));
    let top: Vec<Value> = mine
        .iter()
        .take(20)
        .map(|p| json!({"pid": p.pid, "start": p.start, "comm": p.comm, "rss": p.rss, "cpu": p.cpu, "cmd": procs::cmdline(p.pid, &p.comm)}))
        .collect();
    let mut groups: HashMap<&str, (usize, u64, f64)> = HashMap::new();
    for p in &mine {
        let e = groups.entry(p.comm.as_str()).or_insert((0, 0, 0.0));
        e.0 += 1;
        e.1 += p.rss;
        e.2 = round1(e.2 + p.cpu);
    }
    let mut groups: Vec<(&str, (usize, u64, f64))> = groups.into_iter().collect();
    groups.sort_by(|a, b| b.1 .1.cmp(&a.1 .1));
    let groups: Vec<Value> = groups
        .iter()
        .take(8)
        .map(|(n, (cnt, rss, cpu))| json!({"name": n, "count": cnt, "rss": rss, "cpu": cpu}))
        .collect();
    let (gate_file, gate_tail) = match c
        .gate_logs
        .is_dir()
        .then(|| latest_gate_log(&c.gate_logs))
        .flatten()
    {
        Some(p) => {
            let t = read_tail(&p, 16384).unwrap_or_default();
            let l = lines(&t);
            (
                p.to_string_lossy().into_owned(),
                l[l.len().saturating_sub(15)..]
                    .iter()
                    .map(|s| s.to_string())
                    .collect::<Vec<_>>(),
            )
        }
        None => (String::new(), vec![]),
    };
    let mut warnings = vec![];
    if avail_pct < c.mem_warn_pct {
        warnings.push(format!("Only {avail_pct:.0}% memory available"));
    }
    // Swap fills slowly and stays full on a healthy box; it matters only once memory is short too.
    let swap_hot = swap_used_pct > c.swap_warn_pct && avail_pct < 2.0 * c.mem_warn_pct;
    if swap_hot {
        warnings.push(format!("Swap {swap_used_pct:.0}% used"));
    }
    let holders: Vec<Value> = if swap_total > 0 {
        procs::swap_holders(6, Duration::from_secs(30))
            .into_iter()
            .map(|h| json!({"pid": h.pid, "comm": h.comm, "swap": h.swap}))
            .collect()
    } else {
        vec![]
    };
    let load: Vec<String> = fs::read_to_string("/proc/loadavg")
        .unwrap_or_default()
        .split_whitespace()
        .take(3)
        .map(String::from)
        .collect();
    let cores = unsafe { libc::sysconf(libc::_SC_NPROCESSORS_CONF) };
    let locks: Map<String, Value> = c
        .locks
        .iter()
        .map(|(k, p)| (k.clone(), lock_state(p)))
        .collect();
    json!({
        "cpu": procs::cpu_total(),
        "cores": cores,
        "load": load,
        "mem": {"total": g("MemTotal"), "available": g("MemAvailable"), "availPct": round1(avail_pct)},
        "swap": {"total": swap_total, "used": swap_used, "usedPct": round1(swap_used_pct), "hot": swap_hot, "holders": holders},
        "disk": {"total": disk_total, "free": disk_free},
        "locks": locks,
        "gate": {"file": gate_file, "tail": gate_tail},
        "top": top,
        "groups": groups,
        "warnings": warnings,
    })
}

// ---- worktrees

pub fn list_worktrees(c: &Cfg, memo: &mut GitMemo) -> Vec<Map<String, Value>> {
    let out = git(&c.repo, &["worktree", "list", "--porcelain"], Some(memo)).unwrap_or_default();
    let mut trees = vec![];
    let mut cur = Map::new();
    for line in out.lines().chain(std::iter::once("")) {
        if line.is_empty() {
            if !cur.is_empty() {
                trees.push(std::mem::take(&mut cur));
            }
        } else if let Some(p) = line.strip_prefix("worktree ") {
            cur.insert("path".into(), json!(p));
        } else if let Some(b) = line.strip_prefix("branch ") {
            cur.insert(
                "branch".into(),
                json!(b.strip_prefix("refs/heads/").unwrap_or(b)),
            );
        }
    }
    trees
}

pub fn collect_worktrees(
    c: &Cfg,
    mut trees: Vec<Map<String, Value>>,
    sessions: &[Value],
    memo: &mut GitMemo,
) -> Vec<Map<String, Value>> {
    let main = c.repo.to_string_lossy().into_owned();
    let activity: Vec<(String, f64)> = sessions
        .iter()
        .filter_map(|s| {
            Some((
                s.get("cwd")?.as_str()?.to_string(),
                s.get("lastActivity")?.as_f64()?,
            ))
        })
        .collect();
    let repo_dirs = gitfiles::dirs(&c.repo);
    let main_sha = repo_dirs
        .as_ref()
        .and_then(|d| gitfiles::resolve(d, ORIGIN_MAIN));
    for t in trees.iter_mut() {
        let path = util::as_str(t.get("path"));
        t.insert("main".into(), json!(path == main));
        let status = if Path::new(&path).is_dir() {
            let act = activity_in(&path, &activity);
            worktree_status(
                c,
                Path::new(&path),
                gitfiles::dirs(Path::new(&path)).as_ref(),
                act,
                memo,
            )
        } else {
            None
        };
        t.insert("dirty".into(), json!(status.map(|s| lines(&s).len())));
        let branch = t.get("branch").and_then(Value::as_str).map(String::from);
        let merged = match &branch {
            Some(b)
                if !b.is_empty()
                    && main_sha.is_some()
                    && repo_dirs
                        .as_ref()
                        .and_then(|d| gitfiles::resolve(d, &format!("refs/heads/{b}")))
                        .is_some() =>
            {
                let tip = repo_dirs
                    .as_ref()
                    .and_then(|d| gitfiles::resolve(d, &format!("refs/heads/{b}")))
                    .unwrap();
                by_commit("merged", &tip, main_sha.as_deref().unwrap(), || {
                    let rc = util::run(
                        &util::argv(&["git", "merge-base", "--is-ancestor", b, "origin/main"]),
                        Some(&c.repo),
                        Duration::from_secs(30),
                    )
                    .0;
                    (rc == 0).then(String::new)
                })
                .is_some()
            }
            Some(b) if !b.is_empty() => {
                let key = (
                    main.clone(),
                    vec!["merge-base".to_string(), "--is-ancestor".into(), b.clone()],
                );
                if let Some(v) = memo.get(&key) {
                    v.is_some()
                } else {
                    let rc = util::run(
                        &util::argv(&["git", "merge-base", "--is-ancestor", b, "origin/main"]),
                        Some(&c.repo),
                        Duration::from_secs(30),
                    )
                    .0;
                    memo.insert(key, (rc == 0).then(String::new));
                    rc == 0
                }
            }
            _ => false,
        };
        t.insert("merged".into(), json!(merged));
        let prefix = format!("{path}/");
        let names: Vec<Value> = sessions
            .iter()
            .filter(|s| {
                let sc = s.get("cwd").and_then(Value::as_str).unwrap_or("");
                sc == path || sc.starts_with(&prefix)
            })
            .map(|s| s.get("name").cloned().unwrap_or(json!("")))
            .collect();
        t.insert("sessions".into(), Value::Array(names));
    }
    trees
}

pub fn local_branches(c: &Cfg, memo: &mut GitMemo) -> Vec<String> {
    let out = git(
        &c.repo,
        &[
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)",
            "refs/heads/",
        ],
        Some(memo),
    )
    .unwrap_or_default();
    out.lines()
        .filter(|b| *b != "main" && !b.is_empty())
        .take(80)
        .map(String::from)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn map_summary_reads_the_impact_column() {
        let imp = json!({"files": ["a", "b"], "communities": [{"name": "web"}], "god_nodes": [],
                         "rule_hits": {"web-not-api": [["a", "x"]]}})
        .to_string();
        assert_eq!(map_summary(&json!(imp), true), "2 files · 1 area · 1 rule hit");
        assert_eq!(map_summary(&json!(imp), false), "2 files · 1 area · 1 rule hit (older commit)");
        assert_eq!(map_summary(&Value::Null, true), "");
        assert_eq!(map_summary(&json!("{bad"), true), "");
    }

    fn tail(pending: Value) -> Tail {
        Tail {
            mtime: 0.0,
            last: String::new(),
            last_full: "Ship it?".into(),
            pending,
        }
    }

    #[test]
    fn needs_rows() {
        assert!(session_needs("idle", "", "done", false, Some(&tail(Value::Null))).is_null());
        assert!(session_needs("stopped", "", "blocked", true, None).is_null());
        assert!(session_needs("busy", "", "blocked", false, None).is_null());
        assert_eq!(
            session_needs("waiting", "", "", false, None)["kind"],
            "permission"
        );
        assert_eq!(
            session_needs("waiting", "", "", false, None)["label"],
            "permission prompt"
        );
        let r = session_needs("idle", "", "blocked", false, Some(&tail(Value::Null)));
        assert_eq!(
            (r["kind"].as_str(), r["message"].as_str()),
            (Some("reply"), Some("Ship it?"))
        );
        let q = session_needs(
            "waiting",
            "x",
            "",
            false,
            Some(&tail(json!({"name": "AskUserQuestion"}))),
        );
        assert_eq!(q["kind"], "question");
        let p = session_needs(
            "waiting",
            "x",
            "",
            false,
            Some(&tail(json!({"name": "ExitPlanMode"}))),
        );
        assert_eq!(p["label"], "plan approval");
    }

    #[test]
    fn a_result_clears_the_pending_call() {
        let call = json!({"message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}});
        let res = json!({"message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}});
        assert_eq!(pending_tool(std::slice::from_ref(&call))["command"], "ls");
        assert!(pending_tool(&[call, res]).is_null());
    }
}
