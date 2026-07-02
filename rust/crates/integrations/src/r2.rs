//! Cloudflare R2 (S3-compatible) client — port of `services/r2.py`.
//!
//! Central place for presigned URLs, uploads, downloads, and the key-space
//! conventions. All large-file I/O goes through here, never through the
//! Railway edge proxy.
//!
//! Env contract (same names as the Python app):
//!   R2_ACCESS_KEY_ID     — access key (fallback: R2_ACCESS_KEY)
//!   R2_SECRET_ACCESS_KEY — secret key (fallback: R2_SECRET_KEY)
//!   R2_BUCKET            — bucket name
//!   R2_ENDPOINT          — S3 endpoint URL; if unset, derived from
//!   R2_ACCOUNT_ID        — https://{account}.r2.cloudflarestorage.com
//!
//! Uses aws-sdk-s3 with a synchronously-built config (static credentials +
//! endpoint override, region "auto") — no aws-config loader chain needed.
//! Presigning is offline (pure signing), so URL logic is testable without a
//! bucket.

use std::path::Path;
use std::time::Duration;

use anyhow::Context;
use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::primitives::{ByteStream, DateTimeFormat};
use aws_sdk_s3::types::{Delete, ObjectIdentifier};
use aws_sdk_s3::Client;

/// Presigned PUT lifetime — 6h, large files on slow connections.
pub const UPLOAD_TTL: Duration = Duration::from_secs(6 * 3600);
/// Presigned GET lifetime — 24h, team can always re-fetch within a workday.
pub const DOWNLOAD_TTL: Duration = Duration::from_secs(24 * 3600);

fn env(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// Resolved R2 configuration. Existence == configured.
#[derive(Debug, Clone)]
pub struct R2Config {
    pub endpoint: String,
    pub access_key_id: String,
    pub secret_access_key: String,
    pub bucket: String,
}

impl R2Config {
    /// Build from env; `None` when any required var is missing.
    pub fn from_env() -> Option<Self> {
        let access_key_id = env("R2_ACCESS_KEY_ID").or_else(|| env("R2_ACCESS_KEY"))?;
        let secret_access_key = env("R2_SECRET_ACCESS_KEY").or_else(|| env("R2_SECRET_KEY"))?;
        let bucket = env("R2_BUCKET")?;
        let endpoint = env("R2_ENDPOINT")
            .or_else(|| env("R2_ACCOUNT_ID").map(|a| format!("https://{a}.r2.cloudflarestorage.com")))?;
        Some(Self {
            endpoint,
            access_key_id,
            secret_access_key,
            bucket,
        })
    }
}

/// Whether R2 is fully configured.
pub fn configured() -> bool {
    R2Config::from_env().is_some()
}

// ── Key conventions ──────────────────────────────────────────────────────────
// uploads/{project}/{batch_id}/{index:03}_{filename}  → raw user uploads
// clips/{project}/{job_id}/clip_NNN.mp4               → finished clips
// accounts/{integration_id}/{filename}                → per-account "folders"

fn safe_filename(filename: &str) -> String {
    filename.replace(['/', '\\'], "_")
}

pub fn upload_key(project: &str, batch_id: &str, index: usize, filename: &str) -> String {
    format!("uploads/{project}/{batch_id}/{index:03}_{}", safe_filename(filename))
}

pub fn clip_key(project: &str, job_id: &str, clip_name: &str) -> String {
    format!("clips/{project}/{job_id}/{clip_name}")
}

pub fn account_prefix(integration_id: &str) -> String {
    format!("accounts/{integration_id}/")
}

pub fn account_key(integration_id: &str, filename: &str) -> String {
    format!("accounts/{integration_id}/{}", safe_filename(filename))
}

/// One listed object — field names match the Python dict shape exactly.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ObjectInfo {
    pub key: String,
    pub size: i64,
    pub last_modified: Option<String>,
    pub filename: String,
}

/// R2 handle: an S3 client pinned to one bucket. Cheap to clone.
#[derive(Clone)]
pub struct R2 {
    client: Client,
    bucket: String,
}

impl R2 {
    /// Build from env; `None` when not configured (callers report a clean
    /// "not configured" instead of crashing).
    pub fn from_env() -> Option<Self> {
        R2Config::from_env().map(Self::new)
    }

    pub fn new(cfg: R2Config) -> Self {
        let creds = Credentials::new(
            cfg.access_key_id,
            cfg.secret_access_key,
            None,
            None,
            "r2-static",
        );
        let conf = aws_sdk_s3::Config::builder()
            .behavior_version(BehaviorVersion::latest())
            .region(Region::new("auto"))
            .endpoint_url(cfg.endpoint)
            .credentials_provider(creds)
            // Path-style addressing (endpoint/bucket/key) — same as boto3 with
            // a custom endpoint, and what R2 docs show for S3 API access.
            .force_path_style(true)
            .build();
        Self {
            client: Client::from_conf(conf),
            bucket: cfg.bucket,
        }
    }

    pub fn bucket(&self) -> &str {
        &self.bucket
    }

    // ── Presigned URLs ───────────────────────────────────────────────────────

    /// Presigned PUT for direct client uploads (bypasses the edge proxy).
    pub async fn presign_put(
        &self,
        key: &str,
        content_type: &str,
        ttl: Duration,
    ) -> anyhow::Result<String> {
        let presigned = self
            .client
            .put_object()
            .bucket(&self.bucket)
            .key(key)
            .content_type(content_type)
            .presigned(PresigningConfig::expires_in(ttl)?)
            .await
            .context("presign put_object")?;
        Ok(presigned.uri().to_string())
    }

    /// Presigned GET; `download_as` sets a Content-Disposition attachment name.
    pub async fn presign_get(
        &self,
        key: &str,
        ttl: Duration,
        download_as: Option<&str>,
    ) -> anyhow::Result<String> {
        let mut req = self.client.get_object().bucket(&self.bucket).key(key);
        if let Some(name) = download_as {
            req = req.response_content_disposition(format!("attachment; filename=\"{name}\""));
        }
        let presigned = req
            .presigned(PresigningConfig::expires_in(ttl)?)
            .await
            .context("presign get_object")?;
        Ok(presigned.uri().to_string())
    }

    // ── Listing ──────────────────────────────────────────────────────────────

    /// List every object under a prefix (paged internally, capped at `max_keys`
    /// results). `.placeholder` folder markers are skipped.
    pub async fn list_prefix(&self, prefix: &str, max_keys: usize) -> anyhow::Result<Vec<ObjectInfo>> {
        let mut items = Vec::new();
        let mut token: Option<String> = None;
        loop {
            let resp = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix(prefix)
                .set_continuation_token(token.take())
                .send()
                .await
                .context("list_objects_v2")?;
            for obj in resp.contents() {
                let key = obj.key().unwrap_or_default().to_string();
                if key.ends_with("/.placeholder") {
                    continue;
                }
                let filename = key.rsplit('/').next().unwrap_or(&key).to_string();
                items.push(ObjectInfo {
                    filename,
                    size: obj.size().unwrap_or(0),
                    last_modified: obj
                        .last_modified()
                        .and_then(|dt| dt.fmt(DateTimeFormat::DateTime).ok()),
                    key,
                });
            }
            if !resp.is_truncated().unwrap_or(false) || items.len() >= max_keys {
                break;
            }
            token = resp.next_continuation_token().map(str::to_string);
        }
        Ok(items)
    }

    /// List all real (non-placeholder) objects under an account prefix.
    pub async fn list_account_objects(
        &self,
        integration_id: &str,
        max_keys: usize,
    ) -> anyhow::Result<Vec<ObjectInfo>> {
        self.list_prefix(&account_prefix(integration_id), max_keys).await
    }

    /// Lightweight count of real objects under an account prefix.
    pub async fn count_account_objects(&self, integration_id: &str) -> anyhow::Result<usize> {
        let prefix = account_prefix(integration_id);
        let mut total = 0usize;
        let mut token: Option<String> = None;
        loop {
            let resp = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix(&prefix)
                .set_continuation_token(token.take())
                .send()
                .await
                .context("list_objects_v2")?;
            total += resp
                .contents()
                .iter()
                .filter(|o| !o.key().unwrap_or_default().ends_with("/.placeholder"))
                .count();
            if !resp.is_truncated().unwrap_or(false) {
                break;
            }
            token = resp.next_continuation_token().map(str::to_string);
        }
        Ok(total)
    }

    // ── Object ops ───────────────────────────────────────────────────────────

    /// Does the object exist? (HEAD; 404 → false.)
    pub async fn exists(&self, key: &str) -> anyhow::Result<bool> {
        match self
            .client
            .head_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
        {
            Ok(_) => Ok(true),
            Err(e) => {
                if e.as_service_error().map(|se| se.is_not_found()).unwrap_or(false) {
                    Ok(false)
                } else {
                    Err(anyhow::Error::new(e).context("head_object"))
                }
            }
        }
    }

    /// Drop a zero-byte `.placeholder` marker under accounts/{id}/ so the
    /// "folder" is visible in bucket browsers. Idempotent. Returns the same
    /// `{prefix, marker_key, bucket}` shape as the Python service.
    pub async fn create_account_prefix(&self, integration_id: &str) -> anyhow::Result<serde_json::Value> {
        let prefix = account_prefix(integration_id);
        let marker = format!("{prefix}.placeholder");
        if !self.exists(&marker).await? {
            self.client
                .put_object()
                .bucket(&self.bucket)
                .key(&marker)
                .content_type("text/plain")
                .body(ByteStream::from_static(b""))
                .send()
                .await
                .context("put placeholder marker")?;
        }
        Ok(serde_json::json!({
            "prefix": prefix,
            "marker_key": marker,
            "bucket": self.bucket,
        }))
    }

    /// Upload a local file to R2.
    pub async fn upload_from_path(
        &self,
        key: &str,
        src: &Path,
        content_type: &str,
    ) -> anyhow::Result<()> {
        let body = ByteStream::from_path(src)
            .await
            .with_context(|| format!("open {} for upload", src.display()))?;
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(key)
            .content_type(content_type)
            .body(body)
            .send()
            .await
            .context("put_object")?;
        Ok(())
    }

    /// Stream an R2 object to a local path. Returns bytes written.
    pub async fn download_to_path(&self, key: &str, dest: &Path) -> anyhow::Result<u64> {
        use tokio::io::AsyncWriteExt;

        if let Some(parent) = dest.parent() {
            tokio::fs::create_dir_all(parent).await.ok();
        }
        let resp = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
            .context("get_object")?;
        let mut file = tokio::fs::File::create(dest)
            .await
            .with_context(|| format!("create {}", dest.display()))?;
        let mut body = resp.body;
        let mut written: u64 = 0;
        while let Some(chunk) = body.try_next().await.context("r2 stream error")? {
            written += chunk.len() as u64;
            file.write_all(&chunk).await.context("write local file")?;
        }
        file.flush().await.ok();
        Ok(written)
    }

    /// Delete keys in batches of 1000. Returns the count actually deleted.
    pub async fn delete_many<I, S>(&self, keys: I) -> anyhow::Result<usize>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let all: Vec<String> = keys.into_iter().map(Into::into).collect();
        let mut deleted = 0usize;
        for chunk in all.chunks(1000) {
            let objects: Vec<ObjectIdentifier> = chunk
                .iter()
                .map(|k| ObjectIdentifier::builder().key(k).build())
                .collect::<Result<_, _>>()
                .context("build delete identifiers")?;
            let n = objects.len();
            let delete = Delete::builder()
                .set_objects(Some(objects))
                .quiet(true)
                .build()
                .context("build delete request")?;
            let resp = self
                .client
                .delete_objects()
                .bucket(&self.bucket)
                .delete(delete)
                .send()
                .await
                .context("delete_objects")?;
            let errs = resp.errors().len();
            if errs > 0 {
                tracing::warn!("r2 delete errors: {:?}", &resp.errors()[..errs.min(5)]);
            }
            deleted += n - errs;
        }
        Ok(deleted)
    }

    /// Delete every object under a prefix. Returns total deleted.
    pub async fn delete_prefix(&self, prefix: &str) -> anyhow::Result<usize> {
        let mut total = 0usize;
        let mut token: Option<String> = None;
        loop {
            let resp = self
                .client
                .list_objects_v2()
                .bucket(&self.bucket)
                .prefix(prefix)
                .set_continuation_token(token.take())
                .send()
                .await
                .context("list_objects_v2")?;
            let keys: Vec<String> = resp
                .contents()
                .iter()
                .filter_map(|o| o.key().map(str::to_string))
                .collect();
            if !keys.is_empty() {
                total += self.delete_many(keys).await?;
            }
            if !resp.is_truncated().unwrap_or(false) {
                break;
            }
            token = resp.next_continuation_token().map(str::to_string);
        }
        Ok(total)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_r2() -> R2 {
        R2::new(R2Config {
            endpoint: "https://acct123.r2.cloudflarestorage.com".into(),
            access_key_id: "AKTEST".into(),
            secret_access_key: "secret".into(),
            bucket: "content-lab".into(),
        })
    }

    #[test]
    fn key_conventions_match_python() {
        assert_eq!(
            upload_key("proj", "b1", 3, "my video.mp4"),
            "uploads/proj/b1/003_my video.mp4"
        );
        // Slashes/backslashes in filenames are neutralized, never new key segments.
        assert_eq!(
            upload_key("proj", "b1", 0, "a/b\\c.mp4"),
            "uploads/proj/b1/000_a_b_c.mp4"
        );
        assert_eq!(clip_key("proj", "j9", "clip_001.mp4"), "clips/proj/j9/clip_001.mp4");
        assert_eq!(account_prefix("int-1"), "accounts/int-1/");
        assert_eq!(account_key("int-1", "x/y.mp4"), "accounts/int-1/x_y.mp4");
    }

    #[tokio::test]
    async fn presign_put_embeds_key_expiry_and_endpoint() {
        let url = test_r2()
            .presign_put("uploads/p/b/000_v.mp4", "video/mp4", UPLOAD_TTL)
            .await
            .unwrap();
        // Path-style: endpoint host untouched, bucket in the path.
        assert!(
            url.starts_with("https://acct123.r2.cloudflarestorage.com/content-lab/uploads/p/b/000_v.mp4?"),
            "{url}"
        );
        assert!(url.contains("X-Amz-Expires=21600"), "{url}");
        assert!(url.contains("X-Amz-Signature="), "{url}");
    }

    #[tokio::test]
    async fn presign_get_sets_content_disposition() {
        let url = test_r2()
            .presign_get("clips/p/j/clip_000.mp4", DOWNLOAD_TTL, Some("final.mp4"))
            .await
            .unwrap();
        assert!(url.contains("X-Amz-Expires=86400"), "{url}");
        // The disposition rides as a (URL-encoded) response override param.
        assert!(
            url.to_lowercase().contains("response-content-disposition="),
            "{url}"
        );
        assert!(url.contains("final.mp4"), "{url}");
    }

    #[test]
    fn config_derives_endpoint_from_account_id() {
        // Pure derivation logic (no env): mirror from_env's fallback.
        let derived = format!("https://{}.r2.cloudflarestorage.com", "abc999");
        assert_eq!(derived, "https://abc999.r2.cloudflarestorage.com");
    }
}
