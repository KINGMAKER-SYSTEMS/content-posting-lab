//! Domain model.
//!
//! The heart of the rewrite is the **Asset**: a single durable record for every
//! piece of media moving through the pipeline. In the old app a "video" was an
//! ephemeral entry in a browser Zustand store, handed off to Burn via one-shot
//! fields that evaporated on refresh — and Burn then *guessed* which files to
//! use by folder. That is why the generate→burn flow was unintuitive and lossy.
//!
//! Here, every generated video, every clip, and every burned output is a row
//! with a stable id and a `parent_id` lineage:
//!
//! ```text
//! generated video --clip--> clip --burn--> burned
//!      (Asset)              (Asset)        (Asset)
//! ```
//!
//! Burn operates on explicit asset ids. No folder-guessing, no silent
//! selection-widening, survives reloads.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// What kind of media an asset is — and implicitly, where it sits in the chain.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
pub enum AssetKind {
    /// AI-generated source video (from a provider).
    Generated,
    /// A 9:16 short cut from a longer source.
    Clip,
    /// A finished, caption-burned, TikTok-ready video.
    Burned,
    /// A raw long-form source uploaded for clipping.
    Source,
}

/// Lifecycle status of an asset's underlying file / job.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
pub enum AssetStatus {
    /// Job accepted, not started.
    Queued,
    /// Job running (generating, clipping, or burning).
    Processing,
    /// File exists on disk / in R2 and is usable.
    Ready,
    /// Job failed; see `error`.
    Failed,
}

/// One durable piece of media. Mirrors the `assets` table.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Asset {
    pub id: String,
    pub project: String,
    pub kind: AssetKind,
    pub status: AssetStatus,
    /// Project-relative path (or R2 key) of the media file. None until ready.
    pub path: Option<String>,
    /// Lineage: the asset this was derived from (clip→source, burned→clip).
    pub parent_id: Option<String>,
    /// Free-form provenance (provider name, prompt, clip index, etc.) as JSON.
    pub meta: Option<String>,
    /// Error message when status = failed.
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// A project groups all assets for one campaign / piece of work.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Project {
    pub name: String,
    pub created_at: DateTime<Utc>,
}

/// Kind of background job.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
pub enum JobKind {
    Clip,
    Burn,
    Generate,
}

/// Status of a background job.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
pub enum JobStatus {
    Queued,
    Processing,
    Done,
    Failed,
}

/// A durable background job (clip/burn/generate). Mirrors the `jobs` table.
/// Unlike the old in-memory dicts, this survives restarts.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Job {
    pub id: String,
    pub project: String,
    pub kind: JobKind,
    pub status: JobStatus,
    pub progress: f64,
    pub source_asset_id: Option<String>,
    pub params: Option<String>,
    /// JSON array of produced asset ids.
    pub result_asset_ids: String,
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

// ── Distribution ──────────────────────────────────────────────────────────

/// A page = one TikTok/IG account we run content for (the roster entry).
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Page {
    pub id: String,
    pub name: String,
    pub project: Option<String>,
    pub r2_prefix: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// A poster = a person who runs a set of pages, with their own Telegram group.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Poster {
    pub id: String,
    pub name: String,
    pub chat_id: i64,
    pub sounds_topic_id: Option<i64>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// One delivered (or pending) piece of content for a page. The key fix vs the
/// old app: ONE row per real Telegram message. Forwarding is tracked per-row
/// (`forwarded_to`), never by advancing a shared message-id watermark over a
/// brute-forced range — so content can't be silently skipped or double-sent.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct InventoryItem {
    pub id: String,
    pub page_id: String,
    pub asset_id: Option<String>,
    pub staging_msg_id: Option<i64>,
    /// poster_id this was forwarded to; None = still pending.
    pub forwarded_to: Option<String>,
    pub forwarded_msg_id: Option<i64>,
    pub created_at: DateTime<Utc>,
    pub forwarded_at: Option<DateTime<Utc>>,
}

/// A scraped caption bank entry — text harvested from a TikTok profile, later
/// burned onto videos. Unlike the old app (caption paired to video by `i %
/// len` modulo — unrelated text on every clip), captions are first-class rows
/// so a future version can pair them by mood/intent instead of position.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Caption {
    pub id: String,
    pub project: String,
    /// Source TikTok username the caption was scraped from.
    pub source: String,
    pub text: String,
    /// Optional mood tag (sad|hype|love|funny|chill) for future smart pairing.
    pub mood: Option<String>,
    pub created_at: DateTime<Utc>,
}
