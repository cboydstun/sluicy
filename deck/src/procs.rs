//! /proc snapshots. Each refresh reads every process's `stat` once; the command line is read only
//! for the rows the page shows, which is where most of a refresh's CPU went.

use std::collections::HashMap;
use std::fs;
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct Proc {
    pub pid: i32,
    pub comm: String,
    pub ppid: i32,
    pub ticks: u64,
    pub start: u64,
    pub rss: u64,
    pub uid: u32,
    pub cpu: f64,
}

struct Baseline {
    procs: HashMap<i32, (u64, u64, Instant)>,
    cpu: Option<(u64, u64)>,
}

static BASELINE: Mutex<Option<Baseline>> = Mutex::new(None);

fn page_size() -> u64 {
    unsafe { libc::sysconf(libc::_SC_PAGESIZE) as u64 }
}

fn clk_tck() -> f64 {
    unsafe { libc::sysconf(libc::_SC_CLK_TCK) as f64 }
}

pub fn my_uid() -> u32 {
    unsafe { libc::getuid() }
}

pub struct Procs {
    pub procs: HashMap<i32, Proc>,
    pub children: HashMap<i32, Vec<i32>>,
}

/// Parse `/proc/<pid>/stat`. The comm sits in parentheses and may itself hold spaces or `)`.
pub fn parse_stat(pid: i32, raw: &str, page: u64) -> Option<Proc> {
    let open = raw.find('(')?;
    let close = raw.rfind(')')?;
    let comm = raw[open + 1..close].to_string();
    let f: Vec<&str> = raw.get(close + 2..)?.split_whitespace().collect();
    if f.len() < 22 {
        return None;
    }
    Some(Proc {
        pid,
        comm,
        ppid: f[1].parse().ok()?,
        ticks: f[11].parse::<u64>().ok()? + f[12].parse::<u64>().ok()?,
        start: f[19].parse().ok()?,
        rss: f[21].parse::<u64>().ok()? * page,
        uid: 0,
        cpu: 0.0,
    })
}

impl Procs {
    /// This user's processes — all a refresh shows (session trees, the top list, groups).
    /// `record = false` reads without moving the CPU baseline the refresh table measures from.
    pub fn new(record: bool) -> Procs {
        let page = page_size();
        let hz = clk_tck();
        let uid = my_uid();
        let mut procs = HashMap::new();
        let now = Instant::now();
        if let (Ok(rd), Ok(dir)) = (fs::read_dir("/proc"), fs::File::open("/proc")) {
            use std::os::fd::AsRawFd;
            for e in rd.flatten() {
                let name = e.file_name();
                let Some(pid) = name.to_str().and_then(|s| s.parse::<i32>().ok()) else {
                    continue;
                };
                if let Some(p) = read_one(dir.as_raw_fd(), pid, page, Some(uid)) {
                    procs.insert(pid, p);
                }
            }
        }
        let mut guard = BASELINE.lock().unwrap();
        let base = guard.get_or_insert_with(|| Baseline {
            procs: HashMap::new(),
            cpu: None,
        });
        for p in procs.values_mut() {
            p.cpu = match base.procs.get(&p.pid) {
                Some(&(start, ticks, at)) if start == p.start && now > at => {
                    let dt = now.duration_since(at).as_secs_f64();
                    crate::util::round1(100.0 * (p.ticks.saturating_sub(ticks)) as f64 / hz / dt)
                }
                _ => 0.0,
            };
        }
        if record {
            base.procs = procs
                .values()
                .map(|p| (p.pid, (p.start, p.ticks, now)))
                .collect();
        }
        drop(guard);
        let mut children: HashMap<i32, Vec<i32>> = HashMap::new();
        for p in procs.values() {
            children.entry(p.ppid).or_default().push(p.pid);
        }
        Procs { procs, children }
    }

    /// One process of any user, for proc_info and kill. Leaves the CPU baseline alone.
    pub fn one(pid: i32) -> Option<Proc> {
        use std::os::fd::AsRawFd;
        let dir = fs::File::open("/proc").ok()?;
        read_one(dir.as_raw_fd(), pid, page_size(), None)
    }

    pub fn tree(&self, pid: i32) -> Vec<&Proc> {
        let mut out = Vec::new();
        let mut todo = vec![pid];
        while let Some(p) = todo.pop() {
            if let Some(pr) = self.procs.get(&p) {
                out.push(pr);
                if let Some(c) = self.children.get(&p) {
                    todo.extend(c);
                }
            }
        }
        out
    }
}

/// One process: `openat(<pid>/stat)` relative to the held /proc fd, one read into a stack buffer,
/// and `fstat` on the same fd for the owner (the same uid /proc gives the `<pid>` directory).
/// Three syscalls instead of a path walk for the file and another for the directory.
fn read_one(proc_fd: libc::c_int, pid: i32, page: u64, only_uid: Option<u32>) -> Option<Proc> {
    let mut path = [0u8; 24];
    if let Some(uid) = only_uid {
        // The owner from the directory inode costs no stat text: skip other users' processes
        // (kernel threads, root daemons) before the kernel formats their stat line.
        let dir = format!("{pid}");
        path[..dir.len()].copy_from_slice(dir.as_bytes());
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstatat(proc_fd, path.as_ptr() as *const libc::c_char, &mut st, 0) } != 0
            || st.st_uid != uid
        {
            return None;
        }
    }
    let rel = format!("{pid}/stat");
    path[..rel.len()].copy_from_slice(rel.as_bytes());
    path[rel.len()] = 0;
    let fd = unsafe {
        libc::openat(
            proc_fd,
            path.as_ptr() as *const libc::c_char,
            libc::O_RDONLY | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return None;
    }
    let mut buf = [0u8; 1024];
    let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    let stat_ok = unsafe { libc::fstat(fd, &mut st) } == 0;
    unsafe { libc::close(fd) };
    if n <= 0 || !stat_ok {
        return None;
    }
    let raw = std::str::from_utf8(&buf[..n as usize]).ok()?;
    let mut p = parse_stat(pid, raw, page)?;
    p.uid = st.st_uid;
    Some(p)
}

pub fn cmdline(pid: i32, comm: &str) -> String {
    let raw = fs::read(format!("/proc/{pid}/cmdline")).unwrap_or_default();
    let s: Vec<u8> = raw.iter().map(|&b| if b == 0 { b' ' } else { b }).collect();
    let s = String::from_utf8_lossy(&s).trim().to_string();
    if s.is_empty() {
        format!("[{comm}]")
    } else {
        crate::util::head(&s, 300)
    }
}

pub fn cwd(pid: i32) -> String {
    fs::read_link(format!("/proc/{pid}/cwd"))
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Whole-box CPU % since the previous call.
pub fn cpu_total() -> Option<f64> {
    let raw = fs::read_to_string("/proc/stat").ok()?;
    let f: Vec<u64> = raw
        .lines()
        .next()?
        .split_whitespace()
        .skip(1)
        .filter_map(|x| x.parse().ok())
        .collect();
    if f.len() < 4 {
        return None;
    }
    let total: u64 = f.iter().sum();
    let idle = f[3] + f.get(4).copied().unwrap_or(0);
    let mut guard = BASELINE.lock().unwrap();
    let base = guard.get_or_insert_with(|| Baseline {
        procs: HashMap::new(),
        cpu: None,
    });
    let prev = base.cpu.replace((total, idle));
    let (pt, pi) = prev?;
    if total == pt {
        return None;
    }
    Some(crate::util::round1(
        100.0 * (1.0 - (idle.saturating_sub(pi)) as f64 / (total - pt) as f64),
    ))
}

pub fn meminfo() -> HashMap<String, u64> {
    let mut m = HashMap::new();
    if let Ok(raw) = fs::read_to_string("/proc/meminfo") {
        for line in raw.lines() {
            if let Some((k, v)) = line.split_once(':') {
                if let Some(n) = v
                    .split_whitespace()
                    .next()
                    .and_then(|x| x.parse::<u64>().ok())
                {
                    m.insert(k.to_string(), n * 1024);
                }
            }
        }
    }
    m
}

#[derive(Clone)]
pub struct SwapHolder {
    pub pid: i32,
    pub comm: String,
    pub swap: u64,
}

static SWAP_CACHE: Mutex<Option<(Instant, Vec<SwapHolder>)>> = Mutex::new(None);

/// The processes whose pages sit in swap, any user. Swap stays used until a page is touched again,
/// so a full swap with free memory is idle pages, not pressure. Cached `ttl`: it reads every status.
pub fn swap_holders(limit: usize, ttl: Duration) -> Vec<SwapHolder> {
    let mut guard = SWAP_CACHE.lock().unwrap();
    if let Some((at, data)) = guard.as_ref() {
        if at.elapsed() < ttl {
            return data.clone();
        }
    }
    let mut out = Vec::new();
    if let Ok(rd) = fs::read_dir("/proc") {
        for e in rd.flatten() {
            let name = e.file_name();
            let Some(pid) = name.to_str().and_then(|s| s.parse::<i32>().ok()) else {
                continue;
            };
            let Ok(text) = fs::read_to_string(format!("/proc/{pid}/status")) else {
                continue;
            };
            let mut comm = "?".to_string();
            let mut swap = 0u64;
            for line in text.lines() {
                if let Some(v) = line.strip_prefix("Name:") {
                    comm = v.trim().to_string();
                } else if let Some(v) = line.strip_prefix("VmSwap:") {
                    swap = v
                        .split_whitespace()
                        .next()
                        .and_then(|x| x.parse().ok())
                        .unwrap_or(0)
                        * 1024;
                }
            }
            if swap > 0 {
                out.push(SwapHolder { pid, comm, swap });
            }
        }
    }
    out.sort_by(|a, b| b.swap.cmp(&a.swap));
    out.truncate(limit);
    *guard = Some((Instant::now(), out.clone()));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stat_with_odd_comm() {
        let raw = "42 (a) b (c)) S 7 42 42 0 -1 4194560 100 0 0 0 30 12 0 0 20 0 1 0 9999 1000 25 18446744073709551615";
        let p = parse_stat(42, raw, 4096).unwrap();
        assert_eq!(p.comm, "a) b (c)");
        assert_eq!(p.ppid, 7);
        assert_eq!(p.ticks, 42);
        assert_eq!(p.start, 9999);
        assert_eq!(p.rss, 25 * 4096);
    }

    #[test]
    fn lookup_keeps_the_cpu_baseline() {
        Procs::new(true);
        let before = BASELINE.lock().unwrap().as_ref().unwrap().procs.len();
        let me = std::process::id() as i32;
        let at = BASELINE
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .procs
            .get(&me)
            .map(|x| x.2);
        Procs::new(false);
        let g = BASELINE.lock().unwrap();
        let b = g.as_ref().unwrap();
        assert_eq!(b.procs.len(), before);
        assert_eq!(b.procs.get(&me).map(|x| x.2), at);
    }

    #[test]
    fn sees_self() {
        let p = Procs::new(false);
        let me = p.procs.get(&(std::process::id() as i32)).unwrap();
        assert!(me.rss > 0);
        assert_eq!(me.uid, my_uid());
        assert!(!cmdline(me.pid, &me.comm).is_empty());
    }
}
