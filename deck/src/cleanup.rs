//! The Clean up panel: the sluicy conductor's `cleanup.json` (`sluicy conduct` writes it atomically),
//! shown one row per item, each acted on alone and checked against the list as it is at that moment.

use crate::collect::Cfg;
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

/// The list, or an empty one when the file is missing or unreadable (never half a list).
pub fn read(c: &Cfg) -> (Value, Vec<Map<String, Value>>) {
    let Ok(text) = fs::read_to_string(c.sluicy_home.join("cleanup.json")) else {
        return (Value::Null, vec![]);
    };
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&text) else {
        return (Value::Null, vec![]);
    };
    let items = data
        .get("items")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|i| i.as_object().cloned())
                .filter(|i| i.get("id").map(Value::is_string).unwrap_or(false))
                .collect()
        })
        .unwrap_or_default();
    (data.get("generated").cloned().unwrap_or(Value::Null), items)
}

/// One item by id and kind, from the list as it is now.
pub fn item(c: &Cfg, id: &str, kind: &str) -> Option<Map<String, Value>> {
    read(c).1.into_iter().find(|i| {
        i.get("id").and_then(Value::as_str) == Some(id) && i.get("kind").and_then(Value::as_str) == Some(kind)
    })
}

fn s<'a>(m: &'a Map<String, Value>, k: &str) -> &'a str {
    m.get(k).and_then(Value::as_str).unwrap_or("")
}

/// Rows for the panel. A session row gets the listed session's id (same name, cwd in the item's worktree),
/// or `stale` with why it cannot be acted on. Rows whose target is gone, or that this Deck acted on for
/// this `generated`, are left out.
pub fn collect(c: &Cfg, sessions: &[Value], hidden: &(Value, HashSet<String>)) -> Value {
    let (generated, items) = read(c);
    let empty = HashSet::new();
    let done = if hidden.0 == generated { &hidden.1 } else { &empty };
    let mut out = vec![];
    for mut it in items {
        if done.contains(s(&it, "id")) {
            continue;
        }
        let kind = s(&it, "kind").to_string();
        if matches!(kind.as_str(), "session" | "worktree" | "stray") && !Path::new(s(&it, "path")).exists() {
            continue;
        }
        if kind == "session" {
            let path = s(&it, "path").trim_end_matches('/').to_string();
            let name = s(&it, "name").to_string();
            let matched: Vec<&Value> = sessions
                .iter()
                .filter(|x| {
                    let cwd = x.get("cwd").and_then(Value::as_str).unwrap_or("").trim_end_matches('/');
                    x.get("name").and_then(Value::as_str) == Some(name.as_str())
                        && (cwd == path || cwd.starts_with(&format!("{path}/")))
                })
                .collect();
            let st = |k: &str, x: &Value| x.get(k).and_then(Value::as_str).unwrap_or("").to_string();
            match matched.as_slice() {
                [x] if x.get("attachable") != Some(&json!(true)) => {
                    it.insert("stale".into(), json!("not a background session"));
                }
                [x] if matches!(st("status", x).as_str(), "busy" | "waiting")
                    || matches!(st("state", x).as_str(), "blocked" | "working")
                    || !st("waitingFor", x).is_empty() =>
                {
                    it.insert("stale".into(), json!("busy or waiting on you"));
                }
                [x] => {
                    it.insert("sessionId".into(), x.get("id").cloned().unwrap_or(Value::Null));
                }
                _ => {
                    it.insert("stale".into(), json!("session not listed"));
                }
            }
        }
        out.push(Value::Object(it));
    }
    json!({"generated": generated, "items": out})
}

/// `<digits>-<letters or digits>`: the names sluicy gives stray folders (`57-1793835966`, `57-manual`).
pub fn stray_name_ok(name: &str) -> bool {
    match name.split_once('-') {
        Some((a, b)) => {
            !a.is_empty()
                && a.bytes().all(|x| x.is_ascii_digit())
                && !b.is_empty()
                && b.bytes().all(|x| x.is_ascii_alphanumeric())
        }
        None => false,
    }
}

/// The stray folder by name, only when it is a plain directory directly in sluicy's stray folder.
pub fn stray_dir(c: &Cfg, name: &str) -> Option<PathBuf> {
    if !stray_name_ok(name) {
        return None;
    }
    let root = fs::canonicalize(c.sluicy_home.join("stray")).ok()?;
    let d = c.sluicy_home.join("stray").join(name);
    let meta = fs::symlink_metadata(&d).ok()?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return None;
    }
    (fs::canonicalize(&d).ok()?.parent()? == root).then_some(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stray_names() {
        assert!(stray_name_ok("57-1793835966"));
        assert!(stray_name_ok("57-manual"));
        for bad in ["", "57", "-1", "57-", "../x", "57-1/..", "a-1", "57-1.2", "57-a b"] {
            assert!(!stray_name_ok(bad), "{bad}");
        }
    }
}
