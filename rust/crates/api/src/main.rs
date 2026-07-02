//! Content Lab — Axum entrypoint.
//!
//! One process: JSON API under /api, and (in production) it serves the built
//! React SPA. State lives in SQLite on the Railway volume — no JSON blobs, no
//! in-memory job dicts that vanish on restart.

mod miniapp_auth;
mod paths;
mod providers;
mod proxy;
mod routes;
mod state;
mod telegram;

use std::net::SocketAddr;

use anyhow::Context;
use axum::extract::DefaultBodyLimit;
use axum::Router;
use tower_http::trace::TraceLayer;
use tracing_subscriber::EnvFilter;

use crate::state::AppState;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    // DB path: Railway volume if mounted, else local ./data.
    let data_dir = std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| std::path::Path::new(p).exists())
        .unwrap_or_else(|| "./data".to_string());
    std::fs::create_dir_all(&data_dir).ok();
    let db_url = format!("sqlite://{data_dir}/content_lab.db");

    // `migrate` subcommand: one-shot import of the legacy JSON state, then exit.
    //   cargo run -p api -- migrate [json_dir]
    // json_dir defaults to the data dir (where the volume keeps the JSON files).
    // ponytail: std::env::args, no clap — one subcommand doesn't need a parser.
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("migrate") {
        let json_dir = args
            .get(2)
            .cloned()
            .unwrap_or_else(|| data_dir.clone());
        let db = clab_core::Db::connect(&db_url)
            .await
            .context("failed to open database")?;
        let report = clab_core::import::import_json_state(&db, std::path::Path::new(&json_dir))
            .await
            .context("migrate failed — nothing was committed")?;
        println!("{report}");
        return Ok(());
    }

    tracing::info!("opening database at {db_url}");
    let db = clab_core::Db::connect(&db_url)
        .await
        .context("failed to open database")?;

    // Reclaim jobs/assets orphaned by a previous crash/restart so clients don't
    // poll a stuck 'processing' state forever.
    match clab_core::repo::recover_orphaned(&db).await {
        Ok((j, a)) if j > 0 || a > 0 => {
            tracing::warn!("recovered {j} orphaned job(s) and {a} asset(s) from a prior restart");
        }
        Ok(_) => {}
        Err(e) => tracing::error!("orphan recovery failed: {e}"),
    }

    // Telegram bot handle — env token beats stored settings token (hard rule);
    // with neither, the handle boots empty so PUT /api/telegram/bot-token can
    // install one at runtime. We do NOT auto-send on boot.
    let telegram = crate::telegram::Telegram::from_env_or_settings(&db).await;
    if telegram.is_running().await {
        tracing::info!("telegram: bot configured");
    } else {
        tracing::info!("telegram: no token yet — distribution disabled until one is installed");
    }

    let state = AppState::new(db, Some(telegram));

    // Daily forward schedule (config-driven; first act is always a sleep, so
    // nothing sends at startup).
    routes::distribution::spawn_schedule_loop(state.clone());

    // Serve produced media (generated/clips/burned) under /projects/* so a
    // client's <video src="/projects/.../video.mp4"> resolves. Asset paths are
    // stored relative to data_dir, so this maps 1:1.
    let projects_dir = std::path::Path::new(&data_dir).join("projects");
    std::fs::create_dir_all(&projects_dir).ok();
    let media = tower_http::services::ServeDir::new(&projects_dir);

    let app = Router::new()
        .merge(routes::router())
        .nest_service("/projects", media);

    // ABN reverse-proxy (/api/agenticnews/*, /api/pipeline/*) + /fonts + the
    // React SPA (frontend/dist with index.html fallback). Owned by proxy.rs.
    let app = crate::proxy::attach(app, &data_dir);

    let app = app
        // Global request-body ceiling. Axum 0.8 has no default; without this a
        // huge base64 image/overlay (or any body) would buffer in memory and
        // OOM the process. 100MB clears the 50MB base64 fields with headroom;
        // per-field guards in the handlers stay tighter.
        .layer(DefaultBodyLimit::max(100 * 1024 * 1024))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8000);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));

    tracing::info!("content-lab listening on http://{addr}");
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("failed to bind {addr}"))?;
    axum::serve(listener, app)
        .await
        .context("server error")?;
    Ok(())
}
