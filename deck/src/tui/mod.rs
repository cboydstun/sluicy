//! `deck tui`: the Session Deck in a terminal, so no browser has to run for it. It calls the same
//! `Deck::state()` and `Deck::act()` the web page reaches over HTTP, in-process: every collector,
//! cache, validation rule and action is shared. Refreshes and actions run on worker threads so a
//! slow build or a 30 s git call never freezes the keys.

pub mod keys;
pub mod view;

use crate::deck::Deck;
use ratatui::backend::CrosstermBackend;
use ratatui::crossterm::event::{self, Event, KeyEventKind};
use ratatui::crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen, SetTitle};
use ratatui::crossterm::{cursor, execute};
use ratatui::Terminal;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::io::{stdout, Write};
use std::os::unix::process::CommandExt;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const REFRESH: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum Pane {
    Sessions,
    Queue,
    Worktrees,
    Cleanup,
    Processes,
    Gate,
}

pub const PANES: [Pane; 6] = [
    Pane::Sessions,
    Pane::Queue,
    Pane::Worktrees,
    Pane::Cleanup,
    Pane::Processes,
    Pane::Gate,
];
/// The tabs of the lower half (Sessions always sits above them).
pub const TABS: [Pane; 5] = [Pane::Queue, Pane::Worktrees, Pane::Cleanup, Pane::Processes, Pane::Gate];

/// How an action's answer is shown.
#[derive(Clone, Debug, PartialEq)]
pub enum Show {
    /// One line in the status bar (a failure opens a popup with the output).
    Status,
    /// The whole output in a scrollable popup.
    Popup(String),
    /// `session_logs` messages in a popup.
    Logs(String),
    /// `proc_info` answer: ask before SIGTERM, showing what the process is.
    KillConfirm,
}

#[derive(Clone, Debug, PartialEq)]
pub enum FieldKind {
    Text(String),
    Bool(bool),
    Choice(Vec<String>, usize),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Field {
    pub label: String,
    pub kind: FieldKind,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum FormKind {
    NewSession,
    Allow,
    Submit,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Form {
    pub kind: FormKind,
    pub title: String,
    pub fields: Vec<Field>,
    pub focus: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Modal {
    /// `y` runs the action. The body names its target (id, path, ticket, pid+start), never a row.
    Confirm {
        title: String,
        lines: Vec<String>,
        action: String,
        body: Map<String, Value>,
    },
    Popup {
        title: String,
        lines: Vec<String>,
        scroll: u16,
    },
    Form(Form),
    Help,
}

/// What a key asks for; computed by `keys`, carried out by `App::apply`.
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    None,
    Quit,
    Refresh,
    Pane(i32),
    Move(i32),
    Help,
    CloseModal,
    Attach(String),
    Note(String),
    Act {
        action: String,
        body: Map<String, Value>,
        show: Show,
    },
    Ask(Modal),
    Dismiss(String, f64),
    Zoom,
}

pub enum Msg {
    State(Arc<Value>),
    StateFailed(String),
    Done {
        action: String,
        code: u16,
        out: Value,
        show: Show,
    },
}

pub struct App {
    pub state: Option<Arc<Value>>,
    pub pane: Pane,
    /// The lower tab shown while Sessions has focus.
    pub tab: Pane,
    /// Selection per pane, by identity (session id, ticket id, path, pid), so a re-sort keeps it.
    pub sel: HashMap<Pane, String>,
    pub modal: Option<Modal>,
    pub status: Option<(String, bool, Instant)>,
    /// Session id -> statusSince when its "waiting for your reply" was dismissed.
    pub dismissed: HashMap<String, f64>,
    pub busy: usize,
    pub quit: bool,
    /// The focused pane fills the screen.
    pub zoom: bool,
    /// NO_COLOR: modifiers only.
    pub no_color: bool,
    notified: HashSet<String>,
    first_state: bool,
}

pub fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

pub fn arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v.get(k)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

pub fn num(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(Value::as_f64).unwrap_or(0.0)
}

impl Default for App {
    fn default() -> Self {
        App {
            state: None,
            pane: Pane::Sessions,
            tab: Pane::Queue,
            sel: HashMap::new(),
            modal: None,
            status: None,
            dismissed: HashMap::new(),
            busy: 0,
            quit: false,
            zoom: false,
            no_color: false,
            notified: HashSet::new(),
            first_state: true,
        }
    }
}

impl App {
    /// The need a session shows, unless it is a dismissed reply-need with the same status as when dismissed.
    pub fn need<'a>(&self, sess: &'a Value) -> Option<&'a Value> {
        let n = sess.get("needs").filter(|n| n.is_object())?;
        if s(n, "kind") == "reply" {
            if let Some(at) = self.dismissed.get(s(sess, "id")) {
                if *at == num(sess, "statusSince") {
                    return None;
                }
            }
        }
        Some(n)
    }

    /// Sessions in display order: those needing the person first.
    pub fn sessions(&self) -> Vec<&Value> {
        let Some(st) = &self.state else { return vec![] };
        let all = arr(st, "sessions");
        let mut out: Vec<&Value> = all.iter().filter(|x| self.need(x).is_some()).collect();
        out.extend(all.iter().filter(|x| self.need(x).is_none()));
        out
    }

    pub fn needs(&self) -> Vec<&Value> {
        self.sessions()
            .into_iter()
            .filter(|x| self.need(x).is_some())
            .collect()
    }

    /// The identity keys of a pane's rows, in display order.
    pub fn row_keys(&self, pane: Pane) -> Vec<String> {
        let Some(st) = &self.state else { return vec![] };
        let id = |v: &Value, k: &str| {
            v.get(k)
                .map(|x| crate::util::as_str(Some(x)))
                .unwrap_or_default()
        };
        match pane {
            Pane::Sessions => self.sessions().iter().map(|x| id(x, "id")).collect(),
            Pane::Queue => arr(&st["queue"], "tickets")
                .iter()
                .map(|t| id(t, "id"))
                .collect(),
            Pane::Worktrees => arr(st, "worktrees").iter().map(|t| id(t, "path")).collect(),
            Pane::Cleanup => arr(&st["cleanup"], "items").iter().map(|t| id(t, "id")).collect(),
            Pane::Processes => arr(&st["system"], "top")
                .iter()
                .map(|p| id(p, "pid"))
                .collect(),
            Pane::Gate => vec![],
        }
    }

    pub fn selected_index(&self, pane: Pane) -> Option<usize> {
        let keys = self.row_keys(pane);
        if keys.is_empty() {
            return None;
        }
        Some(
            self.sel
                .get(&pane)
                .and_then(|k| keys.iter().position(|x| x == k))
                .unwrap_or(0),
        )
    }

    /// The selected row of the focused pane.
    pub fn selected(&self, pane: Pane) -> Option<Value> {
        let i = self.selected_index(pane)?;
        let st = self.state.as_ref()?;
        let rows: Vec<&Value> = match pane {
            Pane::Sessions => self.sessions(),
            Pane::Queue => arr(&st["queue"], "tickets").iter().collect(),
            Pane::Worktrees => arr(st, "worktrees").iter().collect(),
            Pane::Cleanup => arr(&st["cleanup"], "items").iter().collect(),
            Pane::Processes => arr(&st["system"], "top").iter().collect(),
            Pane::Gate => vec![],
        };
        rows.get(i).map(|v| (*v).clone())
    }

    fn move_sel(&mut self, d: i32) {
        let keys = self.row_keys(self.pane);
        if keys.is_empty() {
            return;
        }
        let i = self.selected_index(self.pane).unwrap_or(0) as i32;
        let j = (i + d).clamp(0, keys.len() as i32 - 1) as usize;
        self.sel.insert(self.pane, keys[j].clone());
    }

    pub fn note(&mut self, text: impl Into<String>, err: bool) {
        self.status = Some((text.into(), err, Instant::now()));
    }

    /// A new state: keep it, and say which needs are new (for desktop notifications).
    pub fn take_state(&mut self, st: Arc<Value>) -> Vec<String> {
        self.state = Some(st);
        let mut fresh = vec![];
        let mut now = HashSet::new();
        for x in self.needs() {
            let n = self.need(x).unwrap();
            let key = format!("{}|{}|{}", s(x, "id"), s(n, "kind"), num(x, "statusSince"));
            if !self.notified.contains(&key) && !self.first_state {
                fresh.push(format!("{}: {}", s(x, "name"), s(n, "label")));
            }
            now.insert(key);
        }
        self.notified = now;
        self.first_state = false;
        fresh
    }

    /// The result of an action.
    pub fn done(&mut self, action: &str, code: u16, out: &Value, show: Show) {
        let ok = code == 200;
        let text = out.get("output").and_then(Value::as_str).unwrap_or("");
        let err = out.get("error").and_then(Value::as_str).unwrap_or("");
        let lines = |t: &str| t.lines().map(String::from).collect::<Vec<_>>();
        match show {
            Show::Logs(title) if ok => {
                let mut l = vec![];
                if out.get("found") == Some(&json!(false)) {
                    l.push("(no transcript found)".to_string());
                }
                for m in arr(out, "messages") {
                    let at = s(m, "at");
                    let at = at.get(11..19).unwrap_or("");
                    l.push(format!(
                        "{at} {:<9} {}",
                        s(m, "role"),
                        s(m, "text").lines().next().unwrap_or("")
                    ));
                    for more in s(m, "text").lines().skip(1) {
                        l.push(format!("{:18} {more}", ""));
                    }
                }
                let scroll = l.len().saturating_sub(10) as u16;
                self.modal = Some(Modal::Popup {
                    title,
                    lines: l,
                    scroll,
                });
            }
            Show::Popup(title) if ok => {
                let l = lines(text);
                let scroll = l.len().saturating_sub(10) as u16;
                self.modal = Some(Modal::Popup {
                    title,
                    lines: l,
                    scroll,
                });
            }
            Show::KillConfirm if ok => {
                let mib = num(out, "rss") / 1048576.0;
                let mut body = Map::new();
                body.insert("pid".into(), out["pid"].clone());
                body.insert("start".into(), out["start"].clone());
                self.modal = Some(Modal::Confirm {
                    title: format!("SIGTERM {} ({})?", out["pid"], s(out, "comm")),
                    lines: vec![
                        format!("command: {}", s(out, "cmd")),
                        format!("folder:  {}", s(out, "cwd")),
                        format!("memory:  {mib:.0} MB"),
                    ],
                    action: "kill".into(),
                    body,
                });
            }
            _ if ok => {
                let last = text.trim().lines().last().unwrap_or("done").to_string();
                self.note(format!("{action}: {last}"), false);
            }
            _ => {
                let msg = if err.is_empty() {
                    text.to_string()
                } else {
                    err.to_string()
                };
                let l = lines(&msg);
                if l.len() > 1 {
                    self.modal = Some(Modal::Popup {
                        title: format!("{action} failed ({code})"),
                        lines: l,
                        scroll: 0,
                    });
                }
                self.note(
                    format!("{action} failed: {}", msg.lines().last().unwrap_or("")),
                    true,
                );
            }
        }
    }
}

// ---- the terminal

fn restore_terminal() {
    let _ = terminal::disable_raw_mode();
    let _ = execute!(stdout(), LeaveAlternateScreen, cursor::Show);
}

fn enter_terminal() -> std::io::Result<()> {
    terminal::enable_raw_mode()?;
    execute!(stdout(), EnterAlternateScreen, cursor::Hide)
}

/// Run `claude attach <id>` in this terminal and come back when it exits. Ctrl+Z (or Ctrl+B d)
/// detaches and exits 0. Attach's own ← detaches too, but then runs `claude agents` and only
/// exits when that list is quit, so the hint points at Ctrl+Z.
fn attach(deck: &Deck, id: &str) -> String {
    restore_terminal();
    println!(
        "Attaching to {id}. Ctrl+Z returns to the deck (← opens Claude's agent list; quit it to come back).\r"
    );
    unsafe {
        // The attached session takes Ctrl-C / Ctrl-Z; the deck under it must not.
        libc::signal(libc::SIGINT, libc::SIG_IGN);
        libc::signal(libc::SIGTSTP, libc::SIG_IGN);
    }
    let res = unsafe {
        std::process::Command::new(&deck.c.claude)
            .args(["attach", id])
            .current_dir(&deck.c.repo)
            .pre_exec(|| {
                libc::signal(libc::SIGINT, libc::SIG_DFL);
                libc::signal(libc::SIGTSTP, libc::SIG_DFL);
                Ok(())
            })
            .status()
    };
    unsafe {
        libc::signal(libc::SIGINT, libc::SIG_DFL);
        libc::signal(libc::SIGTSTP, libc::SIG_DFL);
    }
    let _ = enter_terminal();
    attach_outcome(id, res)
}

/// The status line after `claude attach` returns. On success it repeats the way back, because the
/// banner printed before attach is gone the moment attach draws.
fn attach_outcome(id: &str, res: std::io::Result<std::process::ExitStatus>) -> String {
    match res {
        Ok(st) if st.success() => {
            format!("back from {id} · Ctrl+Z returns straight here; ← opens Claude's agent list")
        }
        Ok(st) => format!("claude attach {id} exited with {st}"),
        Err(e) => format!("could not run claude attach: {e}"),
    }
}

fn notify(lines: &[String]) {
    if lines.is_empty() || std::env::var("DECK_NOTIFY").as_deref() == Ok("off") {
        return;
    }
    let _ = std::process::Command::new("notify-send")
        .args(["--app-name=Session Deck", "Needs you", &lines.join("\n")])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
}

fn spawn_refresher(deck: Arc<Deck>, tx: Sender<Msg>) -> Sender<()> {
    let (wake, woken) = mpsc::channel::<()>();
    std::thread::spawn(move || loop {
        let msg = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| deck.state())) {
            Ok(st) => Msg::State(st),
            Err(_) => Msg::StateFailed("refresh failed (see stderr)".into()),
        };
        if tx.send(msg).is_err() {
            return;
        }
        match woken.recv_timeout(REFRESH) {
            Ok(()) | Err(mpsc::RecvTimeoutError::Timeout) => while woken.try_recv().is_ok() {},
            Err(mpsc::RecvTimeoutError::Disconnected) => return,
        }
    });
    wake
}

type Job = (String, Map<String, Value>, Show);

fn spawn_worker(deck: Arc<Deck>, tx: Sender<Msg>, wake: Sender<()>) -> Sender<Job> {
    let (jobs, rx): (Sender<Job>, Receiver<Job>) = mpsc::channel();
    std::thread::spawn(move || {
        for (action, body, show) in rx {
            let (code, out) =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| deck.act(&action, &body)))
                    .unwrap_or_else(|_| (500, json!({"error": "the action panicked"})));
            let changed = !matches!(action.as_str(), "session_logs" | "ticket_log" | "proc_info");
            if tx
                .send(Msg::Done {
                    action,
                    code,
                    out,
                    show,
                })
                .is_err()
            {
                return;
            }
            if changed {
                let _ = wake.send(());
            }
        }
    });
    jobs
}

pub fn run(deck: Arc<Deck>) -> std::io::Result<()> {
    // SIGTERM / SIGHUP: give the terminal back, then leave. Blocked here, before any thread starts,
    // so only the sigwait thread takes them. (std::process resets the mask in each child.)
    let mut set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGTERM);
        libc::sigaddset(&mut set, libc::SIGHUP);
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
    }
    std::thread::spawn(move || {
        let mut sig = 0;
        unsafe { libc::sigwait(&set, &mut sig) };
        restore_terminal();
        std::process::exit(0);
    });
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        // A panic on a worker thread is caught and reported; only the UI thread's ends the deck.
        if std::thread::current().name() == Some("main") {
            restore_terminal();
        }
        default_hook(info);
    }));

    enter_terminal()?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout()))?;
    let (tx, rx) = mpsc::channel();
    let wake = spawn_refresher(deck.clone(), tx.clone());
    let jobs = spawn_worker(deck.clone(), tx, wake.clone());
    let mut app = App {
        no_color: std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()),
        ..App::default()
    };
    let mut title = String::new();
    let mut dirty = true;
    while !app.quit {
        for msg in rx.try_iter() {
            dirty = true;
            match msg {
                Msg::State(st) => notify(&app.take_state(st)),
                Msg::StateFailed(e) => app.note(e, true),
                Msg::Done {
                    action,
                    code,
                    out,
                    show,
                } => {
                    app.busy = app.busy.saturating_sub(1);
                    app.done(&action, code, &out, show);
                }
            }
        }
        if dirty {
            let n = app.needs().len();
            let want = if n > 0 {
                format!("({n}) Session Deck")
            } else {
                "Session Deck".into()
            };
            if want != title {
                let _ = execute!(stdout(), SetTitle(&want));
                title = want;
            }
            term.draw(|f| view::draw(f, &app))?;
            dirty = false;
        }
        if !event::poll(Duration::from_millis(150))? {
            if let Some((_, _, at)) = &app.status {
                if at.elapsed() > Duration::from_secs(8) {
                    app.status = None;
                    dirty = true;
                }
            }
            continue;
        }
        let ev = event::read()?;
        dirty = true;
        let Event::Key(key) = ev else { continue };
        if key.kind != KeyEventKind::Press {
            continue;
        }
        let intent = keys::intent(&mut app, key);
        match intent {
            Intent::None => {}
            Intent::Quit => app.quit = true,
            Intent::Refresh => {
                let _ = wake.send(());
                app.note("refreshing…", false);
            }
            Intent::Pane(d) => {
                let i = PANES.iter().position(|p| *p == app.pane).unwrap_or(0) as i32;
                app.pane = PANES[(i + d).rem_euclid(PANES.len() as i32) as usize];
                if app.pane != Pane::Sessions {
                    app.tab = app.pane;
                }
            }
            Intent::Move(d) => app.move_sel(d),
            Intent::Help => app.modal = Some(Modal::Help),
            Intent::Zoom => app.zoom = !app.zoom,
            Intent::CloseModal => app.modal = None,
            Intent::Note(t) => app.note(t, false),
            Intent::Ask(m) => app.modal = Some(m),
            Intent::Dismiss(id, since) => {
                app.dismissed.insert(id, since);
                app.note("dismissed until its status changes", false);
            }
            Intent::Act { action, body, show } => {
                app.modal = None;
                app.busy += 1;
                app.note(format!("{action}…"), false);
                let _ = jobs.send((action, body, show));
            }
            Intent::Attach(id) => {
                let msg = attach(&deck, &id);
                term.clear()?;
                app.note(msg, false);
                let _ = wake.send(());
            }
        }
    }
    restore_terminal();
    let _ = stdout().flush();
    Ok(())
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// A state with a permission need (bg1, background), a reply need (r1), an interactive session,
    /// two worktrees, one ticket and one process.
    pub fn fixture_app() -> App {
        let st = json!({
            "now": 1000.0,
            "agentsError": "",
            "sessions": [
                {"id": "s-int", "name": "my-feature", "status": "busy", "attachable": false, "needs": null,
                 "worktree": {"branch": "fix/dirty", "dirty": 1, "ahead": "0", "behind": "0"}, "cwd": "/r/wt", "statusSince": 1.0, "last": "Fixed it."},
                {"id": "bg1", "name": "bg-task", "status": "waiting", "attachable": true,
                 "needs": {"kind": "permission", "label": "permission prompt", "tool": {"name": "Bash", "command": "rm -rf build"}, "message": ""},
                 "worktree": {"branch": "main"}, "cwd": "/r", "statusSince": 5.0},
                {"id": "r1", "name": "asker", "status": "idle", "attachable": true,
                 "needs": {"kind": "reply", "label": "waiting for your reply", "tool": null, "message": "Deploy the API too?"},
                 "worktree": {"branch": "fix/r"}, "cwd": "/r", "statusSince": 9.0}
            ],
            "queue": {"tickets": [{"id": 7, "branch": "fix/dirty", "state": "blocked", "mode": "deploy", "components": "web", "detail": "waiting"}],
                      "holds": [], "alerts": ["ticket 7 blocked"], "guards": true, "vps": {"stamps": {"api": "abc123"}, "lease": ["free"]}},
            "system": {"cpu": 12.0, "load": ["1.0"], "mem": {"availPct": 9.0}, "swap": {"usedPct": 3.0, "hot": false},
                       "locks": {"push gate": {"held": false}}, "warnings": ["Only 9% memory available"],
                       "top": [{"pid": 42, "start": 7, "comm": "node", "rss": 104857600, "cpu": 1.5, "cmd": "node server.js"}],
                       "gate": {"file": "", "tail": []}},
            "worktrees": [{"path": "/r", "branch": "main", "main": true, "dirty": 0, "merged": false, "sessions": []},
                          {"path": "/r/wt", "branch": "fix/dirty", "main": false, "dirty": 1, "merged": false, "sessions": ["my-feature"]}],
            "branches": ["fix/dirty", "fix/r"],
            "cleanup": {"generated": 1.0, "items": [
                {"kind": "session", "id": "session:old", "name": "old", "path": "/r/.claude/worktrees/old", "label": "Session old: shipped", "sessionId": "done1"},
                {"kind": "session", "id": "session:busy", "name": "busy", "path": "/r/.claude/worktrees/busy", "label": "Session busy", "stale": "busy or waiting on you"},
                {"kind": "worktree", "id": "worktree:/r/old", "path": "/r/old", "label": "Worktree old"},
                {"kind": "tickets", "id": "tickets:4,6", "tickets": [4, 6], "label": "2 red tickets"},
                {"kind": "stray", "id": "stray:57-1", "path": "/s/stray/57-1", "label": "Stray 57-1"},
                {"kind": "debt", "id": "debt:5", "debt": 5, "label": "Debt 5"}
            ]}
        });
        let mut app = App::default();
        app.take_state(Arc::new(st));
        app
    }

    #[test]
    fn attach_outcome_repeats_the_way_back() {
        use std::os::unix::process::ExitStatusExt;
        let ok = attach_outcome("bg1", Ok(std::process::ExitStatus::from_raw(0)));
        assert!(ok.starts_with("back from bg1") && ok.contains("Ctrl+Z"), "{ok}");
        let bad = attach_outcome("bg1", Ok(std::process::ExitStatus::from_raw(1 << 8)));
        assert_eq!(bad, "claude attach bg1 exited with exit status: 1");
        let err = attach_outcome("bg1", Err(std::io::Error::other("nope")));
        assert_eq!(err, "could not run claude attach: nope");
    }

    #[test]
    fn needs_sort_first_and_selection_follows_identity() {
        let mut app = fixture_app();
        let order: Vec<String> = app
            .sessions()
            .iter()
            .map(|x| s(x, "id").to_string())
            .collect();
        assert_eq!(order, ["bg1", "r1", "s-int"]);
        app.move_sel(2);
        assert_eq!(app.selected(Pane::Sessions).unwrap()["id"], "s-int");
        // A change of order keeps the same session selected.
        app.dismissed.insert("r1".into(), 9.0);
        assert_eq!(app.selected(Pane::Sessions).unwrap()["id"], "s-int");
        assert_eq!(app.selected_index(Pane::Sessions), Some(1)); // r1 left the needs group above it
    }

    #[test]
    fn only_new_needs_notify() {
        let mut app = fixture_app();
        let st = app.state.clone().unwrap();
        assert!(app.take_state(st.clone()).is_empty()); // the same needs again
        let mut v = (*st).clone();
        v["sessions"][0]["needs"] =
            json!({"kind": "question", "label": "question", "tool": null, "message": ""});
        v["sessions"][0]["statusSince"] = json!(11.0);
        assert_eq!(
            app.take_state(Arc::new(v)),
            vec!["my-feature: question".to_string()]
        );
    }

    #[test]
    fn failures_and_logs_are_shown() {
        let mut app = fixture_app();
        app.done(
            "release",
            400,
            &json!({"error": "bad ticket"}),
            Show::Status,
        );
        assert!(app.status.as_ref().unwrap().1);
        app.done(
            "session_logs",
            200,
            &json!({"found": true, "messages": [{"role": "user", "text": "ship it", "at": "2026-11-04T13:00:00Z"}]}),
            Show::Logs("t".into()),
        );
        let Some(Modal::Popup { lines, .. }) = &app.modal else {
            panic!()
        };
        assert_eq!(lines, &vec!["13:00:00 user      ship it".to_string()]);
    }
}
