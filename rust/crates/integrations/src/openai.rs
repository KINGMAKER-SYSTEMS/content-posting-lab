//! OpenAI chat-completions client — the two lab uses are GPT-4.1 vision OCR
//! (caption scraping) and GPT-4.1-mini fuzzy matching (sound sync).
//!
//! ponytail: messages are raw `serde_json::Value`s, not a typed message enum —
//! both callers build one small payload; type the API when a third caller
//! disagrees about shape.

use serde_json::{json, Value};

use crate::http::client;

/// Env override for tests / proxies (same pattern as XAI_BASE_URL).
fn base_url() -> String {
    std::env::var("OPENAI_BASE_URL").unwrap_or_else(|_| "https://api.openai.com".into())
}

fn api_key() -> anyhow::Result<String> {
    std::env::var("OPENAI_API_KEY")
        .ok()
        .filter(|k| !k.trim().is_empty())
        .ok_or_else(|| anyhow::anyhow!("OPENAI_API_KEY not configured"))
}

/// Whether OpenAI-backed features can run right now.
pub fn configured() -> bool {
    api_key().is_ok()
}

/// One chat-completion round trip; returns the assistant message content.
///
/// `messages` follow the OpenAI wire shape, e.g.
/// `[{"role":"user","content":"..."}]` or vision parts
/// `[{"role":"user","content":[{"type":"text",...},{"type":"image_url",...}]}]`.
pub async fn chat(model: &str, messages: Vec<Value>) -> anyhow::Result<String> {
    let resp = client()
        .post(format!("{}/v1/chat/completions", base_url()))
        .bearer_auth(api_key()?)
        .json(&json!({ "model": model, "messages": messages }))
        .send()
        .await?;

    let status = resp.status();
    let body: Value = resp.json().await?;
    if !status.is_success() {
        let msg = body["error"]["message"].as_str().unwrap_or("unknown error");
        anyhow::bail!("openai {status}: {msg}");
    }
    body["choices"][0]["message"]["content"]
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| anyhow::anyhow!("openai: empty completion"))
}

/// Convenience: single text prompt → completion text.
pub async fn chat_text(model: &str, prompt: &str) -> anyhow::Result<String> {
    chat(model, vec![json!({"role": "user", "content": prompt})]).await
}

/// Convenience: vision prompt over one base64 PNG/JPEG (`data:` URL built here).
pub async fn chat_vision(
    model: &str,
    prompt: &str,
    image_b64: &str,
    mime: &str,
) -> anyhow::Result<String> {
    chat(
        model,
        vec![json!({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": format!("data:{mime};base64,{image_b64}")}}
            ]
        })],
    )
    .await
}
