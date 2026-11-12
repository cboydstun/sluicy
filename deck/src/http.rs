//! A minimal HTTP/1.1 server on std: one thread per connection, one request per connection
//! (`Connection: close`). Binds 127.0.0.1 only.
//! Every request needs the token; POSTs also need a matching Origin and Host.

use crate::deck::Deck;
use crate::util;
use serde_json::{json, Map, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::time::Duration;

const PAGE: &str = include_str!("../index.html");

struct Request {
    method: String,
    path: String,
    query: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    fn query_param(&self, name: &str) -> Option<String> {
        self.query.split('&').find_map(|kv| {
            let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
            (k == name).then(|| pct_decode(v))
        })
    }
}

fn pct_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < b.len() => {
                let hex = |c: u8| (c as char).to_digit(16);
                match (hex(b[i + 1]), hex(b[i + 2])) {
                    (Some(h), Some(l)) => {
                        out.push((h * 16 + l) as u8);
                        i += 2;
                    }
                    _ => out.push(b'%'),
                }
            }
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn read_request(stream: &TcpStream) -> Option<Request> {
    let mut r = BufReader::new(stream.try_clone().ok()?);
    let mut line = String::new();
    r.read_line(&mut line).ok()?;
    let mut parts = line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?.to_string();
    let mut headers = Vec::new();
    loop {
        let mut h = String::new();
        if r.read_line(&mut h).ok()? == 0 {
            break;
        }
        let h = h.trim_end_matches(['\r', '\n']);
        if h.is_empty() {
            break;
        }
        if let Some((k, v)) = h.split_once(':') {
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
        if headers.len() > 100 {
            return None;
        }
    }
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (target, String::new()),
    };
    let mut req = Request {
        method,
        path,
        query,
        headers,
        body: Vec::new(),
    };
    let len: usize = req
        .header("Content-Length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let len = len.min(1 << 20);
    if len > 0 {
        let mut body = vec![0u8; len];
        r.read_exact(&mut body).ok()?;
        req.body = body;
    }
    Some(req)
}

fn reason(code: u16) -> &'static str {
    match code {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        _ => "Internal Server Error",
    }
}

fn send(mut s: &TcpStream, code: u16, body: &[u8], ctype: &str) {
    let head = format!(
        "HTTP/1.1 {code} {}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n",
        reason(code),
        body.len()
    );
    let _ = s
        .write_all(head.as_bytes())
        .and_then(|_| s.write_all(body))
        .and_then(|_| s.flush());
}

fn send_json(s: &TcpStream, code: u16, v: &Value) {
    send(
        s,
        code,
        &serde_json::to_vec(v).unwrap_or_default(),
        "application/json",
    );
}

struct Access {
    token: String,
    hosts: [String; 2],
    origins: [String; 2],
}

impl Access {
    fn authorised(&self, req: &Request, post: bool) -> bool {
        let Some(host) = req.header("Host") else {
            return false;
        };
        if !self.hosts.iter().any(|h| h == host) {
            return false;
        }
        let tok = req
            .header("X-Deck-Token")
            .map(String::from)
            .or_else(|| req.query_param("t"))
            .unwrap_or_default();
        if !util::ct_eq(tok.as_bytes(), self.token.as_bytes()) {
            return false;
        }
        if post
            && !req
                .header("Origin")
                .map(|o| self.origins.iter().any(|a| a == o))
                .unwrap_or(false)
        {
            return false;
        }
        true
    }
}

fn stream_term(s: &TcpStream, deck: &Deck, key: &str) {
    let Some(t) = deck.term(key) else {
        return send_json(s, 404, &json!({"error": "no terminal"}));
    };
    let mut w = s;
    if w.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").is_err() {
        return;
    }
    t.buf.lock().unwrap().readers += 1;
    let mut offset = 0u64;
    loop {
        // A ping every 5 s finds a closed page quickly.
        let (start, data, closed) = t.read_from(offset, Duration::from_secs(5));
        let r = if !data.is_empty() {
            offset = start + data.len() as u64;
            w.write_all(format!("data: {}\n\n", util::base64(&data)).as_bytes())
        } else if closed {
            let _ = w.write_all(b"event: closed\ndata: \n\n");
            break;
        } else {
            w.write_all(b": ping\n\n")
        };
        if r.and_then(|_| w.flush()).is_err() {
            break;
        }
    }
    let mut b = t.buf.lock().unwrap();
    b.readers -= 1;
    b.last_reader = std::time::Instant::now();
}

fn handle(stream: TcpStream, deck: &Deck, access: &Access) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let Some(req) = read_request(&stream) else {
        return;
    };
    match req.method.as_str() {
        "GET" => {
            if !access.authorised(&req, false) {
                return send_json(&stream, 403, &json!({"error": "forbidden"}));
            }
            if req.path == "/" {
                return send(&stream, 200, PAGE.as_bytes(), "text/html; charset=utf-8");
            }
            if req.path == "/api/state" {
                let st = deck.state();
                return send_json(&stream, 200, &st);
            }
            if let Some(key) = req
                .path
                .strip_prefix("/api/term/")
                .and_then(|r| r.strip_suffix("/stream"))
            {
                if key.len() == 12
                    && key
                        .bytes()
                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                {
                    let _ = stream.set_write_timeout(Some(Duration::from_secs(30)));
                    return stream_term(&stream, deck, key);
                }
            }
            send_json(&stream, 404, &json!({"error": "not found"}))
        }
        "POST" => {
            if !access.authorised(&req, true) {
                return send_json(&stream, 403, &json!({"error": "forbidden"}));
            }
            let Some(name) = req.path.strip_prefix("/api/action/").filter(|n| {
                !n.is_empty() && n.bytes().all(|c| c.is_ascii_lowercase() || c == b'_')
            }) else {
                return send_json(&stream, 404, &json!({"error": "not found"}));
            };
            let body: Value = if req.body.is_empty() {
                json!({})
            } else {
                match serde_json::from_slice(&req.body) {
                    Ok(v) => v,
                    Err(_) => return send_json(&stream, 400, &json!({"error": "bad json"})),
                }
            };
            let empty = Map::new();
            let obj = body.as_object().unwrap_or(&empty);
            let (code, out) = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                deck.act(name, obj)
            })) {
                Ok(r) => r,
                Err(p) => {
                    let msg = p
                        .downcast_ref::<String>()
                        .cloned()
                        .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()))
                        .unwrap_or_else(|| "panic".into());
                    (500, json!({"error": msg}))
                }
            };
            send_json(&stream, code, &out)
        }
        _ => send_json(&stream, 404, &json!({"error": "not found"})),
    }
}

pub fn serve(port: u16, token: String, deck: Arc<Deck>) -> std::io::Result<()> {
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let hosts = [format!("127.0.0.1:{port}"), format!("localhost:{port}")];
    let origins = [
        format!("http://{}", hosts[0]),
        format!("http://{}", hosts[1]),
    ];
    let access = Arc::new(Access {
        token,
        hosts,
        origins,
    });
    for conn in listener.incoming() {
        let Ok(stream) = conn else { continue };
        let deck = deck.clone();
        let access = access.clone();
        std::thread::Builder::new()
            .stack_size(256 * 1024)
            .spawn(move || handle(stream, &deck, &access))
            .ok();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::pct_decode;

    #[test]
    fn decodes_query() {
        assert_eq!(pct_decode("abc"), "abc");
        assert_eq!(pct_decode("a%2Fb"), "a/b");
        assert_eq!(pct_decode("a%zz"), "a%zz");
        assert_eq!(pct_decode("a%"), "a%");
        assert_eq!(pct_decode("tok-xx_"), "tok-xx_");
    }
}
