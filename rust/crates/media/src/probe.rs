//! ffprobe wrapper — duration + dimensions, with the rotation→dimension-swap
//! handling the old `_probe` did (a phone-shot vertical video reports landscape
//! dimensions + a 90° rotation tag).

use anyhow::{Context, Result};
use tokio::process::Command;

#[derive(Debug, Clone)]
pub struct VideoInfo {
    pub width: u32,
    pub height: u32,
    pub duration_secs: f64,
}

impl VideoInfo {
    /// True if already (close to) 9:16, within the same 0.02 ratio tolerance the
    /// old clipper used to decide passthrough-vs-crop.
    pub fn is_vertical_9x16(&self) -> bool {
        if self.height == 0 {
            return false;
        }
        let ratio = self.width as f64 / self.height as f64;
        (ratio - 9.0 / 16.0).abs() < 0.02
    }
}

/// Probe a media file for width, height (rotation-corrected), and duration.
pub async fn probe(path: &str) -> Result<VideoInfo> {
    // One ffprobe call, CSV out: width,height,rotation(optional),duration.
    let out = Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_tags=rotate:format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
            path,
        ])
        .output()
        .await
        .context("failed to spawn ffprobe")?;

    if !out.status.success() {
        anyhow::bail!(
            "ffprobe failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }

    let text = String::from_utf8_lossy(&out.stdout);
    let mut width = 0u32;
    let mut height = 0u32;
    let mut rotate = 0i32;
    let mut duration = 0.0f64;

    for line in text.lines() {
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        match k.trim() {
            "width" => width = v.trim().parse().unwrap_or(0),
            "height" => height = v.trim().parse().unwrap_or(0),
            "rotate" | "TAG:rotate" => rotate = v.trim().parse().unwrap_or(0),
            "duration" => duration = v.trim().parse().unwrap_or(0.0),
            _ => {}
        }
    }

    // A 90°/270° rotation means the displayed frame is portrait — swap.
    if rotate.abs() % 180 == 90 {
        std::mem::swap(&mut width, &mut height);
    }

    // ffprobe exits 0 even when there's no video stream (e.g. an audio-only
    // file) — width/height stay 0 and a degenerate clip job would follow. Fail
    // fast with a clear message instead of producing an empty/confusing job.
    if width == 0 || height == 0 {
        anyhow::bail!("no video stream / zero dimensions in {path}");
    }
    if duration <= 0.0 {
        anyhow::bail!("zero or missing duration in {path}");
    }

    Ok(VideoInfo {
        width,
        height,
        duration_secs: duration,
    })
}
