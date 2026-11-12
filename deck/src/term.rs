//! Terminals: a pty running `claude attach <id>` (or the queue status loop), buffered so a page
//! can stream it from any offset.

use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

const MAX_BUF: usize = 1 << 20;
const KEEP_BUF: usize = 1 << 19;

pub struct Buf {
    pub data: Vec<u8>,
    /// Byte offset of data[0] in the whole stream.
    pub base: u64,
    pub closed: bool,
    /// Open SSE streams.
    pub readers: usize,
    pub last_reader: Instant,
}

pub struct Terminal {
    pub session: String,
    pub readonly: bool,
    pid: libc::pid_t,
    master: File,
    pub buf: Mutex<Buf>,
    pub cond: Condvar,
}

impl Terminal {
    pub fn spawn(
        argv: &[String],
        cwd: &Path,
        session: &str,
        readonly: bool,
    ) -> std::io::Result<Arc<Terminal>> {
        let mut master_fd: libc::c_int = -1;
        let mut slave_fd: libc::c_int = -1;
        let rc = unsafe {
            libc::openpty(
                &mut master_fd,
                &mut slave_fd,
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null(),
            )
        };
        if rc != 0 {
            return Err(std::io::Error::last_os_error());
        }
        unsafe { libc::fcntl(master_fd, libc::F_SETFD, libc::FD_CLOEXEC) };
        let slave = unsafe { OwnedFd::from_raw_fd(slave_fd) };
        let master = unsafe { File::from_raw_fd(master_fd) };
        let mut cmd = Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .current_dir(cwd)
            .stdin(Stdio::from(slave.try_clone()?))
            .stdout(Stdio::from(slave.try_clone()?))
            .stderr(Stdio::from(slave));
        if std::env::var_os("TERM").is_none() {
            cmd.env("TERM", "xterm-256color");
        }
        unsafe {
            // A new session with the pty as its controlling terminal,, as pty.fork() gives.
            cmd.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = cmd.spawn()?;
        let pid = child.id() as libc::pid_t;
        let t = Arc::new(Terminal {
            session: session.to_string(),
            readonly,
            pid,
            master,
            buf: Mutex::new(Buf {
                data: Vec::new(),
                base: 0,
                closed: false,
                readers: 0,
                last_reader: Instant::now(),
            }),
            cond: Condvar::new(),
        });
        let pump = t.clone();
        std::thread::spawn(move || {
            let mut reader = match pump.master.try_clone() {
                Ok(f) => f,
                Err(_) => return,
            };
            let mut chunk = vec![0u8; 65536];
            loop {
                let n = reader.read(&mut chunk).unwrap_or(0);
                let mut b = pump.buf.lock().unwrap();
                if n == 0 {
                    b.closed = true;
                    pump.cond.notify_all();
                    break;
                }
                b.data.extend_from_slice(&chunk[..n]);
                if b.data.len() > MAX_BUF {
                    let cut = b.data.len() - KEEP_BUF;
                    b.data.drain(..cut);
                    b.base += cut as u64;
                }
                pump.cond.notify_all();
            }
            let _ = child.wait();
        });
        Ok(t)
    }

    pub fn closed(&self) -> bool {
        self.buf.lock().unwrap().closed
    }

    /// Seconds since a stream last read this terminal; 0 while one is connected.
    pub fn idle_for(&self) -> f64 {
        let b = self.buf.lock().unwrap();
        if b.readers > 0 {
            0.0
        } else {
            b.last_reader.elapsed().as_secs_f64()
        }
    }

    pub fn touch(&self) {
        self.buf.lock().unwrap().last_reader = Instant::now();
    }

    /// (start offset, bytes from there, closed). Waits up to `wait` when nothing new is buffered.
    pub fn read_from(&self, offset: u64, wait: Duration) -> (u64, Vec<u8>, bool) {
        let mut b = self.buf.lock().unwrap();
        if offset >= b.base + b.data.len() as u64 && !b.closed {
            b = self.cond.wait_timeout(b, wait).unwrap().0;
        }
        let start = offset.max(b.base);
        let from = ((start - b.base) as usize).min(b.data.len());
        (start, b.data[from..].to_vec(), b.closed)
    }

    pub fn write(&self, data: &[u8]) -> std::io::Result<()> {
        if self.closed() {
            return Err(std::io::Error::other("terminal closed"));
        }
        (&self.master).write_all(data)
    }

    pub fn resize(&self, cols: u16, rows: u16) {
        if self.closed() {
            return;
        }
        let ws = libc::winsize {
            ws_row: rows,
            ws_col: cols,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        unsafe { libc::ioctl(self.master.as_raw_fd(), libc::TIOCSWINSZ, &ws) };
    }

    pub fn close(&self) {
        unsafe { libc::kill(self.pid, libc::SIGHUP) };
    }
}
