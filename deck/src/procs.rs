//! Process and memory snapshots. On Linux each refresh reads every process's `/proc/<pid>/stat` once;
//! on macOS (no /proc) the same figures come from libproc, sysctl and the Mach host statistics (the
//! `mac` module). The command line is read only for the rows the page shows, which is where most of a
//! refresh's CPU went.

use std::collections::HashMap;
#[cfg(not(target_os = "macos"))]
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

/// Units of `Proc::ticks` per second: clock ticks from /proc on Linux, nanoseconds on macOS.
#[cfg(not(target_os = "macos"))]
fn clk_tck() -> f64 {
    unsafe { libc::sysconf(libc::_SC_CLK_TCK) as f64 }
}

#[cfg(target_os = "macos")]
fn clk_tck() -> f64 {
    1e9
}

pub fn my_uid() -> u32 {
    unsafe { libc::getuid() }
}

pub struct Procs {
    pub procs: HashMap<i32, Proc>,
    pub children: HashMap<i32, Vec<i32>>,
}

/// Parse `/proc/<pid>/stat`. The comm sits in parentheses and may itself hold spaces or `)`.
#[cfg_attr(target_os = "macos", allow(dead_code))] // Linux's reader; its test runs everywhere
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
        let now = Instant::now();
        let mut procs = all_procs(page, uid);
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
    #[cfg(target_os = "macos")]
    pub fn one(pid: i32) -> Option<Proc> {
        mac::one(pid)
    }

    #[cfg(not(target_os = "macos"))]
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

/// Every process of `uid`, from /proc.
#[cfg(not(target_os = "macos"))]
fn all_procs(page: u64, uid: u32) -> HashMap<i32, Proc> {
    let mut procs = HashMap::new();
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
    procs
}

#[cfg(target_os = "macos")]
fn all_procs(_page: u64, uid: u32) -> HashMap<i32, Proc> {
    mac::all(uid)
}

/// One process: `openat(<pid>/stat)` relative to the held /proc fd, one read into a stack buffer,
/// and `fstat` on the same fd for the owner (the same uid /proc gives the `<pid>` directory).
/// Three syscalls instead of a path walk for the file and another for the directory.
#[cfg(not(target_os = "macos"))]
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

/// The argv, NUL-separated (the layout of /proc/<pid>/cmdline).
#[cfg(not(target_os = "macos"))]
fn raw_cmdline(pid: i32) -> Vec<u8> {
    fs::read(format!("/proc/{pid}/cmdline")).unwrap_or_default()
}

#[cfg(target_os = "macos")]
fn raw_cmdline(pid: i32) -> Vec<u8> {
    mac::cmdline(pid)
}

pub fn cmdline(pid: i32, comm: &str) -> String {
    let raw = raw_cmdline(pid);
    let s: Vec<u8> = raw.iter().map(|&b| if b == 0 { b' ' } else { b }).collect();
    let s = String::from_utf8_lossy(&s).trim().to_string();
    if s.is_empty() {
        format!("[{comm}]")
    } else {
        crate::util::head(&s, 300)
    }
}

#[cfg(target_os = "macos")]
pub fn cwd(pid: i32) -> String {
    mac::cwd(pid)
}

#[cfg(not(target_os = "macos"))]
pub fn cwd(pid: i32) -> String {
    fs::read_link(format!("/proc/{pid}/cwd"))
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// (total, idle) CPU ticks since boot, all cores.
#[cfg(not(target_os = "macos"))]
fn cpu_ticks() -> Option<(u64, u64)> {
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
    Some((f.iter().sum(), f[3] + f.get(4).copied().unwrap_or(0)))
}

#[cfg(target_os = "macos")]
fn cpu_ticks() -> Option<(u64, u64)> {
    mac::cpu_ticks()
}

/// Whole-box CPU % since the previous call.
pub fn cpu_total() -> Option<f64> {
    let (total, idle) = cpu_ticks()?;
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

/// /proc/meminfo's figures in bytes, keyed as there. Empty when unknown: callers must not read
/// a missing MemTotal as "no memory free".
#[cfg(target_os = "macos")]
pub fn meminfo() -> HashMap<String, u64> {
    mac::meminfo()
}

#[cfg(not(target_os = "macos"))]
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
    let mut out: Vec<SwapHolder> = Vec::new();
    // macOS keeps no per-process swap figure (its swap is the compressor's overflow): none listed.
    #[cfg(not(target_os = "macos"))]
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

/// macOS has no /proc: processes from libproc (`proc_listallpids`, `proc_pidinfo`), the argv from
/// `sysctl(KERN_PROCARGS2)`, memory from sysctl and CPU from the Mach host statistics. Same shapes
/// as the Linux readers, so nothing above this module knows which OS it is on.
#[cfg(target_os = "macos")]
#[allow(deprecated)] // libc points mach_host_self and friends at the mach2 crate; one call is not worth a dependency
mod mac {
    use super::Proc;
    use std::collections::HashMap;
    use std::ffi::CString;
    use std::mem::{size_of, zeroed};
    use std::sync::OnceLock;

    /// Mach absolute-time units -> nanoseconds (1/1 on Intel, 125/3 on Apple silicon).
    fn timebase() -> (u64, u64) {
        static TB: OnceLock<(u64, u64)> = OnceLock::new();
        *TB.get_or_init(|| {
            let mut tb: libc::mach_timebase_info = unsafe { zeroed() };
            if unsafe { libc::mach_timebase_info(&mut tb) } != 0 || tb.denom == 0 {
                return (1, 1);
            }
            (tb.numer as u64, tb.denom as u64)
        })
    }

    fn task_all(pid: i32) -> Option<libc::proc_taskallinfo> {
        let mut info: libc::proc_taskallinfo = unsafe { zeroed() };
        let size = size_of::<libc::proc_taskallinfo>() as libc::c_int;
        let n = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDTASKALLINFO,
                0,
                &mut info as *mut _ as *mut libc::c_void,
                size,
            )
        };
        (n == size).then_some(info)
    }

    fn c_str(raw: &[libc::c_char]) -> String {
        let bytes: Vec<u8> = raw
            .iter()
            .take_while(|&&c| c != 0)
            .map(|&c| c as u8)
            .collect();
        String::from_utf8_lossy(&bytes).into_owned()
    }

    fn to_proc(pid: i32, info: &libc::proc_taskallinfo) -> Proc {
        let (b, t) = (&info.pbsd, &info.ptinfo);
        let (numer, denom) = timebase();
        let mach = t.pti_total_user as u128 + t.pti_total_system as u128;
        let name = c_str(&b.pbi_name);
        Proc {
            pid,
            comm: if name.is_empty() {
                c_str(&b.pbi_comm)
            } else {
                name
            },
            ppid: b.pbi_ppid as i32,
            ticks: (mach * numer as u128 / denom as u128) as u64,
            // Microseconds since the epoch: unique per pid lifetime, which is all `start` is for.
            start: b.pbi_start_tvsec * 1_000_000 + b.pbi_start_tvusec,
            rss: t.pti_resident_size,
            uid: b.pbi_uid,
            cpu: 0.0,
        }
    }

    pub fn all(uid: u32) -> HashMap<i32, Proc> {
        let mut out = HashMap::new();
        let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
        if count <= 0 {
            return out;
        }
        let mut pids = vec![0 as libc::c_int; count as usize + 64];
        let bytes = (pids.len() * size_of::<libc::c_int>()) as libc::c_int;
        let n = unsafe { libc::proc_listallpids(pids.as_mut_ptr() as *mut libc::c_void, bytes) };
        for &pid in pids.iter().take(n.max(0) as usize) {
            if pid <= 0 {
                continue;
            }
            // Other users' processes refuse TASKALLINFO (EPERM), which skips them as /proc's uid check does.
            if let Some(info) = task_all(pid).filter(|i| i.pbsd.pbi_uid == uid) {
                out.insert(pid, to_proc(pid, &info));
            }
        }
        out
    }

    /// Any user's process. libproc refuses another user's task and BSD info (EPERM; root's
    /// launchd even its BSD info), so the owner, parent and start time — what proc_info and the kill
    /// guard check — come from `ps`, which reads them through sysctl(KERN_PROC_PID).
    pub fn one(pid: i32) -> Option<Proc> {
        if let Some(i) = task_all(pid) {
            return Some(to_proc(pid, &i));
        }
        let out = std::process::Command::new("/bin/ps")
            .args(["-o", "uid=,ppid=,rss=,lstart=,comm=", "-p", &pid.to_string()])
            .env("LC_ALL", "C")
            .output()
            .ok()?;
        parse_ps_one(pid, &String::from_utf8_lossy(&out.stdout))
    }

    /// `uid ppid rss(KiB) <lstart: Thu Nov 19 11:34:00 2026> comm`. `start` is the lstart second in
    /// microseconds since the epoch, the unit `to_proc` uses.
    pub(super) fn parse_ps_one(pid: i32, line: &str) -> Option<Proc> {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 8 {
            return None;
        }
        const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
        let mon = MONTHS.iter().position(|m| *m == f[4])? as i32;
        let hms: Vec<i32> = f[6].split(':').filter_map(|x| x.parse().ok()).collect();
        if hms.len() != 3 {
            return None;
        }
        let mut tm: libc::tm = unsafe { zeroed() };
        tm.tm_year = f[7].parse::<i32>().ok()? - 1900;
        tm.tm_mon = mon;
        tm.tm_mday = f[5].parse().ok()?;
        tm.tm_hour = hms[0];
        tm.tm_min = hms[1];
        tm.tm_sec = hms[2];
        tm.tm_isdst = -1;
        let secs = unsafe { libc::mktime(&mut tm) };
        if secs < 0 {
            return None;
        }
        let comm = f[8..].join(" ");
        Some(Proc {
            pid,
            comm: comm.rsplit('/').next().unwrap_or(&comm).to_string(),
            ppid: f[1].parse().ok()?,
            ticks: 0,
            start: secs as u64 * 1_000_000,
            rss: f[2].parse::<u64>().ok()? * 1024,
            uid: f[0].parse().ok()?,
            cpu: 0.0,
        })
    }

    /// KERN_PROCARGS2 is argc (an int), the exec path, NUL padding, then argc NUL-terminated args.
    pub fn cmdline(pid: i32) -> Vec<u8> {
        let mut size: usize = 0;
        let mut mib = [libc::CTL_KERN, libc::KERN_PROCARGS2, pid];
        if unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                3,
                std::ptr::null_mut(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        } != 0
            || size < 4
        {
            return vec![];
        }
        let mut buf = vec![0u8; size];
        if unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                3,
                buf.as_mut_ptr() as *mut libc::c_void,
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        } != 0
        {
            return vec![];
        }
        buf.truncate(size);
        parse_procargs(&buf)
    }

    pub(super) fn parse_procargs(buf: &[u8]) -> Vec<u8> {
        if buf.len() < 4 {
            return vec![];
        }
        let argc = i32::from_ne_bytes([buf[0], buf[1], buf[2], buf[3]]).max(0) as usize;
        let mut i = 4;
        while i < buf.len() && buf[i] != 0 {
            i += 1; // the exec path
        }
        while i < buf.len() && buf[i] == 0 {
            i += 1; // its padding
        }
        let mut out = Vec::new();
        for _ in 0..argc {
            let end = buf[i..]
                .iter()
                .position(|&b| b == 0)
                .map_or(buf.len(), |p| i + p);
            if i >= end && i >= buf.len() {
                break;
            }
            if !out.is_empty() {
                out.push(0);
            }
            out.extend_from_slice(&buf[i..end]);
            i = end + 1;
            if i >= buf.len() {
                break;
            }
        }
        out
    }

    pub fn cwd(pid: i32) -> String {
        let mut info: libc::proc_vnodepathinfo = unsafe { zeroed() };
        let size = size_of::<libc::proc_vnodepathinfo>() as libc::c_int;
        let n = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDVNODEPATHINFO,
                0,
                &mut info as *mut _ as *mut libc::c_void,
                size,
            )
        };
        if n != size {
            return String::new();
        }
        let flat: Vec<libc::c_char> = info.pvi_cdir.vip_path.iter().flatten().copied().collect();
        c_str(&flat)
    }

    pub fn cpu_ticks() -> Option<(u64, u64)> {
        static HOST: OnceLock<libc::mach_port_t> = OnceLock::new();
        let host = *HOST.get_or_init(|| unsafe { libc::mach_host_self() });
        let mut info: libc::host_cpu_load_info = unsafe { zeroed() };
        let mut count = libc::HOST_CPU_LOAD_INFO_COUNT;
        let rc = unsafe {
            libc::host_statistics(
                host,
                libc::HOST_CPU_LOAD_INFO,
                &mut info as *mut _ as libc::host_info_t,
                &mut count,
            )
        };
        if rc != 0 {
            return None;
        }
        let t = info.cpu_ticks.map(|x| x as u64);
        Some((t.iter().sum(), t[libc::CPU_STATE_IDLE as usize]))
    }

    // Only for plain-old-data `T` (integers, `xsw_usage`): `zeroed()` must be a valid value of it, and the
    // kernel writes raw bytes into it.
    fn sysctl_raw<T: Copy>(name: &str) -> Option<T> {
        let name = CString::new(name).ok()?;
        let mut value: T = unsafe { zeroed() };
        let mut size = size_of::<T>();
        let rc = unsafe {
            libc::sysctlbyname(
                name.as_ptr(),
                &mut value as *mut T as *mut libc::c_void,
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        };
        (rc == 0 && size == size_of::<T>()).then_some(value)
    }

    /// MemAvailable is the kernel's own free percentage (`kern.memorystatus_level`, what Activity
    /// Monitor and `memory_pressure` show, and what sluicy's memory governor reads) times RAM.
    pub fn meminfo() -> HashMap<String, u64> {
        let mut m = HashMap::new();
        let Some(total) = sysctl_raw::<u64>("hw.memsize") else {
            return m;
        };
        m.insert("MemTotal".into(), total);
        if let Some(level) = sysctl_raw::<u32>("kern.memorystatus_level") {
            m.insert("MemAvailable".into(), total / 100 * level.min(100) as u64);
        }
        if let Some(sw) = sysctl_raw::<libc::xsw_usage>("vm.swapusage") {
            m.insert("SwapTotal".into(), sw.xsu_total);
            m.insert("SwapFree".into(), sw.xsu_avail);
        }
        m
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn mac_procargs_skip_the_exec_path_and_padding() {
        let mut raw = 2i32.to_ne_bytes().to_vec();
        raw.extend_from_slice(b"/bin/sleep\0\0\0\0sleep\0300\0PATH=/bin\0");
        assert_eq!(mac::parse_procargs(&raw), b"sleep\0300");
        assert!(mac::parse_procargs(b"\x01").is_empty());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn mac_ps_line_for_another_users_process() {
        let p = mac::parse_ps_one(1, "    0     0  12288 Thu Nov 19 08:01:02 2026     /sbin/launchd\n").unwrap();
        assert_eq!((p.uid, p.ppid, p.rss, p.comm.as_str()), (0, 0, 12288 * 1024, "launchd"));
        assert_eq!(p.start % 1_000_000, 0);
        assert!(mac::parse_ps_one(1, "").is_none());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn mac_meminfo_has_the_linux_keys() {
        let m = meminfo();
        assert!(m["MemTotal"] > 0);
        assert!(m["MemAvailable"] <= m["MemTotal"]);
    }

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
