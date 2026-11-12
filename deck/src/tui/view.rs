//! Drawing: pure functions from the state JSON (and the App's selection and modal) to widgets.
//!
//! Colours follow the terminal's theme: secondary text is the terminal's own foreground with
//! SGR dim, the selection is reverse video, and named colours carry meaning only (red needs and
//! failures, green busy/live, magenta waiting/held, cyan the focused pane). `NO_COLOR` keeps the
//! modifiers and drops every colour. Panes are a single top rule, sized to their content.

use super::{arr, keys, num, s, App, FieldKind, Modal, Pane, TABS};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Cell, Clear, Paragraph, Row, Table, TableState, Wrap};
use ratatui::Frame;
use serde_json::Value;

const DIM: Style = Style::new().add_modifier(Modifier::DIM);
const BOLD: Style = Style::new().add_modifier(Modifier::BOLD);
const WARN: Style = Style::new().fg(Color::Magenta);
const BAD: Style = Style::new().fg(Color::Red);
const GOOD: Style = Style::new().fg(Color::Green);
const HEAD: Style = Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD);
/// The focused pane's title: reverse video, readable on any palette.
const FOCUS: Style = Style::new()
    .fg(Color::Cyan)
    .add_modifier(Modifier::REVERSED)
    .add_modifier(Modifier::BOLD);
const SEL: Style = Style::new()
    .add_modifier(Modifier::REVERSED)
    .add_modifier(Modifier::BOLD);
/// The selected row of a pane without focus: still visible, so the cursor never vanishes.
const SEL_IDLE: Style = Style::new()
    .add_modifier(Modifier::REVERSED)
    .add_modifier(Modifier::DIM);

pub const NEEDS_MAX: usize = 6;

fn mb(bytes: f64) -> String {
    if bytes >= 1024.0 * 1048576.0 {
        format!("{:.1}G", bytes / (1024.0 * 1048576.0))
    } else {
        format!("{:.0}M", bytes / 1048576.0)
    }
}

fn ago(now: f64, at: f64) -> String {
    if at <= 0.0 {
        return "-".into();
    }
    let d = (now - at).max(0.0) as u64;
    match d {
        0..=59 => format!("{d}s"),
        60..=3599 => format!("{}m", d / 60),
        3600..=86399 => format!("{}h", d / 3600),
        _ => format!("{}d", d / 86400),
    }
}

fn short(t: &str, n: usize) -> String {
    let t = t.lines().next().unwrap_or("");
    if t.chars().count() <= n {
        t.to_string()
    } else {
        format!(
            "{}…",
            t.chars().take(n.saturating_sub(1)).collect::<String>()
        )
    }
}

/// The widest of `items`, within [lo, hi].
fn fit<'a>(items: impl Iterator<Item = &'a str>, lo: u16, hi: u16) -> u16 {
    (items.map(|x| x.chars().count()).max().unwrap_or(0) as u16).clamp(lo, hi)
}

/// A session's branch without what its name already says: `worktree-<name>` reads as "own".
pub fn branch_label(branch: &str, name: &str) -> String {
    let rest = branch.strip_prefix("worktree-").unwrap_or(branch);
    if !name.is_empty() && rest == name {
        "(own worktree)".into()
    } else {
        rest.to_string()
    }
}

/// A pane: one top rule carrying its title.
fn rule(title: Line<'static>, focused: bool) -> Block<'static> {
    Block::new()
        .borders(Borders::TOP)
        .border_style(if focused {
            Style::new().fg(Color::Cyan)
        } else {
            DIM
        })
        .title(title)
}

fn title(text: &str, focused: bool) -> Line<'static> {
    if focused {
        Line::from(Span::styled(format!(" ▶ {text} "), FOCUS))
    } else {
        Line::from(Span::styled(format!(" {text} "), BOLD))
    }
}

/// Rows for each part of the screen.
#[derive(Debug, PartialEq)]
pub struct Heights {
    pub header: u16,
    pub warn: u16,
    pub needs: u16,
    pub sessions: u16,
    pub lower: u16,
}

pub fn heights(app: &App, height: u16, header: u16, warn: u16) -> Heights {
    let rest = height.saturating_sub(header + warn + 1); // footer
    let n = app.needs().len();
    let mut h = Heights {
        header,
        warn,
        needs: 0,
        sessions: 0,
        lower: 0,
    };
    if app.zoom {
        if app.pane == Pane::Sessions {
            h.sessions = rest;
        } else {
            h.lower = rest;
        }
        return h;
    }
    if n > 0 {
        h.needs = 1 + n.min(NEEDS_MAX) as u16 + u16::from(n > NEEDS_MAX);
    }
    let avail = rest.saturating_sub(h.needs);
    // The lower pane: rule + header + 5 body rows on a short screen, 8 from 30 rows.
    let lower_min: u16 = if height < 30 { 7 } else { 10 };
    let want = app.sessions().len() as u16 + 2; // rule + header + rows
    h.sessions = want.min(avail.saturating_sub(lower_min)).max(3.min(avail));
    h.lower = avail.saturating_sub(h.sessions);
    h
}

pub fn draw(f: &mut Frame, app: &App) {
    let area = f.area();
    let Some(st) = app.state.clone() else {
        f.render_widget(
            Paragraph::new("Session Deck: reading sessions, queue and laptop…"),
            area,
        );
        return;
    };
    let head = header_line(&st);
    let head_w: usize = head.spans.iter().map(|s| s.content.chars().count()).sum();
    let header_h = if head_w > area.width as usize { 2 } else { 1 };
    let warnings = warning_line(&st);
    let h = heights(app, area.height, header_h, u16::from(warnings.is_some()));
    let [header, warn, needs_area, sessions, lower, footer] = Layout::vertical([
        Constraint::Length(h.header),
        Constraint::Length(h.warn),
        Constraint::Length(h.needs),
        Constraint::Length(h.sessions),
        Constraint::Length(h.lower),
        Constraint::Length(1),
    ])
    .areas(area);
    f.render_widget(Paragraph::new(head).wrap(Wrap { trim: true }), header);
    if let Some(w) = warnings {
        f.render_widget(w, warn);
    }
    if h.needs > 0 {
        draw_needs(f, app, needs_area);
    }
    if h.sessions > 0 {
        draw_sessions(f, app, &st, sessions);
    }
    if h.lower > 0 {
        draw_lower(f, app, &st, lower);
    }
    f.render_widget(footer_line(app, area.width), footer);
    if let Some(m) = &app.modal {
        draw_modal(f, app, m, area);
    }
    if app.no_color {
        for cell in f.buffer_mut().content.iter_mut() {
            cell.set_fg(Color::Reset);
            cell.set_bg(Color::Reset);
        }
    }
}

fn header_line(st: &Value) -> Line<'static> {
    let sys = &st["system"];
    let cpu = sys
        .get("cpu")
        .and_then(Value::as_f64)
        .map(|c| format!("{c:.0}%"))
        .unwrap_or("-".into());
    let avail = num(&sys["mem"], "availPct");
    let swap = &sys["swap"];
    let load = arr(sys, "load")
        .first()
        .and_then(Value::as_str)
        .unwrap_or("-")
        .to_string();
    let mut spans = vec![
        Span::styled(" Session Deck ", HEAD),
        Span::raw(format!(" cpu {cpu}  ")),
        Span::styled(
            format!("mem {avail:.0}% free  "),
            if avail < 15.0 { BAD } else { Style::new() },
        ),
        Span::styled(
            format!("swap {:.0}%  ", num(swap, "usedPct")),
            if swap["hot"] == true {
                BAD
            } else {
                Style::new()
            },
        ),
        Span::raw(format!("load {load}  ")),
    ];
    if let Some(locks) = sys["locks"].as_object() {
        for (k, l) in locks {
            let held = l["held"] == true;
            spans.push(Span::styled(
                format!("{k} {}  ", if held { "HELD" } else { "free" }),
                if held { WARN } else { DIM },
            ));
        }
    }
    let guards = st["queue"]["guards"] == true;
    spans.push(Span::styled(
        format!("guards {}  ", if guards { "on" } else { "off" }),
        DIM,
    ));
    let vps = &st["queue"]["vps"];
    if let Some(stamps) = vps["stamps"].as_object() {
        let st: Vec<String> = stamps
            .iter()
            .map(|(k, v)| format!("{k} {}", v.as_str().unwrap_or("?")))
            .collect();
        spans.push(Span::styled(format!("prod {}  ", st.join(" ")), DIM));
    }
    let lease = arr(vps, "lease")
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>()
        .join(" ");
    if !lease.is_empty() {
        spans.push(Span::styled(
            format!("lease {lease}"),
            if lease == "free" { DIM } else { WARN },
        ));
    }
    Line::from(spans)
}

fn warning_line(st: &Value) -> Option<Paragraph<'static>> {
    let mut parts: Vec<String> = arr(&st["system"], "warnings")
        .iter()
        .filter_map(Value::as_str)
        .map(String::from)
        .collect();
    let ae = s(st, "agentsError");
    if !ae.is_empty() {
        parts.push(format!("claude agents: {ae}"));
    }
    (!parts.is_empty()).then(|| {
        Paragraph::new(Line::styled(
            format!(" ⚠ {}", parts.join("  ·  ")),
            BAD.add_modifier(Modifier::BOLD),
        ))
    })
}

/// What a session is waiting on, in one line.
fn need_detail(n: &Value) -> String {
    let tool = &n["tool"];
    if let Some(q) = arr(tool, "questions").first() {
        let opts: Vec<&str> = arr(q, "options").iter().map(|o| s(o, "label")).collect();
        return format!("{}  [{}]", s(q, "question"), opts.join(" / "));
    }
    for k in ["command", "plan", "line"] {
        if !s(tool, k).is_empty() {
            return s(tool, k).to_string();
        }
    }
    s(n, "message").to_string()
}

fn draw_needs(f: &mut Frame, app: &App, area: Rect) {
    let needs = app.needs();
    let mut lines = vec![];
    for x in needs.iter().take(NEEDS_MAX) {
        let n = app.need(x).unwrap();
        let head = format!("{}  {}  ", s(x, "name"), s(n, "label"));
        let room = (area.width as usize).saturating_sub(head.chars().count() + 3);
        lines.push(Line::from(vec![
            Span::styled(" ! ", BAD.add_modifier(Modifier::BOLD)),
            Span::styled(format!("{}  ", s(x, "name")), BOLD),
            Span::styled(format!("{}  ", s(n, "label")), BAD),
            Span::raw(short(&need_detail(n), room)),
        ]));
    }
    if needs.len() > NEEDS_MAX {
        lines.push(Line::styled(
            format!(
                "   +{} more in Sessions (marked !)",
                needs.len() - NEEDS_MAX
            ),
            DIM,
        ));
    }
    let t = Line::from(vec![
        Span::styled(
            format!(" Needs you ({}) ", needs.len()),
            BAD.add_modifier(Modifier::BOLD),
        ),
        Span::styled(" o open (Ctrl+Z back) · D dismiss a reply ", DIM),
    ]);
    f.render_widget(
        Paragraph::new(lines).block(
            Block::new()
                .borders(Borders::TOP)
                .border_style(BAD)
                .title(t),
        ),
        area,
    );
}

fn table_state(app: &App, pane: Pane) -> TableState {
    TableState::default().with_selected(app.selected_index(pane))
}

fn sel_style(focused: bool) -> Style {
    if focused {
        SEL
    } else {
        SEL_IDLE
    }
}

fn draw_sessions(f: &mut Frame, app: &App, st: &Value, area: Rect) {
    let now = num(st, "now");
    let w = area.width;
    let wide = w >= 150;
    let mid = w >= 120;
    let sessions = app.sessions();
    let labels: Vec<String> = sessions
        .iter()
        .map(|x| branch_label(s(&x["worktree"], "branch"), s(x, "name")))
        .collect();
    let name_w = fit(
        sessions.iter().map(|x| s(x, "name")),
        8,
        if mid { 28 } else { 16 },
    );
    let branch_w = fit(
        labels.iter().map(String::as_str),
        6,
        if mid { 32 } else { 18 },
    );
    let mut widths = vec![
        Constraint::Length(1),
        Constraint::Length(name_w),
        Constraint::Length(7),
        Constraint::Length(branch_w),
    ];
    let mut head = vec!["", "session", "status", "branch"];
    if mid {
        widths.push(Constraint::Length(8));
        head.push("changes");
    }
    if wide {
        widths.push(Constraint::Length(13));
        head.push("ticket");
    }
    if mid {
        widths.push(Constraint::Length(5));
        head.push("cpu");
    }
    if w >= 90 {
        widths.push(Constraint::Length(5));
        head.push("mem");
    }
    if wide {
        widths.push(Constraint::Length(4));
        head.push("act");
    }
    widths.push(Constraint::Min(10));
    head.push("last message");
    let rows: Vec<Row> = sessions
        .iter()
        .zip(&labels)
        .map(|(x, label)| {
            let need = app.need(x).is_some();
            let status = s(x, "status");
            let (mark, ms) = if need {
                ("!", BAD.add_modifier(Modifier::BOLD))
            } else if status == "busy" {
                ("●", GOOD)
            } else if x["stopped"] == true {
                ("·", DIM)
            } else {
                ("○", Style::new())
            };
            let wt = &x["worktree"];
            let mut cells = vec![
                Cell::from(Span::styled(mark, ms)),
                Cell::from(Span::styled(
                    s(x, "name").to_string(),
                    if need { BOLD } else { Style::new() },
                )),
                Cell::from(Span::styled(
                    status.to_string(),
                    match status {
                        "waiting" => WARN,
                        "busy" => GOOD,
                        "stopped" => DIM,
                        _ => Style::new(),
                    },
                )),
                Cell::from(short(label, branch_w as usize)),
            ];
            if mid {
                let dirty = wt
                    .get("dirty")
                    .and_then(Value::as_u64)
                    .filter(|d| *d > 0)
                    .map(|d| format!("±{d}"))
                    .unwrap_or_default();
                let ahead = if s(wt, "ahead") != "0" && !s(wt, "ahead").is_empty() {
                    format!("↑{}", s(wt, "ahead"))
                } else {
                    String::new()
                };
                let behind = if s(wt, "behind") != "0" && !s(wt, "behind").is_empty() {
                    format!("↓{}", s(wt, "behind"))
                } else {
                    String::new()
                };
                cells.push(Cell::from(Span::styled(
                    format!("{dirty} {ahead}{behind}").trim().to_string(),
                    WARN,
                )));
            }
            if wide {
                let ticket = if x["ticket"].is_object() {
                    format!("#{} {}", x["ticket"]["id"], s(&x["ticket"], "state"))
                } else {
                    String::new()
                };
                cells.push(Cell::from(ticket));
            }
            if mid {
                cells.push(Cell::from(format!("{:.0}%", num(x, "cpu"))));
            }
            if w >= 90 {
                cells.push(Cell::from(mb(num(x, "rss"))));
            }
            if wide {
                cells.push(Cell::from(Span::styled(
                    ago(now, num(x, "lastActivity")),
                    DIM,
                )));
            }
            cells.push(Cell::from(Span::styled(s(x, "last").to_string(), DIM)));
            Row::new(cells)
        })
        .collect();
    let focused = app.pane == Pane::Sessions;
    let t = Table::new(rows, widths)
        .header(Row::new(head).style(DIM.add_modifier(Modifier::BOLD)))
        .block(rule(
            title(&format!("Sessions ({})", sessions.len()), focused),
            focused,
        ))
        .row_highlight_style(sel_style(focused));
    f.render_stateful_widget(t, area, &mut table_state(app, Pane::Sessions));
}

/// The lower pane's rule: its tabs, the shown one marked.
fn tabs_title(shown: Pane, focused: bool, cleanup: usize) -> Line<'static> {
    let clean = if cleanup > 0 { format!("Clean up ({cleanup})") } else { "Clean up".to_string() };
    let names = ["Queue".to_string(), "Worktrees".into(), clean, "Processes".into(), "Gate log".into()];
    let mut spans = vec![Span::raw(" ")];
    for (i, p) in TABS.iter().enumerate() {
        if i > 0 {
            spans.push(Span::styled(" │ ", DIM));
        }
        spans.push(if *p == shown && focused {
            Span::styled(format!(" ▶ {} ", names[i]), FOCUS)
        } else if *p == shown {
            Span::styled(names[i].clone(), BOLD.add_modifier(Modifier::UNDERLINED))
        } else {
            Span::styled(names[i].clone(), DIM)
        });
    }
    spans.push(Span::raw(" "));
    Line::from(spans)
}

fn draw_lower(f: &mut Frame, app: &App, st: &Value, area: Rect) {
    let tab = if app.pane == Pane::Sessions {
        app.tab
    } else {
        app.pane
    };
    let focused = app.pane != Pane::Sessions;
    let w = area.width;
    let block = rule(tabs_title(tab, focused, arr(&st["cleanup"], "items").len()), focused);
    let mut ts = table_state(app, tab);
    let hl = sel_style(focused);
    match tab {
        Pane::Queue => {
            let q = &st["queue"];
            let extra: Vec<Line> =
                arr(q, "holds")
                    .iter()
                    .map(|h| {
                        Line::styled(
                            format!(" hold: {}", serde_json::to_string(h).unwrap_or_default()),
                            WARN,
                        )
                    })
                    .chain(arr(q, "alerts").iter().rev().take(3).map(|a| {
                        Line::styled(format!(" alert: {}", a.as_str().unwrap_or("")), DIM)
                    }))
                    .collect();
            let inner = block.inner(area);
            f.render_widget(block, area);
            let extra_h = (extra.len() as u16)
                .min(5)
                .min(inner.height.saturating_sub(3));
            let [top, bottom] =
                Layout::vertical([Constraint::Min(2), Constraint::Length(extra_h)]).areas(inner);
            let tickets = arr(q, "tickets");
            let branch_w = fit(
                tickets.iter().map(|t| s(t, "branch")),
                6,
                if w >= 120 { 44 } else { 24 },
            );
            let mut widths = vec![
                Constraint::Length(4),
                Constraint::Length(9),
                Constraint::Length(6),
            ];
            let mut head = vec!["#", "state", "mode"];
            if w >= 100 {
                widths.push(Constraint::Length(14));
                head.push("apps");
            }
            widths.push(Constraint::Length(branch_w));
            widths.push(Constraint::Min(10));
            head.extend(["branch", "detail"]);
            let rows: Vec<Row> = tickets
                .iter()
                .map(|t| {
                    let state = s(t, "state");
                    let style = match state {
                        "live" | "merged" => GOOD,
                        "failed" | "conflict" => BAD,
                        "queued" => Style::new(),
                        _ => WARN,
                    };
                    let mut cells = vec![
                        Cell::from(format!("{}", t["id"])),
                        Cell::from(Span::styled(state.to_string(), style)),
                        Cell::from(s(t, "mode").to_string()),
                    ];
                    if w >= 100 {
                        cells.push(Cell::from(s(t, "components").to_string()));
                    }
                    cells.push(Cell::from(short(s(t, "branch"), branch_w as usize)));
                    cells.push(Cell::from(Span::styled(s(t, "detail").to_string(), DIM)));
                    Row::new(cells)
                })
                .collect();
            let t = Table::new(rows, widths)
                .header(Row::new(head).style(DIM.add_modifier(Modifier::BOLD)))
                .row_highlight_style(hl);
            f.render_stateful_widget(t, top, &mut ts);
            f.render_widget(Paragraph::new(extra), bottom);
        }
        Pane::Worktrees => {
            let trees = arr(st, "worktrees");
            let names: Vec<&str> = trees
                .iter()
                .map(|t| s(t, "path").rsplit('/').next().unwrap_or(""))
                .collect();
            let name_w = fit(names.iter().copied(), 8, if w >= 120 { 36 } else { 20 });
            let branch_w = fit(
                trees.iter().map(|t| s(t, "branch")),
                6,
                if w >= 120 { 40 } else { 20 },
            );
            let rows: Vec<Row> = trees
                .iter()
                .zip(&names)
                .map(|(t, name)| {
                    let dirty = t.get("dirty").and_then(Value::as_u64);
                    Row::new(vec![
                        Cell::from(short(name, name_w as usize)),
                        Cell::from(short(s(t, "branch"), branch_w as usize)),
                        Cell::from(Span::styled(
                            dirty
                                .map(|d| {
                                    if d > 0 {
                                        format!("{d} changed")
                                    } else {
                                        "clean".into()
                                    }
                                })
                                .unwrap_or("?".into()),
                            if dirty.unwrap_or(0) > 0 { WARN } else { DIM },
                        )),
                        Cell::from(Span::styled(
                            if t["main"] == true {
                                "main"
                            } else if t["merged"] == true {
                                "merged"
                            } else {
                                ""
                            },
                            GOOD,
                        )),
                        Cell::from(
                            arr(t, "sessions")
                                .iter()
                                .filter_map(Value::as_str)
                                .collect::<Vec<_>>()
                                .join(", "),
                        ),
                    ])
                })
                .collect();
            let widths = [
                Constraint::Length(name_w),
                Constraint::Length(branch_w),
                Constraint::Length(10),
                Constraint::Length(6),
                Constraint::Min(8),
            ];
            let t = Table::new(rows, widths)
                .header(
                    Row::new(["worktree", "branch", "files", "", "sessions"])
                        .style(DIM.add_modifier(Modifier::BOLD)),
                )
                .block(block)
                .row_highlight_style(hl);
            f.render_stateful_widget(t, area, &mut ts);
        }
        Pane::Processes => {
            let top = arr(&st["system"], "top");
            let rows: Vec<Row> = top
                .iter()
                .map(|p| {
                    let mut cells = vec![
                        Cell::from(format!("{}", p["pid"])),
                        Cell::from(short(s(p, "comm"), 14)),
                        Cell::from(mb(num(p, "rss"))),
                    ];
                    if w >= 90 {
                        cells.push(Cell::from(format!("{:.1}%", num(p, "cpu"))));
                    }
                    cells.push(Cell::from(Span::styled(s(p, "cmd").to_string(), DIM)));
                    Row::new(cells)
                })
                .collect();
            let mut widths = vec![
                Constraint::Length(8),
                Constraint::Length(14),
                Constraint::Length(6),
            ];
            let mut head = vec!["pid", "name", "mem"];
            if w >= 90 {
                widths.push(Constraint::Length(6));
                head.push("cpu");
            }
            widths.push(Constraint::Min(10));
            head.push("command");
            let t = Table::new(rows, widths)
                .header(Row::new(head).style(DIM.add_modifier(Modifier::BOLD)))
                .block(block)
                .row_highlight_style(hl);
            f.render_stateful_widget(t, area, &mut ts);
        }
        Pane::Cleanup => {
            let items = arr(&st["cleanup"], "items");
            if items.is_empty() {
                f.render_widget(
                    Paragraph::new(Line::styled("nothing to clean up (the conductor refreshes the list every 5 min)", DIM)).block(block),
                    area,
                );
                return;
            }
            let rows: Vec<Row> = items
                .iter()
                .map(|it| {
                    let stale = s(it, "stale");
                    let style = if stale.is_empty() { Style::new() } else { DIM };
                    let label = if stale.is_empty() { s(it, "label").to_string() } else { format!("{} — not now: {stale}", s(it, "label")) };
                    Row::new(vec![
                        Cell::from(s(it, "kind").to_string()),
                        Cell::from(short(&label, w.saturating_sub(14) as usize)),
                    ])
                    .style(style)
                })
                .collect();
            let t = Table::new(rows, [Constraint::Length(9), Constraint::Min(10)])
                .header(Row::new(vec!["kind", "what (x to clean up, asks first)"]).style(DIM.add_modifier(Modifier::BOLD)))
                .block(block)
                .row_highlight_style(hl);
            f.render_stateful_widget(t, area, &mut ts);
        }
        _ => {
            let g = &st["system"]["gate"];
            let mut lines: Vec<Line> = vec![Line::styled(
                if s(g, "file").is_empty() {
                    "no gate log yet".to_string()
                } else {
                    s(g, "file").to_string()
                },
                DIM,
            )];
            lines.extend(
                arr(g, "tail")
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|l| Line::raw(crate::util::strip_ansi(l))),
            );
            f.render_widget(Paragraph::new(lines).block(block), area);
        }
    }
}

/// The footer: an action's result first (it matters most), then the pane's keys, then the
/// global ones, shortened to `? help  q quit` on a narrow window.
fn footer_line(app: &App, width: u16) -> Paragraph<'static> {
    let keys = match app.pane {
        Pane::Sessions => "o open (Ctrl+Z back)  l logs  s stop  x remove  D dismiss  t submit  n new",
        Pane::Queue => "⏎ log  R release  a allow  t submit",
        Pane::Worktrees => "d remove",
        Pane::Cleanup => "x clean up this row",
        Pane::Processes => "K stop process",
        Pane::Gate => "",
    };
    let mut spans = vec![];
    if let Some((t, err, _)) = &app.status {
        spans.push(Span::styled(
            format!(" {t} "),
            if *err { BAD } else { GOOD },
        ));
    }
    if app.busy > 0 {
        spans.push(Span::styled(" working… ", WARN));
    }
    spans.push(Span::styled(format!(" {keys} "), BOLD));
    let zoom = if app.zoom { "z unzoom" } else { "z zoom" };
    let global = if width >= 120 {
        format!(" tab pane  {zoom}  r refresh  ? help  q quit ")
    } else {
        " ? help  q quit ".to_string()
    };
    spans.push(Span::styled(global, DIM));
    Paragraph::new(Line::from(spans))
}

fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width.saturating_sub(2));
    let h = h.min(area.height.saturating_sub(2));
    Rect {
        x: area.x + (area.width - w) / 2,
        y: area.y + (area.height - h) / 2,
        width: w,
        height: h,
    }
}

fn modal_block(title: String, style: Style) -> Block<'static> {
    Block::bordered()
        .title(Span::styled(
            format!(" {title} "),
            style.add_modifier(Modifier::BOLD),
        ))
        .border_style(style)
}

fn draw_modal(f: &mut Frame, app: &App, m: &Modal, area: Rect) {
    match m {
        Modal::Help => {
            let text = vec![
                Line::styled("Keys", HEAD),
                Line::raw(""),
                Line::raw("tab / shift-tab   next / previous pane"),
                Line::raw("↑↓ j k  PgUp PgDn  move"),
                Line::raw(
                    "z                 zoom the focused pane to full height (again to go back)",
                ),
                Line::raw("r                 refresh now (it also refreshes every 3 s)"),
                Line::raw("n                 new background session"),
                Line::raw(""),
                Line::styled("Sessions", HEAD),
                Line::raw("o / ⏎   open: claude attach here; Ctrl+Z (or Ctrl+B d) comes back; ← is Claude's agent list"),
                Line::raw("l       transcript    s stop    x remove    t submit its branch"),
                Line::raw("D       dismiss a \"waiting for your reply\" until its status changes"),
                Line::styled("Queue", HEAD),
                Line::raw("⏎ log   R release   a allow   t submit"),
                Line::styled("Worktrees", HEAD),
                Line::raw("d       remove a clean, merged worktree with no session"),
                Line::styled("Clean up", HEAD),
                Line::raw("x / ⏎   act on the conductor's row (session, worktree, tickets, stray, debt); asks first"),
                Line::styled("Processes", HEAD),
                Line::raw("K       SIGTERM (shows the process before asking)"),
                Line::raw(""),
                Line::styled(
                    "Dangerous keys ask y/n first. NO_COLOR=1 drops colours. Any key closes this.",
                    DIM,
                ),
            ];
            let r = centered(area, 80, text.len() as u16 + 2);
            f.render_widget(Clear, r);
            f.render_widget(
                Paragraph::new(text).block(modal_block(
                    "Session Deck".into(),
                    Style::new().fg(Color::Cyan),
                )),
                r,
            );
        }
        Modal::Confirm { title, lines, .. } => {
            let mut text: Vec<Line> = lines.iter().map(|l| Line::raw(l.clone())).collect();
            text.push(Line::raw(""));
            text.push(Line::styled("y yes   n / esc no", BOLD));
            let r = centered(area, 90, text.len() as u16 + 2);
            f.render_widget(Clear, r);
            f.render_widget(
                Paragraph::new(text)
                    .wrap(Wrap { trim: false })
                    .block(modal_block(title.clone(), WARN)),
                r,
            );
        }
        Modal::Popup {
            title,
            lines,
            scroll,
        } => {
            let r = centered(
                area,
                area.width.saturating_sub(6),
                area.height.saturating_sub(4),
            );
            f.render_widget(Clear, r);
            let text: Vec<Line> = lines.iter().map(|l| Line::raw(l.clone())).collect();
            let hint = format!("{title}  ·  ↑↓ PgUp PgDn g G  ·  esc close");
            f.render_widget(
                Paragraph::new(text)
                    .wrap(Wrap { trim: false })
                    .scroll((*scroll, 0))
                    .block(modal_block(hint, Style::new().fg(Color::Cyan))),
                r,
            );
        }
        Modal::Form(form) => {
            let mut text = vec![];
            for (i, fld) in form.fields.iter().enumerate() {
                let focus = i == form.focus;
                let value = match &fld.kind {
                    FieldKind::Text(t) => format!("{t}{}", if focus { "▏" } else { "" }),
                    FieldKind::Bool(b) => (if *b { "[x]" } else { "[ ]" }).to_string(),
                    FieldKind::Choice(o, at) => format!("‹ {} ›", o[*at]),
                };
                text.push(Line::from(vec![
                    Span::styled(
                        format!("{} {:<34}", if focus { "›" } else { " " }, fld.label),
                        if focus { HEAD } else { DIM },
                    ),
                    Span::styled(value, if focus { BOLD } else { Style::new() }),
                ]));
            }
            if form.kind == super::FormKind::Submit {
                let b = keys::branches(app);
                text.push(Line::raw(""));
                text.push(Line::styled(
                    format!(
                        "branches: {}",
                        b.iter().take(8).cloned().collect::<Vec<_>>().join("  ")
                    ),
                    DIM,
                ));
            }
            text.push(Line::raw(""));
            text.push(Line::styled(
                "tab/↑↓ field  space/←→ toggle or choose  ⏎ send  esc cancel",
                DIM,
            ));
            let r = centered(area, 96, text.len() as u16 + 2);
            f.render_widget(Clear, r);
            f.render_widget(
                Paragraph::new(text)
                    .wrap(Wrap { trim: false })
                    .block(modal_block(
                        form.title.clone(),
                        Style::new().fg(Color::Cyan),
                    )),
                r,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tui::tests::fixture_app;
    use crate::tui::PANES;
    use ratatui::backend::TestBackend;
    use ratatui::buffer::Buffer;
    use ratatui::Terminal;
    use serde_json::json;
    use std::sync::Arc;

    fn render(app: &App, w: u16, h: u16) -> Buffer {
        let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
        t.draw(|f| draw(f, app)).unwrap();
        t.backend().buffer().clone()
    }

    fn text(buf: &Buffer) -> String {
        let a = buf.area;
        (0..a.height)
            .map(|y| {
                (0..a.width)
                    .map(|x| buf[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn screen(app: &App, w: u16, h: u16) -> String {
        text(&render(app, w, h))
    }

    /// The row holding `needle`, if any.
    fn row_of(out: &str, needle: &str) -> Option<usize> {
        out.lines().position(|l| l.contains(needle))
    }

    #[test]
    fn sessions_footer_says_how_to_come_back() {
        let mut app = fixture_app();
        app.pane = Pane::Sessions;
        let out = screen(&app, 160, 40);
        assert!(out.contains("o open (Ctrl+Z back)"), "{out}");
    }

    #[test]
    fn needs_first_with_detail() {
        let app = fixture_app();
        let out = screen(&app, 160, 40);
        assert!(out.contains("Needs you (2)"), "{out}");
        assert!(out.contains("rm -rf build"));
        assert!(row_of(&out, "bg-task").unwrap() < row_of(&out, "Sessions (3)").unwrap());
        assert!(out.contains("Queue"));
        assert!(out.contains("fix/dirty"));
        assert!(out.contains("Only 9% memory available"));
    }

    #[test]
    fn fits_gnome_default_and_bigger_windows() {
        let app = fixture_app();
        for (w, h, lower_rows) in [(80u16, 24u16, 5usize), (120, 30, 8), (200, 50, 8)] {
            let out = screen(&app, w, h);
            for name in ["my-feature", "bg-task", "asker"] {
                assert!(
                    out.matches(name).count() >= 1,
                    "{name} missing at {w}x{h}:\n{out}"
                );
            }
            // The lower pane: from its rule to the footer, minus its header row.
            let rule = row_of(&out, "Worktrees").unwrap();
            let body = (h as usize - 1) - rule - 2;
            assert!(
                body >= lower_rows,
                "lower pane has {body} body rows at {w}x{h}:\n{out}"
            );
        }
        // At 80 columns the last message still has room.
        let out = screen(&app, 80, 24);
        let line = out
            .lines()
            .find(|l| l.contains("my-feature") && !l.contains("Needs"))
            .unwrap();
        assert!(line.contains("Fixed it."), "{line}");
    }

    #[test]
    fn colours_follow_the_theme() {
        let mut app = fixture_app();
        let mut bufs = vec![render(&app, 160, 40)];
        for m in [
            Modal::Help,
            Modal::Form(keys::submit_form("x", &["api".to_string(), "web".to_string()], &[])),
            Modal::Confirm {
                title: "t".into(),
                lines: vec!["l".into()],
                action: "kill".into(),
                body: Default::default(),
            },
            Modal::Popup {
                title: "p".into(),
                lines: vec!["x".into()],
                scroll: 0,
            },
        ] {
            app.modal = Some(m);
            bufs.push(render(&app, 160, 40));
        }
        for buf in &bufs {
            for c in buf.content.iter() {
                for col in [c.fg, c.bg] {
                    assert!(
                        !matches!(
                            col,
                            Color::DarkGray
                                | Color::Gray
                                | Color::Yellow
                                | Color::LightYellow
                                | Color::Rgb(..)
                                | Color::Indexed(_)
                        ),
                        "{col:?}"
                    );
                }
            }
        }
        app.modal = None;
        app.no_color = true;
        let buf = render(&app, 160, 40);
        assert!(buf
            .content
            .iter()
            .all(|c| c.fg == Color::Reset && c.bg == Color::Reset));
    }

    #[test]
    fn selection_and_focus_are_reverse_video() {
        let app = fixture_app();
        let buf = render(&app, 160, 40);
        let out = text(&buf);
        let y = out
            .lines()
            .position(|l| {
                l.contains("bg-task")
                    && l.contains("waiting")
                    && !l.contains("Needs")
                    && !l.contains("rm -rf")
            })
            .unwrap() as u16;
        let x = out
            .lines()
            .nth(y as usize)
            .unwrap()
            .find("bg-task")
            .unwrap() as u16;
        assert!(buf[(x, y)].modifier.contains(Modifier::REVERSED));
        let ty = row_of(&out, "▶ Sessions").unwrap() as u16;
        let tx = out
            .lines()
            .nth(ty as usize)
            .unwrap()
            .chars()
            .position(|c| c == '▶')
            .unwrap() as u16;
        assert!(buf[(tx, ty)].modifier.contains(Modifier::REVERSED));
    }

    #[test]
    fn zoom_gives_the_focused_pane_the_screen() {
        let mut app = fixture_app();
        app.zoom = true;
        let out = screen(&app, 120, 30);
        assert!(
            !out.contains("Worktrees") && !out.contains("Needs you"),
            "{out}"
        );
        assert!(out.contains("Sessions (3)"));
        app.pane = Pane::Queue;
        let out = screen(&app, 120, 30);
        assert!(
            !out.contains("Sessions (3)") && out.contains("▶ Queue"),
            "{out}"
        );
    }

    #[test]
    fn many_needs_cap_with_more_line() {
        let mut app = fixture_app();
        let mut st = (*app.state.clone().unwrap()).clone();
        let tmpl = st["sessions"][1].clone();
        for i in 0..8 {
            let mut x = tmpl.clone();
            x["id"] = json!(format!("n{i}"));
            x["name"] = json!(format!("needer-{i}"));
            st["sessions"].as_array_mut().unwrap().push(x);
        }
        app.take_state(Arc::new(st));
        let out = screen(&app, 160, 50);
        assert!(
            out.contains("Needs you (10)") && out.contains("+4 more"),
            "{out}"
        );
    }

    /// Layout work: `cargo test print_screens -- --ignored --nocapture` prints the fixture at
    /// GNOME Terminal's default size and a large window.
    #[test]
    #[ignore]
    fn print_screens() {
        let app = fixture_app();
        for (w, h) in [(80, 24), (160, 40)] {
            println!("--- {w}x{h}\n{}", screen(&app, w, h));
        }
    }

    #[test]
    fn branch_label_drops_what_the_name_says() {
        assert_eq!(
            branch_label("worktree-rust-deck", "rust-deck"),
            "(own worktree)"
        );
        assert_eq!(branch_label("worktree-other", "rust-deck"), "other");
        assert_eq!(branch_label("fix/x", "a"), "fix/x");
    }

    #[test]
    fn empty_and_tiny_screens_do_not_panic() {
        let app = App::default();
        assert!(screen(&app, 80, 10).contains("reading sessions"));
        let mut app = fixture_app();
        screen(&app, 20, 5);
        screen(&app, 1, 1);
        app.modal = Some(Modal::Help);
        screen(&app, 30, 8);
        app.modal = Some(Modal::Form(keys::submit_form("x", &["api".to_string(), "web".to_string()], &[])));
        screen(&app, 160, 40);
        for p in PANES {
            app.modal = None;
            app.pane = p;
            for (w, h) in [(120, 30), (80, 24), (60, 12)] {
                screen(&app, w, h);
                app.zoom = !app.zoom;
                screen(&app, w, h);
            }
        }
    }
}
