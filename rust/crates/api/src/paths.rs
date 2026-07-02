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
}
