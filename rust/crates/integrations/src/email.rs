//! Cloudflare Email Routing API client (port of `services/email_routing.py`).
//!
//! Manages forwarding rules (`alias@domain -> destination`) and destination
//! addresses via the CF REST API. Pure proxy — rules live in Cloudflare, not
//! local state.
//!
//! Env contract (same names as the Python app):
//!   CF_API_TOKEN    — API token with Email Routing permissions
//!   CF_ZONE_ID      — Zone ID for the domain
//!   CF_ACCOUNT_ID   — Account ID
//!   CF_EMAIL_DOMAIN — Domain for email aliases (e.g. yourdomain.com)
//!   CF_API_BASE_URL — override for tests (default https://api.cloudflare.com/client/v4)
//!
//! All functions take an [`EmailConfig`] (built once via [`EmailConfig::from_env`])
//! so tests can construct configs directly instead of racing on env vars.

use reqwest::Method;
use serde_json::{json, Value};

use crate::http::client;

/// Default Cloudflare API base.
pub const DEFAULT_BASE_URL: &str = "https://api.cloudflare.com/client/v4";

/// Resolved CF Email Routing configuration. Existence == configured.
#[derive(Debug, Clone)]
pub struct EmailConfig {
    pub token: String,
    pub zone_id: String,
    pub account_id: String,
    pub domain: String,
    pub base_url: String,
}

fn env(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

impl EmailConfig {
    /// Build from env. `None` when any required var is missing — endpoints then
    /// report "not configured" instead of failing mid-request.
    pub fn from_env() -> Option<Self> {
        Some(Self {
            token: env("CF_API_TOKEN")?,
            zone_id: env("CF_ZONE_ID")?,
            account_id: env("CF_ACCOUNT_ID")?,
            domain: env("CF_EMAIL_DOMAIN")?,
            base_url: env("CF_API_BASE_URL").unwrap_or_else(|| DEFAULT_BASE_URL.to_string()),
        })
    }
}

/// Whether CF Email Routing is fully configured.
pub fn configured() -> bool {
    EmailConfig::from_env().is_some()
}

/// One CF API round trip; returns the parsed envelope JSON on 2xx.
async fn cf_request(
    cfg: &EmailConfig,
    method: Method,
    path: &str,
    body: Option<Value>,
) -> anyhow::Result<Value> {
    let url = format!("{}{}", cfg.base_url, path);
    let mut req = client().request(method, url).bearer_auth(&cfg.token);
    if let Some(b) = body {
        req = req.json(&b);
    }
    let resp = req.send().await?;
    let status = resp.status();
    if !status.is_success() {
        let text = resp.text().await.unwrap_or_default();
        // CF errors carry an `errors[]` array; surface the raw body (truncated)
        // so ops can see the actual reason, mirroring httpx raise_for_status.
        let snippet: String = text.chars().take(500).collect();
        anyhow::bail!("cloudflare {status}: {snippet}");
    }
    Ok(resp.json().await?)
}

/// List all email routing rules for the zone.
pub async fn list_rules(cfg: &EmailConfig) -> anyhow::Result<Vec<Value>> {
    let data = cf_request(
        cfg,
        Method::GET,
        &format!("/zones/{}/email/routing/rules", cfg.zone_id),
        None,
    )
    .await?;
    Ok(data["result"].as_array().cloned().unwrap_or_default())
}

/// Create an email routing rule: `alias@domain -> destination`.
/// Returns the CF `result` object for the new rule.
pub async fn create_rule(
    cfg: &EmailConfig,
    alias: &str,
    destination: &str,
) -> anyhow::Result<Value> {
    let payload = json!({
        "actions": [{ "type": "forward", "value": [destination] }],
        "matchers": [{
            "type": "literal",
            "field": "to",
            "value": format!("{alias}@{}", cfg.domain),
        }],
        "enabled": true,
        "name": format!("Roster: {alias}"),
    });
    let data = cf_request(
        cfg,
        Method::POST,
        &format!("/zones/{}/email/routing/rules", cfg.zone_id),
        Some(payload),
    )
    .await?;
    Ok(data["result"].clone())
}

/// Delete an email routing rule.
pub async fn delete_rule(cfg: &EmailConfig, rule_id: &str) -> anyhow::Result<()> {
    cf_request(
        cfg,
        Method::DELETE,
        &format!("/zones/{}/email/routing/rules/{rule_id}", cfg.zone_id),
        None,
    )
    .await?;
    Ok(())
}

/// List destination addresses for the account (verified ones carry a
/// truthy `verified` timestamp; unverified are `null`).
pub async fn list_destinations(cfg: &EmailConfig) -> anyhow::Result<Vec<Value>> {
    let data = cf_request(
        cfg,
        Method::GET,
        &format!("/accounts/{}/email/routing/addresses", cfg.account_id),
        None,
    )
    .await?;
    Ok(data["result"].as_array().cloned().unwrap_or_default())
}

/// Add a new destination address (triggers a verification email from CF).
/// Returns the CF `result` object.
pub async fn add_destination(cfg: &EmailConfig, email: &str) -> anyhow::Result<Value> {
    let data = cf_request(
        cfg,
        Method::POST,
        &format!("/accounts/{}/email/routing/addresses", cfg.account_id),
        Some(json!({ "email": email })),
    )
    .await?;
    Ok(data["result"].clone())
}

// ── test support ─────────────────────────────────────────────────────────────

/// Minimal HTTP/1.1 mock server for integration-client tests (no live API
/// calls). One canned handler answers every request; each response closes the
/// connection so reqwest never reuses a stale socket. Shared with the other
/// wave-1 integration modules' tests (crate-internal, test builds only).
#[cfg(test)]
pub(crate) mod test_http {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
        haystack.windows(needle.len()).position(|w| w == needle)
    }

    /// Spawn the mock; returns (base_url, join_handle). `handler` receives the
    /// raw request head (start-line + headers) and body, returns (status, body).
    pub(crate) async fn spawn<F>(handler: F) -> (String, tokio::task::JoinHandle<()>)
    where
        F: Fn(&str, &str) -> (u16, String) + Send + Sync + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                let mut buf: Vec<u8> = Vec::with_capacity(8192);
                let mut tmp = [0u8; 4096];
                // Read until end of headers.
                let head_end = loop {
                    match sock.read(&mut tmp).await {
                        Ok(0) | Err(_) => break None,
                        Ok(n) => {
                            buf.extend_from_slice(&tmp[..n]);
                            if let Some(pos) = find(&buf, b"\r\n\r\n") {
                                break Some(pos + 4);
                            }
                            if buf.len() > (1 << 20) {
                                break None;
                            }
                        }
                    }
                };
                let Some(head_end) = head_end else { continue };
                let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
                let content_len = head
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        if k.eq_ignore_ascii_case("content-length") {
                            v.trim().parse::<usize>().ok()
                        } else {
                            None
                        }
                    })
                    .unwrap_or(0);
                while buf.len() < head_end + content_len {
                    match sock.read(&mut tmp).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => buf.extend_from_slice(&tmp[..n]),
                    }
                }
                let body = String::from_utf8_lossy(&buf[head_end..]).to_string();
                let (status, resp_body) = handler(&head, &body);
                let resp = format!(
                    "HTTP/1.1 {status} MOCK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    resp_body.len(),
                    resp_body
                );
                let _ = sock.write_all(resp.as_bytes()).await;
                let _ = sock.shutdown().await;
            }
        });
        (base, handle)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_cfg(base_url: String) -> EmailConfig {
        EmailConfig {
            token: "tok-123".into(),
            zone_id: "zone-1".into(),
            account_id: "acct-1".into(),
            domain: "pages.example.com".into(),
            base_url,
        }
    }

    #[tokio::test]
    async fn create_rule_sends_python_parity_payload() {
        let (base, server) = test_http::spawn(|head, body| {
            assert!(head.starts_with("POST /zones/zone-1/email/routing/rules HTTP/1.1"));
            assert!(head.to_lowercase().contains("authorization: bearer tok-123"));
            let v: Value = serde_json::from_str(body).unwrap();
            assert_eq!(v["actions"][0]["type"], "forward");
            assert_eq!(v["actions"][0]["value"][0], "dest@example.com");
            assert_eq!(v["matchers"][0]["type"], "literal");
            assert_eq!(v["matchers"][0]["field"], "to");
            assert_eq!(v["matchers"][0]["value"], "my-page@pages.example.com");
            assert_eq!(v["enabled"], true);
            assert_eq!(v["name"], "Roster: my-page");
            (
                200,
                r#"{"success":true,"result":{"id":"rule-9","enabled":true}}"#.into(),
            )
        })
        .await;

        let rule = create_rule(&test_cfg(base), "my-page", "dest@example.com")
            .await
            .unwrap();
        assert_eq!(rule["id"], "rule-9");
        server.abort();
    }

    #[tokio::test]
    async fn list_destinations_returns_result_array() {
        let (base, server) = test_http::spawn(|head, _| {
            assert!(head.starts_with("GET /accounts/acct-1/email/routing/addresses HTTP/1.1"));
            (
                200,
                r#"{"result":[{"email":"a@x.com","verified":"2026-01-01T00:00:00Z"},{"email":"b@x.com","verified":null}]}"#.into(),
            )
        })
        .await;

        let dests = list_destinations(&test_cfg(base)).await.unwrap();
        assert_eq!(dests.len(), 2);
        assert_eq!(dests[0]["email"], "a@x.com");
        server.abort();
    }

    #[tokio::test]
    async fn delete_rule_hits_rule_path_and_upstream_error_bubbles() {
        let (base, server) = test_http::spawn(|head, _| {
            if head.starts_with("DELETE /zones/zone-1/email/routing/rules/rule-ok ") {
                (200, r#"{"success":true,"result":null}"#.into())
            } else {
                (403, r#"{"success":false,"errors":[{"message":"nope"}]}"#.into())
            }
        })
        .await;

        let cfg = test_cfg(base);
        delete_rule(&cfg, "rule-ok").await.unwrap();
        let err = delete_rule(&cfg, "rule-bad").await.unwrap_err();
        assert!(err.to_string().contains("403"), "got: {err}");
        server.abort();
    }

    #[test]
    fn list_rules_tolerates_missing_result() {
        // Envelope-shape guard: result missing/None → empty vec (Python
        // `.get("result", [])` parity). Exercised via the parser directly.
        let data: Value = serde_json::json!({ "success": true });
        assert!(data["result"].as_array().cloned().unwrap_or_default().is_empty());
    }
}
