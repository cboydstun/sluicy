//! Reading a worktree's HEAD and refs straight from the git directory, so a refresh need not
//! start `git rev-parse` per worktree, and so answers that depend only on commits (ahead/behind,
//! merged) can be cached by commit id for as long as those commits stay put.
//! Anything unusual (no .git, a symbolic ref chain, a missing ref) returns None and the caller
//! falls back to running git.

use std::fs;
use std::path::{Path, PathBuf};

pub struct Dirs {
    /// The worktree's own git dir (holds HEAD).
    pub git_dir: PathBuf,
    /// The repository's common dir (holds refs and packed-refs).
    pub common: PathBuf,
}

pub fn dirs(worktree: &Path) -> Option<Dirs> {
    let dot = worktree.join(".git");
    let md = fs::metadata(&dot).ok()?;
    let git_dir = if md.is_dir() {
        dot
    } else {
        let text = fs::read_to_string(&dot).ok()?;
        let p = PathBuf::from(text.trim().strip_prefix("gitdir:")?.trim());
        if p.is_absolute() {
            p
        } else {
            worktree.join(p)
        }
    };
    let common = match fs::read_to_string(git_dir.join("commondir")) {
        Ok(c) => {
            let p = PathBuf::from(c.trim());
            if p.is_absolute() {
                p
            } else {
                git_dir.join(p)
            }
        }
        Err(_) => git_dir.clone(),
    };
    Some(Dirs { git_dir, common })
}

fn is_sha(s: &str) -> bool {
    (s.len() == 40 || s.len() == 64) && s.bytes().all(|c| c.is_ascii_hexdigit())
}

/// `git rev-parse --abbrev-ref HEAD`: the branch name, or "HEAD" when detached.
pub fn branch(d: &Dirs) -> Option<String> {
    let head = fs::read_to_string(d.git_dir.join("HEAD")).ok()?;
    let head = head.trim();
    if let Some(r) = head.strip_prefix("ref:") {
        let r = r.trim();
        return Some(r.strip_prefix("refs/heads/").unwrap_or(r).to_string());
    }
    is_sha(head).then(|| "HEAD".to_string())
}

/// The commit a full ref name points at: the loose file first, then packed-refs.
pub fn resolve(d: &Dirs, refname: &str) -> Option<String> {
    for base in [&d.git_dir, &d.common] {
        if let Ok(t) = fs::read_to_string(base.join(refname)) {
            let t = t.trim();
            return is_sha(t).then(|| t.to_string());
        }
    }
    let packed = fs::read_to_string(d.common.join("packed-refs")).ok()?;
    packed.lines().find_map(|l| {
        let (sha, name) = l.split_once(' ')?;
        (name == refname && is_sha(sha)).then(|| sha.to_string())
    })
}

/// The commit HEAD points at.
pub fn head_sha(d: &Dirs) -> Option<String> {
    let head = fs::read_to_string(d.git_dir.join("HEAD")).ok()?;
    let head = head.trim();
    match head.strip_prefix("ref:") {
        Some(r) => resolve(d, r.trim()),
        None => is_sha(head).then(|| head.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn sh(dir: &Path, args: &[&str]) -> String {
        let o = Command::new("git")
            .args(args)
            .current_dir(dir)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .output()
            .unwrap();
        assert!(
            o.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&o.stderr)
        );
        String::from_utf8_lossy(&o.stdout).trim().to_string()
    }

    #[test]
    fn matches_git_in_main_and_linked_worktrees() {
        let root = std::env::temp_dir().join(format!("deck-gitfiles-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let repo = root.join("repo");
        fs::create_dir_all(&repo).unwrap();
        sh(&repo, &["init", "-q", "-b", "main"]);
        fs::write(repo.join("a"), "a").unwrap();
        sh(&repo, &["add", "a"]);
        sh(
            &repo,
            &[
                "-c",
                "user.email=t@e",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "a",
            ],
        );
        sh(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
        sh(
            &repo,
            &[
                "worktree",
                "add",
                "-q",
                "-b",
                "fix/x",
                root.join("wt").to_str().unwrap(),
            ],
        );
        sh(
            &repo,
            &[
                "worktree",
                "add",
                "-q",
                "--detach",
                root.join("det").to_str().unwrap(),
            ],
        );
        for (dir, want) in [
            (repo.clone(), "main"),
            (root.join("wt"), "fix/x"),
            (root.join("det"), "HEAD"),
        ] {
            let d = dirs(&dir).unwrap();
            assert_eq!(branch(&d).unwrap(), want);
            assert_eq!(
                branch(&d).unwrap(),
                sh(&dir, &["rev-parse", "--abbrev-ref", "HEAD"])
            );
            assert_eq!(head_sha(&d).unwrap(), sh(&dir, &["rev-parse", "HEAD"]));
            assert_eq!(
                resolve(&d, "refs/remotes/origin/main").unwrap(),
                sh(&dir, &["rev-parse", "origin/main"])
            );
        }
        // Packed refs resolve too.
        sh(&repo, &["pack-refs", "--all"]);
        let d = dirs(&root.join("wt")).unwrap();
        assert_eq!(
            resolve(&d, "refs/remotes/origin/main").unwrap(),
            sh(&repo, &["rev-parse", "origin/main"])
        );
        assert_eq!(
            head_sha(&d).unwrap(),
            sh(&root.join("wt"), &["rev-parse", "HEAD"])
        );
        assert!(dirs(&root).is_none());
        let _ = fs::remove_dir_all(&root);
    }
}
