//! xAI Grok Imagine Video adapter. Port of providers/grok.py.

use std::time::Duration;

use anyhow::{anyhow, Context};
use serde_json::json;

use super::{truncate, GenParams, Provider};

pub struct XaiProvider {
    key: String,
    base: String,
}

impl XaiProvider {
    pub fn new(key: String) -> Self {
        // XAI_BASE_URL override exists for integration testing against a mock;
        // defaults to the real endpoint.
        let base = std::env::var("XAI_BASE_URL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| "https://api.x.ai/v1".to_string());
        Self { key, base }
    }
}

#[async_trait::async_trait]
impl Provider for XaiProvider {
    async fn generate(
        &self,
        client: &reqwest::Client,
        _model_id: &str,
        params: &GenParams,
        progress: &(dyn Fn(f64) + Send + Sync),
    ) -> anyhow::Result<String> {
        let mut payload = json!({
            "model": "grok-imagine-video",
            "prompt": params.prompt,
            "duration": params.duration,
            "aspect_ratio": params.aspect_ratio,
            "resolution": params.resolution,
        });
        if let Some(img) = &params.image_data_uri {
            payload["image"] = json!({ "url": img });
        }

        progress(0.05);
        let resp = client
            .post(format!("{}/videos/generations", self.base))
            .bearer_auth(&self.key)
            .json(&payload)
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .context("xAI submit request failed")?;

        if !resp.status().is_success() {
            // Body may echo request fields but never our Authorization header.
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow!("xAI submit failed ({status}): {}", truncate(&body, 300)));
        }

        let v: serde_json::Value = resp.json().await.context("xAI submit: bad JSON")?;
        let request_id = v
            .get("request_id")
            .and_then(|x| x.as_str())
            .ok_or_else(|| anyhow!("xAI submit: no request_id in response"))?
            .to_string();

        progress(0.15);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(600);
        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err(anyhow!("xAI generation timed out after 600s"));
            }
            tokio::time::sleep(Duration::from_secs(5)).await;

            let r = client
                .get(format!("{}/videos/{request_id}", self.base))
                .bearer_auth(&self.key)
                .timeout(Duration::from_secs(30))
                .send()
                .await
                .context("xAI poll request failed")?;
            let data: serde_json::Value = r.json().await.context("xAI poll: bad JSON")?;

            if let Some(url) = data.get("video").and_then(|x| x.get("url")).and_then(|x| x.as_str())
            {
                progress(0.6);
                return Ok(url.to_string());
            }
            if let Some(err) = data.get("error") {
                return Err(anyhow!("xAI error: {}", truncate(&err.to_string(), 300)));
            }
            if data.get("status").and_then(|x| x.as_str()) == Some("expired") {
                return Err(anyhow!("xAI request expired"));
            }
        }
    }
}
