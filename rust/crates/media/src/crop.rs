//! Cropping — the craft that makes the lab valuable.
//!
//! Two techniques, ported faithfully from the Python:
//!
//! 1. **Aspect-aware single crop** (`crop_rect`): turn any source into one 9:16
//!    frame, cropping the correct axis — sides if the source is wider than 9:16,
//!    top/bottom if it's taller. (The old Rust path only handled the wider case
//!    and would have mangled a too-tall source.)
//!
//! 2. **Multi-crop** (`multi_crop_offsets`): the YIELD MULTIPLIER. One 16:9
//!    source → several distinct 9:16 framings, so one generation becomes many
//!    posts:
//!      - dual:      2 staggered crops at 15% / 85% of the horizontal margin
//!      - triptych:  3 even crops — left / center / right
//!      - both:      the 5 unique crops of dual + triptych combined
//!
//! Offsets are kept bit-for-bit identical to providers/base.py so output framing
//! matches what the team already tuned on.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use anyhow::{Context, Result};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

use crate::probe::VideoInfo;

const TIKTOK_ENCODE: &[&str] = &[
    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
    "-minrate", "8M", "-maxrate", "20M", "-bufsize", "20M",
    "-profile:v", "high", "-level", "4.2",
    "-movflags", "+faststart",
    "-an",
];

/// Multi-crop modes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CropMode {
    /// Single center/aspect-aware crop (1 output).
    Single,
    /// 2 staggered crops (15% / 85% of the margin).
    Dual,
    /// 3 even crops: left, center, right.
    Triptych,
    /// 5 unique crops: dual + triptych combined.
    Both,
}

impl CropMode {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "single" | "none" => Some(Self::Single),
            "dual" => Some(Self::Dual),
            "triptych" => Some(Self::Triptych),
            "both" => Some(Self::Both),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Single => "single",
            Self::Dual => "dual",
            Self::Triptych => "triptych",
            Self::Both => "both",
        }
    }
}

/// A crop rectangle in source pixels.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CropRect {
    pub w: u32,
    pub h: u32,
    pub x: u32,
    pub y: u32,
}

/// Aspect-aware single 9:16 crop rect for a source of the given dimensions.
/// Crops sides if wider than 9:16, top/bottom if taller. Pure — unit-tested.
pub fn crop_rect(info: &VideoInfo) -> CropRect {
    let (sw, sh) = (info.width, info.height);
    let target = 9.0 / 16.0; // width / height
    let src_ratio = sw as f64 / sh as f64;
    if src_ratio > target {
        // Wider than 9:16 → crop the sides, full height.
        let cw = (sh as f64 * target).round() as u32;
        let cx = (sw.saturating_sub(cw)) / 2;
        CropRect { w: cw, h: sh, x: cx, y: 0 }
    } else {
        // Taller than 9:16 → crop top/bottom, full width.
        let ch = (sw as f64 / target).round() as u32;
        let cy = (sh.saturating_sub(ch)) / 2;
        CropRect { w: sw, h: ch, x: 0, y: cy }
    }
}

/// The horizontal x-offsets for a multi-crop of a 16:9 source, given its width
/// and height. Returns one 9:16 crop column per offset (all full-height,
/// width = h*9/16). Bit-for-bit identical to providers/base.py. Pure —
/// unit-tested so the framing can never silently drift.
pub fn multi_crop_offsets(mode: CropMode, src_w: u32, src_h: u32) -> (u32, Vec<u32>) {
    let crop_w = (src_h as f64 * 9.0 / 16.0) as u32; // truncates, matching Python int()
    let margin = src_w.saturating_sub(crop_w);
    let m = margin as f64;
    let offsets: Vec<u32> = match mode {
        CropMode::Both => {
            let tri_spacing = (margin / 2) as f64;
            vec![
                (m * 0.15) as u32,
                (m * 0.85) as u32,
                0,
                tri_spacing as u32,
                margin,
            ]
        }
        CropMode::Triptych => {
            let spacing = (margin / 2) as f64;
            vec![0, spacing as u32, margin]
        }
        CropMode::Dual => vec![(m * 0.15) as u32, (m * 0.85) as u32],
        CropMode::Single => vec![], // single uses crop_rect, not offsets
    };
    (crop_w, offsets)
}

/// How many outputs a mode produces (for pre-allocating asset rows / progress).
pub fn output_count(mode: CropMode) -> usize {
    match mode {
        CropMode::Single => 1,
        CropMode::Dual => 2,
        CropMode::Triptych => 3,
        CropMode::Both => 5,
    }
}

/// Run a single ffmpeg crop+scale to a 1080x1920 9:16 short.
/// `rect` is the source-pixel crop; the result is scaled to 1080x1920 (lanczos).
pub async fn crop_one(
    input: &Path,
    output: &Path,
    rect: CropRect,
    start_secs: Option<f64>,
    len_secs: Option<f64>,
) -> Result<()> {
    let vf = format!(
        "crop={}:{}:{}:{},scale=1080:1920:flags=lanczos",
        rect.w, rect.h, rect.x, rect.y
    );
    let mut args: Vec<String> = vec!["-y".into()];
    if let Some(s) = start_secs {
        args.extend(["-ss".into(), format!("{s}")]);
    }
    args.extend(["-i".into(), input.to_string_lossy().into_owned()]);
    if let Some(l) = len_secs {
        args.extend(["-t".into(), format!("{l}")]);
    }
    args.extend(["-vf".into(), vf]);
    args.extend(TIKTOK_ENCODE.iter().map(|s| s.to_string()));
    args.push(output.to_string_lossy().into_owned());

    run(&args).await
}

/// Multi-crop a source into N 9:16 outputs named `<stem>_crop{i}.mp4` in
/// `out_dir`. Returns the produced paths in order. A failure on one crop returns
/// an error (the caller decides whether to keep partial results).
pub async fn multi_crop(
    input: &Path,
    out_dir: &Path,
    info: &VideoInfo,
    mode: CropMode,
) -> Result<Vec<PathBuf>> {
    let (crop_w, offsets) = multi_crop_offsets(mode, info.width, info.height);
    let mut outputs = Vec::with_capacity(offsets.len());
    for (i, x) in offsets.iter().enumerate() {
        let out = out_dir.join(format!("crop{i}.mp4"));
        let rect = CropRect { w: crop_w, h: info.height, x: *x, y: 0 };
        crop_one(input, &out, rect, None, None)
            .await
            .with_context(|| format!("multi-crop {i} failed"))?;
        outputs.push(out);
    }
    Ok(outputs)
}

/// Run ffmpeg, capturing stderr tail for errors. (No progress stream here —
/// crops are short single passes; the clip pipeline keeps the -progress path.)
async fn run(args: &[String]) -> Result<()> {
    let mut child = Command::new("ffmpeg")
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .context("failed to spawn ffmpeg")?;
    let stderr = child.stderr.take().context("no ffmpeg stderr")?;
    let tail = tokio::spawn(async move {
        let mut reader = BufReader::new(stderr).lines();
        let mut tail = String::new();
        while let Ok(Some(line)) = reader.next_line().await {
            tail.push_str(&line);
            tail.push('\n');
            if tail.len() > 4000 {
                tail = tail.split_off(tail.len() - 4000);
            }
        }
        tail
    });
    let status = child.wait().await.context("ffmpeg wait failed")?;
    let tail = tail.await.unwrap_or_default();
    if !status.success() {
        anyhow::bail!("ffmpeg crop exited {}: {}", status, tail.trim());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn info(w: u32, h: u32) -> VideoInfo {
        VideoInfo { width: w, height: h, duration_secs: 10.0 }
    }

    #[test]
    fn aspect_aware_wide_source_crops_sides() {
        // 1920x1080 (16:9, wider than 9:16) → crop sides, full height.
        let r = crop_rect(&info(1920, 1080));
        assert_eq!(r.h, 1080);
        assert_eq!(r.w, 608); // round(1080 * 9/16)
        assert_eq!(r.x, (1920 - 608) / 2);
        assert_eq!(r.y, 0);
    }

    #[test]
    fn aspect_aware_tall_source_crops_top_bottom() {
        // 1080x1920 is exactly 9:16 — ratio == target, falls to the "taller"
        // branch, full width, height = round(1080 / (9/16)) = 1920 (no-op crop).
        let r = crop_rect(&info(1080, 1920));
        assert_eq!(r.w, 1080);
        assert_eq!(r.h, 1920);
        // A genuinely too-tall source (1080x2400) crops top/bottom.
        let r2 = crop_rect(&info(1080, 2400));
        assert_eq!(r2.w, 1080);
        assert_eq!(r2.h, 1920); // round(1080 * 16/9)
        assert_eq!(r2.y, (2400 - 1920) / 2);
    }

    #[test]
    fn multi_crop_offsets_match_python() {
        // 1920x1080: crop_w = int(1080*9/16) = 607, margin = 1313.
        let (cw, dual) = multi_crop_offsets(CropMode::Dual, 1920, 1080);
        assert_eq!(cw, 607);
        assert_eq!(dual, vec![(1313.0 * 0.15) as u32, (1313.0 * 0.85) as u32]);

        let (_, tri) = multi_crop_offsets(CropMode::Triptych, 1920, 1080);
        assert_eq!(tri, vec![0, 1313 / 2, 1313]);

        let (_, both) = multi_crop_offsets(CropMode::Both, 1920, 1080);
        assert_eq!(both.len(), 5);
        assert_eq!(both[0], (1313.0 * 0.15) as u32);
        assert_eq!(both[2], 0);
        assert_eq!(both[4], 1313);
    }

    #[test]
    fn output_counts() {
        assert_eq!(output_count(CropMode::Single), 1);
        assert_eq!(output_count(CropMode::Dual), 2);
        assert_eq!(output_count(CropMode::Triptych), 3);
        assert_eq!(output_count(CropMode::Both), 5);
    }
}
