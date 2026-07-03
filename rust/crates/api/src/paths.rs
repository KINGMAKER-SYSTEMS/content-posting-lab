//! Path-safety helpers — input confinement for everything that touches the
//! filesystem or ffmpeg.
//!
//! The spine review found three real path-injection vectors, all because
//! request-supplied strings (`project`, `source_path`) reached ffmpeg / the
//! filesystem unvalidated:
//!   - an absolute `source_path` let a caller point ffprobe/ffmpeg at any file
//!     (arbitrary-file-read attempt + a readability oracle via error messages),
//!   - a `project` of `../../x` was joined into output dirs and the stored asset
//!     path, a path-traversal *write* primitive.
//!
//! Everything user-supplied that becomes a path component goes through here.

use std::path::{Component, Path, PathBuf};

/// Sanitize a project name to a filesystem-safe slug. Lowercase, spaces→hyphens,
/// strip anything that isn't `[a-z0-9_-]`, block traversal, cap length. Returns
/// None if nothing safe remains. (Single source of truth — both the /projects
/// endpoint and the clip endpoint use this so they can't drift.)
pub fn sanitize_project_name(name: &str) -> Option<String> {
    if name.contains("..") || name.contains('/') || name.contains('\\') {
        return None;
    }
    let s: String = name
        .to_lowercase()
        .replace(' ', "-")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .take(100)
        .collect();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// Confine a caller-supplied *relative* media path under `data_dir` and return
/// the absolute path. Rejects absolute paths and any `..` / root components, so
/// the result is provably inside `data_dir`.
///
/// Used for the clip source: a client may only reference files that live under
/// the data directory (e.g. a previously-uploaded source), never `/etc/passwd`
/// or `../secrets`.
pub fn confine_under_data(data_dir: &str, rel: &str) -> Result<PathBuf, String> {
    let p = Path::new(rel);
    if p.is_absolute() {
        return Err("path must be relative (absolute paths are not allowed)".into());
    }
    // Reject any traversal or weird components up front.
    for comp in p.components() {
        match comp {
            Component::Normal(_) => {}
            Component::CurDir => {}
            _ => return Err("path may not contain '..' or root components".into()),
        }
    }
    let base = PathBuf::from(data_dir);
    let joined = base.join(p);

    // Defense in depth: if the path exists, canonicalize and re-check the
    // prefix (catches symlink escapes). If it doesn't exist yet, the component
    // check above already guarantees confinement.
    if let Ok(canon) = joined.canonicalize() {
        let base_canon = base.canonicalize().unwrap_or(base.clone());
        if !canon.starts_with(&base_canon) {
            return Err("path escapes the data directory".into());
        }
        return Ok(canon);
    }
    Ok(joined)
}

/// Return `path` as a data-dir-relative string, ready for `confine_under_data`.
///
/// Uploaded sources come back from the R2 / stage-streamed endpoints as an
/// ABSOLUTE path in production (`data_dir()` is the absolute
/// `RAILWAY_VOLUME_MOUNT_PATH`), then the frontend hands that exact string to
/// process-batch. `confine_under_data` rejects absolute paths outright, so
/// without this every uploaded source is un-processable in prod (URL-download
/// sources return a relative path and were the only ones that worked).
///
/// An already-relative path is returned unchanged. An absolute path is accepted
/// ONLY if it lives under `data_dir` (relativized); an absolute path that
/// escapes the data dir is rejected. Traversal validation still happens in
/// `confine_under_data` on the returned value.
pub fn relativize_under_data(data_dir: &str, path: &str) -> Result<String, String> {
    let p = Path::new(path);
    if !p.is_absolute() {
        return Ok(path.to_string());
    }
    let base = PathBuf::from(data_dir);
    strip_base(p, &base).ok_or_else(|| "absolute path is not under the data directory".into())
}

/// Strip `base` from `p`, returning the relative remainder as a string. Falls
/// back to comparing canonicalized forms so symlinked / `.`-laden data dirs still
/// match. None if `p` is not under `base`.
fn strip_base(p: &Path, base: &Path) -> Option<String> {
    if let Ok(rel) = p.strip_prefix(base) {
        return Some(rel.to_string_lossy().to_string());
    }
    if let (Ok(pc), Ok(bc)) = (p.canonicalize(), base.canonicalize()) {
        if let Ok(rel) = pc.strip_prefix(&bc) {
            return Some(rel.to_string_lossy().to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitize_basic() {
        assert_eq!(sanitize_project_name("Drake Release!!!").as_deref(), Some("drake-release"));
        assert_eq!(sanitize_project_name("ok_name-1").as_deref(), Some("ok_name-1"));
    }

    #[test]
    fn sanitize_blocks_traversal() {
        assert_eq!(sanitize_project_name("../../etc"), None);
        assert_eq!(sanitize_project_name("a/b"), None);
        assert_eq!(sanitize_project_name("a\\b"), None);
        assert_eq!(sanitize_project_name("..").map(|_| ()), None.map(|_: ()| ()));
    }

    #[test]
    fn confine_rejects_absolute() {
        assert!(confine_under_data("/data", "/etc/passwd").is_err());
        assert!(confine_under_data("/data", "/proc/self/environ").is_err());
    }

    #[test]
    fn confine_rejects_traversal() {
        assert!(confine_under_data("/data", "../secret").is_err());
        assert!(confine_under_data("/data", "a/../../b").is_err());
    }

    #[test]
    fn confine_allows_clean_relative() {
        let r = confine_under_data("/data", "projects/x/clips/source.mp4").unwrap();
        assert!(r.starts_with("/data"));
    }

    #[test]
    fn relativize_passes_relative_through() {
        assert_eq!(
            relativize_under_data("/data", "projects/x/src.mp4").unwrap(),
            "projects/x/src.mp4"
        );
    }

    #[test]
    fn relativize_accepts_absolute_under_data() {
        // The prod upload case: data_dir is absolute, uploads hand back absolute paths.
        assert_eq!(
            relativize_under_data("/app/projects", "/app/projects/projects/x/clips/s.mp4").unwrap(),
            "projects/x/clips/s.mp4"
        );
        // …and the relativized form still confines cleanly.
        let abs = confine_under_data(
            "/app/projects",
            &relativize_under_data("/app/projects", "/app/projects/projects/x/clips/s.mp4").unwrap(),
        )
        .unwrap();
        assert!(abs.starts_with("/app/projects"));
    }

    #[test]
    fn relativize_rejects_absolute_outside_data() {
        assert!(relativize_under_data("/app/projects", "/etc/passwd").is_err());
        assert!(relativize_under_data("/app/projects", "/app/other/x.mp4").is_err());
    }
}
