//! AI video generation providers.
//!
//! Every provider follows the same shape — submit a prompt, poll until the
//! video is ready, return its URL — so they share one `Provider` trait. v1 has
//! two adapters: xAI (Grok Imagine) and Replicate (per-model input builders).
//!
//! Leaner than the old 7-entry registry: the recon flagged that the Wan family
//! had three near-identical entries and pruna×2 was one model behind a flag.
//! v1 keeps a curated set; more can be added to MODELS without new code.

mod replicate;
mod xai;

use serde::Serialize;

/// Parameters for one generation request (the subset the spine needs).
#[derive(Debug, Clone)]
pub struct GenParams {
    pub prompt: String,
    pub aspect_ratio: String,
    pub resolution: String,
    pub duration: u32,
    /// Optional first-frame image as a data URI (for image-to-video models).
    pub image_data_uri: Option<String>,
}

/// A configured generation provider. Implementors own the submit+poll loop and
/// return the finished video's URL. They must NEVER include the API key in any
/// returned error string.
#[async_trait::async_trait]
pub trait Provider: Send + Sync {
    /// Submit the job, poll to completion, and return the output video URL.
    /// `progress` is called with a coarse 0.0..1.0 as the job advances.
    async fn generate(
        &self,
        client: &reqwest::Client,
        model_id: &str,
        params: &GenParams,
        progress: &(dyn Fn(f64) + Send + Sync),
    ) -> anyhow::Result<String>;
}

/// One selectable model in the UI/registry.
#[derive(Debug, Clone, Serialize)]
pub struct ModelInfo {
    /// Stable id used in the API (e.g. "grok", "hailuo", "wan-t2v").
    pub id: &'static str,
    pub name: &'static str,
    pub group: &'static str,
    /// Which provider adapter handles it.
    pub provider: ProviderKind,
    /// The provider-specific model id passed to the adapter.
    pub model_id: &'static str,
    /// Which env var must be set for this model to be available.
    pub key_env: &'static str,
    /// Whether this model requires a first-frame image (image-to-video).
    pub needs_image: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ProviderKind {
    Xai,
    Replicate,
}

/// The curated model registry. Lean on purpose — add rows, not code.
pub const MODELS: &[ModelInfo] = &[
    ModelInfo {
        id: "grok",
        name: "Grok Imagine",
        group: "xAI",
        provider: ProviderKind::Xai,
        model_id: "grok-imagine-video",
        key_env: "XAI_API_KEY",
        needs_image: false,
    },
    ModelInfo {
        id: "hailuo",
        name: "Hailuo 2.3",
        group: "MiniMax",
        provider: ProviderKind::Replicate,
        model_id: "minimax/hailuo-2.3",
        key_env: "REPLICATE_API_TOKEN",
        needs_image: false,
    },
    ModelInfo {
        id: "wan-t2v",
        name: "Wan 2.2 Text-to-Video",
        group: "Wan",
        provider: ProviderKind::Replicate,
        model_id: "wan-video/wan-2.2-t2v-fast",
        key_env: "REPLICATE_API_TOKEN",
        needs_image: false,
    },
    ModelInfo {
        id: "wan-i2v",
        name: "Wan 2.2 Image-to-Video",
        group: "Wan",
        provider: ProviderKind::Replicate,
        model_id: "wan-video/wan-2.2-i2v-a14b",
        key_env: "REPLICATE_API_TOKEN",
        needs_image: true,
    },
];

/// Truncate a string to at most `max` CHARACTERS for safe inclusion in an
/// error/log line. Char-boundary-safe — slicing `&s[..byte]` would panic on a
/// multi-byte UTF-8 boundary (common in API error bodies with emoji / curly
/// quotes / non-ASCII). Shared by both provider adapters.
pub(crate) fn truncate(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        None => s.to_string(),
        Some((i, _)) => format!("{}…", &s[..i]),
    }
}

/// Look up a model by its public id.
pub fn find_model(id: &str) -> Option<&'static ModelInfo> {
    MODELS.iter().find(|m| m.id == id)
}

/// Models whose key env var is set (i.e. usable right now).
pub fn available_models() -> Vec<&'static ModelInfo> {
    MODELS
        .iter()
        .filter(|m| {
            std::env::var(m.key_env)
                .map(|v| !v.trim().is_empty())
                .unwrap_or(false)
        })
        .collect()
}

/// Build the concrete provider for a model, reading its key from env.
/// Returns None if the key is missing.
pub fn provider_for(model: &ModelInfo) -> Option<Box<dyn Provider>> {
    let key = std::env::var(model.key_env).ok().filter(|v| !v.trim().is_empty())?;
    match model.provider {
        ProviderKind::Xai => Some(Box::new(xai::XaiProvider::new(key))),
        ProviderKind::Replicate => Some(Box::new(replicate::ReplicateProvider::new(key))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncate_is_char_boundary_safe() {
        // The bug this guards: slicing &s[..max] at a byte index would panic on
        // a multi-byte char. "😀" is 4 bytes; truncating to 1 char must not panic.
        let s = "😀😀😀error";
        let out = truncate(s, 2);
        assert!(out.ends_with('…'));
        assert_eq!(out.chars().count(), 3); // 2 chars + ellipsis
        // Short strings pass through unchanged.
        assert_eq!(truncate("ok", 10), "ok");
        // ASCII still works.
        assert!(truncate("abcdef", 3).starts_with("abc"));
    }

    #[test]
    fn registry_lookups() {
        assert!(find_model("grok").is_some());
        assert!(find_model("hailuo").is_some());
        assert!(find_model("nope").is_none());
        // wan-i2v requires an image; grok does not.
        assert!(find_model("wan-i2v").unwrap().needs_image);
        assert!(!find_model("grok").unwrap().needs_image);
    }
}
