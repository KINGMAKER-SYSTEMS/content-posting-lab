//! Caption scraping routes — ported from `routers/captions.py` (+ the
//! `scraper/` helpers it pulled in).
//!
//! The pipeline: yt-dlp lists a TikTok profile's video URLs (metadata only —
//! never a video download), the cover thumbnails are fetched, and a GPT vision
//! model OCRs the burned-in caption text off each cover, with per-step progress
//! broadcast over WebSocket to every socket watching the job. Results land as
//! `captions.csv` on disk — the exact file the export/history endpoints and the
//! React app expect — AND as first-class caption rows in SQLite (the rewrite's
//! durable caption bank).
//!
//! Event names, payload field names, and the CSV column set match the Python
//! app byte-for-byte so `frontend/src/pages/Captions.tsx` needs no changes.

use std::collections::HashMap;
use std::path::{Path as FsPath, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use anyhow::Context;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::broadcast;

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        // WS routes must use any() in axum 0.8 (the upgrade isn't a plain GET
        // as far as method routing is concerned).
        .route("/api/captions/ws/{job_id}", any(ws_scrape))
        .route("/api/captions/export/{username}", get(export_captions))
        .route("/api/captions/history", get(caption_history))
        .route("/api/captions/rename-batch", post(rename_batch))
}

// ── Paths ────────────────────────────────────────────────────────────────────

fn data_dir() -> String {
    std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| FsPath::new(p).exists())
        .unwrap_or_else(|| "./data".to_string())
}

/// `<data>/projects/<project>/captions` — one folder per scraped username
/// underneath (mirrors the old `get_project_caption_dir`).
fn caption_dir(project: &str) -> PathBuf {
    PathBuf::from(data_dir())
        .join("projects")
        .join(project)
        .join("captions")
}

fn project_slug(project: Option<&str>) -> Result<String, ApiError> {
    crate::paths::sanitize_project_name(project.unwrap_or("quick-test"))
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))
}

/// A batch (username) folder name that is safe as a single path component.
/// Rejects separators and dot-components — the Python app passed these raw
/// (a traversal hole); we refuse instead.
fn safe_batch_name(name: &str) -> Option<&str> {
    if name.is_empty() || name == "." || name == ".." || name.contains('/') || name.contains('\\')
    {
        return None;
    }
    Some(name)
}

// ── WebSocket client registry ────────────────────────────────────────────────
//
// One broadcast channel per job_id; every socket watching that job subscribes.
// In-memory, like the old `_ws_clients` dict — a restart loses the live feed
// but the CSV + DB rows survive.

fn registry() -> &'static Mutex<HashMap<String, broadcast::Sender<String>>> {
    static REG: OnceLock<Mutex<HashMap<String, broadcast::Sender<String>>>> = OnceLock::new();
    REG.get_or_init(Default::default)
}

fn sender_for(job_id: &str) -> broadcast::Sender<String> {
    let mut map = registry().lock().unwrap();
    map.entry(job_id.to_string())
        .or_insert_with(|| broadcast::channel(256).0)
        .clone()
}

/// Serialize `{ "event": <event>, ...data }` — the exact wire shape the old
/// `_broadcast` produced.
fn event_msg(event: &str, mut data: Value) -> String {
    if let Some(map) = data.as_object_mut() {
        map.insert("event".to_string(), json!(event));
    }
    data.to_string()
}

fn emit(tx: &broadcast::Sender<String>, event: &str, data: Value) {
    // No subscribers is fine (send only fails when nobody's listening).
    let _ = tx.send(event_msg(event, data));
}

// ── WebSocket endpoint ───────────────────────────────────────────────────────

#[derive(Deserialize)]
struct WsInbound {
    action: Option<String>,
    profile_url: Option<String>,
    max_videos: Option<i64>,
    sort: Option<String>,
    project: Option<String>,
}

async fn ws_scrape(
    ws: WebSocketUpgrade,
    Path(job_id): Path<String>,
    State(st): State<AppState>,
) -> Response {
    ws.on_upgrade(move |sock| handle_ws(sock, job_id, st))
}

async fn handle_ws(mut sock: WebSocket, job_id: String, st: AppState) {
    tracing::info!("captions WS connected: {}", &job_id[..job_id.len().min(8)]);
    let tx = sender_for(&job_id);
    let mut rx = tx.subscribe();
    loop {
        tokio::select! {
            out = rx.recv() => match out {
                Ok(text) => {
                    if sock.send(Message::Text(text.into())).await.is_err() {
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(n)) => {
                    tracing::warn!("captions WS lagged, dropped {n} events");
                }
                Err(broadcast::error::RecvError::Closed) => break,
            },
            inbound = sock.recv() => match inbound {
                Some(Ok(Message::Text(raw))) => {
                    let Ok(msg) = serde_json::from_str::<WsInbound>(&raw) else { continue };
                    if msg.action.as_deref() == Some("start") {
                        let Some(profile_url) =
                            msg.profile_url.filter(|s| !s.trim().is_empty())
                        else {
                            emit(&tx, "error", json!({"error": "profile_url is required"}));
                            continue;
                        };
                        let max_videos = msg.max_videos.unwrap_or(20).clamp(1, 50) as usize;
                        let sort = msg.sort.unwrap_or_else(|| "latest".to_string());
                        tracing::info!(
                            "start scrape: profile={} max={} job={}",
                            &profile_url[..profile_url.len().min(60)],
                            max_videos,
                            &job_id[..job_id.len().min(8)],
                        );
                        let db = st.db.clone();
                        let tx = tx.clone();
                        tokio::spawn(async move {
                            if let Err(e) =
                                run_pipeline(db, &tx, profile_url, max_videos, sort, msg.project)
                                    .await
                            {
                                tracing::error!("caption pipeline failed: {e:#}");
                                emit(&tx, "error", json!({"error": format!("{e:#}")}));
                            }
                        });
                    }
                }
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                Some(Ok(_)) => {}
            }
        }
    }
    tracing::info!("captions WS disconnected: {}", &job_id[..job_id.len().min(8)]);
}

// ── Pipeline ─────────────────────────────────────────────────────────────────

struct Row {
    index: usize,
    video_url: String,
    video_id: String,
    frame_path: Option<PathBuf>,
    caption: Option<String>,
    mood: Option<String>,
    error: Option<String>,
}

const DOWNLOAD_BATCH: usize = 5;
const OCR_BATCH: usize = 3;

async fn run_pipeline(
    db: clab_core::Db,
    tx: &broadcast::Sender<String>,
    profile_url_raw: String,
    max_videos: usize,
    sort: String,
    project: Option<String>,
) -> anyhow::Result<()> {
    let profile_url = normalize_profile_url(&profile_url_raw);
    let username = extract_username(&profile_url).unwrap_or_else(|| "unknown".to_string());

    // Project-scoped output directory (always project-scoped for persistence).
    let project = crate::paths::sanitize_project_name(project.as_deref().unwrap_or("quick-test"))
        .unwrap_or_else(|| "quick-test".to_string());
    let job_dir = caption_dir(&project).join(&username);
    let frames_dir = job_dir.join("frames");
    tokio::fs::create_dir_all(&frames_dir).await?;

    // Phase 1: list video URLs via yt-dlp (no browser). The Playwright+stealth
    // popular-sort path is a deferred sidecar — see the TODO in media::ytdlp.
    emit(tx, "status", json!({"text": format!("Listing videos for @{username}...")}));
    let video_urls = media::ytdlp::list_profile_videos(&profile_url, max_videos, &sort).await?;
    let total = video_urls.len();
    emit(tx, "urls_collected", json!({"count": total, "urls": video_urls}));

    if total == 0 {
        emit(tx, "all_complete", json!({"results": [], "csv": null, "username": username}));
        return Ok(());
    }

    let mut rows: Vec<Row> = video_urls
        .iter()
        .enumerate()
        .map(|(i, url)| Row {
            index: i,
            video_url: url.clone(),
            video_id: video_id(url),
            frame_path: None,
            caption: None,
            mood: None,
            error: None,
        })
        .collect();

    // Phase 2: download thumbnails (concurrent, batches of 5).
    let mut start = 0;
    while start < total {
        let end = (start + DOWNLOAD_BATCH).min(total);
        let futs = rows[start..end].iter().map(|row| {
            let i = row.index;
            let url = row.video_url.clone();
            let vid = row.video_id.clone();
            let thumb_path = frames_dir.join(format!("{vid}.jpg"));
            let tx = tx.clone();
            async move {
                emit(&tx, "downloading", json!({"index": i, "total": total, "video_id": vid}));
                // Timeout each thumbnail to prevent stalling the batch.
                let fetched =
                    tokio::time::timeout(Duration::from_secs(45), fetch_thumbnail(&url, &thumb_path))
                        .await;
                let err = match fetched {
                    Err(_) => Some("Thumbnail download timed out".to_string()),
                    Ok(Err(e)) => Some(format!("{e:#}")),
                    Ok(Ok(())) => match tokio::fs::read(&thumb_path).await {
                        Ok(bytes) => {
                            let b64 = b64_encode(&bytes);
                            emit(
                                &tx,
                                "frame_ready",
                                json!({
                                    "index": i, "total": total, "video_id": vid,
                                    "b64": b64, "video_url": url,
                                }),
                            );
                            return (i, Ok(thumb_path));
                        }
                        Err(e) => Some(format!("failed to read thumbnail: {e}")),
                    },
                };
                let err = err.unwrap_or_default();
                tracing::warn!("thumbnail failed for {vid}: {err}");
                emit(
                    &tx,
                    "frame_error",
                    json!({"index": i, "total": total, "video_id": vid, "error": err}),
                );
                (i, Err(err))
            }
        });
        for (i, res) in futures::future::join_all(futs).await {
            match res {
                Ok(path) => rows[i].frame_path = Some(path),
                Err(e) => rows[i].error = Some(e),
            }
        }
        start = end;
    }

    // Phase 3: GPT vision caption extraction (small batches to dodge rate
    // limits). Tesseract is dead — vision OCR only.
    emit(tx, "ocr_starting", json!({"total": total}));
    let mut start = 0;
    while start < total {
        let end = (start + OCR_BATCH).min(total);
        let futs = rows[start..end].iter().map(|row| {
            let i = row.index;
            let vid = row.video_id.clone();
            let frame_path = row.frame_path.clone();
            let prior_error = row.error.clone();
            let tx = tx.clone();
            async move {
                let Some(frame_path) = frame_path.filter(|_| prior_error.is_none()) else {
                    // Frame never arrived — report an empty OCR result (same
                    // payload the old app sent: no mood key on the skip path).
                    emit(
                        &tx,
                        "ocr_done",
                        json!({
                            "index": i, "total": total, "video_id": vid,
                            "caption": "", "error": prior_error,
                        }),
                    );
                    return (i, None);
                };
                emit(&tx, "ocr_started", json!({"index": i, "total": total, "video_id": vid}));

                let mut error: Option<String> = None;
                let caption = match tokio::time::timeout(
                    Duration::from_secs(30),
                    extract_caption(&frame_path),
                )
                .await
                {
                    Ok(Ok(c)) => c,
                    Ok(Err(e)) => {
                        tracing::error!("OCR failed for {vid}: {e:#}");
                        error = Some(format!("{e:#}"));
                        String::new()
                    }
                    Err(_) => {
                        tracing::warn!("OCR timed out for {vid}");
                        error = Some("Caption extraction timed out".to_string());
                        String::new()
                    }
                };

                // Mood only when a caption was extracted; any failure → "chill".
                let mood = if !caption.trim().is_empty() {
                    match tokio::time::timeout(Duration::from_secs(10), analyze_mood(&caption)).await
                    {
                        Ok(m) => Some(m),
                        Err(_) => Some("chill".to_string()),
                    }
                } else {
                    None
                };

                emit(
                    &tx,
                    "ocr_done",
                    json!({
                        "index": i, "total": total, "video_id": vid,
                        "caption": caption, "mood": mood, "error": error,
                    }),
                );
                (i, Some((caption, mood, error)))
            }
        });
        for (i, res) in futures::future::join_all(futs).await {
            if let Some((caption, mood, error)) = res {
                rows[i].caption = Some(caption);
                rows[i].mood = mood;
                if error.is_some() {
                    rows[i].error = error;
                }
            }
        }
        start = end;
    }

    // Write the CSV (same columns + \r\n line endings as Python's csv module).
    let csv_path = job_dir.join("captions.csv");
    let mut csv = String::from("video_id,video_url,caption,mood,error\r\n");
    for r in &rows {
        csv.push_str(&csv_row(&[
            &r.video_id,
            &r.video_url,
            r.caption.as_deref().unwrap_or(""),
            r.mood.as_deref().unwrap_or(""),
            r.error.as_deref().unwrap_or(""),
        ]));
    }
    tokio::fs::write(&csv_path, csv).await?;

    // Persist to the DB caption bank (rewrite-only addition; CSV stays the
    // parity contract, so a DB hiccup must not fail the scrape).
    let bank: Vec<(String, Option<String>)> = rows
        .iter()
        .filter_map(|r| {
            let text = r.caption.as_deref().unwrap_or("").trim();
            (!text.is_empty()).then(|| (text.to_string(), r.mood.clone()))
        })
        .collect();
    let persisted: Result<(), sqlx::Error> = async {
        clab_core::repo::create_project(&db, &project).await?;
        clab_core::repo::captions::replace_source_captions(&db, &project, &username, &bank).await?;
        Ok(())
    }
    .await;
    if let Err(e) = persisted {
        tracing::warn!("failed to persist caption bank for {project}/{username}: {e}");
    }

    let results: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "index": r.index,
                "video_id": r.video_id,
                "video_url": r.video_url,
                "caption": r.caption.clone().unwrap_or_default(),
                "mood": r.mood,
                "error": r.error,
            })
        })
        .collect();
    emit(
        tx,
        "all_complete",
        json!({"results": results, "csv": csv_path.to_string_lossy(), "username": username}),
    );
    Ok(())
}

/// Fetch a video's cover image: yt-dlp's thumbnail writer first, then the old
/// urllib-style fallback (ask yt-dlp for the cover URL, fetch it over HTTP).
async fn fetch_thumbnail(video_url: &str, dest: &FsPath) -> anyhow::Result<()> {
    let primary = media::ytdlp::get_thumbnail(video_url, dest).await;
    if primary.is_ok() {
        return Ok(());
    }
    let thumb_url = match media::ytdlp::thumbnail_url(video_url).await {
        Ok(u) if !u.is_empty() => u,
        _ => return primary,
    };
    let resp = integrations::http::client()
        .get(&thumb_url)
        .timeout(Duration::from_secs(15))
        .send()
        .await
        .context("Thumbnail download failed")?;
    if !resp.status().is_success() {
        anyhow::bail!("Thumbnail download failed: HTTP {}", resp.status());
    }
    let bytes = resp.bytes().await.context("Thumbnail download failed")?;
    tokio::fs::write(dest, &bytes).await.context("Thumbnail download failed")?;
    Ok(())
}

// ── GPT vision OCR + mood (ported from scraper/{caption_extractor,sentiment_analyzer}.py) ──

const OCR_SYSTEM_PROMPT: &str = "You are an OCR assistant. Extract ONLY the burned-in caption text \
visible on this TikTok video screenshot. The captions are typically \
large white or colored text overlaid directly on the video content, \
often with a black outline, shadow, or highlight background. \
They are the main hook/story text that creators add to their videos.\n\n\
IGNORE all TikTok UI elements: username, likes count, comments count, \
share button, description text at the bottom, sound name, hashtags, \
and any watermarks.\n\n\
Return ONLY the caption text exactly as it appears, preserving line \
breaks. Output the raw text only — no labels, headers, quotes, or preamble \
like 'Text on image:'. If no burned-in caption is visible, return exactly: NO_CAPTION";

const MOOD_TAGS: [&str; 5] = ["sad", "hype", "love", "funny", "chill"];

const MOOD_SYSTEM_PROMPT: &str = "You are a mood/sentiment classifier for short-form video captions (TikTok style). \
The purpose is to match caption tone to song/music mood so they don't contradict each other.\n\n\
For each caption, pick exactly ONE mood tag from this list:\n\
sad, hype, love, funny, chill\n\n\
Definitions:\n\
- sad: heartbreak, loss, missing someone, pain, longing, emotional vulnerability, breakup energy\n\
- hype: motivational, confident, flexing, aggressive, energetic, empowering, pump-up\n\
- love: romantic, devotion, partner appreciation, loyalty, affection, deep connection\n\
- funny: humor, sarcasm, jokes, absurd observations, lighthearted, playful\n\
- chill: laid-back, unbothered, reflective, wholesome, relatable, peaceful, grateful\n\n\
Respond with ONLY valid JSON. No markdown fences.";

/// OCR vision model — gpt-5.4-nano is ~10x cheaper than gpt-4.1 with
/// equivalent OCR quality on TikTok covers; override with OCR_MODEL.
fn ocr_model() -> String {
    std::env::var("OCR_MODEL").unwrap_or_else(|_| "gpt-5.4-nano".to_string())
}

/// Send a cover image to the vision model and extract the burned-in caption.
/// Returns "" when no caption is visible. Retries with exponential backoff on
/// rate-limit (429) errors, like the Python client did.
async fn extract_caption(frame_path: &FsPath) -> anyhow::Result<String> {
    let bytes = tokio::fs::read(frame_path).await.context("read frame")?;
    let b64 = b64_encode(&bytes);
    let messages = vec![
        json!({"role": "system", "content": OCR_SYSTEM_PROMPT}),
        json!({
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": format!("data:image/jpeg;base64,{b64}")}},
                {"type": "text",
                 "text": "Extract the burned-in caption text from this TikTok video screenshot."}
            ]
        }),
    ];

    const MAX_RETRIES: u32 = 4;
    let mut attempt: u32 = 0;
    loop {
        match integrations::openai::chat(&ocr_model(), messages.clone()).await {
            Ok(text) => {
                let t = text.trim();
                return Ok(if t == "NO_CAPTION" { String::new() } else { t.to_string() });
            }
            Err(e) if attempt < MAX_RETRIES && is_rate_limited(&e) => {
                let wait = (2u64.saturating_pow(attempt) * 2).min(30);
                tracing::warn!(
                    "OCR rate limited, retrying in {wait}s (attempt {}/{MAX_RETRIES})",
                    attempt + 1
                );
                tokio::time::sleep(Duration::from_secs(wait)).await;
                attempt += 1;
            }
            Err(e) => return Err(e),
        }
    }
}

fn is_rate_limited(e: &anyhow::Error) -> bool {
    e.to_string().contains("429")
}

/// Classify a caption into one of the 5 music-aligned mood tags. Any failure
/// (API error, bad JSON, unknown tag) falls back to "chill", matching Python.
async fn analyze_mood(caption: &str) -> String {
    if caption.trim().is_empty() {
        return "chill".to_string();
    }
    let messages = vec![
        json!({"role": "system", "content": MOOD_SYSTEM_PROMPT}),
        json!({
            "role": "user",
            "content": format!(
                "Classify this caption:\n\"{}\"\n\nReturn JSON: {{\"mood\": \"<tag>\"}}",
                caption.trim()
            )
        }),
    ];
    match integrations::openai::chat("gpt-4.1-mini", messages).await {
        Ok(text) => parse_mood_response(&text),
        Err(e) => {
            tracing::warn!("mood analysis failed: {e:#}");
            "chill".to_string()
        }
    }
}

/// Parse the mood model's reply: strip markdown fences, decode JSON, validate
/// the tag. Anything off-spec → "chill".
fn parse_mood_response(text: &str) -> String {
    let mut t = text.trim();
    if t.starts_with("```") {
        t = t.split_once('\n').map(|(_, rest)| rest).unwrap_or(t);
        t = t.rsplit_once("```").map(|(body, _)| body).unwrap_or(t);
        t = t.trim();
    }
    serde_json::from_str::<Value>(t)
        .ok()
        .and_then(|v| v["mood"].as_str().map(|m| m.trim().to_lowercase()))
        .filter(|m| MOOD_TAGS.contains(&m.as_str()))
        .unwrap_or_else(|| "chill".to_string())
}

// ── Export endpoint ──────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct ExportQuery {
    project: Option<String>,
}

/// GET /api/captions/export/{username} — download the batch's captions CSV.
async fn export_captions(
    Path(username): Path<String>,
    Query(q): Query<ExportQuery>,
) -> Result<Response, ApiError> {
    let username = safe_batch_name(&username)
        .ok_or_else(|| ApiError::BadRequest("invalid username".into()))?;
    let project = project_slug(q.project.as_deref())?;
    let csv_path = caption_dir(&project).join(username).join("captions.csv");
    let bytes = tokio::fs::read(&csv_path).await.map_err(|_| ApiError::NotFound)?;

    // ASCII-safe attachment filename (renamed batches may contain any chars).
    let fname: String = format!("{username}_captions.csv")
        .chars()
        .filter(|c| c.is_ascii_graphic() || *c == ' ')
        .filter(|c| *c != '"')
        .collect();
    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "text/csv; charset=utf-8".to_string()),
            (header::CONTENT_DISPOSITION, format!("attachment; filename=\"{fname}\"")),
        ],
        bytes,
    )
        .into_response())
}

// ── History endpoint ─────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct HistoryQuery {
    #[serde(default = "default_project")]
    project: String,
}

fn default_project() -> String {
    "quick-test".to_string()
}

/// GET /api/captions/history — every scraped batch for a project, with caption
/// previews, newest first. Reads the CSVs on disk (the parity contract).
async fn caption_history(Query(q): Query<HistoryQuery>) -> Result<Json<Value>, ApiError> {
    let project = project_slug(Some(&q.project))?;
    let dir = caption_dir(&project);
    if !dir.exists() {
        return Ok(Json(json!({"batches": []})));
    }

    // Collect username dirs, sorted by name (matches Python's sorted iterdir).
    let mut user_dirs: Vec<PathBuf> = Vec::new();
    let mut rd = tokio::fs::read_dir(&dir).await.map_err(anyhow::Error::from)?;
    while let Ok(Some(entry)) = rd.next_entry().await {
        if entry.path().is_dir() {
            user_dirs.push(entry.path());
        }
    }
    user_dirs.sort();

    let mut batches: Vec<Value> = Vec::new();
    for user_dir in user_dirs {
        let csv_path = user_dir.join("captions.csv");
        let Ok(content) = tokio::fs::read_to_string(&csv_path).await else {
            continue; // no CSV → not a batch
        };
        let username = user_dir
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();

        let mut captions: Vec<Value> = Vec::new();
        let records = parse_csv(&content);
        if let Some((headers, rows)) = records.split_first() {
            let col = |name: &str| headers.iter().position(|h| h == name);
            let (c_cap, c_mood, c_vid) = (col("caption"), col("mood"), col("video_id"));
            for row in rows {
                let cell = |idx: Option<usize>| {
                    idx.and_then(|i| row.get(i)).cloned().unwrap_or_default()
                };
                captions.push(json!({
                    "text": cell(c_cap),
                    // Old CSVs without a mood column → null (Python's
                    // row.get("mood", None)); present-but-empty → "".
                    "mood": c_mood.map(|i| row.get(i).cloned().unwrap_or_default()),
                    "video_id": cell(c_vid),
                }));
            }
        }

        let modified_at = tokio::fs::metadata(&csv_path)
            .await
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let sample: Vec<Value> = captions.iter().take(3).map(|c| c["text"].clone()).collect();
        batches.push(json!({
            "username": username,
            "caption_count": captions.len(),
            "modified_at": modified_at,
            "captions": captions,
            "sample": sample,
        }));
    }

    // Most recent first.
    batches.sort_by(|a, b| {
        let (ma, mb) = (
            a["modified_at"].as_f64().unwrap_or(0.0),
            b["modified_at"].as_f64().unwrap_or(0.0),
        );
        mb.partial_cmp(&ma).unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(Json(json!({"batches": batches})))
}

// ── Rename endpoint ──────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct RenameBody {
    project: Option<String>,
    #[serde(default)]
    old_name: String,
    #[serde(default)]
    new_name: String,
}

/// POST /api/captions/rename-batch — rename a caption batch (username folder),
/// keeping the DB caption bank in step.
async fn rename_batch(
    State(st): State<AppState>,
    Json(body): Json<RenameBody>,
) -> Result<Response, ApiError> {
    let project = project_slug(body.project.as_deref())?;
    let old_name = body.old_name;
    let new_name = sanitize_new_name(&body.new_name);

    if old_name.is_empty() || body.new_name.trim().is_empty() {
        return Err(ApiError::BadRequest(
            "Both old_name and new_name are required".into(),
        ));
    }
    if new_name.is_empty() {
        return Err(ApiError::BadRequest("Invalid new name".into()));
    }
    // The Python app passed old_name to the filesystem raw; we refuse
    // traversal-shaped names instead.
    let old_name = safe_batch_name(&old_name)
        .ok_or_else(|| ApiError::BadRequest("Invalid old name".into()))?
        .to_string();

    let dir = caption_dir(&project);
    let old_path = dir.join(&old_name);
    let new_path = dir.join(&new_name);

    if !old_path.exists() {
        return Ok((
            StatusCode::NOT_FOUND,
            Json(json!({"error": format!("Batch '{old_name}' not found")})),
        )
            .into_response());
    }
    if new_path.exists() {
        return Ok((
            StatusCode::CONFLICT,
            Json(json!({"error": format!("Batch '{new_name}' already exists")})),
        )
            .into_response());
    }

    tokio::fs::rename(&old_path, &new_path)
        .await
        .context("rename batch folder")
        .map_err(ApiError::Other)?;

    // Keep the DB rows pointing at the new source name (best-effort).
    if let Err(e) =
        clab_core::repo::captions::rename_source(&st.db, &project, &old_name, &new_name).await
    {
        tracing::warn!("caption bank rename failed for {project}: {e}");
    }

    Ok(Json(json!({"renamed": true, "old_name": old_name, "new_name": new_name})).into_response())
}

// ── Pure helpers (ported string handling) ────────────────────────────────────

/// Normalize a profile input to a full TikTok URL (accepts `@user`, `user`, or
/// a full URL).
fn normalize_profile_url(raw: &str) -> String {
    let pu = raw.trim();
    if pu.starts_with('@') {
        format!("https://www.tiktok.com/{pu}")
    } else if !pu.starts_with("http") {
        format!("https://www.tiktok.com/@{pu}")
    } else {
        pu.to_string()
    }
}

/// Extract the `@username` from a TikTok profile URL (Python's `@([\w.]+)`).
/// Dot-only names are rejected — they'd be path components like `..`.
fn extract_username(url: &str) -> Option<String> {
    let at = url.find('@')?;
    let name: String = url[at + 1..]
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '.')
        .collect();
    if name.is_empty() || name.chars().all(|c| c == '.') {
        return None;
    }
    Some(name)
}

/// The numeric TikTok video id from a `/video/<digits>` URL, else "unknown".
fn video_id(url: &str) -> String {
    if let Some(pos) = url.find("/video/") {
        let digits: String = url[pos + 7..]
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .collect();
        if !digits.is_empty() {
            return digits;
        }
    }
    "unknown".to_string()
}

/// Python's rename sanitizer, verbatim: strip separators and `..`, then trim.
fn sanitize_new_name(name: &str) -> String {
    name.replace(['/', '\\'], "").replace("..", "").trim().to_string()
}

/// One CSV record with minimal quoting + `\r\n` terminator (matches Python's
/// csv.writer defaults, so old and new files are byte-compatible).
fn csv_row(fields: &[&str]) -> String {
    let mut out = String::new();
    for (i, f) in fields.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        if f.contains(',') || f.contains('"') || f.contains('\n') || f.contains('\r') {
            out.push('"');
            out.push_str(&f.replace('"', "\"\""));
            out.push('"');
        } else {
            out.push_str(f);
        }
    }
    out.push_str("\r\n");
    out
}

/// Minimal RFC-4180 CSV parser (quoted fields may contain commas, doubled
/// quotes, and newlines). Blank lines are skipped. Returns records of fields;
/// the first record is the header row.
fn parse_csv(input: &str) -> Vec<Vec<String>> {
    let mut records: Vec<Vec<String>> = Vec::new();
    let mut record: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut chars = input.chars().peekable();

    let end_record =
        |records: &mut Vec<Vec<String>>, record: &mut Vec<String>, field: &mut String| {
            // A lone empty field from a blank line is not a record.
            if record.is_empty() && field.is_empty() {
                return;
            }
            record.push(std::mem::take(field));
            records.push(std::mem::take(record));
        };

    while let Some(c) = chars.next() {
        if in_quotes {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    chars.next();
                    field.push('"');
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(c);
            }
        } else {
            match c {
                '"' => in_quotes = true,
                ',' => record.push(std::mem::take(&mut field)),
                '\r' => {
                    if chars.peek() == Some(&'\n') {
                        chars.next();
                    }
                    end_record(&mut records, &mut record, &mut field);
                }
                '\n' => end_record(&mut records, &mut record, &mut field),
                _ => field.push(c),
            }
        }
    }
    end_record(&mut records, &mut record, &mut field);
    records
}

/// Minimal standard-base64 ENcoder (frame previews over WS + vision data URLs;
/// avoids the base64 crate, mirroring jobs.rs's hand-rolled decoder).
fn b64_encode(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | (b[2] as u32);
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { TABLE[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[n as usize & 63] as char } else { '=' });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_profile_url_variants() {
        assert_eq!(
            normalize_profile_url("@drake"),
            "https://www.tiktok.com/@drake"
        );
        assert_eq!(
            normalize_profile_url("drake"),
            "https://www.tiktok.com/@drake"
        );
        assert_eq!(
            normalize_profile_url("  @drake  "),
            "https://www.tiktok.com/@drake"
        );
        assert_eq!(
            normalize_profile_url("https://www.tiktok.com/@drake"),
            "https://www.tiktok.com/@drake"
        );
    }

    #[test]
    fn extract_username_variants() {
        assert_eq!(
            extract_username("https://www.tiktok.com/@some.user_1").as_deref(),
            Some("some.user_1")
        );
        assert_eq!(
            extract_username("https://www.tiktok.com/@user?lang=en").as_deref(),
            Some("user")
        );
        assert_eq!(extract_username("https://www.tiktok.com/"), None);
        // dot-only "names" would be `.`/`..` path components — rejected
        assert_eq!(extract_username("https://www.tiktok.com/@.."), None);
    }

    #[test]
    fn video_id_extraction() {
        assert_eq!(video_id("https://www.tiktok.com/@u/video/7301234567890123456"), "7301234567890123456");
        assert_eq!(video_id("https://www.tiktok.com/@u/video/123?is_copy=1"), "123");
        assert_eq!(video_id("https://www.tiktok.com/@u"), "unknown");
        assert_eq!(video_id("https://t/video/abc"), "unknown");
    }

    #[test]
    fn sanitize_new_name_matches_python() {
        assert_eq!(sanitize_new_name("  ok name "), "ok name");
        assert_eq!(sanitize_new_name("a/b\\c"), "abc");
        assert_eq!(sanitize_new_name("../../etc"), "etc");
        assert_eq!(sanitize_new_name("  /  "), "");
    }

    #[test]
    fn safe_batch_name_blocks_traversal() {
        assert!(safe_batch_name("user1").is_some());
        assert!(safe_batch_name("renamed batch").is_some());
        assert!(safe_batch_name("a..b").is_some()); // no separator → single component
        assert!(safe_batch_name("..").is_none());
        assert!(safe_batch_name(".").is_none());
        assert!(safe_batch_name("a/b").is_none());
        assert!(safe_batch_name("a\\b").is_none());
        assert!(safe_batch_name("").is_none());
    }

    #[test]
    fn csv_roundtrip_with_quotes_commas_newlines() {
        let mut file = String::from("video_id,video_url,caption,mood,error\r\n");
        file.push_str(&csv_row(&["123", "https://t/video/123", "hello, \"world\"\nline2", "hype", ""]));
        file.push_str(&csv_row(&["456", "https://t/video/456", "plain", "", "boom"]));

        let records = parse_csv(&file);
        assert_eq!(records.len(), 3);
        assert_eq!(records[0], vec!["video_id", "video_url", "caption", "mood", "error"]);
        assert_eq!(records[1][2], "hello, \"world\"\nline2");
        assert_eq!(records[1][3], "hype");
        assert_eq!(records[2][2], "plain");
        assert_eq!(records[2][4], "boom");
    }

    #[test]
    fn parse_csv_skips_blank_lines_and_handles_lf_only() {
        let records = parse_csv("a,b\n\n1,2\n");
        assert_eq!(records, vec![vec!["a", "b"], vec!["1", "2"]]);
        // no trailing newline
        let records = parse_csv("a,b\n1,2");
        assert_eq!(records.len(), 2);
    }

    #[test]
    fn b64_encode_known_vectors() {
        assert_eq!(b64_encode(b""), "");
        assert_eq!(b64_encode(b"f"), "Zg==");
        assert_eq!(b64_encode(b"fo"), "Zm8=");
        assert_eq!(b64_encode(b"foo"), "Zm9v");
        assert_eq!(b64_encode(b"hello!"), "aGVsbG8h");
    }

    #[test]
    fn event_msg_merges_event_key() {
        let msg = event_msg("status", json!({"text": "hi"}));
        let v: Value = serde_json::from_str(&msg).unwrap();
        assert_eq!(v["event"], "status");
        assert_eq!(v["text"], "hi");
    }

    #[test]
    fn parse_mood_response_variants() {
        assert_eq!(parse_mood_response(r#"{"mood": "hype"}"#), "hype");
        assert_eq!(parse_mood_response("```json\n{\"mood\": \"sad\"}\n```"), "sad");
        assert_eq!(parse_mood_response(r#"{"mood": "HYPE "}"#), "hype");
        // unknown tag / garbage → chill
        assert_eq!(parse_mood_response(r#"{"mood": "angsty"}"#), "chill");
        assert_eq!(parse_mood_response("not json"), "chill");
        assert_eq!(parse_mood_response(r#"{"vibe": "sad"}"#), "chill");
    }
}
