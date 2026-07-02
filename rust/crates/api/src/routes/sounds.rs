//! Sound library, per-page playlists, and sound-assignment sends.
//!
//! Parity port of the sounds sections of `routers/telegram.py`:
//!   - `/api/telegram/sounds*`            — library CRUD + the unified sync
//!     (Campaign Hub says what's active, Notion CRM owns the TikTok Sound
//!     Links, GPT-4.1-mini bridges naming differences),
//!   - `/api/telegram/pages/{id}/playlist*`, `/api/telegram/playlists`
//!     — per-page ordered playlists (Campaign Hub calls these),
//!   - `/api/telegram/sound-assignments/*` — the personalized per-poster
//!     daily message, grouped by page, sent to each poster's "Campaign
//!     Sounds" topic (auto-created on first send).
//!
//! Response shapes match the Python app field-for-field — the React frontend
//! and Campaign Hub consume these unchanged.

use std::collections::HashMap;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use clab_core::repo;
use clab_core::repo::sounds::Sound;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::state::AppState;
use crate::telegram::Telegram;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/telegram/sounds", get(list_sounds).post(create_sound))
        .route("/api/telegram/sounds/sync", post(sync_sounds))
        .route("/api/telegram/sounds/forward-all", post(forward_sounds_all))
        .route("/api/telegram/sounds/forward/{poster_id}", post(forward_sounds_to_poster))
        .route("/api/telegram/sounds/{sound_id}", put(update_sound).delete(delete_sound))
        .route(
            "/api/telegram/pages/{integration_id}/playlist",
            get(get_playlist).put(replace_playlist).delete(clear_playlist),
        )
        .route("/api/telegram/pages/{integration_id}/playlist/songs", post(add_song))
        .route(
            "/api/telegram/pages/{integration_id}/playlist/songs/{sound_id}",
            axum::routing::delete(remove_song),
        )
        .route("/api/telegram/playlists", get(list_playlists))
        .route("/api/telegram/sound-assignments/send-all", post(send_assignments_all))
        .route("/api/telegram/sound-assignments/send/{poster_id}", post(send_assignments))
}

// ── Error type ───────────────────────────────────────────────────────────────
// The old FastAPI app used the full HTTPException status range here (400 hub
// unconfigured, 502 upstream failures, 503 bot down) and clients key off those
// codes, so this module carries its own status-preserving error. The body
// includes both `detail` (FastAPI's key, what existing clients read) and
// `error` (this app's ApiError convention).

#[derive(Debug)]
pub(super) struct HttpError {
    pub status: StatusCode,
    pub message: String,
}

impl HttpError {
    pub fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self { status, message: message.into() }
    }
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, msg)
    }
    pub fn not_found(msg: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, msg)
    }
    pub fn bad_gateway(msg: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_GATEWAY, msg)
    }
    pub fn unavailable(msg: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, msg)
    }
}

impl IntoResponse for HttpError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({ "detail": self.message, "error": self.message })),
        )
            .into_response()
    }
}

impl From<sqlx::Error> for HttpError {
    fn from(e: sqlx::Error) -> Self {
        if matches!(e, sqlx::Error::RowNotFound) {
            return Self::not_found("not found");
        }
        tracing::error!("db error: {e}");
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "internal error")
    }
}

type ApiResult = Result<Json<Value>, HttpError>;

// ── Sound library CRUD ───────────────────────────────────────────────────────

fn default_true() -> bool {
    true
}

#[derive(Deserialize)]
struct SoundsQuery {
    #[serde(default = "default_true")]
    active_only: bool,
}

/// GET /api/telegram/sounds — list sounds (raw array, like the old app).
async fn list_sounds(State(st): State<AppState>, Query(q): Query<SoundsQuery>) -> ApiResult {
    let sounds = repo::sounds::list_sounds(&st.db, q.active_only).await?;
    Ok(Json(serde_json::to_value(sounds).unwrap_or_else(|_| json!([]))))
}

#[derive(Deserialize)]
struct SoundCreate {
    url: String,
    label: String,
}

/// POST /api/telegram/sounds — add a sound; returns the created entry.
async fn create_sound(State(st): State<AppState>, Json(b): Json<SoundCreate>) -> ApiResult {
    let sound = repo::sounds::add_sound(&st.db, &b.url, &b.label).await?;
    Ok(Json(serde_json::to_value(sound).unwrap_or_default()))
}

/// DELETE /api/telegram/sounds/{sound_id}
async fn delete_sound(State(st): State<AppState>, Path(sound_id): Path<String>) -> ApiResult {
    if !repo::sounds::remove_sound(&st.db, &sound_id).await? {
        return Err(HttpError::not_found("Sound not found"));
    }
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct SoundUpdate {
    url: Option<String>,
    label: Option<String>,
    active: Option<bool>,
}

/// PUT /api/telegram/sounds/{sound_id} — update active status, url, or label.
async fn update_sound(
    State(st): State<AppState>,
    Path(sound_id): Path<String>,
    Json(b): Json<SoundUpdate>,
) -> ApiResult {
    if let Some(active) = b.active {
        if !repo::sounds::toggle_sound(&st.db, &sound_id, active).await? {
            return Err(HttpError::not_found("Sound not found"));
        }
    }
    if b.url.is_some() || b.label.is_some() {
        if !repo::sounds::update_sound(&st.db, &sound_id, b.url.as_deref(), b.label.as_deref())
            .await?
        {
            return Err(HttpError::not_found("Sound not found"));
        }
    }
    Ok(Json(json!({ "ok": true, "sound_id": sound_id })))
}

// ── Unified sound sync (Campaign Hub + Notion + AI matching) ────────────────

/// POST /api/telegram/sounds/sync
async fn sync_sounds(State(st): State<AppState>) -> ApiResult {
    if !integrations::hub::configured() {
        return Err(HttpError::bad_request("Campaign Hub URL not configured."));
    }

    // Notion fetch is best-effort here; sync_sound_status refetches (and
    // fails) if it's missing — same control flow as the old router.
    let mut notion_data = None;
    if integrations::notion::campaigns_configured() {
        match integrations::notion::fetch_campaigns_with_sounds().await {
            Ok(data) => notion_data = Some(data),
            Err(e) => {
                tracing::warn!("Notion fetch failed (continuing with Hub only): {e:#}");
            }
        }
    }

    match sync::sync_sound_status(&st.db, notion_data).await {
        Ok(result) => Ok(Json(result)),
        Err(e) => {
            tracing::error!("Sound sync failed: {e:#}");
            Err(HttpError::bad_gateway(format!("Sound sync failed: {e}")))
        }
    }
}

// ── Forward the active-sound list to poster groups ───────────────────────────

fn require_bot(st: &AppState) -> Result<&Telegram, HttpError> {
    st.telegram
        .as_ref()
        .ok_or_else(|| HttpError::unavailable("Telegram bot is not running"))
}

/// Get or create a poster's "Campaign Sounds" topic; persists a fresh topic id.
/// Returns None when the poster has no group or creation fails.
async fn ensure_sounds_topic(
    st: &AppState,
    tg: &Telegram,
    poster: &clab_core::Poster,
) -> Option<i64> {
    if let Some(tid) = poster.sounds_topic_id {
        return Some(tid);
    }
    if poster.chat_id == 0 {
        return None;
    }
    match tg.create_topic(poster.chat_id, "Campaign Sounds").await {
        Ok(tid) => {
            if let Err(e) = repo::sounds::set_poster_sounds_topic(&st.db, &poster.id, tid).await {
                tracing::warn!("failed to persist sounds topic for {}: {e}", poster.id);
            }
            Some(tid)
        }
        Err(e) => {
            tracing::warn!("Failed to create Campaign Sounds topic for {}: {e:#}", poster.id);
            None
        }
    }
}

/// The old `html.escape(s, quote=True)`.
fn escape_html(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

/// The plain-text "Active Sounds" digest (forward endpoints).
fn active_sounds_text(sounds: &[Sound]) -> String {
    let today = chrono::Local::now().format("%B %d, %Y");
    let mut parts = vec![format!("\u{1f3b5} Active Sounds \u{2014} {today}"), String::new()];
    for s in sounds {
        parts.push(format!("\u{2022} {}", s.label));
        parts.push(format!("  {}", s.url));
        parts.push(String::new());
    }
    parts.join("\n")
}

/// POST /api/telegram/sounds/forward/{poster_id}
async fn forward_sounds_to_poster(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
) -> ApiResult {
    let tg = require_bot(&st)?;

    let poster = repo::get_poster(&st.db, &poster_id)
        .await?
        .ok_or_else(|| HttpError::not_found("Poster not found"))?;
    if poster.chat_id == 0 {
        return Err(HttpError::bad_request("Poster has no chat_id"));
    }

    let sounds_topic_id = ensure_sounds_topic(&st, tg, &poster)
        .await
        .ok_or_else(|| HttpError::bad_gateway("Failed to create Campaign Sounds topic"))?;

    let sounds = repo::sounds::list_sounds(&st.db, true).await?;
    if sounds.is_empty() {
        return Ok(Json(json!({ "ok": true, "sent": 0, "message": "No active sounds to send" })));
    }

    let text = escape_html(&active_sounds_text(&sounds));
    tg.send_html(poster.chat_id, Some(sounds_topic_id), &text)
        .await
        .map_err(|e| HttpError::bad_gateway(format!("Failed to send sounds: {e}")))?;

    Ok(Json(json!({ "ok": true, "sent": sounds.len(), "poster_id": poster_id })))
}

/// POST /api/telegram/sounds/forward-all
async fn forward_sounds_all(State(st): State<AppState>) -> ApiResult {
    let tg = require_bot(&st)?;

    let sounds = repo::sounds::list_sounds(&st.db, true).await?;
    if sounds.is_empty() {
        return Ok(Json(json!({ "ok": true, "sent_to": 0, "sound_count": 0, "errors": [] })));
    }

    let text = escape_html(&active_sounds_text(&sounds));
    let posters = repo::list_posters(&st.db).await?;
    let mut sent_to = 0u32;
    let mut topics_created = 0u32;
    let mut errors: Vec<String> = Vec::new();

    for poster in &posters {
        if poster.chat_id == 0 {
            continue;
        }
        let had_topic = poster.sounds_topic_id.is_some();
        let Some(topic_id) = ensure_sounds_topic(&st, tg, poster).await else {
            errors.push(format!("{}: failed to create topic", poster.name));
            continue;
        };
        if !had_topic {
            topics_created += 1;
        }

        match tg.send_html(poster.chat_id, Some(topic_id), &text).await {
            Ok(_) => sent_to += 1,
            Err(e) => errors.push(format!("{}: {e}", poster.name)),
        }

        // Small delay between posters to avoid rate limits.
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }

    Ok(Json(json!({
        "ok": true,
        "sent_to": sent_to,
        "topics_created": topics_created,
        "sound_count": sounds.len(),
        "total_posters": posters.len(),
        "errors": errors,
    })))
}

// ── Page playlists ───────────────────────────────────────────────────────────

/// The `{integration_id, sound_ids, sounds}` body every playlist route returns.
async fn playlist_body(st: &AppState, integration_id: &str) -> Result<Value, HttpError> {
    let sound_ids = repo::sounds::get_page_playlist(&st.db, integration_id).await?;
    let sounds = repo::sounds::resolve_playlist_sounds(&st.db, integration_id, false).await?;
    Ok(json!({
        "integration_id": integration_id,
        "sound_ids": sound_ids,
        "sounds": sounds,
    }))
}

/// GET /api/telegram/pages/{integration_id}/playlist — includes inactive
/// sounds so the UI can surface them struck-through.
async fn get_playlist(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
) -> ApiResult {
    Ok(Json(playlist_body(&st, &integration_id).await?))
}

#[derive(Deserialize)]
struct PlaylistReplace {
    sound_ids: Vec<String>,
}

/// PUT — replace wholesale. Order preserved, dedups, drops invalid ids.
async fn replace_playlist(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
    Json(b): Json<PlaylistReplace>,
) -> ApiResult {
    repo::sounds::set_page_playlist(&st.db, &integration_id, &b.sound_ids).await?;
    Ok(Json(playlist_body(&st, &integration_id).await?))
}

#[derive(Deserialize)]
struct SongAdd {
    sound_id: String,
}

/// POST .../playlist/songs — append (idempotent); 400 for unknown sounds.
async fn add_song(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
    Json(b): Json<SongAdd>,
) -> ApiResult {
    if repo::sounds::add_song_to_page(&st.db, &integration_id, &b.sound_id)
        .await?
        .is_none()
    {
        return Err(HttpError::bad_request(format!("Sound {} not in pool", b.sound_id)));
    }
    Ok(Json(playlist_body(&st, &integration_id).await?))
}

/// DELETE .../playlist/songs/{sound_id}
async fn remove_song(
    State(st): State<AppState>,
    Path((integration_id, sound_id)): Path<(String, String)>,
) -> ApiResult {
    repo::sounds::remove_song_from_page(&st.db, &integration_id, &sound_id).await?;
    Ok(Json(playlist_body(&st, &integration_id).await?))
}

/// DELETE .../playlist — drop the page's playlist entirely.
async fn clear_playlist(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
) -> ApiResult {
    let existed = repo::sounds::clear_page_playlist(&st.db, &integration_id).await?;
    Ok(Json(json!({ "ok": true, "existed": existed })))
}

/// GET /api/telegram/playlists — the full map, for the UI to load at once.
async fn list_playlists(State(st): State<AppState>) -> ApiResult {
    let map = repo::sounds::get_all_page_playlists(&st.db).await?;
    Ok(Json(serde_json::to_value(map).unwrap_or_else(|_| json!({}))))
}

// ── Sound assignments (per-page playlists → per-poster personalized sends) ──

/// `{integration_id: display_name}` from the roster (old `_page_name_lookup`).
async fn page_name_lookup(st: &AppState) -> Result<HashMap<String, String>, sqlx::Error> {
    let pages = repo::roster::list_pages_full(&st.db).await?;
    Ok(pages
        .into_iter()
        .map(|p| {
            let name = display_name(&p);
            (p.id, name)
        })
        .collect())
}

/// name → display_name → identifier → integration_id (old `_page_display_name`).
fn display_name(p: &repo::roster::RosterPage) -> String {
    if !p.name.is_empty() {
        return p.name.clone();
    }
    let extra: Value = p
        .extra
        .as_deref()
        .and_then(|raw| serde_json::from_str(raw).ok())
        .unwrap_or(Value::Null);
    for key in ["display_name", "identifier"] {
        if let Some(v) = extra[key].as_str().filter(|s| !s.is_empty()) {
            return v.to_string();
        }
    }
    p.id.clone()
}

struct PosterMessage {
    text: String,
    page_count: usize,
    song_count: usize,
    sections: Vec<Value>,
    skipped_pages: Vec<Value>,
}

impl PosterMessage {
    fn to_value(&self, poster: &clab_core::Poster) -> Value {
        json!({
            "poster_id": poster.id,
            "poster_name": poster.name,
            "text": self.text,
            "page_count": self.page_count,
            "song_count": self.song_count,
            "sections": self.sections,
            "skipped_pages": self.skipped_pages,
        })
    }
}

/// Build the personalized daily message for a poster: walk their pages,
/// resolve each playlist (active sounds only), group by page. Telegram HTML
/// so song names are clickable links.
async fn build_poster_message(
    st: &AppState,
    poster: &clab_core::Poster,
    page_names: &HashMap<String, String>,
) -> Result<PosterMessage, sqlx::Error> {
    let page_ids = repo::sounds::pages_for_poster(&st.db, &poster.id).await?;

    let mut sections: Vec<Value> = Vec::new();
    let mut skipped_pages: Vec<Value> = Vec::new();
    let mut total_songs = 0usize;

    for page_id in &page_ids {
        let page_name = page_names.get(page_id).cloned().unwrap_or_else(|| page_id.clone());
        let sounds = repo::sounds::resolve_playlist_sounds(&st.db, page_id, true).await?;
        if sounds.is_empty() {
            skipped_pages.push(json!({ "integration_id": page_id, "page_name": page_name }));
            continue;
        }
        total_songs += sounds.len();
        sections.push(json!({
            "integration_id": page_id,
            "page_name": page_name,
            "songs": sounds
                .iter()
                .map(|s| json!({ "id": s.id, "label": s.label, "url": s.url }))
                .collect::<Vec<_>>(),
        }));
    }

    let today = chrono::Local::now().format("%B %d, %Y");
    let mut lines = vec![
        format!("\u{1f3b5} <b>Today's Sound Assignments — {today}</b>"),
        String::new(),
    ];
    if sections.is_empty() {
        lines.push("No active sounds assigned to your pages today.".into());
    } else {
        for section in &sections {
            let page_name = section["page_name"].as_str().unwrap_or("");
            lines.push(format!("\u{1f4f1} <b>{}</b>", escape_html(page_name)));
            for song in section["songs"].as_array().into_iter().flatten() {
                let label = escape_html(song["label"].as_str().unwrap_or(""));
                let url = song["url"].as_str().unwrap_or("");
                if url.is_empty() {
                    lines.push(format!("  • {label}"));
                } else {
                    lines.push(format!("  • <a href=\"{}\">{label}</a>", escape_html(url)));
                }
            }
            lines.push(String::new());
        }
    }

    Ok(PosterMessage {
        text: format!("{}\n", lines.join("\n").trim_end()),
        page_count: sections.len(),
        song_count: total_songs,
        sections,
        skipped_pages,
    })
}

/// The single send path (old `telegram_bot.send_sound_assignments`): build,
/// skip when empty, auto-create the topic, send. Returns the result dict.
async fn send_sound_assignments(
    st: &AppState,
    tg: &Telegram,
    poster: &clab_core::Poster,
    page_names: &HashMap<String, String>,
) -> anyhow::Result<Value> {
    if poster.chat_id == 0 {
        return Ok(json!({ "sent": false, "reason": "missing_chat_id" }));
    }

    let message = build_poster_message(st, poster, page_names).await?;
    if message.song_count == 0 {
        return Ok(json!({
            "sent": false,
            "reason": "no_songs",
            "song_count": 0,
            "page_count": 0,
            "skipped_pages": message.skipped_pages,
            "preview": message.to_value(poster),
        }));
    }

    let Some(topic_id) = ensure_sounds_topic(st, tg, poster).await else {
        return Ok(json!({ "sent": false, "reason": "topic_create_failed" }));
    };

    tg.send_html(poster.chat_id, Some(topic_id), &message.text).await?;
    Ok(json!({
        "sent": true,
        "song_count": message.song_count,
        "page_count": message.page_count,
        "skipped_pages": message.skipped_pages,
    }))
}

/// The bot that owns sound-assignment sends. The old app used a separate
/// send-only bot (SOUNDS_BOT_TOKEN); here the one configured bot serves both
/// paths, with the same 503 message external callers already handle.
fn require_sounds_bot(st: &AppState) -> Result<&Telegram, HttpError> {
    st.telegram.as_ref().ok_or_else(|| {
        HttpError::unavailable("Sounds bot is not running (SOUNDS_BOT_TOKEN not set?)")
    })
}

/// POST /api/telegram/sound-assignments/send/{poster_id}
async fn send_assignments(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
) -> ApiResult {
    let tg = require_sounds_bot(&st)?;

    let poster = repo::get_poster(&st.db, &poster_id)
        .await?
        .ok_or_else(|| HttpError::not_found("Poster not found"))?;
    if poster.chat_id == 0 {
        return Err(HttpError::bad_request("Poster has no chat_id"));
    }

    let page_names = page_name_lookup(&st).await?;
    let result = send_sound_assignments(&st, tg, &poster, &page_names)
        .await
        .map_err(|e| HttpError::bad_gateway(format!("Send failed: {e}")))?;

    if !result["sent"].as_bool().unwrap_or(false) {
        if result["reason"] == "topic_create_failed" {
            return Err(HttpError::bad_gateway("Failed to create Campaign Sounds topic"));
        }
        // {"ok": true, "sent": false, "poster_id": ..., **result}
        let mut body = json!({ "ok": true, "sent": false, "poster_id": poster_id });
        if let (Some(obj), Some(extra)) = (body.as_object_mut(), result.as_object()) {
            for (k, v) in extra {
                obj.insert(k.clone(), v.clone());
            }
        }
        return Ok(Json(body));
    }

    Ok(Json(json!({
        "ok": true,
        "sent": true,
        "poster_id": poster_id,
        "song_count": result["song_count"],
        "page_count": result["page_count"],
        "skipped_pages": result["skipped_pages"],
    })))
}

/// POST /api/telegram/sound-assignments/send-all — sequential, rate-limited.
async fn send_assignments_all(State(st): State<AppState>) -> ApiResult {
    let tg = require_sounds_bot(&st)?;

    let page_names = page_name_lookup(&st).await?;
    let posters = repo::list_posters(&st.db).await?;

    let mut sent: Vec<Value> = Vec::new();
    let mut skipped: Vec<Value> = Vec::new();
    let mut errors: Vec<Value> = Vec::new();

    for poster in &posters {
        if poster.chat_id == 0 {
            skipped.push(json!({ "poster_id": poster.id, "reason": "missing_chat_id" }));
            continue;
        }

        let result = match send_sound_assignments(&st, tg, poster, &page_names).await {
            Ok(r) => r,
            Err(e) => {
                let msg: String = e.to_string().chars().take(200).collect();
                errors.push(json!({ "poster_id": poster.id, "error": msg }));
                continue;
            }
        };

        if result["sent"].as_bool().unwrap_or(false) {
            sent.push(json!({
                "poster_id": poster.id,
                "song_count": result["song_count"],
                "page_count": result["page_count"],
            }));
        } else if result["reason"] == "topic_create_failed" {
            errors.push(json!({ "poster_id": poster.id, "error": "topic_create_failed" }));
        } else {
            let reason = result["reason"].as_str().unwrap_or("skipped");
            skipped.push(json!({ "poster_id": poster.id, "reason": reason }));
        }

        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }

    Ok(Json(json!({
        "ok": true,
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
        "total_posters": posters.len(),
    })))
}

// ── Sync engine (port of services/campaign_hub.py) ──────────────────────────

mod sync {
    use std::collections::HashMap;

    use integrations::hub::HubCampaign;
    use integrations::notion::CampaignSound;
    use serde_json::{json, Value};

    use clab_core::repo;

    /// Minimum combined artist|song similarity for the deterministic fuzzy
    /// fallback (tuned so real spelling variants score ~0.91-0.97 while
    /// unrelated names stay below ~0.78).
    const FUZZY_MATCH_THRESHOLD: f64 = 0.88;

    #[derive(Debug, Clone)]
    pub struct NotionEntry {
        pub id: String,
        pub artist: String,
        pub song: String,
        pub url: String,
    }

    /// Lowercase, fold a few accents, keep `[a-z0-9 ]`, collapse whitespace.
    fn slugify(text: &str) -> String {
        let lowered: String = text
            .to_lowercase()
            .chars()
            .map(|c| match c {
                'ø' => 'o',
                'é' => 'e',
                'á' => 'a',
                other => other,
            })
            .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == ' ')
            .collect();
        lowered.split_whitespace().collect::<Vec<_>>().join(" ")
    }

    fn nospace(text: &str) -> String {
        text.replace(' ', "")
    }

    /// First artist of a collab ("A x B", "A & B", "A feat B", ...).
    fn first_collab(artist: &str) -> String {
        let words: Vec<&str> = artist.split_whitespace().collect();
        for (i, w) in words.iter().enumerate() {
            if i > 0 && matches!(*w, "x" | "&" | "and" | "feat" | "ft" | "with") {
                return words[..i].join(" ");
            }
        }
        artist.trim().to_string()
    }

    /// Normalized keys for the deterministic matching pass.
    fn match_keys(artist: &str, song: &str) -> Vec<String> {
        let mut keys: Vec<String> = Vec::new();
        let a = slugify(artist);
        let s = slugify(song);

        if !a.is_empty() && !s.is_empty() {
            keys.push(format!("{a}|{s}"));
            keys.push(format!("{}|{s}", nospace(&a)));
            keys.push(format!("{}|{}", nospace(&a), nospace(&s)));
            let s1 = s.split_whitespace().next().unwrap_or(&s).to_string();
            if s1.len() > 3 {
                keys.push(format!("{a}|{s1}"));
                keys.push(format!("{}|{s1}", nospace(&a)));
            }
        }
        if !a.is_empty() {
            keys.push(format!("artist:{a}"));
            keys.push(format!("artist:{}", nospace(&a)));
        }
        let fa = if a.is_empty() { String::new() } else { first_collab(&a) };
        if !fa.is_empty() && fa != a {
            keys.push(format!("artist:{fa}"));
            keys.push(format!("artist:{}", nospace(&fa)));
            if !s.is_empty() {
                keys.push(format!("{fa}|{s}"));
            }
        }
        keys.sort();
        keys.dedup();
        keys
    }

    // ── difflib.SequenceMatcher.ratio() (no junk, short strings) ────────────

    fn longest_match(
        a: &[char],
        b2j: &HashMap<char, Vec<usize>>,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, ch) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(js) = b2j.get(ch) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = if j == 0 { 1 } else { j2len.get(&(j - 1)).copied().unwrap_or(0) + 1 };
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        (besti, bestj, bestsize)
    }

    fn count_matches(
        a: &[char],
        b2j: &HashMap<char, Vec<usize>>,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> usize {
        let (i, j, k) = longest_match(a, b2j, alo, ahi, blo, bhi);
        if k == 0 {
            return 0;
        }
        k + count_matches(a, b2j, alo, i, blo, j)
            + count_matches(a, b2j, i + k, ahi, j + k, bhi)
    }

    /// `difflib.SequenceMatcher(None, a, b).ratio()` for short strings.
    fn seq_ratio(a: &str, b: &str) -> f64 {
        let a: Vec<char> = a.chars().collect();
        let b: Vec<char> = b.chars().collect();
        if a.is_empty() && b.is_empty() {
            return 1.0;
        }
        let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
        for (j, ch) in b.iter().enumerate() {
            b2j.entry(*ch).or_default().push(j);
        }
        let matches = count_matches(&a, &b2j, 0, a.len(), 0, b.len());
        2.0 * matches as f64 / (a.len() + b.len()) as f64
    }

    /// Deterministic stdlib-equivalent fuzzy fallback: catches near-miss
    /// spellings ("Alex Nicol" vs "Alex Nichol") with zero key overlap,
    /// without paying for an AI call.
    fn fuzzy_match_notion<'a>(
        artist: &str,
        song: &str,
        notion_entries: &'a [NotionEntry],
    ) -> Option<&'a NotionEntry> {
        let hub_key = format!("{}|{}", slugify(artist), slugify(song));
        if hub_key.trim_matches('|').is_empty() {
            return None;
        }
        let mut best: Option<&NotionEntry> = None;
        let mut best_ratio = FUZZY_MATCH_THRESHOLD;
        for n in notion_entries {
            if n.url.is_empty() {
                continue;
            }
            let notion_key = format!("{}|{}", slugify(&n.artist), slugify(&n.song));
            let ratio = seq_ratio(&hub_key, &notion_key);
            if ratio >= best_ratio {
                best_ratio = ratio;
                best = Some(n);
            }
        }
        best
    }

    // ── AI matching (GPT-4.1-mini via the shared OpenAI client) ─────────────

    #[derive(Debug, Clone)]
    struct Unmatched {
        slug: String,
        artist: String,
        song: String,
    }

    fn ai_prompt(hub_campaigns: &[Unmatched], notion_entries: &[NotionEntry]) -> String {
        let hub_list = hub_campaigns
            .iter()
            .map(|c| format!("- [{}] {} - {}", c.slug, c.artist, c.song))
            .collect::<Vec<_>>()
            .join("\n");
        let notion_list = notion_entries
            .iter()
            .map(|n| format!("- [{}] {} - {}", n.id, n.artist, n.song))
            .collect::<Vec<_>>()
            .join("\n");
        format!(
            "Match each campaign to its Notion CRM entry. These are music campaigns where the same artist+song may be spelled differently between systems (typos, abbreviations like \"Cam\" vs \"Cameron\", \"Mon Rovia\" vs \"Monrovia\", \"r2\" suffix means round 2 of same campaign, etc).\n\nCAMPAIGNS (need sound links):\n{hub_list}\n\nNOTION CRM ENTRIES (have sound links):\n{notion_list}\n\nReturn a JSON object mapping each campaign slug to the Notion entry ID it matches, or null if no match exists.\nExample: {{\"artist_song_slug\": \"notion-page-id\", \"other_slug\": null}}\n\nOnly return confident matches. If uncertain, use null. A campaign with \"R2\"/\"r2\" in the name is round 2 of the same song — match it to the original Notion entry."
        )
    }

    /// Tolerant parse of the model's JSON object (strips optional code fences).
    fn parse_ai_response(content: &str) -> HashMap<String, Option<String>> {
        let trimmed = content
            .trim()
            .trim_start_matches("```json")
            .trim_start_matches("```")
            .trim_end_matches("```")
            .trim();
        serde_json::from_str::<Value>(trimmed)
            .ok()
            .and_then(|v| v.as_object().cloned())
            .map(|obj| {
                obj.into_iter()
                    .map(|(k, v)| (k, v.as_str().map(str::to_string)))
                    .collect()
            })
            .unwrap_or_default()
    }

    async fn ai_match_campaigns(
        hub_campaigns: &[Unmatched],
        notion_entries: &[NotionEntry],
    ) -> HashMap<String, Option<String>> {
        if !integrations::openai::configured()
            || hub_campaigns.is_empty()
            || notion_entries.is_empty()
        {
            return HashMap::new();
        }
        match integrations::openai::chat_text(
            "gpt-4.1-mini",
            &ai_prompt(hub_campaigns, notion_entries),
        )
        .await
        {
            Ok(content) => parse_ai_response(&content),
            Err(e) => {
                tracing::warn!("AI campaign matching failed (non-fatal): {e:#}");
                HashMap::new()
            }
        }
    }

    // ── The sync itself ──────────────────────────────────────────────────────

    fn url_key(url: &str) -> String {
        url.trim_end_matches('/').to_lowercase()
    }

    fn hub_label(artist: &str, song: &str) -> String {
        if !artist.is_empty() && !song.is_empty() {
            format!("{artist} - {song}")
        } else if !artist.is_empty() {
            artist.to_string()
        } else {
            song.to_string()
        }
    }

    /// Full sound sync: Campaign Hub as source of truth for what's active,
    /// Notion for the sound links, deterministic + fuzzy + AI matching, then
    /// reconcile the local library (add / reactivate / deactivate).
    pub async fn sync_sound_status(
        db: &clab_core::Db,
        notion_campaigns: Option<Vec<CampaignSound>>,
    ) -> anyhow::Result<Value> {
        let all_hub = integrations::hub::fetch_all_campaigns().await?;
        let (active_hub, completed_hub): (Vec<HubCampaign>, Vec<HubCampaign>) =
            all_hub.into_iter().partition(|c| c.is_active());

        let notion_campaigns = match notion_campaigns {
            Some(v) => v,
            None => integrations::notion::fetch_campaigns_with_sounds().await?,
        };

        // Notion lookup by deterministic keys; later entries win a key clash.
        let notion_entries: Vec<NotionEntry> = notion_campaigns
            .iter()
            .map(|n| NotionEntry {
                id: n.notion_page_id.clone(),
                artist: n.artist.clone(),
                song: n.song.clone(),
                url: n.tiktok_url.clone(),
            })
            .collect();
        let mut notion_by_key: HashMap<String, usize> = HashMap::new();
        for (idx, entry) in notion_entries.iter().enumerate() {
            for key in match_keys(&entry.artist, &entry.song) {
                notion_by_key.insert(key, idx);
            }
        }

        // Pass 1: deterministic keys, then the free fuzzy fallback.
        struct Matched {
            url: String,
            label: String,
        }
        let mut matched_active: Vec<Matched> = Vec::new();
        let mut unmatched_for_ai: Vec<Unmatched> = Vec::new();
        let mut fuzzy_matched = 0usize;

        for c in &active_hub {
            let mut notion_entry: Option<&NotionEntry> = match_keys(&c.artist, &c.song)
                .iter()
                .find_map(|key| notion_by_key.get(key))
                .map(|&idx| &notion_entries[idx]);

            if !notion_entry.map(|n| !n.url.is_empty()).unwrap_or(false) {
                if let Some(fuzzy) = fuzzy_match_notion(&c.artist, &c.song, &notion_entries) {
                    notion_entry = Some(fuzzy);
                    fuzzy_matched += 1;
                }
            }

            match notion_entry.filter(|n| !n.url.is_empty()) {
                Some(n) => matched_active.push(Matched {
                    url: n.url.clone(),
                    label: hub_label(&c.artist, &c.song),
                }),
                None => unmatched_for_ai.push(Unmatched {
                    slug: c.slug.clone(),
                    artist: c.artist.clone(),
                    song: c.song.clone(),
                }),
            }
        }

        // Pass 2: AI matching for what's left.
        let mut ai_matched = 0usize;
        if !unmatched_for_ai.is_empty() && !notion_entries.is_empty() {
            let ai_results = ai_match_campaigns(&unmatched_for_ai, &notion_entries).await;
            let notion_by_id: HashMap<&str, &NotionEntry> =
                notion_entries.iter().map(|n| (n.id.as_str(), n)).collect();

            let mut still_unmatched: Vec<Unmatched> = Vec::new();
            for item in unmatched_for_ai {
                let hit = ai_results
                    .get(&item.slug)
                    .and_then(|id| id.as_deref())
                    .and_then(|id| notion_by_id.get(id))
                    .filter(|n| !n.url.is_empty());
                match hit {
                    Some(n) => {
                        matched_active.push(Matched {
                            url: n.url.clone(),
                            label: hub_label(&item.artist, &item.song),
                        });
                        ai_matched += 1;
                    }
                    None => still_unmatched.push(item),
                }
            }
            unmatched_for_ai = still_unmatched;
        }

        // Reconcile the library.
        let existing_sounds = repo::sounds::list_sounds(db, false).await?;
        let existing_by_url: HashMap<String, &repo::sounds::Sound> =
            existing_sounds.iter().map(|s| (url_key(&s.url), s)).collect();

        let mut sounds_added = 0usize;
        let mut sounds_deactivated = 0usize;
        let mut sounds_reactivated = 0usize;
        let mut errors: Vec<String> = Vec::new();

        let mut active_urls: std::collections::HashSet<String> = std::collections::HashSet::new();
        for m in &matched_active {
            let key = url_key(&m.url);
            active_urls.insert(key.clone());

            match existing_by_url.get(&key) {
                Some(existing) => {
                    if !existing.active {
                        match repo::sounds::toggle_sound(db, &existing.id, true).await {
                            Ok(_) => sounds_reactivated += 1,
                            Err(e) => errors.push(format!("reactivate {}: {e}", m.label)),
                        }
                    }
                }
                None => match repo::sounds::add_sound(db, &m.url, &m.label).await {
                    Ok(_) => sounds_added += 1,
                    Err(e) => errors.push(format!("add {}: {e}", m.label)),
                },
            }
        }

        // Deactivate anything no longer active (snapshot from before the adds).
        for sound in &existing_sounds {
            if !active_urls.contains(&url_key(&sound.url)) && sound.active {
                match repo::sounds::toggle_sound(db, &sound.id, false).await {
                    Ok(_) => sounds_deactivated += 1,
                    Err(e) => errors.push(format!("deactivate {}: {e}", sound.label)),
                }
            }
        }

        Ok(json!({
            "active_campaigns": active_hub.len(),
            "completed_campaigns": completed_hub.len(),
            "sounds_added": sounds_added,
            "sounds_deactivated": sounds_deactivated,
            "sounds_reactivated": sounds_reactivated,
            "matched_deterministic": matched_active.len() - ai_matched - fuzzy_matched,
            "matched_fuzzy": fuzzy_matched,
            "matched_ai": ai_matched,
            "unmatched": unmatched_for_ai
                .iter()
                .map(|u| format!("{} - {}", u.artist, u.song))
                .collect::<Vec<_>>(),
            "errors": errors,
        }))
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn slugify_normalizes() {
            assert_eq!(slugify("  MØ & Friends! "), "mo friends");
            assert_eq!(slugify("Beyoncé"), "beyonce");
            assert_eq!(slugify("A   B"), "a b");
        }

        #[test]
        fn first_collab_splits_on_connectors() {
            assert_eq!(first_collab("alice x bob"), "alice");
            assert_eq!(first_collab("alice and the band"), "alice");
            assert_eq!(first_collab("alice feat bob"), "alice");
            assert_eq!(first_collab("alice"), "alice");
            assert_eq!(first_collab("xander smith"), "xander smith", "leading word kept");
        }

        #[test]
        fn match_keys_cover_variants() {
            let keys = match_keys("Mon Rovia", "To Watch The World");
            assert!(keys.contains(&"mon rovia|to watch the world".to_string()));
            assert!(keys.contains(&"monrovia|to watch the world".to_string()));
            assert!(keys.contains(&"artist:monrovia".to_string()));
            // first-word song key only when > 3 chars ("to" is skipped)
            assert!(!keys.contains(&"mon rovia|to".to_string()));
        }

        #[test]
        fn seq_ratio_matches_difflib_values() {
            // Values cross-checked against Python difflib.
            assert!((seq_ratio("abcd", "abcd") - 1.0).abs() < 1e-9);
            assert!((seq_ratio("abcd", "bcde") - 0.75).abs() < 1e-9);
            assert!((seq_ratio("abc", "xyz") - 0.0).abs() < 1e-9);
            let r = seq_ratio("alex nicol|waiting", "alex nichol|waiting");
            assert!(r > 0.9 && r < 1.0, "spelling variant scores high: {r}");
        }

        #[test]
        fn fuzzy_finds_spelling_variants_only() {
            let entries = vec![
                NotionEntry {
                    id: "n1".into(),
                    artist: "Alex Nichol".into(),
                    song: "Waiting".into(),
                    url: "https://t/1".into(),
                },
                NotionEntry {
                    id: "n2".into(),
                    artist: "Someone Else".into(),
                    song: "Other".into(),
                    url: "https://t/2".into(),
                },
            ];
            let hit = fuzzy_match_notion("Alex Nicol", "Waiting", &entries).unwrap();
            assert_eq!(hit.id, "n1");
            assert!(fuzzy_match_notion("Total Stranger", "Nope", &entries).is_none());
            assert!(fuzzy_match_notion("", "", &entries).is_none());
        }

        #[test]
        fn ai_response_parses_with_and_without_fences() {
            let plain = r#"{"slug-a": "n1", "slug-b": null}"#;
            let parsed = parse_ai_response(plain);
            assert_eq!(parsed["slug-a"], Some("n1".to_string()));
            assert_eq!(parsed["slug-b"], None);

            let fenced = "```json\n{\"slug-a\": \"n1\"}\n```";
            assert_eq!(parse_ai_response(fenced)["slug-a"], Some("n1".to_string()));

            assert!(parse_ai_response("not json").is_empty());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escape_html_matches_python_html_escape() {
        assert_eq!(
            escape_html(r#"A & B <C> "D" 'E'"#),
            "A &amp; B &lt;C&gt; &quot;D&quot; &#x27;E&#x27;"
        );
    }

    #[test]
    fn http_error_body_carries_both_keys() {
        let resp = HttpError::bad_request("nope").into_response();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }
}
