//! Keys -> intents. Everything that names a target takes it from the selected row's identity
//! (session id, ticket id, worktree path, pid + start), never from a row index.

use super::{arr, s, App, Field, FieldKind, Form, FormKind, Intent, Modal, Pane, Show};
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Map, Value};

fn body(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}

fn act(action: &str, b: Map<String, Value>, show: Show) -> Intent {
    Intent::Act {
        action: action.into(),
        body: b,
        show,
    }
}

fn confirm(title: String, lines: Vec<String>, action: &str, b: Map<String, Value>) -> Intent {
    Intent::Ask(Modal::Confirm {
        title,
        lines,
        action: action.into(),
        body: b,
    })
}

fn text(label: &str, v: &str) -> Field {
    Field {
        label: label.into(),
        kind: FieldKind::Text(v.into()),
    }
}

fn choice(label: &str, opts: &[&str], at: usize) -> Field {
    Field {
        label: label.into(),
        kind: FieldKind::Choice(opts.iter().map(|x| x.to_string()).collect(), at),
    }
}

fn flag(label: &str, v: bool) -> Field {
    Field {
        label: label.into(),
        kind: FieldKind::Bool(v),
    }
}

pub fn new_session_form() -> Form {
    Form {
        kind: FormKind::NewSession,
        title: "New background session".into(),
        fields: vec![
            text("Name", ""),
            text("First instruction", ""),
            flag("Own worktree", true),
            flag("Ask questions first", true),
        ],
        focus: 0,
    }
}

/// The repo's components and waivers, as the state's queue reports them (from its `.sluicy.toml`).
pub fn repo_lists(app: &App) -> (Vec<String>, Vec<String>) {
    let q = app.state.as_ref().map(|st| st["queue"].clone()).unwrap_or(Value::Null);
    let list = |k: &str| -> Vec<String> {
        q[k].as_array()
            .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
            .unwrap_or_default()
    };
    (list("components"), list("waivers"))
}

const DEPLOY_PREFIX: &str = "Deploy ";

pub fn allow_form(target: &str, comps: &[String]) -> Form {
    let opts: Vec<&str> = comps.iter().map(String::as_str).collect();
    Form {
        kind: FormKind::Allow,
        title: "Allow a deploy".into(),
        fields: vec![
            text("Commit, ticket, unstamped or hold", target),
            choice("Component", if opts.is_empty() { &["(none)"] } else { &opts }, 0),
            text("Reason", ""),
        ],
        focus: 2,
    }
}

pub fn submit_form(branch: &str, comps: &[String], waivers: &[String]) -> Form {
    let mut w = vec!["none"];
    w.extend(waivers.iter().map(String::as_str));
    let mut fields = vec![text("Branch", branch), choice("Mode", &["ride", "deploy", "hold"], 0)];
    fields.extend(comps.iter().map(|c| flag(&format!("{DEPLOY_PREFIX}{c}"), false)));
    fields.push(flag("--recreate", false));
    fields.push(choice("Waiver", &w, 0));
    fields.push(text("Waiver reason", ""));
    Form {
        kind: FormKind::Submit,
        title: "Submit a branch to the queue".into(),
        fields,
        focus: 0,
    }
}

fn field_by(f: &Form, label: &str) -> usize {
    f.fields.iter().position(|x| x.label == label).unwrap_or(0)
}

fn field_text(f: &Form, i: usize) -> String {
    match &f.fields[i].kind {
        FieldKind::Text(t) => t.trim().to_string(),
        FieldKind::Choice(o, at) => o[*at].clone(),
        FieldKind::Bool(b) => b.to_string(),
    }
}

fn field_bool(f: &Form, i: usize) -> bool {
    matches!(f.fields[i].kind, FieldKind::Bool(true))
}

/// The action and body a filled form sends.
pub fn form_action(f: &Form) -> (&'static str, Map<String, Value>) {
    match f.kind {
        FormKind::NewSession => (
            "session_start",
            body(&[
                ("name", json!(field_text(f, 0))),
                ("prompt", json!(field_text(f, 1))),
                ("worktree", json!(field_bool(f, 2))),
                ("brainstorm", json!(field_bool(f, 3))),
            ]),
        ),
        FormKind::Allow => (
            "allow",
            body(&[
                ("target", json!(field_text(f, 0))),
                ("component", json!(field_text(f, 1))),
                ("reason", json!(field_text(f, 2))),
            ]),
        ),
        FormKind::Submit => {
            let comps: Vec<&str> = f
                .fields
                .iter()
                .enumerate()
                .filter(|(i, x)| x.label.starts_with(DEPLOY_PREFIX) && field_bool(f, *i))
                .map(|(_, x)| &x.label[DEPLOY_PREFIX.len()..])
                .collect();
            let mut waivers = Map::new();
            let w = field_text(f, field_by(f, "Waiver"));
            if w != "none" {
                waivers.insert(w, json!(field_text(f, field_by(f, "Waiver reason"))));
            }
            (
                "submit",
                body(&[
                    ("branch", json!(field_text(f, 0))),
                    ("mode", json!(field_text(f, 1))),
                    ("components", json!(comps)),
                    ("recreate", json!(field_bool(f, field_by(f, "--recreate")))),
                    ("waivers", Value::Object(waivers)),
                ]),
            )
        }
    }
}

fn form_key(f: &mut Form, key: KeyEvent) -> Intent {
    let n = f.fields.len();
    match key.code {
        KeyCode::Esc => return Intent::CloseModal,
        KeyCode::Tab | KeyCode::Down => f.focus = (f.focus + 1) % n,
        KeyCode::BackTab | KeyCode::Up => f.focus = (f.focus + n - 1) % n,
        KeyCode::Enter => {
            let (action, b) = form_action(f);
            return act(action, b, Show::Status);
        }
        code => match &mut f.fields[f.focus].kind {
            FieldKind::Text(t) => match code {
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => t.push(c),
                KeyCode::Backspace => {
                    t.pop();
                }
                _ => {}
            },
            FieldKind::Bool(b) => {
                if matches!(code, KeyCode::Char(' ') | KeyCode::Left | KeyCode::Right) {
                    *b = !*b;
                }
            }
            FieldKind::Choice(o, at) => match code {
                KeyCode::Right | KeyCode::Char(' ') => *at = (*at + 1) % o.len(),
                KeyCode::Left => *at = (*at + o.len() - 1) % o.len(),
                _ => {}
            },
        },
    }
    Intent::None
}

fn modal_key(m: &mut Modal, key: KeyEvent) -> Intent {
    match m {
        Modal::Help => Intent::CloseModal,
        Modal::Confirm { action, body, .. } => match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => act(action, body.clone(), Show::Status),
            KeyCode::Char('n') | KeyCode::Esc | KeyCode::Char('q') => Intent::CloseModal,
            _ => Intent::None,
        },
        Modal::Popup { scroll, lines, .. } => {
            let max = lines.len().saturating_sub(1) as u16;
            match key.code {
                KeyCode::Esc | KeyCode::Char('q') | KeyCode::Enter => return Intent::CloseModal,
                KeyCode::Down | KeyCode::Char('j') => *scroll = (*scroll + 1).min(max),
                KeyCode::Up | KeyCode::Char('k') => *scroll = scroll.saturating_sub(1),
                KeyCode::PageDown | KeyCode::Char(' ') => *scroll = (*scroll + 20).min(max),
                KeyCode::PageUp => *scroll = scroll.saturating_sub(20),
                KeyCode::Home | KeyCode::Char('g') => *scroll = 0,
                KeyCode::End | KeyCode::Char('G') => *scroll = max,
                _ => {}
            }
            Intent::None
        }
        Modal::Form(f) => form_key(f, key),
    }
}

/// What `key` asks for. Edits inside a form or popup happen here and return `Intent::None`.
pub fn intent(app: &mut App, key: KeyEvent) -> Intent {
    if let Some(m) = app.modal.as_mut() {
        return modal_key(m, key);
    }
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Char('c') if ctrl => return Intent::Quit,
        KeyCode::Char('q') => return Intent::Quit,
        KeyCode::Tab => return Intent::Pane(1),
        KeyCode::BackTab => return Intent::Pane(-1),
        KeyCode::Down | KeyCode::Char('j') => return Intent::Move(1),
        KeyCode::Up | KeyCode::Char('k') => return Intent::Move(-1),
        KeyCode::PageDown => return Intent::Move(10),
        KeyCode::PageUp => return Intent::Move(-10),
        KeyCode::Char('r') => return Intent::Refresh,
        KeyCode::Char('?') => return Intent::Help,
        KeyCode::Char('z') => return Intent::Zoom,
        KeyCode::Char('n') => return Intent::Ask(Modal::Form(new_session_form())),
        _ => {}
    }
    let pane = app.pane;
    let Some(row) = app.selected(pane) else {
        return Intent::None;
    };
    match pane {
        Pane::Sessions => session_key(app, &row, key.code),
        Pane::Queue => {
            let id = row["id"].clone();
            match key.code {
                KeyCode::Enter => act(
                    "ticket_log",
                    body(&[("ticket", id.clone())]),
                    Show::Popup(format!("Ticket {id} log")),
                ),
                KeyCode::Char('R') => confirm(
                    format!("Release ticket {id}?"),
                    vec![format!(
                        "{} · {} · {}",
                        s(&row, "branch"),
                        s(&row, "state"),
                        s(&row, "mode")
                    )],
                    "release",
                    body(&[("ticket", id)]),
                ),
                KeyCode::Char('a') => {
                    let (comps, _) = repo_lists(app);
                    Intent::Ask(Modal::Form(allow_form(&crate::util::as_str(Some(&id)), &comps)))
                }
                KeyCode::Char('t') => {
                    let (comps, waivers) = repo_lists(app);
                    Intent::Ask(Modal::Form(submit_form(s(&row, "branch"), &comps, &waivers)))
                }
                _ => Intent::None,
            }
        }
        Pane::Worktrees => match key.code {
            KeyCode::Char('d') if row.get("main") != Some(&json!(true)) => confirm(
                "Remove this worktree?".into(),
                vec![
                    s(&row, "path").to_string(),
                    format!("branch {}", s(&row, "branch")),
                ],
                "worktree_remove",
                body(&[("path", row["path"].clone())]),
            ),
            KeyCode::Char('d') => Intent::Note("the main checkout cannot be removed".into()),
            _ => Intent::None,
        },
        Pane::Cleanup => match key.code {
            KeyCode::Char('x') | KeyCode::Enter => cleanup_key(&row),
            _ => Intent::None,
        },
        Pane::Processes => match key.code {
            KeyCode::Char('K') => act(
                "proc_info",
                body(&[("pid", row["pid"].clone())]),
                Show::KillConfirm,
            ),
            _ => Intent::None,
        },
        Pane::Gate => Intent::None,
    }
}

/// One Clean up row: a confirm that names exactly what goes, with the action's own identity in the body
/// (the server checks it against cleanup.json again).
fn cleanup_key(row: &Value) -> Intent {
    if let Some(why) = row.get("stale").and_then(Value::as_str) {
        return Intent::Note(format!("not now: {why}"));
    }
    let label = s(row, "label").to_string();
    let id = s(row, "id").to_string();
    match s(row, "kind") {
        "session" => confirm(
            format!("Remove session {}?", s(row, "name")),
            vec![label, format!("claude rm: the session and its worktree {}", s(row, "path"))],
            "session_rm",
            body(&[("id", row["sessionId"].clone())]),
        ),
        "worktree" => confirm(
            "Remove this worktree?".into(),
            vec![label, s(row, "path").to_string()],
            "worktree_remove",
            body(&[("path", row["path"].clone())]),
        ),
        "tickets" => confirm(
            "Cancel these tickets?".into(),
            vec![label, "Marks them cancelled; no code or branch is touched.".into()],
            "ticket_cancel",
            body(&[("id", json!(id)), ("tickets", row["tickets"].clone())]),
        ),
        "stray" => confirm(
            "Delete this stray folder?".into(),
            vec![label, s(row, "path").to_string()],
            "stray_rm",
            body(&[("name", json!(id.trim_start_matches("stray:")))]),
        ),
        "debt" => confirm(
            "Mark this debt done?".into(),
            vec![label],
            "debt_done",
            body(&[("debt", row["debt"].clone())]),
        ),
        other => Intent::Note(format!("unknown clean up item {other}")),
    }
}

fn session_key(app: &App, row: &Value, code: KeyCode) -> Intent {
    let id = row["id"].clone();
    let name = s(row, "name").to_string();
    let attachable = row.get("attachable") == Some(&json!(true));
    let background = |i: Intent| {
        if attachable {
            i
        } else {
            Intent::Note(format!(
                "{name} runs in a terminal of its own: answer it there"
            ))
        }
    };
    match code {
        KeyCode::Enter | KeyCode::Char('o') => {
            background(Intent::Attach(crate::util::as_str(Some(&id))))
        }
        KeyCode::Char('l') => background(act(
            "session_logs",
            body(&[("id", id)]),
            Show::Logs(format!("{name} transcript")),
        )),
        KeyCode::Char('s') => background(confirm(
            format!("Stop {name}?"),
            vec![s(row, "cwd").to_string()],
            "session_stop",
            body(&[("id", id)]),
        )),
        KeyCode::Char('x') => background(confirm(
            format!("Remove {name}?"),
            vec![
                "Removes the session and its worktree (`claude rm`; it refuses unpushed commits).".into(),
                s(row, "cwd").to_string(),
            ],
            "session_rm",
            body(&[("id", id)]),
        )),
        KeyCode::Char('D') => match app.need(row) {
            Some(n) if s(n, "kind") == "reply" => Intent::Dismiss(
                crate::util::as_str(Some(&id)),
                super::num(row, "statusSince"),
            ),
            _ => Intent::Note("only a \"waiting for your reply\" can be dismissed".into()),
        },
        KeyCode::Char('t') => {
            let (comps, waivers) = repo_lists(app);
            Intent::Ask(Modal::Form(submit_form(s(&row["worktree"], "branch"), &comps, &waivers)))
        }
        _ => Intent::None,
    }
}

/// The branch list, for the submit form's hint.
pub fn branches(app: &App) -> Vec<String> {
    app.state
        .as_ref()
        .map(|st| {
            arr(st, "branches")
                .iter()
                .map(|b| crate::util::as_str(Some(b)))
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tui::tests::fixture_app;
    use ratatui::crossterm::event::KeyEventState;

    fn k(code: KeyCode) -> KeyEvent {
        KeyEvent {
            code,
            modifiers: KeyModifiers::NONE,
            kind: ratatui::crossterm::event::KeyEventKind::Press,
            state: KeyEventState::NONE,
        }
    }

    #[test]
    fn open_attaches_background_sessions_only() {
        let mut app = fixture_app();
        // The needing session sorts first and is a background one.
        assert_eq!(
            intent(&mut app, k(KeyCode::Enter)),
            Intent::Attach("bg1".into())
        );
        app.sel.insert(Pane::Sessions, "s-int".into());
        assert!(matches!(
            intent(&mut app, k(KeyCode::Enter)),
            Intent::Note(_)
        ));
    }

    #[test]
    fn cleanup_rows_confirm_with_their_own_identity() {
        let mut app = fixture_app();
        app.pane = Pane::Cleanup;
        let cases = [
            ("session:old", "session_rm", json!({"id": "done1"})),
            ("worktree:/r/old", "worktree_remove", json!({"path": "/r/old"})),
            ("tickets:4,6", "ticket_cancel", json!({"id": "tickets:4,6", "tickets": [4, 6]})),
            ("stray:57-1", "stray_rm", json!({"name": "57-1"})),
            ("debt:5", "debt_done", json!({"debt": 5})),
        ];
        for (row, want, want_body) in cases {
            app.sel.insert(Pane::Cleanup, row.into());
            let Intent::Ask(Modal::Confirm { action, body, .. }) = intent(&mut app, k(KeyCode::Char('x'))) else {
                panic!("{row}: no confirm")
            };
            assert_eq!(action, want, "{row}");
            assert_eq!(Value::Object(body), want_body, "{row}");
        }
        app.sel.insert(Pane::Cleanup, "session:busy".into());
        assert!(matches!(intent(&mut app, k(KeyCode::Char('x'))), Intent::Note(n) if n.contains("busy")));
    }

    #[test]
    fn confirms_carry_identity() {
        let mut app = fixture_app();
        let Intent::Ask(Modal::Confirm { action, body, .. }) =
            intent(&mut app, k(KeyCode::Char('s')))
        else {
            panic!()
        };
        assert_eq!(
            (action.as_str(), &body["id"]),
            ("session_stop", &json!("bg1"))
        );
        app.pane = Pane::Worktrees;
        app.sel.insert(Pane::Worktrees, "/r/wt".into());
        let Intent::Ask(Modal::Confirm { action, body, .. }) =
            intent(&mut app, k(KeyCode::Char('d')))
        else {
            panic!()
        };
        assert_eq!(
            (action.as_str(), &body["path"]),
            ("worktree_remove", &json!("/r/wt"))
        );
        app.sel.insert(Pane::Worktrees, "/r".into());
        assert!(matches!(
            intent(&mut app, k(KeyCode::Char('d'))),
            Intent::Note(_)
        ));
    }

    #[test]
    fn y_runs_the_confirmed_action_and_n_cancels() {
        let mut app = fixture_app();
        let Intent::Ask(m) = intent(&mut app, k(KeyCode::Char('x'))) else {
            panic!()
        };
        app.modal = Some(m.clone());
        assert_eq!(intent(&mut app, k(KeyCode::Char('n'))), Intent::CloseModal);
        app.modal = Some(m);
        let Intent::Act { action, .. } = intent(&mut app, k(KeyCode::Char('y'))) else {
            panic!()
        };
        assert_eq!(action, "session_rm");
    }

    #[test]
    fn kill_asks_proc_info_first() {
        let mut app = fixture_app();
        app.pane = Pane::Processes;
        let Intent::Act { action, body, show } = intent(&mut app, k(KeyCode::Char('K'))) else {
            panic!()
        };
        assert_eq!(
            (action.as_str(), &body["pid"], show),
            ("proc_info", &json!(42), Show::KillConfirm)
        );
        app.done("proc_info", 200, &json!({"pid": 42, "start": 7, "comm": "node", "cmd": "node x", "cwd": "/tmp", "rss": 1048576}), Show::KillConfirm);
        let Some(Modal::Confirm {
            action,
            body,
            lines,
            ..
        }) = &app.modal
        else {
            panic!()
        };
        assert_eq!((action.as_str(), &body["start"]), ("kill", &json!(7)));
        assert!(lines[0].contains("node x"));
    }

    #[test]
    fn forms_build_bodies() {
        let mut app = fixture_app();
        app.modal = Some(Modal::Form(new_session_form()));
        for c in "fix it".chars() {
            intent(&mut app, k(KeyCode::Char(c)));
        }
        intent(&mut app, k(KeyCode::Tab));
        for c in "go".chars() {
            intent(&mut app, k(KeyCode::Char(c)));
        }
        intent(&mut app, k(KeyCode::Tab));
        intent(&mut app, k(KeyCode::Char(' '))); // own worktree off
        let Intent::Act { action, body, .. } = intent(&mut app, k(KeyCode::Enter)) else {
            panic!()
        };
        assert_eq!(action, "session_start");
        assert_eq!(
            Value::Object(body),
            json!({"name": "fix it", "prompt": "go", "worktree": false, "brainstorm": true})
        );
        // "Ask questions first" is the fourth field and can be switched off.
        let mut f = new_session_form();
        f.fields[0].kind = FieldKind::Text("quick".into());
        f.fields[1].kind = FieldKind::Text("go".into());
        app.modal = Some(Modal::Form(f));
        for _ in 0..3 {
            intent(&mut app, k(KeyCode::Tab));
        }
        intent(&mut app, k(KeyCode::Char(' ')));
        let Intent::Act { body, .. } = intent(&mut app, k(KeyCode::Enter)) else {
            panic!()
        };
        assert_eq!(body["brainstorm"], json!(false));
        assert_eq!(body["worktree"], json!(true));

        let comps: Vec<String> = ["api", "web", "admin"].iter().map(|x| x.to_string()).collect();
        let mut f = submit_form("fix/x", &comps, &["VISUALS_OK".to_string()]);
        if let FieldKind::Choice(_, at) = &mut f.fields[1].kind {
            *at = 1;
        }
        f.fields[3].kind = FieldKind::Bool(true);
        f.fields[6].kind = FieldKind::Choice(vec!["none".into(), "VISUALS_OK".into()], 1);
        f.fields[7].kind = FieldKind::Text("copy only".into());
        let (a, b) = form_action(&f);
        assert_eq!(a, "submit");
        assert_eq!(
            Value::Object(b),
            json!({"branch": "fix/x", "mode": "deploy", "components": ["web"], "recreate": false, "waivers": {"VISUALS_OK": "copy only"}})
        );
    }

    #[test]
    fn dismiss_only_reply_needs() {
        let mut app = fixture_app();
        assert!(matches!(
            intent(&mut app, k(KeyCode::Char('D'))),
            Intent::Note(_)
        )); // bg1 needs permission
        app.sel.insert(Pane::Sessions, "r1".into());
        assert_eq!(
            intent(&mut app, k(KeyCode::Char('D'))),
            Intent::Dismiss("r1".into(), 9.0)
        );
    }
}
