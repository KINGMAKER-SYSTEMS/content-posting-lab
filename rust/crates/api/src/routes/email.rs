//! /api/email — Cloudflare Email Routing proxy (port of `routers/email_routing.py`).
//!
//! KEEP set from the contract CUTLIST:
//!   GET    /api/email/status
//!   GET    /api/email/destinations
//!   POST   /api/email/destinations
//!   POST   /api/email/auto-create
//!   DELETE /api/email/rules/{rule_id}
//!
//! Rules live in Cloudflare, not local state; the only local touch is the
//! roster-page link (email_alias / email_rule_id / fwd_destination), which
//! rides in `pages.extra` JSON per the wave-0 schema. That read/merge is done
//! here with direct sqlx until the roster workstream lands a typed page repo
//! (seam noted in the wave report).
//!
//! Status mapping (Python parity): 503 not configured, 502 CF upstream
//! failure, 400 bad alias, 409 alias-exists / rule-page mismatch, 422
//! unverified destination.

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use integrations::email::{self as cf, EmailConfig};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/email/status", get(email_status))
        .route(
            "/api/email/destinations",
            get(get_destinations).post(add_destination_address),
        )
        .route("/api/email/auto-create", post(auto_create_for_page))
        .route("/api/email/rules/{rule_id}", delete(delete_email_rule))
}

// ── error helpers ────────────────────────────────────────────────────────────

fn err(status: StatusCode, msg: impl Into<String>) -> Response {
    (status, Json(json!({ "error": msg.into() }))).into_response()
}

/// 503 body — used with `EmailConfig::from_env().ok_or_else(not_configured)?`,
/// mirroring Python `_require_configured()`.
fn not_configured() -> Response {
    err(
        StatusCode::SERVICE_UNAVAILABLE,
        "CF Email Routing not configured",
    )
}

/// CF upstream failure → 502 (mirrors Python `_cf_upstream()`).
fn upstream(e: anyhow::Error) -> Response {
    err(StatusCode::BAD_GATEWAY, e.to_string())
}

/// Local DB failure → opaque 500 (detail goes to the log, like ApiError::Db).
fn db_err(e: sqlx::Error) -> Response {
    tracing::error!("email route db error: {e}");
    err(StatusCode::INTERNAL_SERVER_ERROR, "internal error")
}

// ── status ───────────────────────────────────────────────────────────────────

/// Check if CF Email Routing is configured.
async fn email_status() -> Json<Value> {
    match EmailConfig::from_env() {
        Some(cfg) => Json(json!({ "configured": true, "domain": cfg.domain })),
        None => Json(json!({ "configured": false, "domain": Value::Null })),
    }
}

// ── destinations ─────────────────────────────────────────────────────────────

/// List verified destination addresses.
async fn get_destinations() -> Result<Json<Value>, Response> {
    let cfg = EmailConfig::from_env().ok_or_else(not_configured)?;
    let destinations = cf::list_destinations(&cfg).await.map_err(upstream)?;
    Ok(Json(json!({ "destinations": destinations })))
}

#[derive(Deserialize)]
struct AddDestinationRequest {
    email: String,
}

/// Add a new destination address (triggers CF verification email).
async fn add_destination_address(
    Json(req): Json<AddDestinationRequest>,
) -> Result<Json<Value>, Response> {
    let cfg = EmailConfig::from_env().ok_or_else(not_configured)?;
    let dest = cf::add_destination(&cfg, &req.email).await.map_err(upstream)?;
    Ok(Json(json!({ "destination": dest })))
}

// ── auto-create for roster page ──────────────────────────────────────────────

#[derive(Deserialize)]
struct AutoCreateRequest {
    integration_id: String,
    account_name: String,
    destination: String,
}

/// Sanitize an account name to a valid email local part — Python parity:
/// lowercase, keep alphanumerics + `-_.`, map the rest to `-`, then strip
/// leading/trailing `-` and `.` (but not `_`).
fn sanitize_alias(account_name: &str) -> String {
    let mapped: String = account_name
        .to_lowercase()
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect();
    mapped.trim_matches(|c| c == '-' || c == '.').to_string()
}

/// Python truthiness for a JSON value — CF's `verified` field is an ISO
/// timestamp (truthy) when verified, null otherwise.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn destination_email(d: &Value) -> String {
    d["email"].as_str().unwrap_or("").trim().to_lowercase()
}

/// Auto-generate an email alias for a roster page and create the CF rule.
async fn auto_create_for_page(
    State(st): State<AppState>,
    Json(req): Json<AutoCreateRequest>,
) -> Result<Json<Value>, Response> {
    let cfg = EmailConfig::from_env().ok_or_else(not_configured)?;

    let alias = sanitize_alias(&req.account_name);
    if alias.is_empty() {
        return Err(err(
            StatusCode::BAD_REQUEST,
            "Invalid account name for email alias",
        ));
    }
    let full_alias = format!("{alias}@{}", cfg.domain);

    // Reject an alias that already has a rule.
    let existing_rules = cf::list_rules(&cfg).await.map_err(upstream)?;
    let taken = existing_rules.iter().any(|rule| {
        rule["matchers"]
            .as_array()
            .map(|ms| ms.iter().any(|m| m["value"] == full_alias.as_str()))
            .unwrap_or(false)
    });
    if taken {
        return Err(err(
            StatusCode::CONFLICT,
            format!("Alias {full_alias} already exists"),
        ));
    }

    // Reject destinations CF hasn't verified — a rule pointing at an unverified
    // address silently drops mail, breaking the sale-intake handoff.
    let destinations = cf::list_destinations(&cfg).await.map_err(upstream)?;
    let wanted = req.destination.trim().to_lowercase();
    let verified = destinations
        .iter()
        .filter(|d| truthy(&d["verified"]))
        .any(|d| destination_email(d) == wanted);
    if !verified {
        return Err(err(
            StatusCode::UNPROCESSABLE_ENTITY,
            format!(
                "Destination {} is not a verified Cloudflare destination",
                req.destination
            ),
        ));
    }

    let rule = cf::create_rule(&cfg, &alias, &req.destination)
        .await
        .map_err(upstream)?;

    // Link to the roster page (only when the page exists — Python parity).
    let page = page_row(&st.db, &req.integration_id).await.map_err(db_err)?;
    if page.is_some() {
        let rule_id = rule["id"].as_str().unwrap_or("").to_string();
        set_email_fields(
            &st.db,
            &req.integration_id,
            json!(full_alias),
            json!(rule_id),
            json!(req.destination),
        )
        .await
        .map_err(db_err)?;
    }
    let page_view = page_row(&st.db, &req.integration_id)
        .await
        .map_err(db_err)?
        .map(|r| r.view())
        .unwrap_or(Value::Null);

    Ok(Json(json!({
        "rule": rule,
        "alias": full_alias,
        "page": page_view,
    })))
}

// ── delete rule ──────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct DeleteRuleQuery {
    integration_id: Option<String>,
}

/// Delete an email routing rule and optionally unlink it from a roster page.
async fn delete_email_rule(
    State(st): State<AppState>,
    Path(rule_id): Path<String>,
    Query(q): Query<DeleteRuleQuery>,
) -> Result<Json<Value>, Response> {
    let cfg = EmailConfig::from_env().ok_or_else(not_configured)?;

    // When an integration_id is supplied, validate the rule actually belongs to
    // that page BEFORE deleting anything — otherwise a mismatched pair silently
    // nukes the wrong CF rule and leaves the page's real rule orphaned.
    let mut page: Option<PageRow> = None;
    if let Some(iid) = &q.integration_id {
        page = page_row(&st.db, iid).await.map_err(db_err)?;
        if let Some(p) = &page {
            let linked = p.email_rule_id();
            if !linked.is_empty() && linked != rule_id {
                return Err(err(
                    StatusCode::CONFLICT,
                    format!(
                        "rule_id {rule_id} does not belong to page {iid} (linked rule is {linked})"
                    ),
                ));
            }
        }
    }

    cf::delete_rule(&cfg, &rule_id).await.map_err(upstream)?;

    // Unlink from the roster page.
    if let (Some(iid), Some(_)) = (&q.integration_id, &page) {
        set_email_fields(&st.db, iid, Value::Null, Value::Null, Value::Null)
            .await
            .map_err(db_err)?;
    }

    Ok(Json(json!({ "deleted": true })))
}

// ── roster-page access (pages.extra JSON — see module header) ───────────────

#[derive(sqlx::FromRow)]
struct PageRow {
    id: String,
    name: String,
    provider: Option<String>,
    project: Option<String>,
    drive_folder_url: Option<String>,
    drive_folder_id: Option<String>,
    poster_name: Option<String>,
    source: Option<String>,
    tiktok_url: Option<String>,
    extra: Option<String>,
}

impl PageRow {
    fn extra_obj(&self) -> serde_json::Map<String, Value> {
        self.extra
            .as_deref()
            .and_then(|s| serde_json::from_str::<Value>(s).ok())
            .and_then(|v| match v {
                Value::Object(o) => Some(o),
                _ => None,
            })
            .unwrap_or_default()
    }

    fn email_rule_id(&self) -> String {
        self.extra_obj()
            .get("email_rule_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    }

    /// The page as JSON: extra fields (incl. email_alias / email_rule_id /
    /// fwd_destination) overlaid with the typed columns.
    fn view(&self) -> Value {
        let mut obj = self.extra_obj();
        obj.insert("integration_id".into(), json!(self.id));
        obj.insert("name".into(), json!(self.name));
        obj.insert("provider".into(), json!(self.provider));
        obj.insert("project".into(), json!(self.project));
        obj.insert("drive_folder_url".into(), json!(self.drive_folder_url));
        obj.insert("drive_folder_id".into(), json!(self.drive_folder_id));
        obj.insert("poster_name".into(), json!(self.poster_name));
        obj.insert("source".into(), json!(self.source));
        obj.insert("tiktok_url".into(), json!(self.tiktok_url));
        Value::Object(obj)
    }
}

async fn page_row(db: &clab_core::Db, id: &str) -> Result<Option<PageRow>, sqlx::Error> {
    sqlx::query_as::<_, PageRow>(
        "SELECT id, name, provider, project, drive_folder_url, drive_folder_id,
                poster_name, source, tiktok_url, extra
         FROM pages WHERE id = ?",
    )
    .bind(id)
    .fetch_optional(db.reader())
    .await
}

/// Merge the three email-link fields into `pages.extra` (null = unlinked,
/// matching the Python dict which keeps the keys with None values).
async fn set_email_fields(
    db: &clab_core::Db,
    id: &str,
    alias: Value,
    rule_id: Value,
    destination: Value,
) -> Result<(), sqlx::Error> {
    let row: Option<(Option<String>,)> = sqlx::query_as("SELECT extra FROM pages WHERE id = ?")
        .bind(id)
        .fetch_optional(db.writer())
        .await?;
    let mut extra = row
        .and_then(|(s,)| s)
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| match v {
            Value::Object(o) => Some(o),
            _ => None,
        })
        .unwrap_or_default();
    extra.insert("email_alias".into(), alias);
    extra.insert("email_rule_id".into(), rule_id);
    extra.insert("fwd_destination".into(), destination);

    sqlx::query("UPDATE pages SET extra = ?, updated_at = ? WHERE id = ?")
        .bind(Value::Object(extra).to_string())
        .bind(chrono::Utc::now())
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tokio::sync::Mutex;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tower::ServiceExt;

    /// These tests mutate CF_* env vars — serialize them (async-aware so the
    /// guard can be held across awaits without tripping await_holding_lock).
    static ENV_LOCK: Mutex<()> = Mutex::const_new(());

    fn set_cf_env(base_url: Option<&str>) {
        for k in [
            "CF_API_TOKEN",
            "CF_ZONE_ID",
            "CF_ACCOUNT_ID",
            "CF_EMAIL_DOMAIN",
            "CF_API_BASE_URL",
        ] {
            std::env::remove_var(k);
        }
        if let Some(base) = base_url {
            std::env::set_var("CF_API_TOKEN", "t");
            std::env::set_var("CF_ZONE_ID", "z1");
            std::env::set_var("CF_ACCOUNT_ID", "a1");
            std::env::set_var("CF_EMAIL_DOMAIN", "pages.example.com");
            std::env::set_var("CF_API_BASE_URL", base);
        }
    }

    /// Minimal HTTP mock for the CF upstream (same pattern as the
    /// integrations-crate test helper; duplicated because cfg(test) items
    /// don't cross crate boundaries).
    async fn spawn_mock<F>(handler: F) -> (String, tokio::task::JoinHandle<()>)
    where
        F: Fn(&str) -> (u16, String) + Send + Sync + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else { break };
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                let head_end = loop {
                    match sock.read(&mut tmp).await {
                        Ok(0) | Err(_) => break None,
                        Ok(n) => {
                            buf.extend_from_slice(&tmp[..n]);
                            if let Some(p) =
                                buf.windows(4).position(|w| w == b"\r\n\r\n")
                            {
                                break Some(p + 4);
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
                        k.eq_ignore_ascii_case("content-length")
                            .then(|| v.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                while buf.len() < head_end + content_len {
                    match sock.read(&mut tmp).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => buf.extend_from_slice(&tmp[..n]),
                    }
                }
                let (status, body) = handler(&head);
                let resp = format!(
                    "HTTP/1.1 {status} MOCK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes()).await;
                let _ = sock.shutdown().await;
            }
        });
        (base, handle)
    }

    async fn test_state() -> AppState {
        // Db::connect_temp is cfg(test)-private to clab_core; open a throwaway
        // file DB in the OS tempdir instead.
        let path = std::env::temp_dir().join(format!("email-test-{}.db", uuid::Uuid::new_v4()));
        let db = clab_core::Db::connect(&format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        AppState::new(db, None)
    }

    async fn insert_page(st: &AppState, id: &str, name: &str, extra: Option<&str>) {
        sqlx::query(
            "INSERT INTO pages (id, name, project, r2_prefix, created_at, updated_at, extra)
             VALUES (?, ?, NULL, NULL, ?, ?, ?)",
        )
        .bind(id)
        .bind(name)
        .bind(chrono::Utc::now())
        .bind(chrono::Utc::now())
        .bind(extra)
        .execute(st.db.writer())
        .await
        .unwrap();
    }

    async fn call(st: &AppState, req: Request<Body>) -> (StatusCode, Value) {
        let app = router().with_state(st.clone());
        let resp = app.oneshot(req).await.unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap()
        };
        (status, body)
    }

    fn json_req(method: &str, uri: &str, body: Value) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap()
    }

    #[test]
    fn sanitize_alias_matches_python() {
        assert_eq!(sanitize_alias("My Cool Page!"), "my-cool-page");
        assert_eq!(sanitize_alias("a.b_c-d"), "a.b_c-d");
        assert_eq!(sanitize_alias("---...---"), "");
        assert_eq!(sanitize_alias("..hi.."), "hi");
        // '_' is kept at the edges (Python strip("-.") doesn't strip it).
        assert_eq!(sanitize_alias("_page_"), "_page_");
        assert_eq!(sanitize_alias("Página Única"), "página-única");
    }

    #[tokio::test]
    async fn status_reports_unconfigured_and_configured() {
        let _g = ENV_LOCK.lock().await;
        let st = test_state().await;

        set_cf_env(None);
        let (code, body) = call(&st, Request::get("/api/email/status").body(Body::empty()).unwrap()).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body, serde_json::json!({ "configured": false, "domain": null }));

        set_cf_env(Some("http://127.0.0.1:1"));
        let (code, body) = call(&st, Request::get("/api/email/status").body(Body::empty()).unwrap()).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body, serde_json::json!({ "configured": true, "domain": "pages.example.com" }));
        set_cf_env(None);
    }

    #[tokio::test]
    async fn destinations_require_config_then_proxy() {
        let _g = ENV_LOCK.lock().await;
        let st = test_state().await;

        set_cf_env(None);
        let (code, _) = call(&st, Request::get("/api/email/destinations").body(Body::empty()).unwrap()).await;
        assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);

        let (base, server) = spawn_mock(|head| {
            if head.starts_with("GET /accounts/a1/email/routing/addresses ") {
                (200, r#"{"result":[{"email":"ops@x.com","verified":"2026-01-01"}]}"#.into())
            } else {
                (500, "{}".into())
            }
        })
        .await;
        set_cf_env(Some(&base));
        let (code, body) = call(&st, Request::get("/api/email/destinations").body(Body::empty()).unwrap()).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body["destinations"][0]["email"], "ops@x.com");
        set_cf_env(None);
        server.abort();
    }

    #[tokio::test]
    async fn auto_create_full_flow_and_guards() {
        let _g = ENV_LOCK.lock().await;
        let st = test_state().await;
        insert_page(&st, "int-1", "My Page", None).await;

        let (base, server) = spawn_mock(|head| {
            if head.starts_with("GET /zones/z1/email/routing/rules ") {
                // One existing rule for the "taken" alias.
                (200, r#"{"result":[{"id":"r-old","matchers":[{"type":"literal","field":"to","value":"taken@pages.example.com"}]}]}"#.into())
            } else if head.starts_with("GET /accounts/a1/email/routing/addresses ") {
                (200, r#"{"result":[{"email":"Ops@X.com","verified":"2026-01-01"},{"email":"new@x.com","verified":null}]}"#.into())
            } else if head.starts_with("POST /zones/z1/email/routing/rules ") {
                (200, r#"{"result":{"id":"r-new","enabled":true}}"#.into())
            } else {
                (500, "{}".into())
            }
        })
        .await;
        set_cf_env(Some(&base));

        // 409: alias collides with an existing matcher.
        let (code, _) = call(&st, json_req("POST", "/api/email/auto-create", serde_json::json!({
            "integration_id": "int-1", "account_name": "Taken", "destination": "ops@x.com"
        }))).await;
        assert_eq!(code, StatusCode::CONFLICT);

        // 422: destination exists but is unverified.
        let (code, _) = call(&st, json_req("POST", "/api/email/auto-create", serde_json::json!({
            "integration_id": "int-1", "account_name": "My Page", "destination": "new@x.com"
        }))).await;
        assert_eq!(code, StatusCode::UNPROCESSABLE_ENTITY);

        // 400: alias sanitizes to empty.
        let (code, _) = call(&st, json_req("POST", "/api/email/auto-create", serde_json::json!({
            "integration_id": "int-1", "account_name": "!!!", "destination": "ops@x.com"
        }))).await;
        assert_eq!(code, StatusCode::BAD_REQUEST);

        // Happy path: rule created, page linked, response carries rule+alias+page.
        let (code, body) = call(&st, json_req("POST", "/api/email/auto-create", serde_json::json!({
            "integration_id": "int-1", "account_name": "My Page", "destination": "OPS@x.com "
        }))).await;
        assert_eq!(code, StatusCode::OK, "{body}");
        assert_eq!(body["rule"]["id"], "r-new");
        assert_eq!(body["alias"], "my-page@pages.example.com");
        assert_eq!(body["page"]["integration_id"], "int-1");
        assert_eq!(body["page"]["email_alias"], "my-page@pages.example.com");
        assert_eq!(body["page"]["email_rule_id"], "r-new");
        assert_eq!(body["page"]["fwd_destination"], "OPS@x.com ");

        set_cf_env(None);
        server.abort();
    }

    #[tokio::test]
    async fn delete_rule_validates_page_link_then_unlinks() {
        let _g = ENV_LOCK.lock().await;
        let st = test_state().await;
        insert_page(
            &st,
            "int-2",
            "Linked Page",
            Some(r#"{"email_alias":"x@pages.example.com","email_rule_id":"r-1","fwd_destination":"ops@x.com"}"#),
        )
        .await;

        let (base, server) = spawn_mock(|head| {
            if head.starts_with("DELETE /zones/z1/email/routing/rules/") {
                (200, r#"{"result":null,"success":true}"#.into())
            } else {
                (500, "{}".into())
            }
        })
        .await;
        set_cf_env(Some(&base));

        // Mismatched rule ↔ page → 409, nothing deleted.
        let (code, body) = call(
            &st,
            Request::delete("/api/email/rules/r-wrong?integration_id=int-2")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(code, StatusCode::CONFLICT);
        assert!(body["error"].as_str().unwrap().contains("linked rule is r-1"));

        // Matching rule → deleted and page unlinked (keys kept as nulls).
        let (code, body) = call(
            &st,
            Request::delete("/api/email/rules/r-1?integration_id=int-2")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body, serde_json::json!({ "deleted": true }));
        let page = page_row(&st.db, "int-2").await.unwrap().unwrap();
        let extra = page.extra_obj();
        assert_eq!(extra.get("email_alias"), Some(&Value::Null));
        assert_eq!(extra.get("email_rule_id"), Some(&Value::Null));
        assert_eq!(extra.get("fwd_destination"), Some(&Value::Null));

        set_cf_env(None);
        server.abort();
    }
}
