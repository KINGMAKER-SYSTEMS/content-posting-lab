//! `media` — ffmpeg / ffprobe / yt-dlp orchestration by shelling out to the
//! binaries (no libav C bindings; the research pass confirmed shell-out is the
//! pragmatic, deploy-friendly choice since we only do whole-file transcodes).
//!
//! v1 surface (the spine): probe a file, clip a long video into 9:16 shorts,
//! and burn a caption-overlay PNG + color correction onto a clip.

pub mod cc;
pub mod crop;
pub mod ffmpeg;
pub mod probe;

pub use cc::{build_cc_filter, ColorCorrection};
pub use crop::{crop_rect, multi_crop, multi_crop_offsets, output_count, CropMode, CropRect};
pub use probe::{probe, VideoInfo};
