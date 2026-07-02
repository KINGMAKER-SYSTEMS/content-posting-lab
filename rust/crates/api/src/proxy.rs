//! SPA + reverse-proxy layer — bolted on after every API route is merged.
//!
//! Three responsibilities (see fns below):
//! 1. `attach_abn_proxy` — reverse-proxies `/api/agenticnews/*` and
//!    `/api/pipeline/*` to the still-Python ABN factory service
//!    (`ABN_UPSTREAM_URL`).
//! 2. `attach_fonts` — serves the repo's `fonts/` directory at `/fonts`.
//! 3. `attach_spa` — serves `frontend/dist` at `/`, falling back to
//!    `index.html` for client-side routes.
//!
//! Route registration order matters: the ABN proxy and `/fonts` are real
//! routes merged into the router, so they always win over the catch-all SPA
//! `fallback_service` — the fallback only ever fires for a path nothing else
//! claimed. `/api/*` (routes::router()), `/projects/*` (nest_service in
//! main.rs), and any future `/ws/*` routes are registered before this
//! function runs, so they're already "claimed" by the time the SPA fallback
//! goes in.

use axum::body::Body;
use axum::extract::Request;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use tower_http::services::{ServeDir, ServeFile};

use crate::state::AppState;

/// Env var carrying the base URL of the still-Python ABN factory service.
const ABN_UPSTREAM_ENV: &str = "ABN_UPSTREAM_URL";
const ABN_UPSTREAM_DEFAULT: &str = "http://127.0.0.1:8001";

/// Wrap the API router with static-serving + reverse-proxy layers.
pub fn attach(app: Router<AppState>, _data_dir: &str) -> Router<AppState> {
    let app = app
        .route("/api/agenticnews/{*rest}", any(proxy_agenticnews))
        .route("/api/agenticnews", any(proxy_agenticnews))
        .route("/api/pipeline/{*rest}", any(proxy_pipeline))
        .route("/api/pipeline", any(proxy_pipeline));

    // Guard rails so the SPA `fallback_service` (installed below) never swallows
    // an unmatched `/api/*` or `/ws/*` request and answers it with index.html.
    // Specific real routes always beat these wildcards, so this only fires for
    // paths no real handler claimed — where a JSON 404 is correct, not the SPA.
    let app = app
        .route("/api/{*rest}", any(api_not_found))
        .route("/ws/{*rest}", any(api_not_found));

    let app = attach_fonts(app);
    attach_spa(app)
}

/// 404 for any `/api/*` or `/ws/*` path that no real route matched. Keeps the
/// SPA fallback from masking a genuinely-missing API endpoint as a 200 HTML
/// page (which would break a client `fetch()` expecting JSON).
async fn api_not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(serde_json::json!({ "error": "not found" })),
    )
        .into_response()
}

/// Mount `/fonts` -> `<repo_root>/fonts`. Relative to process CWD, matching
/// the Python `StaticFiles(directory="fonts")` mount. Robust to a missing
/// directory: `ServeDir` just 404s per-file rather than panicking at boot.
fn attach_fonts(app: Router<AppState>) -> Router<AppState> {
    let fonts_dir = fonts_dir_path();
    let serve_fonts = ServeDir::new(fonts_dir).not_found_service(NotFoundJson);
    app.nest_service("/fonts", serve_fonts)
}

/// Resolve the fonts directory: `FONTS_DIR` env override, else `./fonts`
/// relative to CWD (matching Python's relative mount).
fn fonts_dir_path() -> std::path::PathBuf {
    std::env::var("FONTS_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::path::PathBuf::from("fonts"))
}

/// Resolve the frontend build directory: `FRONTEND_DIST_DIR` env override,
/// else `./frontend/dist` relative to CWD (matching Python's `FRONTEND_DIR`).
fn frontend_dist_path() -> std::path::PathBuf {
    std::env::var("FRONTEND_DIST_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::path::PathBuf::from("frontend/dist"))
}

/// Serve the built React SPA at `/`, falling back to `index.html` for any
/// unmatched route (client-side routing). If `frontend/dist` doesn't exist
/// (dev without a build), install a fallback that always answers 503 with a
/// clear message instead of panicking at boot or silently 404ing.
fn attach_spa(app: Router<AppState>) -> Router<AppState> {
    let dist = frontend_dist_path();
    let index = dist.join("index.html");

    if dist.is_dir() && index.is_file() {
        let serve_dir = ServeDir::new(dist).fallback(ServeFile::new(index));
        app.fallback_service(serve_dir)
    } else {
        tracing::warn!(
            "frontend dist not found at {} — SPA disabled, unmatched routes return 503",
            dist.display()
        );
        app.fallback(frontend_not_built)
    }
}

async fn frontend_not_built() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(serde_json::json!({
            "error": "frontend not built — run `npm run build` in frontend/"
        })),
    )
        .into_response()
}

/// A tiny `Service` that always answers 404 JSON — used as the ServeDir
/// fallback for `/fonts` so a missing directory/file degrades to a normal
/// JSON 404 instead of the default empty-body 404.
#[derive(Clone, Copy)]
struct NotFoundJson;

impl<ReqBody> tower::Service<axum::http::Request<ReqBody>> for NotFoundJson
where
    ReqBody: Send + 'static,
{
    type Response = Response;
    type Error = std::convert::Infallible;
    type Future = std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Response, Self::Error>> + Send>,
    >;

    fn poll_ready(
        &mut self,
        _cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Result<(), Self::Error>> {
        std::task::Poll::Ready(Ok(()))
    }

    fn call(&mut self, _req: axum::http::Request<ReqBody>) -> Self::Future {
        Box::pin(async move {
            Ok((
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({ "error": "not found" })),
            )
                .into_response())
        })
    }
}

async fn proxy_agenticnews(req: Request) -> Response {
    proxy_request(req).await
}

async fn proxy_pipeline(req: Request) -> Response {
    proxy_request(req).await
}

/// The shared reverse-proxy client. Built once, reused across requests
/// (connection pooling). Deliberately separate from
/// `integrations::http::client()` — that client has a 30s timeout, but ABN
/// render/assemble jobs proxied through here can legitimately run far
/// longer, and this proxy must not impose a timeout the upstream job itself
/// doesn't have.
fn proxy_client() -> &'static reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .build()
            .expect("failed to build proxy reqwest client")
    })
}

fn upstream_base() -> String {
    std::env::var(ABN_UPSTREAM_ENV).unwrap_or_else(|_| ABN_UPSTREAM_DEFAULT.to_string())
}

/// 100MB — generous enough for render payloads passed through this proxy.
/// This is a buffering cap, not a hard product limit; it mirrors the outer
/// `DefaultBodyLimit` main.rs applies to the whole app.
const MAX_PROXY_BODY: usize = 100 * 1024 * 1024;

/// Headers that must not be forwarded as-is across a proxy hop (RFC 7230 §6.1
/// plus framing headers reqwest/axum set for us on each leg).
const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
];

fn strip_hop_by_hop(src: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::with_capacity(src.len());
    for (name, value) in src.iter() {
        if HOP_BY_HOP.contains(&name.as_str()) {
            continue;
        }
        out.append(name.clone(), value.clone());
    }
    out
}

/// Forward `req` to the ABN upstream verbatim (method, path+query, headers,
/// body) and relay the response back verbatim. Returns 502 JSON if the
/// upstream can't be reached at all.
async fn proxy_request(req: Request) -> Response {
    let base = upstream_base();
    let path_and_query = req
        .uri()
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| req.uri().path().to_string());
    let url = format!("{}{}", base.trim_end_matches('/'), path_and_query);

    let method = req.method().clone();
    let reqwest_method = match reqwest::Method::from_bytes(method.as_str().as_bytes()) {
        Ok(m) => m,
        Err(_) => return bad_gateway("unsupported method"),
    };

    let headers = strip_hop_by_hop(req.headers());
    let body_bytes = match axum::body::to_bytes(req.into_body(), MAX_PROXY_BODY).await {
        Ok(b) => b,
        Err(e) => {
            tracing::warn!("proxy: failed reading request body: {e}");
            return bad_gateway("failed to read request body");
        }
    };

    let upstream_req = proxy_client()
        .request(reqwest_method, &url)
        .headers(headers)
        .body(body_bytes);

    let upstream_resp = match upstream_req.send().await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!("proxy: upstream {url} unreachable: {e}");
            return bad_gateway("ABN upstream unreachable");
        }
    };

    relay_response(upstream_resp).await
}

async fn relay_response(resp: reqwest::Response) -> Response {
    let status = resp.status();
    let headers = resp.headers().clone();
    let body_stream = resp.bytes_stream();

    let mut builder = Response::builder().status(status.as_u16());
    if let Some(h) = builder.headers_mut() {
        for (name, value) in headers.iter() {
            if HOP_BY_HOP.contains(&name.as_str()) {
                continue;
            }
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(name.as_str().as_bytes()),
                HeaderValue::from_bytes(value.as_bytes()),
            ) {
                h.append(name, value);
            }
        }
    }

    let body = Body::from_stream(body_stream);
    match builder.body(body) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!("proxy: failed to build relayed response: {e}");
            bad_gateway("failed to relay upstream response")
        }
    }
}

fn bad_gateway(msg: &str) -> Response {
    (
        StatusCode::BAD_GATEWAY,
        Json(serde_json::json!({ "error": msg })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Tests that touch process env vars must not run concurrently with each
    // other (env is process-global), so serialize them behind one lock.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn upstream_base_defaults_when_env_unset() {
        let _g = ENV_LOCK.lock().unwrap();
        std::env::remove_var(ABN_UPSTREAM_ENV);
        assert_eq!(upstream_base(), ABN_UPSTREAM_DEFAULT);
    }

    #[test]
    fn upstream_base_honors_env_override() {
        let _g = ENV_LOCK.lock().unwrap();
        std::env::set_var(ABN_UPSTREAM_ENV, "http://example.internal:9999");
        assert_eq!(upstream_base(), "http://example.internal:9999");
        std::env::remove_var(ABN_UPSTREAM_ENV);
    }

    #[test]
    fn fonts_dir_defaults_to_relative_fonts() {
        let _g = ENV_LOCK.lock().unwrap();
        std::env::remove_var("FONTS_DIR");
        assert_eq!(fonts_dir_path(), std::path::PathBuf::from("fonts"));
    }

    #[test]
    fn fonts_dir_honors_env_override() {
        let _g = ENV_LOCK.lock().unwrap();
        std::env::set_var("FONTS_DIR", "/tmp/custom-fonts");
        assert_eq!(
            fonts_dir_path(),
            std::path::PathBuf::from("/tmp/custom-fonts")
        );
        std::env::remove_var("FONTS_DIR");
    }

    #[test]
    fn frontend_dist_defaults_to_relative_path() {
        let _g = ENV_LOCK.lock().unwrap();
        std::env::remove_var("FRONTEND_DIST_DIR");
        assert_eq!(
            frontend_dist_path(),
            std::path::PathBuf::from("frontend/dist")
        );
    }

    #[test]
    fn frontend_dist_honors_env_override() {
        let _g = ENV_LOCK.lock().unwrap();
        std::env::set_var("FRONTEND_DIST_DIR", "/tmp/custom-dist");
        assert_eq!(
            frontend_dist_path(),
            std::path::PathBuf::from("/tmp/custom-dist")
        );
        std::env::remove_var("FRONTEND_DIST_DIR");
    }

    #[test]
    fn strip_hop_by_hop_removes_connection_and_host() {
        let mut headers = HeaderMap::new();
        headers.insert("connection", HeaderValue::from_static("keep-alive"));
        headers.insert("host", HeaderValue::from_static("example.com"));
        headers.insert("x-custom", HeaderValue::from_static("keep-me"));
        let stripped = strip_hop_by_hop(&headers);
        assert!(stripped.get("connection").is_none());
        assert!(stripped.get("host").is_none());
        assert_eq!(stripped.get("x-custom").unwrap(), "keep-me");
    }

    #[test]
    fn proxy_url_join_preserves_query_string() {
        let base = "http://127.0.0.1:8001";
        let path_and_query = "/api/agenticnews/stats?since=123";
        let url = format!("{}{}", base.trim_end_matches('/'), path_and_query);
        assert_eq!(url, "http://127.0.0.1:8001/api/agenticnews/stats?since=123");
    }
}
