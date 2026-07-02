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

/// Build the model-specific `input` object. Mirrors the Python _build_* fns for
/// the four curated models; rejects models we didn't wire up.
fn build_input(model_id: &str, p: &GenParams) -> anyhow::Result<Value> {
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
                "prompt_optimizer": true,
            });
            if let Some(img) = &p.image_data_uri {
                inp["first_frame_image"] = json!(img);
            }
            Ok(inp)
        }
        "wan-video/wan-2.2-t2v-fast" => Ok(json!({
            "prompt": p.prompt,
            "aspect_ratio": p.aspect_ratio,
            "resolution": if p.resolution.is_empty() { "480p".into() } else { p.resolution.clone() },
            "num_frames": 81,
            "frames_per_second": 16,
            "sample_shift": 12,
            "go_fast": true,
            "interpolate_output": true,
        })),
        "wan-video/wan-2.2-i2v-a14b" => {
            let img = p
                .image_data_uri
                .as_ref()
                .ok_or_else(|| anyhow!("Wan I2V requires a first-frame image"))?;
            Ok(json!({
                "prompt": p.prompt,
                "image": img,
                "resolution": if p.resolution.is_empty() { "480p".into() } else { p.resolution.clone() },
                "num_frames": 81,
                "frames_per_second": 16,
                "sample_steps": 40,
                "sample_shift": 5,
                "go_fast": false,
            }))
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
        }
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
