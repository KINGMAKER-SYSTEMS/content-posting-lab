//! yt-dlp orchestration for TikTok caption scraping — **metadata and thumbnails
//! only, never a video download**. Ported from `scraper/frame_extractor.py`.
//!
//! Two operations:
//!   - [`list_profile_videos`] — flat-playlist listing of a profile's video URLs
//!   - [`get_thumbnail`] / [`thumbnail_url`] — fetch a video's cover image
//!
//! Cookie resolution mirrors the Python chain (first hit wins):
//!   1. `YTDLP_COOKIES_FILE` env var — explicit path to a cookies.txt
//!   2. `YTDLP_COOKIES` env var — base64-encoded cookies.txt (decoded to temp)
//!   3. `RAILWAY_VOLUME_MOUNT_PATH/cookies.txt` (deployed volume)
//!   4. `cookies.txt` in CWD (local dev)
//!
//! Arg construction and output parsing are pure functions so they're testable
//! without yt-dlp installed.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result};
use tokio::process::Command;

// ── Cookies ──────────────────────────────────────────────────────────────────

/// Resolve the cookies.txt yt-dlp should use, if any (see module docs for the
/// resolution order). Re-resolves on every call — no stale cache to reset.
pub fn cookies_path() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("YTDLP_COOKIES_FILE") {
        let p = PathBuf::from(explicit);
        if p.exists() {
            return Some(p);
        }
    }
    if let Ok(raw) = std::env::var("YTDLP_COOKIES") {
        if let Ok(data) = b64_decode(raw.trim()) {
            let dest = std::env::temp_dir().join("ytdlp_cookies_env.txt");
            if std::fs::write(&dest, data).is_ok() {
                return Some(dest);
            }
        }
    }
    if let Ok(base) = std::env::var("RAILWAY_VOLUME_MOUNT_PATH") {
        let p = PathBuf::from(base).join("cookies.txt");
        if p.exists() {
            return Some(p);
        }
    }
    let cwd = PathBuf::from("cookies.txt");
    if cwd.exists() {
        return Some(cwd);
    }
    None
}

fn add_cookies(args: &mut Vec<String>, cookies: Option<&Path>) {
    if let Some(cp) = cookies {
        args.push("--cookies".into());
        args.push(cp.to_string_lossy().into_owned());
    }
}

// ── Arg construction (pure) ──────────────────────────────────────────────────

/// yt-dlp args for a flat-playlist profile listing (URLs only, no downloads).
pub fn list_args(profile_url: &str, max_videos: usize, cookies: Option<&Path>) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "--flat-playlist".into(),
        "--no-warnings".into(),
        "--no-check-certificates".into(),
        "--playlist-end".into(),
        max_videos.to_string(),
        "--print".into(),
        "webpage_url".into(),
        profile_url.into(),
    ];
    add_cookies(&mut args, cookies);
    args
}

/// yt-dlp args to download a video's cover thumbnail as jpg. `thumb_base` is
/// the output path WITHOUT extension (yt-dlp appends its own).
pub fn thumbnail_args(video_url: &str, thumb_base: &Path, cookies: Option<&Path>) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "--no-download".into(),
        "--no-warnings".into(),
        "--no-check-certificates".into(),
        "--write-thumbnail".into(),
        "--convert-thumbnails".into(),
        "jpg".into(),
        "-o".into(),
        format!("thumbnail:{}", thumb_base.to_string_lossy()),
        video_url.into(),
    ];
    add_cookies(&mut args, cookies);
    args
}

/// yt-dlp args to print a video's thumbnail URL (the urllib-fallback path).
pub fn thumbnail_url_args(video_url: &str, cookies: Option<&Path>) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "--no-download".into(),
        "--no-warnings".into(),
        "--no-check-certificates".into(),
        "--print".into(),
        "thumbnail".into(),
        video_url.into(),
    ];
    add_cookies(&mut args, cookies);
    args
}

/// Parse `--print webpage_url` stdout into a URL list (trims, drops blanks).
pub fn parse_url_list(stdout: &str) -> Vec<String> {
    stdout
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect()
}

// ── Subprocess ───────────────────────────────────────────────────────────────

async fn run_ytdlp(args: &[String]) -> Result<std::process::Output> {
    let child = Command::new("yt-dlp")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // Killed if the future is dropped (e.g. an outer timeout fires).
        .kill_on_drop(true)
        .spawn()
        .context("yt-dlp not found on PATH — install it (brew install yt-dlp / pip install yt-dlp)")?;
    child.wait_with_output().await.context("yt-dlp wait failed")
}

/// List a TikTok profile's video URLs via yt-dlp flat-playlist (no browser,
/// no download). Returns at most `max_videos` URLs, newest-first (yt-dlp's
/// natural profile order = "latest").
pub async fn list_profile_videos(
    profile_url: &str,
    max_videos: usize,
    sort: &str,
) -> Result<Vec<String>> {
    // TODO(playwright-sidecar): the Python app routes sort == "popular" through
    // a Playwright+stealth browser (`scraper/tiktok_scraper.py`) that sorts the
    // profile grid by popularity, and also falls back to it when yt-dlp can't
    // list the profile. That is a deferred Python sidecar — when it exists,
    // call it here for sort == "popular" (and as the listing-failure fallback)
    // before/after the yt-dlp attempt below.
    let _ = sort;

    let args = list_args(profile_url, max_videos, cookies_path().as_deref());
    // Python had no timeout here; we bound it so a wedged yt-dlp can't stall
    // the pipeline forever.
    let out = tokio::time::timeout(Duration::from_secs(180), run_ytdlp(&args))
        .await
        .map_err(|_| anyhow::anyhow!("yt-dlp listing timed out after 180s"))??;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("yt-dlp listing failed: {}", tail(err.trim(), 300));
    }
    let urls = parse_url_list(&String::from_utf8_lossy(&out.stdout));
    Ok(urls.into_iter().take(max_videos).collect())
}

/// Fetch a TikTok video's cover/thumbnail image to `dest` (a `.jpg` path)
/// using yt-dlp's built-in thumbnail writer (handles cookies/headers). Much
/// faster than downloading the video. 30s hard timeout.
pub async fn get_thumbnail(video_url: &str, dest: &Path) -> Result<()> {
    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    // Output template without extension; yt-dlp adds the actual extension.
    let thumb_base = dest.with_extension("");
    let args = thumbnail_args(video_url, &thumb_base, cookies_path().as_deref());
    let out = tokio::time::timeout(Duration::from_secs(30), run_ytdlp(&args))
        .await
        .map_err(|_| anyhow::anyhow!("yt-dlp thumbnail timed out after 30s"))??;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("yt-dlp thumbnail failed: {}", tail(err.trim(), 200));
    }

    // yt-dlp may write as dest (with .jpg) or under some other variant of the
    // stem — normalize whatever it wrote to `dest`.
    if dest.exists() {
        return Ok(());
    }
    if let Some(found) = find_by_stem(dest).await {
        tokio::fs::rename(&found, dest).await.context("rename thumbnail")?;
        return Ok(());
    }
    anyhow::bail!("No thumbnail downloaded")
}

/// Ask yt-dlp for the video's thumbnail URL (fallback when the direct
/// thumbnail write produced nothing). 15s hard timeout.
pub async fn thumbnail_url(video_url: &str) -> Result<String> {
    let args = thumbnail_url_args(video_url, cookies_path().as_deref());
    let out = tokio::time::timeout(Duration::from_secs(15), run_ytdlp(&args))
        .await
        .map_err(|_| anyhow::anyhow!("yt-dlp thumbnail URL lookup timed out"))??;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("yt-dlp thumbnail URL lookup failed: {}", tail(err.trim(), 200));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Find a file in `dest`'s directory whose name starts with `dest`'s stem.
async fn find_by_stem(dest: &Path) -> Option<PathBuf> {
    let stem = dest.file_stem()?.to_str()?.to_string();
    let parent = dest.parent()?;
    let mut rd = tokio::fs::read_dir(parent).await.ok()?;
    while let Ok(Some(entry)) = rd.next_entry().await {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(&stem) && entry.path().is_file() {
            return Some(entry.path());
        }
    }
    None
}

/// Last `n` characters of a string (char-boundary safe) — mirrors the Python
/// `stderr[-n:]` error-tail convention.
fn tail(s: &str, n: usize) -> &str {
    let count = s.chars().count();
    if count <= n {
        return s;
    }
    let skip = count - n;
    let (idx, _) = s.char_indices().nth(skip).unwrap_or((0, ' '));
    &s[idx..]
}

/// Minimal standard-base64 decoder for the `YTDLP_COOKIES` env var (avoids
/// pulling the base64 crate for one use — same approach as the api crate).
fn b64_decode(s: &str) -> Result<Vec<u8>, ()> {
    fn val(c: u8) -> Result<u8, ()> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'+' => Ok(62),
            b'/' => Ok(63),
            _ => Err(()),
        }
    }
    let clean: Vec<u8> = s.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    let mut out = Vec::with_capacity(clean.len() / 4 * 3);
    for chunk in clean.chunks(4) {
        let pad = chunk.iter().filter(|&&b| b == b'=').count();
        let mut buf = [0u8; 4];
        for (i, &b) in chunk.iter().enumerate() {
            buf[i] = if b == b'=' { 0 } else { val(b)? };
        }
        let n = (buf[0] as u32) << 18 | (buf[1] as u32) << 12 | (buf[2] as u32) << 6 | (buf[3] as u32);
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn list_args_shape() {
        let args = list_args("https://www.tiktok.com/@user", 20, None);
        assert_eq!(args[0], "--flat-playlist");
        assert!(args.contains(&"--no-warnings".to_string()));
        assert!(args.contains(&"--no-check-certificates".to_string()));
        // --playlist-end N pair
        let i = args.iter().position(|a| a == "--playlist-end").unwrap();
        assert_eq!(args[i + 1], "20");
        // --print webpage_url pair
        let p = args.iter().position(|a| a == "--print").unwrap();
        assert_eq!(args[p + 1], "webpage_url");
        // URL last, no cookies flag
        assert_eq!(args.last().unwrap(), "https://www.tiktok.com/@user");
        assert!(!args.contains(&"--cookies".to_string()));
    }

    #[test]
    fn list_args_appends_cookies() {
        let args = list_args("u", 5, Some(Path::new("/tmp/cookies.txt")));
        let i = args.iter().position(|a| a == "--cookies").unwrap();
        assert_eq!(args[i + 1], "/tmp/cookies.txt");
        // cookies come AFTER the url (matches the Python `cmd + [--cookies]`).
        assert!(i > args.iter().position(|a| a == "u").unwrap());
    }

    #[test]
    fn thumbnail_args_shape() {
        let args = thumbnail_args("https://t/video/123", Path::new("/tmp/frames/123"), None);
        assert!(args.contains(&"--no-download".to_string()));
        assert!(args.contains(&"--write-thumbnail".to_string()));
        let c = args.iter().position(|a| a == "--convert-thumbnails").unwrap();
        assert_eq!(args[c + 1], "jpg");
        let o = args.iter().position(|a| a == "-o").unwrap();
        assert_eq!(args[o + 1], "thumbnail:/tmp/frames/123");
        assert_eq!(args.last().unwrap(), "https://t/video/123");
    }

    #[test]
    fn thumbnail_url_args_shape() {
        let args = thumbnail_url_args("https://t/video/9", None);
        let p = args.iter().position(|a| a == "--print").unwrap();
        assert_eq!(args[p + 1], "thumbnail");
        assert!(args.contains(&"--no-download".to_string()));
    }

    #[test]
    fn parse_url_list_trims_and_drops_blanks() {
        let out = "https://a/video/1\n  https://a/video/2  \n\n\nhttps://a/video/3\n";
        assert_eq!(
            parse_url_list(out),
            vec!["https://a/video/1", "https://a/video/2", "https://a/video/3"]
        );
        assert!(parse_url_list("").is_empty());
        assert!(parse_url_list("   \n \n").is_empty());
    }

    #[test]
    fn tail_is_char_boundary_safe() {
        assert_eq!(tail("hello", 300), "hello");
        assert_eq!(tail("hello", 3), "llo");
        // multibyte: must not panic mid-char
        assert_eq!(tail("aé€b", 2), "€b");
    }

    #[test]
    fn b64_decode_roundtrip() {
        assert_eq!(b64_decode("aGVsbG8=").unwrap(), b"hello");
        assert_eq!(b64_decode("aGVsbG8h").unwrap(), b"hello!");
        // whitespace tolerated (env vars often get wrapped)
        assert_eq!(b64_decode("aGVs\nbG8=").unwrap(), b"hello");
        assert!(b64_decode("!!!!").is_err());
    }
}
