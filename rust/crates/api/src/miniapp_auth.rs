//! Telegram Mini App authentication.
//!
//! Validates the `initData` string the Telegram WebApp SDK hands the front
//! end, per <https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app>,
//! then resolves the authenticated Telegram user to the poster they act as.
//!
//! Validation (HMAC variant):
//!   secret_key      = HMAC_SHA256(key = "WebAppData", msg = bot_token)
//!   data_check_str  = "\n".join(sorted "key=value" for all fields except `hash`)
//!   expected_hash   = hex(HMAC_SHA256(key = secret_key, msg = data_check_str))
//!   valid           = constant_time_eq(expected_hash, provided hash)
//!
//! A dev bypass (env `MINIAPP_DEV_AUTH=1` + header `X-Dev-Poster-Id`) exists
//! for local testing without a live bot. Error messages mirror the Python
//! service so client behavior is unchanged.

use std::collections::BTreeMap;

use axum::http::StatusCode;
use clab_core::repo::requests::{self as repo_req, MiniappPoster};
use clab_core::Db;
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Auth failure with the HTTP status the Python `AuthError` carried.
#[derive(Debug)]
pub struct AuthError {
    pub status: StatusCode,
    pub message: String,
}

impl AuthError {
    fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self { status, message: message.into() }
    }
}

impl From<sqlx::Error> for AuthError {
    fn from(e: sqlx::Error) -> Self {
        tracing::error!("miniapp auth db error: {e}");
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
    }
}

/// The identity fields we need out of a validated initData string.
#[derive(Debug, Default, PartialEq)]
pub struct InitUser {
    pub user_id: Option<i64>,
    pub username: Option<String>,
}

fn dev_auth_enabled() -> bool {
    matches!(
        std::env::var("MINIAPP_DEV_AUTH").unwrap_or_default().trim().to_lowercase().as_str(),
        "1" | "true" | "yes"
    )
}

/// How old `auth_date` may be before initData is rejected. 0 disables.
fn max_age_seconds() -> i64 {
    std::env::var("MINIAPP_INITDATA_MAX_AGE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(86400)
}

/// Percent-decode one query component the way Python's `parse_qsl` does:
/// `+` means space, `%XX` decodes, malformed escapes pass through literally.
fn pct_decode(s: &str) -> String {
    fn hex_val(b: u8) -> Option<u8> {
        match b {
            b'0'..=b'9' => Some(b - b'0'),
            b'a'..=b'f' => Some(b - b'a' + 10),
            b'A'..=b'F' => Some(b - b'A' + 10),
            _ => None,
        }
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                if let (Some(h), Some(l)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                    out.push(h << 4 | l);
                    i += 3;
                } else {
                    out.push(b'%');
                    i += 1;
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Parse `k=v&k2=v2` into decoded pairs (blank values kept, last dup wins —
/// same as `dict(parse_qsl(..., keep_blank_values=True))`). BTreeMap gives the
/// sorted-key iteration the data-check-string needs.
fn parse_pairs(init_data: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for seg in init_data.split('&').filter(|s| !s.is_empty()) {
        let (k, v) = seg.split_once('=').unwrap_or((seg, ""));
        out.insert(pct_decode(k), pct_decode(v));
    }
    out
}

/// Validate an initData string against `bot_token` and return the user
/// identity. Pure (clock + max-age injected) so it's directly testable.
pub fn validate_init_data(
    init_data: &str,
    bot_token: &str,
    max_age: i64,
    now_epoch: i64,
) -> Result<InitUser, AuthError> {
    if init_data.is_empty() {
        return Err(AuthError::new(StatusCode::UNAUTHORIZED, "missing initData"));
    }

    let mut pairs = parse_pairs(init_data);
    let provided_hash = pairs
        .remove("hash")
        .filter(|h| !h.is_empty())
        .ok_or_else(|| AuthError::new(StatusCode::UNAUTHORIZED, "initData missing hash"))?;

    let data_check_string = pairs
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("\n");

    let mut secret = HmacSha256::new_from_slice(b"WebAppData").expect("hmac accepts any key size");
    secret.update(bot_token.as_bytes());
    let secret_key = secret.finalize().into_bytes();

    let mut mac = HmacSha256::new_from_slice(&secret_key).expect("hmac accepts any key size");
    mac.update(data_check_string.as_bytes());

    // Constant-time comparison via Mac::verify_slice (mirrors hmac.compare_digest).
    let provided_bytes = hex::decode(&provided_hash)
        .map_err(|_| AuthError::new(StatusCode::FORBIDDEN, "initData signature mismatch"))?;
    mac.verify_slice(&provided_bytes)
        .map_err(|_| AuthError::new(StatusCode::FORBIDDEN, "initData signature mismatch"))?;

    if max_age > 0 {
        let auth_date: i64 = pairs
            .get("auth_date")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        if auth_date != 0 && now_epoch - auth_date > max_age {
            return Err(AuthError::new(StatusCode::FORBIDDEN, "initData expired"));
        }
    }

    // `user` is a JSON blob; a malformed one degrades to "no user" (Python
    // parsed it to None rather than failing validation).
    let user: Option<serde_json::Value> = pairs
        .get("user")
        .and_then(|raw| serde_json::from_str(raw).ok());
    let user = user.unwrap_or(serde_json::Value::Null);
    Ok(InitUser {
        user_id: user.get("id").and_then(serde_json::Value::as_i64),
        username: user
            .get("username")
            .and_then(serde_json::Value::as_str)
            .map(str::to_string),
    })
}

/// Bot token: env `TELEGRAM_BOT_TOKEN` beats the stored settings value.
async fn bot_token(db: &Db) -> Result<Option<String>, sqlx::Error> {
    let env_token = std::env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
    let env_token = env_token.trim();
    if !env_token.is_empty() {
        return Ok(Some(env_token.to_string()));
    }
    Ok(repo_req::get_setting(db, "bot_token")
        .await?
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty()))
}

/// Resolve the poster for an incoming Mini App request. Honors the dev bypass
/// when enabled, otherwise validates initData and maps the Telegram user to a
/// poster via poster_users / telegram_username.
pub async fn resolve_poster_from_request(
    db: &Db,
    init_data: Option<&str>,
    dev_poster_id: Option<&str>,
) -> Result<MiniappPoster, AuthError> {
    if let Some(dev_id) = dev_poster_id.filter(|d| !d.is_empty() && dev_auth_enabled()) {
        return repo_req::get_poster_mini(db, dev_id).await?.ok_or_else(|| {
            AuthError::new(StatusCode::NOT_FOUND, format!("dev poster '{dev_id}' not found"))
        });
    }

    let token = bot_token(db).await?.ok_or_else(|| {
        AuthError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "bot token not configured; cannot validate initData",
        )
    })?;

    let user = validate_init_data(
        init_data.unwrap_or(""),
        &token,
        max_age_seconds(),
        chrono::Utc::now().timestamp(),
    )?;
    if user.user_id.is_none() && user.username.as_deref().unwrap_or("").is_empty() {
        return Err(AuthError::new(StatusCode::UNAUTHORIZED, "initData has no user"));
    }

    repo_req::poster_for_user(db, user.user_id, user.username.as_deref())
        .await?
        .ok_or_else(|| {
            AuthError::new(
                StatusCode::FORBIDDEN,
                "this Telegram account is not linked to a poster — ask an admin to \
                 bind it in the Distribution → Telegram tab",
            )
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "7654321:AAFakeBotTokenForTests_abc123";

    /// Sign decoded (k, v) pairs with the documented algorithm and return the
    /// hex hash — the test-side generator for known-good fixtures.
    fn sign(pairs: &[(&str, &str)], token: &str) -> String {
        let mut sorted: Vec<_> = pairs.to_vec();
        sorted.sort_by_key(|(k, _)| k.to_string());
        let dcs = sorted
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join("\n");
        let mut secret = HmacSha256::new_from_slice(b"WebAppData").unwrap();
        secret.update(token.as_bytes());
        let secret_key = secret.finalize().into_bytes();
        let mut mac = HmacSha256::new_from_slice(&secret_key).unwrap();
        mac.update(dcs.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    /// Minimal percent-encoder for building the wire-format initData string.
    fn pct_encode(s: &str) -> String {
        s.bytes()
            .map(|b| match b {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                    (b as char).to_string()
                }
                _ => format!("%{b:02X}"),
            })
            .collect()
    }

    /// A realistic initData fixture: user JSON is percent-encoded on the wire,
    /// but the hash covers the DECODED values.
    fn fixture(auth_date: i64) -> String {
        let user = r#"{"id":777,"first_name":"Seff","username":"seff_tt"}"#;
        let ad = auth_date.to_string();
        let pairs = [
            ("auth_date", ad.as_str()),
            ("query_id", "AAH1234567890"),
            ("user", user),
        ];
        let hash = sign(&pairs, TOKEN);
        format!(
            "query_id=AAH1234567890&user={}&auth_date={}&hash={}",
            pct_encode(user),
            auth_date,
            hash
        )
    }

    #[test]
    fn known_good_fixture_validates_and_extracts_user() {
        let now = 1_750_000_000;
        let init = fixture(now - 60);
        let user = validate_init_data(&init, TOKEN, 86400, now).unwrap();
        assert_eq!(user.user_id, Some(777));
        assert_eq!(user.username.as_deref(), Some("seff_tt"));
    }

    #[test]
    fn tampered_hash_is_rejected_403() {
        let now = 1_750_000_000;
        let init = fixture(now - 60);
        // Flip the last hex digit of the hash.
        let mut tampered = init.clone();
        let last = tampered.pop().unwrap();
        tampered.push(if last == '0' { '1' } else { '0' });
        let err = validate_init_data(&tampered, TOKEN, 86400, now).unwrap_err();
        assert_eq!(err.status, StatusCode::FORBIDDEN);
        assert_eq!(err.message, "initData signature mismatch");
    }

    #[test]
    fn tampered_payload_is_rejected_403() {
        let now = 1_750_000_000;
        // Valid hash for uid 777, then swap the user blob for uid 666.
        let init = fixture(now - 60);
        let evil = init.replace(&pct_encode("\"id\":777"), &pct_encode("\"id\":666"));
        assert_ne!(init, evil, "replacement must have applied");
        let err = validate_init_data(&evil, TOKEN, 86400, now).unwrap_err();
        assert_eq!(err.status, StatusCode::FORBIDDEN);
        assert_eq!(err.message, "initData signature mismatch");
    }

    #[test]
    fn wrong_bot_token_is_rejected() {
        let now = 1_750_000_000;
        let init = fixture(now - 60);
        let err = validate_init_data(&init, "1:other_token", 86400, now).unwrap_err();
        assert_eq!(err.status, StatusCode::FORBIDDEN);
    }

    #[test]
    fn expired_auth_date_is_rejected_403() {
        let now = 1_750_000_000;
        let init = fixture(now - 86401); // one second past the default window
        let err = validate_init_data(&init, TOKEN, 86400, now).unwrap_err();
        assert_eq!(err.status, StatusCode::FORBIDDEN);
        assert_eq!(err.message, "initData expired");
    }

    #[test]
    fn max_age_zero_disables_expiry() {
        let now = 1_750_000_000;
        let init = fixture(now - 999_999_999); // ancient
        let user = validate_init_data(&init, TOKEN, 0, now).unwrap();
        assert_eq!(user.user_id, Some(777));
    }

    #[test]
    fn missing_initdata_and_missing_hash_are_401() {
        let err = validate_init_data("", TOKEN, 86400, 0).unwrap_err();
        assert_eq!(err.status, StatusCode::UNAUTHORIZED);
        assert_eq!(err.message, "missing initData");

        let err = validate_init_data("auth_date=1&user=x", TOKEN, 86400, 0).unwrap_err();
        assert_eq!(err.status, StatusCode::UNAUTHORIZED);
        assert_eq!(err.message, "initData missing hash");
    }

    #[test]
    fn non_hex_hash_is_signature_mismatch_not_panic() {
        let err = validate_init_data("auth_date=1&hash=nothex!", TOKEN, 86400, 0).unwrap_err();
        assert_eq!(err.status, StatusCode::FORBIDDEN);
        assert_eq!(err.message, "initData signature mismatch");
    }

    #[test]
    fn pct_decode_matches_parse_qsl_semantics() {
        assert_eq!(pct_decode("a%20b+c"), "a b c");
        assert_eq!(pct_decode("%7B%22id%22%3A1%7D"), r#"{"id":1}"#);
        assert_eq!(pct_decode("100%"), "100%"); // malformed escape passes through
        assert_eq!(pct_decode("%zz"), "%zz");
    }
}
