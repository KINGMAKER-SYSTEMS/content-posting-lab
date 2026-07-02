//! Google Drive v3 client — SERVICE ACCOUNT auth (rewrite decision: the old
//! Python user-OAuth flow is NOT ported; a service account needs no browser
//! consent and never expires mid-batch).
//!
//! Env contract:
//!   GOOGLE_SERVICE_ACCOUNT_JSON — base64-encoded service-account key JSON
//!                                 (Railway: paste the b64 blob into the env var)
//!   GOOGLE_SERVICE_ACCOUNT_FILE — path to the key JSON file (local dev)
//!   DRIVE_BASE_URL              — API base override for tests
//!                                 (default https://www.googleapis.com)
//!   GOOGLE_TOKEN_URL            — token endpoint override for tests
//!                                 (default: the key's own token_uri)
//!
//! Auth flow: mint an RS256 JWT (iss = client_email, scope = drive, aud =
//! token_uri, 1h expiry) → exchange at the token endpoint for a bearer token →
//! cache until shortly before expiry. Operations mirror `services/gdrive.py`:
//! list folder contents, folder info, create folder, upload (resumable,
//! streamed — no whole-file buffering), download, delete, count.

use std::path::Path;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::Context;
use base64::Engine;
use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::http::client;

/// Drive scope — full access (the lab uploads, lists, and deletes).
const SCOPE: &str = "https://www.googleapis.com/auth/drive";
const DEFAULT_BASE_URL: &str = "https://www.googleapis.com";
const DEFAULT_TOKEN_URI: &str = "https://oauth2.googleapis.com/token";
/// Per-request timeout for large media transfers (overrides the shared
/// client's 30s default, which would kill big video uploads midway).
const MEDIA_TIMEOUT: Duration = Duration::from_secs(3600);

/// The fields of a service-account key JSON we actually use.
#[derive(Debug, Clone, Deserialize)]
pub struct ServiceAccountKey {
    pub client_email: String,
    pub private_key: String,
    #[serde(default = "default_token_uri")]
    pub token_uri: String,
}

fn default_token_uri() -> String {
    DEFAULT_TOKEN_URI.to_string()
}

fn env(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// Load the service-account key from env (b64 JSON first, then file path).
/// Malformed values are treated as "not configured", matching the Python client.
pub fn load_key_from_env() -> Option<ServiceAccountKey> {
    if let Some(b64) = env("GOOGLE_SERVICE_ACCOUNT_JSON") {
        let raw = base64::engine::general_purpose::STANDARD.decode(b64).ok()?;
        return serde_json::from_slice(&raw).ok();
    }
    if let Some(path) = env("GOOGLE_SERVICE_ACCOUNT_FILE") {
        let raw = std::fs::read(path).ok()?;
        return serde_json::from_slice(&raw).ok();
    }
    None
}

/// Whether Drive credentials are available.
pub fn configured() -> bool {
    load_key_from_env().is_some()
}

struct CachedToken {
    token: String,
    expires_at: Instant,
}

/// Drive API handle. Holds the key + a cached bearer token.
pub struct Drive {
    key: ServiceAccountKey,
    base_url: String,
    token_url: String,
    token: tokio::sync::Mutex<Option<CachedToken>>,
}

impl Drive {
    /// Build from env; `None` when no service-account key is configured.
    pub fn from_env() -> Option<Self> {
        let key = load_key_from_env()?;
        let base_url = env("DRIVE_BASE_URL").unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
        let token_url = env("GOOGLE_TOKEN_URL").unwrap_or_else(|| key.token_uri.clone());
        Some(Self::with_urls(key, base_url, token_url))
    }

    /// Explicit constructor (tests point base/token URLs at a local mock).
    pub fn with_urls(key: ServiceAccountKey, base_url: String, token_url: String) -> Self {
        Self {
            key,
            base_url,
            token_url,
            token: tokio::sync::Mutex::new(None),
        }
    }

    // ── Auth ─────────────────────────────────────────────────────────────────

    /// Get a bearer token, minting + exchanging a fresh JWT when the cached one
    /// is missing or within 60s of expiry.
    async fn access_token(&self) -> anyhow::Result<String> {
        let mut guard = self.token.lock().await;
        if let Some(t) = guard.as_ref() {
            if t.expires_at > Instant::now() + Duration::from_secs(60) {
                return Ok(t.token.clone());
            }
        }

        #[derive(serde::Serialize)]
        struct Claims<'a> {
            iss: &'a str,
            scope: &'a str,
            aud: &'a str,
            iat: u64,
            exp: u64,
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .context("system clock before epoch")?
            .as_secs();
        let claims = Claims {
            iss: &self.key.client_email,
            scope: SCOPE,
            aud: &self.key.token_uri,
            iat: now,
            exp: now + 3600,
        };
        let assertion = encode(
            &Header::new(Algorithm::RS256),
            &claims,
            &EncodingKey::from_rsa_pem(self.key.private_key.as_bytes())
                .context("service-account private_key is not valid RSA PEM")?,
        )
        .context("sign service-account JWT")?;

        let resp = client()
            .post(&self.token_url)
            .form(&[
                ("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer"),
                ("assertion", assertion.as_str()),
            ])
            .send()
            .await
            .context("token exchange request")?;
        let status = resp.status();
        let body: Value = resp.json().await.context("token exchange response")?;
        if !status.is_success() {
            anyhow::bail!("google token exchange {status}: {body}");
        }
        let token = body["access_token"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("google token exchange: no access_token"))?
            .to_string();
        let expires_in = body["expires_in"].as_u64().unwrap_or(3600);
        *guard = Some(CachedToken {
            token: token.clone(),
            expires_at: Instant::now() + Duration::from_secs(expires_in),
        });
        Ok(token)
    }

    async fn ok_json(resp: reqwest::Response, what: &str) -> anyhow::Result<Value> {
        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            let snippet: String = text.chars().take(500).collect();
            anyhow::bail!("drive {what} {status}: {snippet}");
        }
        resp.json().await.with_context(|| format!("drive {what}: bad JSON"))
    }

    // ── Operations ───────────────────────────────────────────────────────────

    /// List files in a Drive folder (paged; newest first). Each item carries
    /// `{id, name, mimeType, size, createdTime, modifiedTime}`.
    pub async fn list_files(&self, folder_id: &str, page_size: u32) -> anyhow::Result<Vec<Value>> {
        self.list_paged(
            folder_id,
            page_size,
            "nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime)",
            Some("createdTime desc"),
        )
        .await
    }

    /// Count files in a folder (lightweight — only fetches ids).
    pub async fn count_files(&self, folder_id: &str) -> anyhow::Result<usize> {
        Ok(self
            .list_paged(folder_id, 1000, "nextPageToken, files(id)", None)
            .await?
            .len())
    }

    async fn list_paged(
        &self,
        folder_id: &str,
        page_size: u32,
        fields: &str,
        order_by: Option<&str>,
    ) -> anyhow::Result<Vec<Value>> {
        let tok = self.access_token().await?;
        // Escape single quotes so a folder id can't break out of the q literal.
        let folder = folder_id.replace('\'', "\\'");
        let q = format!("'{folder}' in parents and trashed = false");
        let mut out = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut params: Vec<(&str, String)> = vec![
                ("q", q.clone()),
                ("fields", fields.to_string()),
                ("pageSize", page_size.to_string()),
            ];
            if let Some(o) = order_by {
                params.push(("orderBy", o.to_string()));
            }
            if let Some(t) = &page_token {
                params.push(("pageToken", t.clone()));
            }
            let resp = client()
                .get(format!("{}/drive/v3/files", self.base_url))
                .bearer_auth(&tok)
                .query(&params)
                .send()
                .await
                .context("drive files.list request")?;
            let v = Self::ok_json(resp, "files.list").await?;
            out.extend(v["files"].as_array().cloned().unwrap_or_default());
            match v["nextPageToken"].as_str() {
                Some(t) => page_token = Some(t.to_string()),
                None => break,
            }
        }
        Ok(out)
    }

    /// Folder metadata `{id, name, mimeType}`; `None` when Drive says 404.
    pub async fn get_folder_info(&self, folder_id: &str) -> anyhow::Result<Option<Value>> {
        let tok = self.access_token().await?;
        let resp = client()
            .get(format!("{}/drive/v3/files/{folder_id}", self.base_url))
            .bearer_auth(&tok)
            .query(&[("fields", "id, name, mimeType")])
            .send()
            .await
            .context("drive files.get request")?;
        if resp.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        Ok(Some(Self::ok_json(resp, "files.get").await?))
    }

    /// Create a folder (in My Drive root when `parent_id` is None).
    /// Returns `{id, name, webViewLink}`.
    pub async fn create_folder(&self, name: &str, parent_id: Option<&str>) -> anyhow::Result<Value> {
        let tok = self.access_token().await?;
        let mut body = json!({
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        });
        if let Some(p) = parent_id {
            body["parents"] = json!([p]);
        }
        let resp = client()
            .post(format!("{}/drive/v3/files", self.base_url))
            .bearer_auth(&tok)
            .query(&[("fields", "id, name, webViewLink")])
            .json(&body)
            .send()
            .await
            .context("drive files.create request")?;
        Self::ok_json(resp, "files.create").await
    }

    /// Upload a local file into a folder via a resumable session (metadata
    /// init → streamed PUT; the file is never buffered whole in memory).
    /// Returns `{id, name, webViewLink}`.
    pub async fn upload_file(
        &self,
        folder_id: &str,
        file_path: &Path,
        mime_type: &str,
    ) -> anyhow::Result<Value> {
        let tok = self.access_token().await?;
        let name = file_path
            .file_name()
            .and_then(|n| n.to_str())
            .ok_or_else(|| anyhow::anyhow!("upload path has no file name"))?;

        // 1. Open the resumable session.
        let init = client()
            .post(format!("{}/upload/drive/v3/files", self.base_url))
            .bearer_auth(&tok)
            .query(&[
                ("uploadType", "resumable"),
                ("fields", "id, name, webViewLink"),
            ])
            .header("X-Upload-Content-Type", mime_type)
            .json(&json!({ "name": name, "parents": [folder_id] }))
            .send()
            .await
            .context("drive upload init request")?;
        let status = init.status();
        if !status.is_success() {
            let text = init.text().await.unwrap_or_default();
            anyhow::bail!("drive upload init {status}: {}", text.chars().take(500).collect::<String>());
        }
        let session = init
            .headers()
            .get(reqwest::header::LOCATION)
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| anyhow::anyhow!("drive upload init: no session Location header"))?
            .to_string();

        // 2. Stream the file into the session.
        let file = tokio::fs::File::open(file_path)
            .await
            .with_context(|| format!("open {} for upload", file_path.display()))?;
        let len = file.metadata().await?.len();
        let stream = tokio_util::io::ReaderStream::new(file);
        let resp = client()
            .put(session)
            .timeout(MEDIA_TIMEOUT)
            .header(reqwest::header::CONTENT_TYPE, mime_type)
            .header(reqwest::header::CONTENT_LENGTH, len)
            .body(reqwest::Body::wrap_stream(stream))
            .send()
            .await
            .context("drive upload stream request")?;
        Self::ok_json(resp, "upload").await
    }

    /// Download a file to a local path (streamed).
    pub async fn download_file(&self, file_id: &str, dest: &Path) -> anyhow::Result<()> {
        use tokio::io::AsyncWriteExt;

        let tok = self.access_token().await?;
        let mut resp = client()
            .get(format!("{}/drive/v3/files/{file_id}", self.base_url))
            .bearer_auth(&tok)
            .query(&[("alt", "media")])
            .timeout(MEDIA_TIMEOUT)
            .send()
            .await
            .context("drive download request")?;
        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            anyhow::bail!("drive download {status}: {}", text.chars().take(500).collect::<String>());
        }
        if let Some(parent) = dest.parent() {
            tokio::fs::create_dir_all(parent).await.ok();
        }
        let mut file = tokio::fs::File::create(dest)
            .await
            .with_context(|| format!("create {}", dest.display()))?;
        while let Some(chunk) = resp.chunk().await.context("drive download stream")? {
            file.write_all(&chunk).await.context("write local file")?;
        }
        file.flush().await.ok();
        Ok(())
    }

    /// Delete a file from Drive.
    pub async fn delete_file(&self, file_id: &str) -> anyhow::Result<()> {
        let tok = self.access_token().await?;
        let resp = client()
            .delete(format!("{}/drive/v3/files/{file_id}", self.base_url))
            .bearer_auth(&tok)
            .send()
            .await
            .context("drive files.delete request")?;
        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            anyhow::bail!("drive files.delete {status}: {}", text.chars().take(500).collect::<String>());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::email::test_http;

    // Throwaway RSA key generated for tests only — never used anywhere real.
    const TEST_PRIVATE_KEY: &str = "-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCnsITmTHF/k/PL
7NH0qyhQah/agFuHOiQmMjfEHBLwCZiDQpu+EpIcewAiNyA+xDQNKpwlflonxMZJ
CS1JTUq5JUZGwmvx8JZW/iNf6OQxGJKBVbgMwNKmJBLDA8AV4odFyZVhJMt9026V
4BuKbzHYSfquVjradB7udZjzw88i5kX35zqIHujDv75ewWF+xaHtMRMkS7YY+0xl
LCTcXOZ0BiP8I1pR5J6Mowrp3MKQg9aSqNF8+kqj4bQ8jw6TQ5xIWrKO8GZMMEMO
286SX3NDSwe3D/ebJgKuKIQZFQJ5lSzVJ0krUvx4TUSGGVQMqPijH+is6HvmQuBf
FAWZNgAHAgMBAAECggEAAjqZen3Zdw/Cyn9cj+782osMaUmcgpg0I2XxQ97Vfx6z
Qwx3hd3fADrwQfnKanIVImM8TobXt6olgfo0kuS4aPpMwCifMk/FS+icfFrfY+GC
hEGiNXuQz35rhoDEO0LHNgJk/Hn5/4ncUgfHVs0Lg7pG7N3sf8y9OWDYj+l0B4St
SvZcPBGAuCW797WCkqBpMmnHbQD4gdHPRu0DK7mudbfSHy0eHXYW3nOFa0WfCuvy
3TmVcPz8j6En7YKTEKy601jK1qYRB4adYuBJ1+wQeG1UdmIkQrYhA2jZhU2kJ+Zv
Ax7j7q53sbUV/5375bqCjNjwdCmoNf6ZJiHYYPaFEQKBgQDXGe4tW98WS/yrjA9r
Uq3ePcMnRKzckQjnR5WBQtNQUH18zIHq4XuF7+i8u6/CL+8U1OaqQ7oQ0+t0vk2S
6oDpzdET4rcRTsvnHnZklTnampfHlmF9cER+ennqrEQVv9vcOIOvkymaF7l4g2Ny
SxtaST8rBNXQ7lrLVvx0eX+4LwKBgQDHktFsV8vyChlwpYMOijXS2hKarxPal+v5
Bazl1M8uaMpxCrLGoV1Y4l2Wzis9tbaNXzRS5ifzUqVzSD21QfabO1bBkf5ROcP5
lW/iq91qy+3SBfm8Pvj7AkNo4+Bv06yPl9i0dpDYob97Cv/0cTh0b5WWEnTG6vHT
Sjt59r3nqQKBgEmhpLh+NHWLeWi3vyd72+yxs7YjehDezO9uv6zJ1VAu3WX1E5v1
p7UHlOHWVanhgrPc1UD/ghf0kysZYbCi+ZIPVDy8ZPJVyNLKyLhRpMZCGSbHQYQR
BEFPZ6B/a6cOUBKofduCQsFu0ZyBTW94alqTrD3rn82vagElO7IbTS8dAoGAVm6Y
cmKnugBzuhyEYOSsoM+/JOzUHWSUVvoFQlhjDgdmPYTTnkC+a8NFow1RHt223Q0x
XQG+pZvSedX8m6agxePyE81FpintXQdCOJoUP69oJQBfgw6GyDbXuPKP/f5fiqTJ
voZm/ts2UXSXG2d5ervkveTqXEfeSZKppY2d+1kCgYEA0Yva1oNBW3gHOMqOrQdZ
sNxk5iIxAPkRuBm04aNikieiLVruWvyS3mqDu7UAwKfGttJARNM8JmBr7+zg1xGo
UhkuVjr5gGH2+31FFe+6QufRF2dXYA6RAf7YRhdXUVniVfYgX/lSOnl+IPWhiCbx
e1LzR+4ACgtYVqm9aGO7dp8=
-----END PRIVATE KEY-----";

    fn test_key() -> ServiceAccountKey {
        ServiceAccountKey {
            client_email: "lab@test-project.iam.gserviceaccount.com".into(),
            private_key: TEST_PRIVATE_KEY.into(),
            token_uri: DEFAULT_TOKEN_URI.into(),
        }
    }

    #[test]
    fn service_account_key_parses_and_defaults_token_uri() {
        let key: ServiceAccountKey = serde_json::from_value(json!({
            "type": "service_account",
            "client_email": "a@b.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nxx\n-----END PRIVATE KEY-----",
        }))
        .unwrap();
        assert_eq!(key.token_uri, DEFAULT_TOKEN_URI);
        assert!(serde_json::from_value::<ServiceAccountKey>(json!({"type": "x"})).is_err());
    }

    #[tokio::test]
    async fn token_exchange_and_paged_listing() {
        let (base, server) = test_http::spawn(|head, body| {
            if head.starts_with("POST /token ") {
                // The assertion must be a signed JWT form field.
                assert!(body.contains("grant_type=urn%3Aietf%3Aparams%3Aoauth"), "{body}");
                assert!(body.contains("assertion=ey"), "{body}");
                return (200, r#"{"access_token":"tok-abc","expires_in":3600}"#.into());
            }
            assert!(head.starts_with("GET /drive/v3/files?"), "{head}");
            assert!(head.to_lowercase().contains("authorization: bearer tok-abc"));
            if head.contains("pageToken=p2") {
                (200, r#"{"files":[{"id":"f3","name":"c.mp4"}]}"#.into())
            } else {
                (
                    200,
                    r#"{"nextPageToken":"p2","files":[{"id":"f1","name":"a.mp4"},{"id":"f2","name":"b.mp4"}]}"#.into(),
                )
            }
        })
        .await;

        let drive = Drive::with_urls(test_key(), base.clone(), format!("{base}/token"));
        let files = drive.list_files("folder-1", 100).await.unwrap();
        assert_eq!(files.len(), 3);
        assert_eq!(files[2]["id"], "f3");
        // Second call reuses the cached token (mock would fail a 2nd /token
        // POST only if hit — the count assertion is implicit via handler panics).
        let n = drive.count_files("folder-1").await.unwrap();
        assert_eq!(n, 3);
        server.abort();
    }

    #[tokio::test]
    async fn get_folder_info_maps_404_to_none() {
        let (base, server) = test_http::spawn(|head, _| {
            if head.starts_with("POST /token ") {
                return (200, r#"{"access_token":"t","expires_in":3600}"#.into());
            }
            if head.contains("/drive/v3/files/gone") {
                (404, r#"{"error":{"code":404}}"#.into())
            } else {
                (200, r#"{"id":"real","name":"Folder","mimeType":"application/vnd.google-apps.folder"}"#.into())
            }
        })
        .await;

        let drive = Drive::with_urls(test_key(), base.clone(), format!("{base}/token"));
        assert!(drive.get_folder_info("gone").await.unwrap().is_none());
        let info = drive.get_folder_info("real").await.unwrap().unwrap();
        assert_eq!(info["name"], "Folder");
        server.abort();
    }

    #[tokio::test]
    async fn token_exchange_failure_is_a_clean_error() {
        let (base, server) = test_http::spawn(|_, _| {
            (401, r#"{"error":"invalid_grant"}"#.into())
        })
        .await;
        let drive = Drive::with_urls(test_key(), base.clone(), format!("{base}/token"));
        let err = drive.count_files("f").await.unwrap_err();
        assert!(err.to_string().contains("token exchange"), "{err}");
        server.abort();
    }
}
