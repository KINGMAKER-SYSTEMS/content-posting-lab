//! ffmpeg primitives: clip a long video into 9:16 shorts, and burn a caption
//! overlay + color correction onto a clip.
//!
//! Two design points carried over from the old app on purpose:
//!   - Caption text is rendered to a transparent PNG *by the browser* and
//!     composited via the ffmpeg `overlay` filter. We keep that contract (decode
//!     the PNG to a temp file, feed as a 2nd input) rather than fighting
//!     `drawtext` fontconfig/escaping — it's the leaner reliable path.
//!   - On Railway (or files >500MB) the x264 preset drops to `fast` to dodge the
//!     proxy timeout. That env/size switch is load-bearing; preserve it.
//!
//! Progress: every long job runs ffmpeg with `-progress pipe:1 -nostats` so a
//! clean key=value stream lands on stdout; we parse `out_time_us` against the
//! probed duration and emit a 0..1 fraction through a callback (the API layer
//! pushes that over SSE).

use std::path::Path;
use std::process::Stdio;

use anyhow::{Context, Result};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

/// TikTok-ready encode args. Kept verbatim from services/ffmpeg.py so output
/// quality is identical to the old burn path (small numeric drift is visible).
const TIKTOK_ENCODE: &[&str] = &[
    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
    "-minrate", "8M", "-maxrate", "20M", "-bufsize", "20M",
    "-profile:v", "high", "-level", "4.2",
    "-movflags", "+faststart",
    "-c:a", "aac", "-b:a", "192k",
];

/// Source-preserving encode (ports Python's `STANDARD_ENCODE_ARGS`): keeps the
/// source frame rate (no `-r`), drops the TikTok rate caps, and copies audio
/// (`-c:a copy`) instead of re-encoding. Used by `/api/video/color-correct` so
/// tweaking a generated clip doesn't silently resample fps or transcode audio.
/// `-preset` is added by the caller (Railway-aware, like the other encoders).
const STANDARD_ENCODE: &[&str] = &[
    "-c:v", "libx264", "-crf", "18",
    "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
    "-c:a", "copy",
];

/// Pick the x264 preset: `fast` when deployed or for large inputs, else
/// `medium`. Mirrors the clipper's RAILWAY_ENVIRONMENT / >500MB switch.
fn preset_for(input: &Path) -> &'static str {
    let deployed = std::env::var("RAILWAY_ENVIRONMENT").is_ok();
    let big = std::fs::metadata(input)
        .map(|m| m.len() > 500 * 1024 * 1024)
        .unwrap_or(false);
    if deployed || big {
        "fast"
    } else {
        "medium"
    }
}

/// Clip `[start, start+len)` of `input` into a single 9:16 1080x1920 short at
/// `output`. If the source is already 9:16 it's scaled, otherwise center-cropped
/// then scaled. Audio is stripped (`-an`) to match the muted-UGC convention.
///
/// `on_progress` receives a 0.0..=1.0 fraction as ffmpeg reports `out_time`.
pub async fn clip_segment<F>(
    input: &Path,
    output: &Path,
    start_secs: f64,
    len_secs: f64,
    info: &crate::probe::VideoInfo,
    mut on_progress: F,
) -> Result<()>
where
    F: FnMut(f64) + Send,
{
    // Aspect-aware crop: handles BOTH a source wider than 9:16 (crop sides) and
    // one taller than 9:16 (crop top/bottom) — then scale to 1080x1920. The old
    // hardcoded `crop=ih*9/16:ih` only worked for wide sources and would distort
    // a too-tall one. Geometry comes from the shared, unit-tested crop_rect.
    let rect = crate::crop::crop_rect(info);
    let vf = format!(
        "crop={}:{}:{}:{},scale=1080:1920:flags=lanczos",
        rect.w, rect.h, rect.x, rect.y
    );

    let preset = preset_for(input);
    let start = format!("{start_secs}");
    let dur = format!("{len_secs}");

    let mut args: Vec<&str> = vec![
        "-y",
        "-ss", &start,      // fast input seek (before -i)
        "-i", input.to_str().context("non-utf8 input path")?,
        "-t", &dur,
        "-an",
        "-vf", &vf,
        "-preset", preset,
    ];
    args.extend_from_slice(TIKTOK_ENCODE);
    // Machine-readable progress to stdout; silence human stats on stderr.
    args.extend_from_slice(&["-progress", "pipe:1", "-nostats"]);
    args.push(output.to_str().context("non-utf8 output path")?);

    run_with_progress(&args, len_secs, &mut on_progress).await
}

/// Burn a pre-rendered caption overlay PNG onto a clip, with optional color
/// correction. `overlay_png` is None for color-correction-only (or a plain copy
/// if `cc_filter` is also None). `cc_filter` is the colorchannelmixer-based
/// filter string built by the caller (ported from build_cc_filter).
pub async fn burn_overlay<F>(
    video: &Path,
    overlay_png: Option<&Path>,
    cc_filter: Option<&str>,
    output: &Path,
    duration_secs: f64,
    mut on_progress: F,
) -> Result<()>
where
    F: FnMut(f64) + Send,
{
    let preset = preset_for(video);
    let video_s = video.to_str().context("non-utf8 video path")?;
    let output_s = output.to_str().context("non-utf8 output path")?;

    // Build the filter graph depending on what's requested.
    // overlay + cc:  [0:v]CC[base];[base][1:v]overlay
    // overlay only:  [0:v][1:v]overlay
    // cc only:       -vf CC
    let mut args: Vec<String> = vec!["-y".into()];

    match (overlay_png, cc_filter) {
        (Some(png), cc) => {
            let png_s = png.to_str().context("non-utf8 overlay path")?;
            args.extend(["-i".into(), video_s.into(), "-i".into(), png_s.into()]);
            let filter = match cc {
                Some(cc) => format!("[0:v]{cc}[base];[base][1:v]overlay=0:0"),
                None => "[0:v][1:v]overlay=0:0".to_string(),
            };
            args.extend(["-filter_complex".into(), filter]);
        }
        (None, Some(cc)) => {
            args.extend(["-i".into(), video_s.into(), "-vf".into(), cc.into()]);
        }
        (None, None) => {
            // Nothing to do but normalize/copy through the encoder.
            args.extend(["-i".into(), video_s.into()]);
        }
    }

    args.extend(["-preset".into(), preset.into()]);
    args.extend(TIKTOK_ENCODE.iter().map(|s| s.to_string()));
    args.extend(["-progress".into(), "pipe:1".into(), "-nostats".into()]);
    args.push(output_s.into());

    let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    run_with_progress(&arg_refs, duration_secs, &mut on_progress).await
}

/// Build the ffmpeg args for a source-preserving color-correct encode. Pure so
/// it's unit-testable: the whole point of this path (vs `burn_overlay`) is that
/// it must NOT force `-r 30` or re-encode audio.
fn cc_args<'a>(input: &'a str, cc_filter: &'a str, preset: &'a str, output: &'a str) -> Vec<&'a str> {
    let mut args: Vec<&str> = vec!["-y", "-i", input, "-vf", cc_filter, "-preset", preset];
    args.extend_from_slice(STANDARD_ENCODE);
    args.extend_from_slice(&["-progress", "pipe:1", "-nostats"]);
    args.push(output);
    args
}

/// Apply a color-correction `-vf` filter (from `build_cc_filter`) to `input`,
/// writing `output`, WITHOUT the TikTok framerate/bitrate/audio re-encode that
/// `burn_overlay` bakes in. `cc_filter` may be `"null"` (ffmpeg no-op).
pub async fn color_correct<F>(
    input: &Path,
    cc_filter: &str,
    output: &Path,
    duration_secs: f64,
    mut on_progress: F,
) -> Result<()>
where
    F: FnMut(f64) + Send,
{
    let input_s = input.to_str().context("non-utf8 input path")?;
    let output_s = output.to_str().context("non-utf8 output path")?;
    let preset = preset_for(input);
    let args = cc_args(input_s, cc_filter, preset, output_s);
    run_with_progress(&args, duration_secs, &mut on_progress).await
}

/// Spawn ffmpeg, stream `-progress` key=value lines from stdout, convert
/// `out_time_us` to a 0..1 fraction against `total_secs`, and call `on_progress`.
/// Errors include the tail of stderr on non-zero exit.
async fn run_with_progress<F>(args: &[&str], total_secs: f64, on_progress: &mut F) -> Result<()>
where
    F: FnMut(f64) + Send,
{
    let mut child = Command::new("ffmpeg")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("failed to spawn ffmpeg")?;

    let stdout = child.stdout.take().context("no ffmpeg stdout")?;
    let stderr = child.stderr.take().context("no ffmpeg stderr")?;

    // Drain stderr concurrently so the pipe never fills and blocks ffmpeg;
    // keep the tail for error reporting.
    let stderr_task = tokio::spawn(async move {
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

    // Parse progress on stdout. `out_time_us` is microseconds of output written.
    let mut reader = BufReader::new(stdout).lines();
    while let Some(line) = reader.next_line().await? {
        if let Some(v) = line.strip_prefix("out_time_us=") {
            if let Ok(us) = v.trim().parse::<f64>() {
                if total_secs > 0.0 {
                    let frac = (us / 1_000_000.0 / total_secs).clamp(0.0, 1.0);
                    on_progress(frac);
                }
            }
        }
    }

    let status = child.wait().await.context("ffmpeg wait failed")?;
    let tail = stderr_task.await.unwrap_or_default();

    if !status.success() {
        anyhow::bail!("ffmpeg exited {}: {}", status, tail.trim());
    }
    on_progress(1.0);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn color_correct_preserves_source_fps_and_audio() {
        let args = cc_args("in.mp4", "eq=contrast=1.1", "medium", "out.mp4");
        // The whole reason this path exists: it must NOT force a framerate or
        // re-encode audio (that's burn_overlay's TikTok encode).
        assert!(args.windows(2).any(|w| w == ["-c:a", "copy"]), "audio must be copied");
        assert!(!args.contains(&"-r"), "must not force an output framerate");
        assert!(!args.contains(&"aac"), "must not re-encode audio to AAC");
        // Sanity: the cc filter and I/O are wired.
        assert!(args.windows(2).any(|w| w == ["-vf", "eq=contrast=1.1"]));
        assert_eq!(args.first(), Some(&"-y"));
        assert_eq!(args.last(), Some(&"out.mp4"));
    }
}
