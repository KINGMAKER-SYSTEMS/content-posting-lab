//! `integrations` — clients for every external service the lab talks to.
//!
//! One shared reqwest client ([`http::client`]); each service gets a thin
//! module. Base URLs are overridable via env vars so tests can point at a
//! local mock (same pattern as the providers' XAI_BASE_URL).
//!
//! Wave-1 file ownership (do not cross-edit):
//! - `openai`           — scaffold (shared by captions + sounds)
//! - `notion`, `hub`    — sounds/roster workstream
//! - `r2`, `email`, `drive` — integrations workstream

pub mod drive;
pub mod email;
pub mod http;
pub mod hub;
pub mod notion;
pub mod openai;
pub mod r2;
