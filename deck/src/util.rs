//! Small helpers: running a fixed argv with a timeout, git, encoding, text trimming.

use serde_json::Value;
use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

/// (exit code, stdout, stderr). 124 means it could not start or ran past `timeout`.
pub type RunOut = (i32, String, String);

pub fn run(argv: &[String], cwd: Option<&Path>, timeout: Duration) -> RunOut {
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_INDEX_FILE");
    if let Some(d) = cwd {
        cmd.current_dir(d);
    }
    let child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return (124, String::new(), e.to_string()),
    };
    let pid = child.id() as libc::pid_t;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });
    let res = match rx.recv_timeout(timeout) {
        Ok(r) => r,
        Err(_) => {
            unsafe { libc::kill(pid, libc::SIGKILL) };
            let _ = rx.recv_timeout(Duration::from_secs(2));
            return (
                124,
                String::new(),
                format!("timed out after {}s", timeout.as_secs()),
            );
        }
    };
    match res {
        Ok(o) => (
            o.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&o.stdout).into_owned(),
            String::from_utf8_lossy(&o.stderr).into_owned(),
        ),
        Err(e) => (124, String::new(), e.to_string()),
    }
}

pub fn argv(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|s| s.to_string()).collect()
}

/// Per-refresh (or per-TTL) memo of git answers, keyed by cwd + args.
pub type GitMemo = HashMap<(String, Vec<String>), Option<String>>;

pub fn git(cwd: &Path, args: &[&str], memo: Option<&mut GitMemo>) -> Option<String> {
    let key = (
        cwd.to_string_lossy().into_owned(),
        args.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
    );
    if let Some(m) = &memo {
        if let Some(v) = m.get(&key) {
            return v.clone();
        }
    }
    let mut a = vec!["git".to_string()];
    a.extend(key.1.iter().cloned());
    let (rc, out, _) = run(&a, Some(cwd), Duration::from_secs(15));
    let res = if rc == 0 {
        Some(out.trim().to_string())
    } else {
        None
    };
    if let Some(m) = memo {
        m.insert(key, res.clone());
    }
    res
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64URL: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn b64_with(data: &[u8], table: &[u8; 64], pad: bool) -> String {
    let mut out = Vec::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let n = match chunk.len() {
            3 => ((chunk[0] as u32) << 16) | ((chunk[1] as u32) << 8) | chunk[2] as u32,
            2 => ((chunk[0] as u32) << 16) | ((chunk[1] as u32) << 8),
            _ => (chunk[0] as u32) << 16,
        };
        out.push(table[(n >> 18) as usize & 63]);
        out.push(table[(n >> 12) as usize & 63]);
        if chunk.len() > 1 {
            out.push(table[(n >> 6) as usize & 63]);
        } else if pad {
            out.push(b'=');
        }
        if chunk.len() > 2 {
            out.push(table[n as usize & 63]);
        } else if pad {
            out.push(b'=');
        }
    }
    String::from_utf8(out).unwrap()
}

pub fn base64(data: &[u8]) -> String {
    b64_with(data, B64, true)
}

pub fn random_bytes(n: usize) -> Vec<u8> {
    let mut buf = vec![0u8; n];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut buf))
        .expect("read /dev/urandom");
    buf
}

/// Same shape as Python's secrets.token_urlsafe(n): n random bytes, url-safe base64, no padding.
pub fn token_urlsafe(n: usize) -> String {
    b64_with(&random_bytes(n), B64URL, false)
}

pub fn token_hex(n: usize) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    random_bytes(n)
        .iter()
        .flat_map(|b| {
            [
                HEX[(b >> 4) as usize] as char,
                HEX[(b & 15) as usize] as char,
            ]
        })
        .collect()
}

pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// The first `n` characters (not bytes).
pub fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// The last `n` characters (not bytes).
pub fn tail_chars(s: &str, n: usize) -> String {
    let count = s.chars().count();
    if count <= n {
        return s.to_string();
    }
    s.chars().skip(count - n).collect()
}

/// Drop `ESC [ <digits;> <letter>` colour/cursor sequences.
pub fn strip_ansi(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == 0x1b && i + 1 < b.len() && b[i + 1] == b'[' {
            let mut j = i + 2;
            while j < b.len() && (b[j].is_ascii_digit() || b[j] == b';') {
                j += 1;
            }
            if j < b.len() && b[j].is_ascii_alphabetic() {
                i = j + 1;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Claude's per-project transcript folder name: every non-alphanumeric character becomes `-`.
pub fn project_dir_name(cwd: &str) -> String {
    cwd.chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect()
}

/// The last `max` bytes of a file, as (lossy) text.
pub fn read_tail(path: &Path, max: u64) -> Option<String> {
    let mut f = File::open(path).ok()?;
    let size = f.metadata().ok()?.len();
    f.seek(SeekFrom::Start(size.saturating_sub(max))).ok()?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf).ok()?;
    Some(String::from_utf8_lossy(&buf).into_owned())
}

/// Python's str.splitlines() for the separators that matter here.
pub fn lines(s: &str) -> Vec<&str> {
    s.lines()
        .map(|l| l.strip_suffix('\r').unwrap_or(l))
        .collect()
}

/// A JSON value as an integer, accepting numbers and numeric strings as int() does.
pub fn as_int(v: Option<&Value>) -> Option<i64> {
    match v? {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse().ok(),
        Value::Bool(b) => Some(*b as i64),
        _ => None,
    }
}

/// str(value) as Python would print it for the scalars bodies carry.
pub fn as_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".into()
            } else {
                "False".into()
            }
        }
        Some(other) => other.to_string(),
    }
}

pub fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

pub fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn mtime_secs(md: &std::fs::Metadata) -> f64 {
    use std::os::unix::fs::MetadataExt;
    md.mtime() as f64 + md.mtime_nsec() as f64 / 1e9
}

pub fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_python() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"hello deck\r\n"), "aGVsbG8gZGVjaw0K");
        assert_eq!(token_urlsafe(24).len(), 32);
        assert_eq!(token_hex(6).len(), 12);
    }

    #[test]
    fn ansi_and_chars() {
        assert_eq!(strip_ansi("\x1b[0;32mgate ok\x1b[0m\n"), "gate ok\n");
        assert_eq!(strip_ansi("a\x1b[2Jb"), "ab");
        assert_eq!(tail_chars("héllo", 3), "llo");
        assert_eq!(head("héllo", 2), "hé");
        assert_eq!(project_dir_name("/home/a.b/c_d"), "-home-a-b-c-d");
    }

    #[test]
    fn run_times_out() {
        let t = std::time::Instant::now();
        let (rc, _, _) = run(&argv(&["sleep", "5"]), None, Duration::from_millis(200));
        assert_eq!(rc, 124);
        assert!(t.elapsed() < Duration::from_secs(3));
        assert_eq!(run(&argv(&["true"]), None, Duration::from_secs(5)).0, 0);
        assert_eq!(
            run(
                &argv(&["/nonexistent-binary"]),
                None,
                Duration::from_secs(5)
            )
            .0,
            124
        );
    }
}
