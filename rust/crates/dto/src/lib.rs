//! Shared DTOs — the API wire contract, usable from both the native server and
//! a wasm client. Pure serde, no sqlx/tokio, so this crate builds for wasm32.
//!
//! `clab_core` re-exports these and attaches the sqlx derives in its own module,
//! keeping the storage concern out of the shared contract. This is what lets a
//! Yew/WASM frontend import the EXACT same `Asset`/`Job` shapes the server sends
//! — change a field here and both ends fail to compile.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AssetKind {
    Generated,
    Clip,
    Burned,
    Source,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AssetStatus {
    Queued,
    Processing,
    Ready,
    Failed,
}

// NB: PartialEq on these is required because Yew state (use_state) needs it.
// A small but real consequence of sharing types with a Yew client — the wire
// contract picks up a frontend-framework constraint.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Asset {
    pub id: String,
    pub project: String,
    pub kind: AssetKind,
    pub status: AssetStatus,
    pub path: Option<String>,
    pub parent_id: Option<String>,
    pub meta: Option<String>,
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum JobKind {
    Clip,
    Burn,
    Generate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum JobStatus {
    Queued,
    Processing,
    Done,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub project: String,
    pub kind: JobKind,
    pub status: JobStatus,
    pub progress: f64,
    pub source_asset_id: Option<String>,
    pub params: Option<String>,
    pub result_asset_ids: String,
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// One selectable generation model (subset surfaced to clients).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModelInfo {
    pub id: String,
    pub name: String,
    pub group: String,
    pub model_id: String,
    pub key_env: String,
    pub needs_image: bool,
}

// ── API request/response envelopes (shared by server + client) ──────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProvidersResponse {
    pub models: Vec<ModelInfo>,
}

/// Body for POST /api/jobs/generate.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GenerateRequest {
    pub project: String,
    pub model: String,
    pub prompt: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub aspect_ratio: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_data_uri: Option<String>,
    /// Optional multi-crop of the generated 16:9 source into N 9:16 clips:
    /// "single" (default), "dual", "triptych", "both". The yield multiplier —
    /// one generation → up to 5 framed shorts.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub crop_mode: Option<String>,
}

/// Response from POST /api/jobs/generate.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GenerateResponse {
    pub job: Job,
    pub asset_id: String,
}

/// Response from GET /api/jobs/{id}.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobResponse {
    pub job: Job,
}

/// Response from GET /api/assets.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssetsResponse {
    pub assets: Vec<Asset>,
}
