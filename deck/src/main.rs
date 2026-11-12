//! Session Deck, in Rust: concurrent Claude sessions, the sluicy queue and the laptop.
//!
//!     deck tui                       in this terminal, no browser (what `npm run deck` runs)
//!     deck [--port 8765] [--no-open] the web page, served on 127.0.0.1
//!
//! The page binds 127.0.0.1, and every request needs the random token printed in the URL.
//! Both front ends call the same Deck::state() and Deck::act().

mod cleanup;
mod collect;
mod deck;
mod gitfiles;
mod http;
mod procs;
mod term;
mod tui;
mod util;

use std::sync::Arc;
use std::time::Duration;

fn main() {
    if std::env::args().nth(1).as_deref() == Some("tui") {
        let deck = Arc::new(deck::Deck::new(collect::Cfg::from_env()));
        if let Err(e) = tui::run(deck) {
            eprintln!("deck tui: {e}");
            std::process::exit(1);
        }
        return;
    }
    serve();
}

fn serve() {
    let mut port: u16 = std::env::var("DECK_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8765);
    let mut open = true;
    let mut token = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--port" => {
                port = args
                    .next()
                    .and_then(|p| p.parse().ok())
                    .unwrap_or_else(|| usage())
            }
            "--no-open" => open = false,
            // For tests only: a fixed token instead of a fresh random one.
            "--token" => token = args.next(),
            "-h" | "--help" => usage(),
            _ => usage(),
        }
    }
    // A closed browser socket must not kill the deck. SIGINT/SIGTERM are blocked in every thread
    // (set before any thread starts) and taken by one sigwait thread below; std::process resets
    // the mask and SIGPIPE in each child, so `claude`, git and ssh start with defaults.
    let mut stop_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        libc::sigemptyset(&mut stop_set);
        libc::sigaddset(&mut stop_set, libc::SIGINT);
        libc::sigaddset(&mut stop_set, libc::SIGTERM);
        libc::pthread_sigmask(libc::SIG_BLOCK, &stop_set, std::ptr::null_mut());
    }
    let token = token.unwrap_or_else(|| util::token_urlsafe(24));
    let deck = Arc::new(deck::Deck::new(collect::Cfg::from_env()));

    let reaper = deck.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs_f64(
            (reaper.c.term_idle / 4.0).clamp(1.0, 30.0),
        ));
        reaper.reap_terminals();
    });

    let url = format!("http://127.0.0.1:{port}/?t={token}");
    println!("Session Deck: {url}\n(Ctrl-C to stop; the token changes on every start)");
    if open {
        let _ = std::process::Command::new("xdg-open")
            .arg(&url)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
    }

    // Ctrl-C / SIGTERM: hang up the attached terminals before leaving.
    let closer = deck.clone();
    std::thread::spawn(move || {
        let mut sig = 0;
        unsafe { libc::sigwait(&stop_set, &mut sig) };
        for (_, t) in closer.term_items() {
            t.close();
        }
        std::process::exit(0);
    });

    if let Err(e) = http::serve(port, token, deck) {
        eprintln!("deck: cannot listen on 127.0.0.1:{port}: {e}");
        std::process::exit(1);
    }
}

fn usage() -> ! {
    eprintln!("usage: deck tui | deck [--port 8765] [--no-open]");
    std::process::exit(2);
}
