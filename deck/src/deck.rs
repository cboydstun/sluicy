//! The deck: state builds (one at a time, shared by concurrent callers) and the actions.
//! Every action is a fixed argv built from values the deck itself listed; nothing reaches a shell.

use crate::cleanup;
use crate::collect::{self, Cfg};
use crate::procs::{self, Procs};
use crate::term::Terminal;
use crate::util::{self, as_int, as_str, GitMemo};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// Appended to the system prompt of a New session with "Ask questions first" on.
/// `~/.config/sluicy/preamble.txt` replaces it.
const BRAINSTORM_PREAMBLE: &str = include_str!("../../share/brainstorm-preamble.txt");

fn preamble() -> String {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_default();
    let own = std::env::var_os("SLUICY_PREAMBLE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| home.join(".config/sluicy/preamble.txt"));
    std::fs::read_to_string(own)
        .ok()
        .filter(|t| !t.trim().is_empty())
        .unwrap_or_else(|| BRAINSTORM_PREAMBLE.to_string())
}

pub const QUEUE_TERM: &str = "queue";

#[derive(Default)]
struct Listed {
    sessions: HashMap<String, Value>,
    branches: HashSet<String>,
    worktrees: HashMap<String, Map<String, Value>>,
}

struct Build {
    building: bool,
    built: u64,
    last: Option<Arc<Value>>,
}

struct Caches {
    /// (when, wall clock before the read, registry fingerprint then, agents, error text)
    agents: Option<(Instant, f64, u64, Vec<Value>, String)>,
    /// Worktree git answers shared by refreshes for the same TTL (None: start afresh).
    git: (Option<Instant>, GitMemo),
}

pub type Reply = (u16, Value);

pub struct Deck {
    pub c: Cfg,
    pub terms: Mutex<HashMap<String, Arc<Terminal>>>,
    listed: Mutex<Listed>,
    caches: Mutex<Caches>,
    build: Mutex<Build>,
    build_cond: Condvar,
    /// Clean up rows acted on, until the conductor rewrites cleanup.json (`generated` changes).
    cleanup_hidden: Mutex<(Value, HashSet<String>)>,
}

fn err(code: u16, msg: &str) -> Reply {
    (code, json!({"error": msg}))
}

fn ran((rc, out, errout): util::RunOut) -> Reply {
    (
        if rc == 0 { 200 } else { 500 },
        json!({"ok": rc == 0, "rc": rc, "output": util::tail_chars(&(out + &errout), 6000)}),
    )
}

/// A session name as `claude -w` will use it for the worktree and branch: "Fix login" -> "fix-login".
pub fn slug(name: &str) -> String {
    let lower = name.to_lowercase();
    let mut out = String::new();
    let mut dash = false;
    for ch in lower.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            if dash && !out.is_empty() {
                out.push('-');
            }
            dash = false;
            out.push(ch);
        } else {
            dash = true;
        }
    }
    let cut: String = out.chars().take(40).collect();
    cut.trim_end_matches('-').to_string()
}

fn valid_name(n: &str) -> bool {
    let b = n.as_bytes();
    !b.is_empty()
        && b.len() <= 40
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

fn valid_allow_target(t: &str) -> bool {
    let hex = (7..=40).contains(&t.len())
        && t.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c));
    let digits = !t.is_empty() && t.bytes().all(|c| c.is_ascii_digit());
    hex || digits || t == "unstamped" || t == "hold"
}

fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

impl Deck {
    pub fn new(c: Cfg) -> Deck {
        Deck {
            c,
            terms: Mutex::new(HashMap::new()),
            listed: Mutex::new(Listed::default()),
            caches: Mutex::new(Caches {
                agents: None,
                git: (None, HashMap::new()),
            }),
            build: Mutex::new(Build {
                building: false,
                built: 0,
                last: None,
            }),
            build_cond: Condvar::new(),
            cleanup_hidden: Mutex::new((Value::Null, HashSet::new())),
        }
    }

    fn ttl(&self) -> Duration {
        Duration::from_secs_f64(self.c.agents_ttl.max(0.0))
    }

    /// `claude agents` is a node start-up: ~0.2 s CPU and a ~140 MB burst. It reads the session
    /// registry and the background-job states, so its answer can only change when those files do
    /// (or a session's process dies). It runs again when that fingerprint moves (at most once per
    /// TTL), when the answer is older than DECK_AGENTS_MAX_AGE, or after an action. TTL 0 = always.
    /// A session whose status changed after the cached read (it started waiting on the person, say)
    /// refetches at once: that must reach the page at the next refresh, not a TTL later.
    fn agents(&self, registry: &HashMap<i64, Value>) -> (Vec<Value>, String) {
        let fp = collect::registry_fingerprint(&self.c);
        let changed = registry
            .values()
            .filter_map(|r| r.get("statusUpdatedAt").and_then(Value::as_f64))
            .fold(0.0, f64::max)
            / 1000.0;
        if let Some((at, wall, seen, a, e)) = &self.caches.lock().unwrap().agents {
            let age = at.elapsed();
            let fresh =
                age < self.ttl() || (*seen == fp && age.as_secs_f64() < self.c.agents_max_age);
            if fresh && changed <= *wall && self.c.agents_ttl > 0.0 {
                return (a.clone(), e.clone());
            }
        }
        let wall = util::now_secs(); // taken before the read, so a change during it forces the next one
        let (a, e) = collect::list_agents(&self.c);
        self.caches.lock().unwrap().agents = Some((Instant::now(), wall, fp, a.clone(), e.clone()));
        (a, e)
    }

    fn take_git_memo(&self) -> GitMemo {
        let mut g = self.caches.lock().unwrap();
        if g.git.0.map(|at| at.elapsed() >= self.ttl()).unwrap_or(true) {
            g.git = (Some(Instant::now()), HashMap::new());
        }
        g.git.1.clone()
    }

    fn put_git_memo(&self, memo: GitMemo) {
        let mut g = self.caches.lock().unwrap();
        if g.git.0.map(|at| at.elapsed() < self.ttl()).unwrap_or(false) {
            g.git.1.extend(memo);
        }
    }

    /// After an action the next refresh must see its effect at once.
    pub fn forget_agents(&self) {
        let mut g = self.caches.lock().unwrap();
        g.agents = None;
        g.git = (None, HashMap::new());
        collect::forget_status();
    }

    /// One build at a time: a caller that arrives during a build gets that build's result.
    pub fn state(&self) -> Arc<Value> {
        {
            let mut b = self.build.lock().unwrap();
            if b.building {
                let seen = b.built;
                while b.building && b.built == seen {
                    b = self
                        .build_cond
                        .wait_timeout(b, Duration::from_secs(30))
                        .unwrap()
                        .0;
                }
                if b.built != seen {
                    if let Some(last) = &b.last {
                        return last.clone();
                    }
                }
            }
            b.building = true;
        }
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.build_state()));
        let mut b = self.build.lock().unwrap();
        b.building = false;
        b.built += 1;
        if let Ok(v) = &res {
            b.last = Some(v.clone());
        }
        self.build_cond.notify_all();
        drop(b);
        match res {
            Ok(v) => v,
            Err(p) => std::panic::resume_unwind(p),
        }
    }

    fn build_state(&self) -> Arc<Value> {
        let procs = Procs::new(true);
        let mut memo = self.take_git_memo();
        let trees = collect::list_worktrees(&self.c, &mut memo);
        let roots: Vec<String> = trees.iter().map(|t| as_str(t.get("path"))).collect();
        let registry = collect::read_registry(&self.c);
        let (agents, agents_err) = self.agents(&registry);
        let sessions =
            collect::collect_sessions(&self.c, &procs, &roots, &agents, &mut memo, &registry);
        let trees = collect::collect_worktrees(&self.c, trees, &sessions, &mut memo);
        let branches = collect::local_branches(&self.c, &mut memo);
        self.put_git_memo(memo);
        {
            let mut l = self.listed.lock().unwrap();
            l.sessions = sessions
                .iter()
                .filter_map(|s| {
                    s.get("id")
                        .filter(|v| util::truthy(Some(v)))
                        .map(|id| (as_str(Some(id)), s.clone()))
                })
                .collect();
            l.branches = branches.iter().cloned().collect();
            l.worktrees = trees
                .iter()
                .map(|t| (as_str(t.get("path")), t.clone()))
                .collect();
        }
        let terminals: Vec<Value> = self
            .term_items()
            .iter()
            .map(|(k, t)| json!({"id": k, "session": t.session, "closed": t.closed()}))
            .collect();
        Arc::new(json!({
            "now": util::now_secs(),
            "sessions": sessions,
            "agentsError": agents_err,
            "queue": collect::collect_queue(&self.c),
            "system": collect::collect_system(&self.c, &procs),
            "worktrees": trees,
            "branches": branches,
            "terminals": terminals,
            "cleanup": cleanup::collect(&self.c, &sessions, &self.cleanup_hidden.lock().unwrap()),
        }))
    }

    pub fn term_items(&self) -> Vec<(String, Arc<Terminal>)> {
        self.terms
            .lock()
            .unwrap()
            .iter()
            .map(|(k, t)| (k.clone(), t.clone()))
            .collect()
    }

    pub fn term(&self, key: &str) -> Option<Arc<Terminal>> {
        self.terms.lock().unwrap().get(key).cloned()
    }

    /// Close terminals no page has streamed for `term_idle` seconds, and forget closed ones.
    pub fn reap_terminals(&self) {
        for (k, t) in self.term_items() {
            if t.closed() || t.idle_for() > self.c.term_idle {
                t.close();
                self.terms.lock().unwrap().remove(&k);
            }
        }
    }

    pub fn act(&self, name: &str, b: &Map<String, Value>) -> Reply {
        let known = matches!(
            name,
            "submit"
                | "allow"
                | "release"
                | "ticket_log"
                | "session_start"
                | "session_stop"
                | "session_rm"
                | "session_logs"
                | "term_open"
                | "term_queue"
                | "term_input"
                | "term_resize"
                | "term_close"
                | "proc_info"
                | "kill"
                | "worktree_remove"
                | "ticket_cancel"
                | "stray_rm"
                | "debt_done"
        );
        if !known {
            return err(404, "no such action");
        }
        let empty = {
            let l = self.listed.lock().unwrap();
            l.sessions.is_empty() && l.branches.is_empty()
        };
        if empty {
            self.state();
        }
        match name {
            "submit" => self.do_submit(b),
            "allow" => self.do_allow(b),
            "release" => self.do_release(b),
            "ticket_log" => self.do_ticket_log(b),
            "session_start" => self.do_session_start(b),
            "session_stop" => self.session_cmd(b, "stop"),
            "session_rm" => self.session_cmd(b, "rm"),
            "session_logs" => self.do_session_logs(b),
            "term_open" => self.do_term_open(b),
            "term_queue" => self.do_term_queue(),
            "term_input" => self.do_term_input(b),
            "term_resize" => self.do_term_resize(b),
            "term_close" => self.do_term_close(b),
            "proc_info" => self.do_proc_info(b),
            "kill" => self.do_kill(b),
            "worktree_remove" => self.do_worktree_remove(b),
            "ticket_cancel" => self.do_ticket_cancel(b),
            "stray_rm" => self.do_stray_rm(b),
            "debt_done" => self.do_debt_done(b),
            _ => err(404, "no such action"),
        }
    }

    fn listed_session(&self, b: &Map<String, Value>, background: bool) -> Result<Value, Reply> {
        let id = as_str(b.get("id"));
        let s = self.listed.lock().unwrap().sessions.get(&id).cloned();
        let Some(s) = s else {
            return Err(err(400, "unknown session; refresh the page"));
        };
        if background && s.get("attachable") != Some(&json!(true)) {
            return Err(err(
                400,
                "only background sessions can be controlled from here",
            ));
        }
        Ok(s)
    }

    fn sluicy_argv(&self, rest: &[String]) -> Vec<String> {
        let mut a = self.c.sluicy.clone();
        a.extend_from_slice(rest);
        a
    }

    fn do_submit(&self, b: &Map<String, Value>) -> Reply {
        let branch = as_str(b.get("branch"));
        if !self.listed.lock().unwrap().branches.contains(&branch) {
            return err(400, "unknown branch");
        }
        let mut rest = vec!["submit".to_string(), "--branch".into(), branch];
        match b.get("mode").and_then(Value::as_str) {
            Some("deploy") => {
                let comps: Vec<String> = b
                    .get("components")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .filter(|x| self.c.components.iter().any(|c| c == x))
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();
                if comps.is_empty() {
                    return err(400, "pick at least one component to deploy");
                }
                rest.push("--deploy".into());
                rest.push(comps.join(","));
                if util::truthy(b.get("recreate")) {
                    rest.push("--recreate".into());
                }
            }
            Some("ride") => rest.push("--ride".into()),
            Some("hold") => rest.push("--no-deploy-until-released".into()),
            _ => return err(400, "pick a mode"),
        }
        if let Some(w) = b.get("waivers").and_then(Value::as_object) {
            for (k, reason) in w {
                let r = as_str(Some(reason)).trim().to_string();
                if !self.c.waivers.contains(k) || r.is_empty() {
                    return err(400, &format!("waiver {k} needs a reason"));
                }
                rest.push("--waive".into());
                rest.push(format!("{k}={r}"));
            }
        }
        ran(util::run(
            &self.sluicy_argv(&rest),
            Some(&self.c.repo),
            Duration::from_secs(60),
        ))
    }

    fn do_allow(&self, b: &Map<String, Value>) -> Reply {
        let target = as_str(b.get("target")).trim().to_string();
        let comp = b.get("component").and_then(Value::as_str).unwrap_or("");
        let reason = as_str(b.get("reason")).trim().to_string();
        if !self.c.components.iter().any(|c| c == comp) || reason.is_empty() || !valid_allow_target(&target) {
            return err(
                400,
                "allow needs a commit, ticket, 'unstamped' or 'hold', a component and a reason",
            );
        }
        let rest = util::argv(&["allow", &target, "--component", comp, "--reason", &reason]);
        ran(util::run(
            &self.sluicy_argv(&rest),
            Some(&self.c.repo),
            Duration::from_secs(30),
        ))
    }

    fn do_release(&self, b: &Map<String, Value>) -> Reply {
        let tid = as_str(b.get("ticket"));
        if !is_digits(&tid) {
            return err(400, "bad ticket");
        }
        ran(util::run(
            &self.sluicy_argv(&util::argv(&["release", &tid])),
            Some(&self.c.repo),
            Duration::from_secs(30),
        ))
    }

    // ---- Clean up panel: one row, one action, each checked against cleanup.json as it is now

    fn hide_cleanup_row(&self, id: &str) {
        let generated = cleanup::read(&self.c).0;
        let mut h = self.cleanup_hidden.lock().unwrap();
        if h.0 != generated {
            *h = (generated, HashSet::new());
        }
        h.1.insert(id.to_string());
    }

    /// Cancel red tickets the conductor listed: each still failed or conflict, never an auto ticket
    /// (cancelling one would lift autoship's two-failure stop).
    fn do_ticket_cancel(&self, b: &Map<String, Value>) -> Reply {
        let Some(item) = cleanup::item(&self.c, &as_str(b.get("id")), "tickets") else {
            return err(400, "not on the clean up list any more; refresh");
        };
        let listed: HashSet<i64> = item
            .get("tickets")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_i64).collect())
            .unwrap_or_default();
        let asked: Vec<i64> = b
            .get("tickets")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_i64).collect())
            .unwrap_or_default();
        if asked.is_empty() {
            return err(400, "no tickets given");
        }
        let conn = collect::sluicy_db(&self.c);
        let (mut results, mut ok) = (vec![], true);
        for tid in asked {
            let row: Option<(String, String)> = conn.as_ref().and_then(|c| {
                c.query_row(
                    "SELECT state, submitted_by FROM tickets WHERE id = ?1",
                    [tid],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .ok()
            });
            let eligible = listed.contains(&tid)
                && matches!(&row, Some((st, by)) if (st == "failed" || st == "conflict") && by != "auto");
            if !eligible {
                results.push(json!({"ticket": tid, "ok": false, "output": "not a listed failed/conflict hand ticket"}));
                ok = false;
                continue;
            }
            let rest = util::argv(&["cancel", &tid.to_string(), "--reason", "Deck clean up"]);
            let (rc, out, e) = util::run(&self.sluicy_argv(&rest), Some(&self.c.repo), Duration::from_secs(30));
            results.push(json!({"ticket": tid, "ok": rc == 0, "output": util::tail_chars(&(out + &e), 500)}));
            ok = ok && rc == 0;
        }
        if ok {
            self.hide_cleanup_row(&as_str(item.get("id")));
        }
        (if ok { 200 } else { 500 }, json!({"ok": ok, "results": results}))
    }

    fn do_stray_rm(&self, b: &Map<String, Value>) -> Reply {
        let name = as_str(b.get("name"));
        let id = format!("stray:{name}");
        let dir = cleanup::item(&self.c, &id, "stray").and_then(|_| cleanup::stray_dir(&self.c, &name));
        let Some(dir) = dir else {
            return err(400, "not a listed stray folder");
        };
        let age = std::fs::metadata(&dir)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.elapsed().ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        if age < 7.0 * 86400.0 {
            return err(409, "a stray is removed only after 7 days");
        }
        if let Err(e) = std::fs::remove_dir_all(&dir) {
            return (500, json!({"ok": false, "output": e.to_string()}));
        }
        self.hide_cleanup_row(&id);
        (200, json!({"ok": true, "output": format!("removed {}", dir.display())}))
    }

    fn do_debt_done(&self, b: &Map<String, Value>) -> Reply {
        let Some(debt) = b.get("debt").and_then(Value::as_i64) else {
            return err(400, "not a listed debt");
        };
        let id = format!("debt:{debt}");
        if cleanup::item(&self.c, &id, "debt").is_none() {
            return err(400, "not a listed debt");
        }
        let res = ran(util::run(
            &self.sluicy_argv(&util::argv(&["debts", "--done", &debt.to_string()])),
            Some(&self.c.repo),
            Duration::from_secs(30),
        ));
        if res.0 == 200 {
            self.hide_cleanup_row(&id);
        }
        res
    }

    fn do_ticket_log(&self, b: &Map<String, Value>) -> Reply {
        let tid = as_str(b.get("ticket"));
        if !is_digits(&tid) {
            return err(400, "bad ticket");
        }
        let path = self.c.sluicy_home.join("logs").join(format!("{tid}.log"));
        let text = if path.is_file() {
            String::from_utf8_lossy(&std::fs::read(&path).unwrap_or_default()).into_owned()
        } else {
            "(no log yet)".into()
        };
        (
            200,
            json!({"output": util::tail_chars(&util::strip_ansi(&text), 60000)}),
        )
    }

    fn do_session_start(&self, b: &Map<String, Value>) -> Reply {
        let name = slug(&as_str(b.get("name")));
        let prompt = as_str(b.get("prompt")).trim().to_string();
        if !valid_name(&name) {
            return err(400, "name: lowercase letters, digits and dashes, up to 40");
        }
        if prompt.is_empty() {
            return err(400, "give the session its first instruction");
        }
        let mut a = vec![
            self.c.claude.clone(),
            "--bg".into(),
            "--permission-mode".into(),
            "plan".into(),
        ];
        if b.get("brainstorm")
            .map(|v| util::truthy(Some(v)))
            .unwrap_or(true)
        {
            a.push("--append-system-prompt".into());
            a.push(preamble().trim().into());
        }
        a.push("-n".into());
        a.push(name.clone());
        if b.get("worktree")
            .map(|v| util::truthy(Some(v)))
            .unwrap_or(true)
        {
            a.push("-w".into());
            a.push(name);
        }
        a.push(prompt);
        let res = util::run(&a, Some(&self.c.repo), Duration::from_secs(60));
        self.forget_agents();
        ran(res)
    }

    fn session_cmd(&self, b: &Map<String, Value>, verb: &str) -> Reply {
        let s = match self.listed_session(b, true) {
            Ok(s) => s,
            Err(e) => return e,
        };
        let res = util::run(
            &[self.c.claude.clone(), verb.into(), as_str(s.get("id"))],
            Some(&self.c.repo),
            Duration::from_secs(30),
        );
        self.forget_agents();
        ran(res)
    }

    fn do_session_logs(&self, b: &Map<String, Value>) -> Reply {
        let s = match self.listed_session(b, true) {
            Ok(s) => s,
            Err(e) => return e,
        };
        // Not `claude logs`: that is the session's screen repaint, which reads as gibberish as text.
        let (cwd, reg, repo) = (
            as_str(s.get("cwd")),
            as_str(s.get("registeredCwd")),
            self.c.repo.to_string_lossy().into_owned(),
        );
        let log = collect::transcript_log(
            &self.c,
            &[&cwd, &reg, &repo],
            &as_str(s.get("sessionId")),
            60,
        );
        (
            200,
            json!({"found": log.is_some(), "messages": log.unwrap_or_default()}),
        )
    }

    fn open_term(&self, session: &str, argv: Vec<String>, readonly: bool) -> Reply {
        let mut terms = self.terms.lock().unwrap();
        for (k, t) in terms.iter() {
            if t.session == session && !t.closed() {
                t.touch(); // its page reconnects next
                return (200, json!({"term": k}));
            }
        }
        match Terminal::spawn(&argv, &self.c.repo, session, readonly) {
            Ok(t) => {
                let k = util::token_hex(6);
                terms.insert(k.clone(), t);
                (200, json!({"term": k}))
            }
            Err(e) => (
                500,
                json!({"error": format!("could not start terminal: {e}")}),
            ),
        }
    }

    fn do_term_open(&self, b: &Map<String, Value>) -> Reply {
        let s = match self.listed_session(b, true) {
            Ok(s) => s,
            Err(e) => return e,
        };
        let id = as_str(s.get("id"));
        self.open_term(
            &id.clone(),
            vec![self.c.claude.clone(), "attach".into(), id],
            false,
        )
    }

    /// The terminal the page shows when no session is open: `sluicy status`, redrawn every
    /// 3 s. Read-only, so a stray Ctrl-C in the page cannot end it.
    fn do_term_queue(&self) -> Reply {
        let status = self.sluicy_argv(&["status".into()]);
        let have_watch = std::env::var_os("PATH")
            .map(|p| std::env::split_paths(&p).any(|d| d.join("watch").is_file()))
            .unwrap_or(false);
        let argv = if have_watch {
            let mut a = util::argv(&["watch", "-t", "-n", "3"]);
            a.extend(status);
            a
        } else {
            let lp = "import subprocess,sys,time\nwhile True:\n print('\\x1b[H\\x1b[2J',end='',flush=True)\n subprocess.run(sys.argv[1:])\n time.sleep(3)";
            let mut a = vec![self.c.python.clone(), "-c".into(), lp.into()];
            a.extend(status);
            a
        };
        self.open_term(QUEUE_TERM, argv, true)
    }

    fn do_term_input(&self, b: &Map<String, Value>) -> Reply {
        let Some(t) = self.term(&as_str(b.get("term"))).filter(|t| !t.closed()) else {
            return err(400, "terminal closed");
        };
        if t.readonly {
            return err(400, "the queue status terminal is read-only");
        }
        match t.write(as_str(b.get("data")).as_bytes()) {
            Ok(()) => (200, json!({"ok": true})),
            Err(e) => (500, json!({"error": format!("OSError: {e}")})),
        }
    }

    fn do_term_resize(&self, b: &Map<String, Value>) -> Reply {
        if let Some(t) = self.term(&as_str(b.get("term"))) {
            let cols = as_int(b.get("cols")).unwrap_or(80).clamp(20, 500) as u16;
            let rows = as_int(b.get("rows")).unwrap_or(24).clamp(5, 200) as u16;
            t.resize(cols, rows);
        }
        (200, json!({"ok": true}))
    }

    fn do_term_close(&self, b: &Map<String, Value>) -> Reply {
        let t = self.terms.lock().unwrap().remove(&as_str(b.get("term")));
        if let Some(t) = t {
            t.close();
        }
        (200, json!({"ok": true}))
    }

    fn do_proc_info(&self, b: &Map<String, Value>) -> Reply {
        let pid = as_int(b.get("pid")).unwrap_or(0) as i32;
        let Some(p) = Procs::one(pid) else {
            return err(404, "no such process");
        };
        (
            200,
            json!({"pid": p.pid, "start": p.start, "comm": p.comm, "rss": p.rss, "cmd": procs::cmdline(p.pid, &p.comm), "uid": p.uid, "cwd": procs::cwd(p.pid)}),
        )
    }

    fn do_kill(&self, b: &Map<String, Value>) -> Reply {
        let (Some(pid), Some(start)) = (as_int(b.get("pid")), as_int(b.get("start"))) else {
            return err(400, "need pid and start");
        };
        let pid = pid as i32;
        let protected = [1, std::process::id() as i32, unsafe { libc::getppid() }];
        let Some(p) = Procs::one(pid).filter(|p| p.start as i64 == start) else {
            return err(409, "that process is gone or the pid was reused; refresh");
        };
        if p.uid != procs::my_uid() || protected.contains(&pid) {
            return err(403, "not a process the deck may stop");
        }
        unsafe { libc::kill(pid, libc::SIGTERM) };
        (
            200,
            json!({"ok": true, "output": format!("SIGTERM sent to {pid} ({})", p.comm)}),
        )
    }

    fn do_worktree_remove(&self, b: &Map<String, Value>) -> Reply {
        let t = self
            .listed
            .lock()
            .unwrap()
            .worktrees
            .get(&as_str(b.get("path")))
            .cloned();
        let Some(t) = t.filter(|t| t.get("main") != Some(&json!(true))) else {
            return err(400, "unknown worktree");
        };
        let dirty = util::truthy(t.get("dirty"));
        let merged = util::truthy(t.get("merged"));
        let has_sessions = util::truthy(t.get("sessions"));
        if dirty || !merged || has_sessions {
            return err(
                409,
                "only a clean, merged worktree with no session in it can be removed",
            );
        }
        // `git worktree remove` itself refuses a dirty tree, so a status up to 10 s old is safe.
        let path = as_str(t.get("path"));
        let res = util::run(
            &util::argv(&["git", "worktree", "remove", &path]),
            Some(Path::new(&self.c.repo)),
            Duration::from_secs(30),
        );
        self.forget_agents();
        ran(res)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_slugs() {
        assert_eq!(slug(" Fix  Login_now! "), "fix-login-now");
        assert_eq!(slug("new-thing"), "new-thing");
        assert_eq!(slug("!!!"), "");
        assert_eq!(
            slug(&"a".repeat(39).to_string().replace("", "")),
            "a".repeat(39)
        );
        assert_eq!(slug(&format!("{}-b", "a".repeat(39))), "a".repeat(39));
        assert!(valid_name("ok"));
        assert!(!valid_name("-x"));
        assert!(!valid_name(""));
    }

    #[test]
    fn allow_targets() {
        assert!(valid_allow_target("abc1234"));
        assert!(valid_allow_target("12"));
        assert!(valid_allow_target("hold"));
        assert!(!valid_allow_target("; rm -rf"));
        assert!(!valid_allow_target("abc12"));
        assert!(!valid_allow_target("ABC1234"));
    }
}
