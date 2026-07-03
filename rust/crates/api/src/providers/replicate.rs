//! Replicate adapter. Port of providers/replicate.py's submit→poll loop plus
//! the per-model input builders for the curated v1 models.

use std::time::Duration;

use anyhow::{anyhow, Context};
use serde_json::{json, Value};

use super::{truncate, GenParams, Provider};

const API: &str = "https://api.replicate.com/v1";

pub struct ReplicateProvider {
    key: String,
}

impl ReplicateProvider {
    pub fn new(key: String) -> Self {
        Self { key }
    }
}

type Extra = serde_json::Map<String, Value>;

// Typed readers over the model-specific `extra` map (frontend-supplied knobs),
// each falling back to the Python builder's default.
fn ex_i(ex: &Extra, k: &str, d: i64) -> i64 {
    ex.get(k).and_then(Value::as_i64).unwrap_or(d)
}
fn ex_f(ex: &Extra, k: &str, d: f64) -> f64 {
    ex.get(k).and_then(Value::as_f64).unwrap_or(d)
}
fn ex_b(ex: &Extra, k: &str, d: bool) -> bool {
    ex.get(k).and_then(Value::as_bool).unwrap_or(d)
}
fn ex_s<'a>(ex: &'a Extra, k: &str) -> Option<&'a str> {
    ex.get(k).and_then(Value::as_str).filter(|s| !s.is_empty())
}

/// Build the model-specific `input` object. Faithful port of the Python
/// `_build_*_input` fns (providers/replicate.py) — reads the frontend's
/// model-specific knobs from `p.extra`, falling back to the same defaults;
/// rejects models we didn't wire up.
fn build_input(model_id: &str, p: &GenParams) -> anyhow::Result<Value> {
    let ex = &p.extra;
    match model_id {
        "minimax/hailuo-2.3" => {
            // Snap duration to {6,10}; 1080p forces 6s.
            let mut duration = if p.duration >= 8 { 10 } else { 6 };
            let mut resolution = p.resolution.clone();
            if resolution != "768p" && resolution != "1080p" {
                resolution = "768p".into();
            }
            if resolution == "1080p" {
                duration = 6;
            }
            let mut inp = json!({
                "prompt": p.prompt,
                "duration": duration,
                "resolution": resolution,
                "prompt_optimizer": ex_b(ex, "optimize_prompt", true),
            });
            if let Some(img) = &p.image_data_uri {
                inp["first_frame_image"] = json!(img);
            }
            Ok(inp)
        }
        "wan-video/wan-2.2-t2v-fast" => {
            let mut inp = json!({
                "prompt": p.prompt,
                "aspect_ratio": if p.aspect_ratio.is_empty() { "16:9".into() } else { p.aspect_ratio.clone() },
                "resolution": if p.resolution.is_empty() { "480p".into() } else { p.resolution.clone() },
                "num_frames": ex_i(ex, "num_frames", 81),
                "frames_per_second": ex_i(ex, "frames_per_second", 16),
                "sample_shift": ex_i(ex, "sample_shift", 12),
                "go_fast": ex_b(ex, "go_fast", true),
                "interpolate_output": ex_b(ex, "interpolate_output", true),
            });
            if let Some(np) = ex_s(ex, "negative_prompt") {
                inp["negative_prompt"] = json!(np);
            }
            if let Some(lora) = ex_s(ex, "lora_weights_transformer") {
                inp["lora_weights_transformer"] = json!(lora);
                inp["lora_scale_transformer"] = json!(ex_f(ex, "lora_scale_transformer", 1.0));
            }
            if let Some(lora2) = ex_s(ex, "lora_weights_transformer_2") {
                inp["lora_weights_transformer_2"] = json!(lora2);
                inp["lora_scale_transformer_2"] = json!(ex_f(ex, "lora_scale_transformer_2", 1.0));
            }
            Ok(inp)
        }
        "wan-video/wan-2.2-i2v-a14b" => {
            let img = p
                .image_data_uri
                .as_ref()
                .ok_or_else(|| anyhow!("Wan I2V requires a first-frame image"))?;
            let mut inp = json!({
                "prompt": p.prompt,
                "image": img,
                "resolution": if p.resolution.is_empty() { "480p".into() } else { p.resolution.clone() },
                "num_frames": ex_i(ex, "num_frames", 81).min(100),
                "frames_per_second": ex_i(ex, "frames_per_second", 16).min(24),
                "sample_steps": ex_i(ex, "sample_steps", 40),
                "sample_shift": ex_i(ex, "sample_shift", 5),
                "go_fast": ex_b(ex, "go_fast", false),
            });
            if let Some(np) = ex_s(ex, "negative_prompt") {
                inp["negative_prompt"] = json!(np);
            }
            Ok(inp)
        }
        other => Err(anyhow!("no input builder for Replicate model: {other}")),
    }
}

#[async_trait::async_trait]
impl Provider for ReplicateProvider {
    async fn generate(
        &self,
        client: &reqwest::Client,
        model_id: &str,
        params: &GenParams,
        progress: &(dyn Fn(f64) + Send + Sync),
    ) -> anyhow::Result<String> {
        let input = build_input(model_id, params)?;

        progress(0.05);
        let resp = client
            .post(format!("{API}/models/{model_id}/predictions"))
            .header("Authorization", format!("Token {}", self.key))
            .json(&json!({ "input": input }))
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .context("Replicate submit request failed")?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow!("Replicate submit failed ({status}): {}", truncate(&body, 300)));
        }

        let pred: Value = resp.json().await.context("Replicate submit: bad JSON")?;
        let pred_id = pred
            .get("id")
            .and_then(|x| x.as_str())
            .ok_or_else(|| anyhow!("Replicate submit: no prediction id"))?
            .to_string();

        progress(0.15);
        let poll_url = format!("{API}/predictions/{pred_id}");
        let deadline = tokio::time::Instant::now() + Duration::from_secs(600);
        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err(anyhow!("Replicate generation timed out after 600s (prediction {pred_id})"));
            }
            tokio::time::sleep(Duration::from_secs(5)).await;

            let r = client
                .get(&poll_url)
                .header("Authorization", format!("Token {}", self.key))
                .timeout(Duration::from_secs(30))
                .send()
                .await
                .context("Replicate poll request failed")?;
            let data: Value = r.json().await.context("Replicate poll: bad JSON")?;
            let status = data.get("status").and_then(|x| x.as_str()).unwrap_or("");

            match status {
                "succeeded" => {
                    progress(0.6);
                    return extract_output(&data);
                }
                "failed" | "canceled" => {
                    let err = data
                        .get("error")
                        .and_then(|x| x.as_str())
                        .unwrap_or("unknown error (no details from API)");
                    return Err(anyhow!("Replicate {status}: {}", truncate(err, 300)));
                }
                _ => {} // starting / processing — keep polling
            }
        }
    }
}

/// Replicate output is a string URL or a list of URLs — take the first.
fn extract_output(data: &Value) -> anyhow::Result<String> {
    match data.get("output") {
        Some(Value::String(s)) => Ok(s.clone()),
        Some(Value::Array(arr)) => arr
            .first()
            .and_then(|x| x.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| anyhow!("Replicate: empty output array")),
        other => Err(anyhow!("Replicate: unexpected output shape: {other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(image: Option<&str>) -> GenParams {
        GenParams {
            prompt: "a neon city".into(),
            aspect_ratio: "9:16".into(),
            resolution: "768p".into(),
            duration: 10,
            image_data_uri: image.map(|s| s.to_string()),
            extra: Default::default(),
        }
    }

    fn params_with(image: Option<&str>, extra: Value) -> GenParams {
        let mut p = params(image);
        p.extra = extra.as_object().cloned().unwrap_or_default();
        p
    }

    #[test]
    fn wan_t2v_defaults_when_no_extra() {
        let inp = build_input("wan-video/wan-2.2-t2v-fast", &params(None)).unwrap();
        assert_eq!(inp["num_frames"], 81);
        assert_eq!(inp["sample_shift"], 12);
        assert_eq!(inp["go_fast"], true);
        // No negative_prompt / lora keys when unset.
        assert!(inp.get("negative_prompt").is_none());
        assert!(inp.get("lora_weights_transformer").is_none());
    }

    #[test]
    fn wan_t2v_threads_advanced_params() {
        // This is the whole parity fix: frontend knobs must reach the input.
        let inp = build_input(
            "wan-video/wan-2.2-t2v-fast",
            &params_with(
                None,
                json!({
                    "num_frames": 121,
                    "sample_shift": 8,
                    "go_fast": false,
                    "negative_prompt": "morphing, 123 text",
                    "lora_weights_transformer": "https://hf/lora.safetensors",
                    "lora_scale_transformer": 0.8
                }),
            ),
        )
        .unwrap();
        assert_eq!(inp["num_frames"], 121);
        assert_eq!(inp["sample_shift"], 8);
        assert_eq!(inp["go_fast"], false);
        // A numeric-looking negative prompt must stay a STRING, not coerce to a number.
        assert_eq!(inp["negative_prompt"], "morphing, 123 text");
        assert_eq!(inp["lora_weights_transformer"], "https://hf/lora.safetensors");
        assert_eq!(inp["lora_scale_transformer"], 0.8);
    }

    #[test]
    fn wan_i2v_caps_num_frames_at_100_and_fps_at_24() {
        let inp = build_input(
            "wan-video/wan-2.2-i2v-a14b",
            &params_with(Some("data:image/png;base64,AAAA"), json!({ "num_frames": 121, "frames_per_second": 30 })),
        )
        .unwrap();
        assert_eq!(inp["num_frames"], 100);
        assert_eq!(inp["frames_per_second"], 24);
    }

    #[test]
    fn hailuo_optimize_prompt_toggle_threads() {
        let inp = build_input(
            "minimax/hailuo-2.3",
            &params_with(None, json!({ "optimize_prompt": false })),
        )
        .unwrap();
        assert_eq!(inp["prompt_optimizer"], false);
    }

    #[test]
    fn hailuo_snaps_duration_and_1080_forces_6() {
        let mut p = params(None);
        p.resolution = "1080p".into();
        p.duration = 10;
        let inp = build_input("minimax/hailuo-2.3", &p).unwrap();
        // 1080p must force duration back to 6.
        assert_eq!(inp["duration"], 6);
        assert_eq!(inp["resolution"], "1080p");
    }

    #[test]
    fn hailuo_duration_snaps_to_6_when_low() {
        let mut p = params(None);
        p.resolution = "768p".into();
        p.duration = 4;
        let inp = build_input("minimax/hailuo-2.3", &p).unwrap();
        assert_eq!(inp["duration"], 6);
    }

    #[test]
    fn wan_i2v_requires_image() {
        let err = build_input("wan-video/wan-2.2-i2v-a14b", &params(None)).unwrap_err();
        assert!(err.to_string().contains("requires a first-frame image"));
        // With an image it builds and includes it.
        let ok = build_input("wan-video/wan-2.2-i2v-a14b", &params(Some("data:image/png;base64,AAAA"))).unwrap();
        assert_eq!(ok["image"], "data:image/png;base64,AAAA");
    }

    #[test]
    fn unknown_model_rejected() {
        assert!(build_input("some/unwired-model", &params(None)).is_err());
    }

    #[test]
    fn extract_output_handles_shapes() {
        assert_eq!(
            extract_output(&json!({ "output": "https://x/v.mp4" })).unwrap(),
            "https://x/v.mp4"
        );
        assert_eq!(
            extract_output(&json!({ "output": ["https://x/a.mp4", "https://x/b.mp4"] })).unwrap(),
            "https://x/a.mp4"
        );
        assert!(extract_output(&json!({ "output": [] })).is_err());
        assert!(extract_output(&json!({ "output": 42 })).is_err());
        assert!(extract_output(&json!({})).is_err());
    }
}
