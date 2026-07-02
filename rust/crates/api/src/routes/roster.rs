//! Page roster routes — parity port of `routers/roster.py`.
//!
//! CRUD over the page roster (project assignment + Drive folder mapping),
//! a duplicate-name audit + dedup, and the Notion Master Pages sync (Notion is
//! canonical for account identity/credentials; the roster keeps app-only
//! fields like project and drive_folder_id). `POST /api/roster/sync` is the
//! legacy Postiz-sync alias — it routes to the Notion sync with the legacy
//! response shape, exactly like the old app.

use std::collections::HashMap;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{get, post, put};
use axum::{Json, Router};
use clab_core::repo::roster as roster_repo;
use serde::Deserialize;
use serde_json::{json, Map, Value};

use super::sounds::HttpError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        // FastAPI served this at "/api/roster/" (trailing slash) and
        // redirected the bare path; axum treats them as distinct, so register
        // both against the same handler.
        .route("/api/roster/", get(list_roster))
        .route("/api/roster", get(list_roster))
        .route("/api/roster/project/{project_name}", get(get_project_pages))
        .route("/api/roster/duplicates", get(find_duplicates))
        .route("/api/roster/dedup", post(dedup_roster))
        .route("/api/roster/sync-notion", post(sync_from_notion))
        .route("/api/roster/sync", post(sync_legacy))
        .route("/api/roster/{integration_id}", put(update_page).delete(delete_page))
}

type ApiResult = Result<Json<Value>, HttpError>;

// ── CRUD ─────────────────────────────────────────────────────────────────────

/// GET /api/roster/ — every page in the roster.
async fn list_roster(State(st): State<AppState>) -> ApiResult {
    let pages = roster_repo::list_pages_full(&st.db).await?;
    let pages: Vec<Value> = pages.iter().map(|p| p.to_value()).collect();
    Ok(Json(json!({ "pages": pages })))
}

/// GET /api/roster/project/{project_name} — project pages enriched with
/// staging-topic status.
async fn get_project_pages(
    State(st): State<AppState>,
    Path(project_name): Path<String>,
) -> ApiResult {
    let pages = roster_repo::list_pages_for_project(&st.db, &project_name).await?;
    let topics = roster_repo::staging_topics(&st.db).await?;

    let enriched: Vec<Value> = pages
        .iter()
        .map(|page| {
            let mut v = page.to_value();
            let topic = topics.get(&page.id);
            if let Some(obj) = v.as_object_mut() {
                obj.insert("has_staging_topic".into(), json!(topic.is_some()));
                obj.insert(
                    "staging_topic_name".into(),
                    topic.map(|(_, name)| json!(name)).unwrap_or(Value::Null),
                );
            }
            v
        })
        .collect();

    Ok(Json(json!({ "pages": enriched })))
}

#[derive(Deserialize)]
struct UpdatePageRequest {
    project: Option<String>,
    drive_folder_url: Option<String>,
}

/// PUT /api/roster/{integration_id} — assign or update a page.
async fn update_page(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
    Json(req): Json<UpdatePageRequest>,
) -> ApiResult {
    let mut data = Map::new();
    if let Some(project) = req.project {
        data.insert("project".into(), json!(project));
    }
    if let Some(url) = req.drive_folder_url {
        let folder_id = roster_repo::parse_drive_folder_id(&url);
        if !url.is_empty() && folder_id.is_none() {
            return Err(HttpError::bad_request(
                "Could not parse Google Drive folder ID from URL",
            ));
        }
        data.insert("drive_folder_url".into(), json!(url));
        data.insert(
            "drive_folder_id".into(),
            folder_id.map(Value::String).unwrap_or(Value::Null),
        );
    }
    let entry = roster_repo::set_page(&st.db, &integration_id, &data).await?;
    Ok(Json(json!({ "page": entry.to_value() })))
}

/// DELETE /api/roster/{integration_id}
async fn delete_page(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
) -> ApiResult {
    if !roster_repo::remove_page(&st.db, &integration_id).await? {
        return Err(HttpError::not_found("Page not found in roster"));
    }
    Ok(Json(json!({ "deleted": true })))
}

// ── Dedup ────────────────────────────────────────────────────────────────────

/// The old emoji-stripping regex's character classes, as a predicate. The
/// legacy `0x24C2..=0x1F251` mega-range subsumed several of the smaller
/// classes (dingbats, misc symbols, flags, variation selector), so the union
/// below is the deduplicated equivalent.
fn is_emoji(c: char) -> bool {
    matches!(u32::from(c),
        0x200D                // zero-width joiner
        | 0x24C2..=0x1F251    // enclosed chars … enclosed ideographs (legacy mega-range)
        | 0x1F300..=0x1F5FF   // symbols & pictographs
        | 0x1F600..=0x1F64F   // emoticons
        | 0x1F680..=0x1F6FF   // transport & map
        | 0x1F900..=0x1F9FF   // supplemental symbols
        | 0x1FA00..=0x1FAFF   // extended symbols
    )
}

/// Normalize a page name for dedup: strip emoji, collapse whitespace, lowercase.
fn normalize_name(name: &str) -> String {
    let stripped: String = name.chars().filter(|c| !is_emoji(*c)).collect();
    stripped
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
        .trim()
        .to_string()
}

/// Group page indices by normalized name, preserving roster order.
fn group_by_name(pages: &[roster_repo::RosterPage]) -> Vec<(String, Vec<usize>)> {
    let mut order: Vec<String> = Vec::new();
    let mut groups: HashMap<String, Vec<usize>> = HashMap::new();
    for (idx, page) in pages.iter().enumerate() {
        let key = if page.name.is_empty() {
            normalize_name(&page.id)
        } else {
            normalize_name(&page.name)
        };
        if !groups.contains_key(&key) {
            order.push(key.clone());
        }
        groups.entry(key).or_default().push(idx);
    }
    order
        .into_iter()
        .map(|key| {
            let ids = groups.remove(&key).unwrap_or_default();
            (key, ids)
        })
        .collect()
}

/// GET /api/roster/duplicates — audit duplicate page names with enough
/// context (topics, inventory, project, drive) to pick a keeper.
async fn find_duplicates(State(st): State<AppState>) -> ApiResult {
    let pages = roster_repo::list_pages_full(&st.db).await?;
    let staging_topics = roster_repo::staging_topics(&st.db).await?;
    let inventory = roster_repo::inventory_counts(&st.db).await?;

    let mut by_name = group_by_name(&pages);
    by_name.sort_by(|a, b| a.0.cmp(&b.0));

    let mut duplicates: Vec<Value> = Vec::new();
    for (name, indices) in &by_name {
        if indices.len() <= 1 {
            continue;
        }
        let entries: Vec<Value> = indices
            .iter()
            .map(|&idx| {
                let page = &pages[idx];
                let topic = staging_topics.get(&page.id);
                let (total, pending, forwarded) =
                    inventory.get(&page.id).copied().unwrap_or((0, 0, 0));
                json!({
                    "integration_id": page.id,
                    "name": page.name,
                    "provider": page.provider.clone().unwrap_or_default(),
                    "has_topic": topic.is_some(),
                    "topic_id": topic.map(|(id, _)| json!(id)).unwrap_or(Value::Null),
                    "topic_name": topic.map(|(_, n)| n.clone()).unwrap_or_default(),
                    "inventory_total": total,
                    "inventory_pending": pending,
                    "inventory_forwarded": forwarded,
                    "has_project": page.project.as_deref().is_some_and(|p| !p.is_empty()),
                    "has_drive": page.drive_folder_id.as_deref().is_some_and(|d| !d.is_empty()),
                })
            })
            .collect();
        duplicates.push(json!({ "name": name, "count": indices.len(), "entries": entries }));
    }

    Ok(Json(json!({
        "total_pages": pages.len(),
        "duplicate_names": duplicates.len(),
        "duplicates": duplicates,
    })))
}

/// POST /api/roster/dedup — for each duplicate set keep the entry with the
/// most data (inventory ≫ topic > project > drive), merge inventory into the
/// keeper, remove the rest (their staging topics go with them).
async fn dedup_roster(State(st): State<AppState>) -> ApiResult {
    let pages = roster_repo::list_pages_full(&st.db).await?;
    let staging_topics = roster_repo::staging_topics(&st.db).await?;
    let inventory = roster_repo::inventory_counts(&st.db).await?;

    let score = |page: &roster_repo::RosterPage| -> i64 {
        let mut s = 0i64;
        s += inventory.get(&page.id).map(|(t, _, _)| *t).unwrap_or(0) * 100;
        if staging_topics.contains_key(&page.id) {
            s += 50;
        }
        if page.project.as_deref().is_some_and(|p| !p.is_empty()) {
            s += 10;
        }
        if page.drive_folder_id.as_deref().is_some_and(|d| !d.is_empty()) {
            s += 5;
        }
        s
    };

    let mut removed_ids: Vec<String> = Vec::new();
    let mut removed_names: Vec<String> = Vec::new();
    let mut inventory_merged = 0u64;
    let mut topics_cleaned = 0u32;

    for (_, indices) in group_by_name(&pages) {
        if indices.len() <= 1 {
            continue;
        }
        // Stable sort keeps roster order among ties, like Python's sorted().
        let mut sorted = indices.clone();
        sorted.sort_by_key(|&idx| std::cmp::Reverse(score(&pages[idx])));
        let keep_id = pages[sorted[0]].id.clone();

        for &idx in &sorted[1..] {
            let dupe = &pages[idx];
            inventory_merged += roster_repo::move_inventory(&st.db, &dupe.id, &keep_id).await?;
            if staging_topics.contains_key(&dupe.id) {
                topics_cleaned += 1;
            }
            removed_ids.push(dupe.id.clone());
            removed_names.push(if dupe.name.is_empty() {
                dupe.id.clone()
            } else {
                dupe.name.clone()
            });
        }
    }

    for dupe_id in &removed_ids {
        roster_repo::remove_page(&st.db, dupe_id).await?;
    }

    let remaining = roster_repo::list_pages_full(&st.db).await?.len();
    Ok(Json(json!({
        "removed": removed_ids.len(),
        "removed_names": removed_names,
        "inventory_merged": inventory_merged,
        "topics_cleaned": topics_cleaned,
        "remaining": remaining,
    })))
}

// ── Notion Master Pages sync ─────────────────────────────────────────────────

/// Run the sync: Notion rows upsert into the roster. Notion is canonical for
/// account identity/credentials/poster; app-only fields (project, drive
/// folder id, CF email routing fields) are preserved from the existing entry.
async fn run_notion_sync(st: &AppState) -> Result<Value, HttpError> {
    if !integrations::notion::pages_configured() {
        return Err(HttpError::unavailable(
            "Notion not configured — set NOTION_API_KEY and NOTION_PAGES_DB",
        ));
    }

    let rows = integrations::notion::fetch_all_pages().await.map_err(|e| {
        match e.downcast_ref::<integrations::notion::NotionApiError>() {
            Some(api) => HttpError::new(
                StatusCode::from_u16(api.status).unwrap_or(StatusCode::BAD_GATEWAY),
                format!("Notion API error: {}", api.body),
            ),
            None => HttpError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("Notion sync failed: {e}"),
            ),
        }
    })?;

    let mut added = 0u32;
    let mut updated = 0u32;
    let mut errors: Vec<String> = Vec::new();

    for row in &rows {
        let existing = match roster_repo::get_page_full(&st.db, &row.integration_id).await {
            Ok(v) => v,
            Err(e) => {
                errors.push(format!("{}: {e}", row.name));
                continue;
            }
        };
        let existing_v = existing.as_ref().map(|p| p.to_value()).unwrap_or(Value::Null);
        let app_only = |key: &str| existing_v.get(key).cloned().unwrap_or(Value::Null);

        // Merge: Notion fields overwrite, app-only fields preserved.
        let merged: Map<String, Value> = json!({
            "name": row.name,
            "provider": row.provider,
            "tiktok_url": row.tiktok_url,
            "signup_email": row.signup_email,
            "fwd_address": row.fwd_address,
            "password": row.password,
            "poster_name": row.poster_name,
            "group": row.group,
            "group_label": row.group_label,
            "account_type": row.account_type,
            "notes": row.notes,
            "notion_page_id": row.notion_page_id,
            "source": "notion",
            "status": row.status,
            "pipeline": row.pipeline,
            "page_type": row.page_type,
            "sounds_reference": row.sounds_reference,
            "go_live_date": row.go_live_date,
            // drive_folder_url: prefer Notion if set, else existing
            "drive_folder_url": if row.drive_folder_url.is_empty() {
                app_only("drive_folder_url")
            } else {
                json!(row.drive_folder_url)
            },
            // App-only fields preserved from the existing entry:
            "project": app_only("project"),
            "drive_folder_id": app_only("drive_folder_id"),
            "email_alias": app_only("email_alias"),
            "email_rule_id": app_only("email_rule_id"),
            "fwd_destination": app_only("fwd_destination"),
        })
        .as_object()
        .cloned()
        .unwrap_or_default();

        match roster_repo::set_page(&st.db, &row.integration_id, &merged).await {
            Ok(_) => {
                if existing.is_some() {
                    updated += 1;
                } else {
                    added += 1;
                }
            }
            Err(e) => errors.push(format!("{}: {e}", row.name)),
        }
    }

    let pages = roster_repo::list_pages_full(&st.db).await?;
    let pages: Vec<Value> = pages.iter().map(|p| p.to_value()).collect();

    Ok(json!({
        "added": added,
        "updated": updated,
        "total_in_notion": rows.len(),
        "errors": errors,
        "pages": pages,
    }))
}

/// POST /api/roster/sync-notion
async fn sync_from_notion(State(st): State<AppState>) -> ApiResult {
    Ok(Json(run_notion_sync(&st).await?))
}

/// POST /api/roster/sync — legacy Postiz-sync alias; runs the Notion sync and
/// answers in the legacy shape so old callers don't break.
async fn sync_legacy(State(st): State<AppState>) -> ApiResult {
    let result = run_notion_sync(&st).await?;
    Ok(Json(json!({
        "added": result["added"],
        "removed": 0, // Notion-driven — we don't auto-remove anything
        "updated": result["updated"],
        "pages": result["pages"],
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_strips_emoji_and_collapses() {
        assert_eq!(normalize_name("Cool Page 🎵🔥"), "cool page");
        assert_eq!(normalize_name("  Cool   PAGE "), "cool page");
        assert_eq!(normalize_name("plain"), "plain");
        // variation selector + ZWJ sequences vanish too
        assert_eq!(normalize_name("star\u{fe0f}\u{200d}name"), "starname");
    }

    #[test]
    fn emoji_predicate_covers_legacy_ranges() {
        for c in ['😀', '🎵', '🚀', '✂', '🤖', '☀'] {
            assert!(is_emoji(c), "{c}");
        }
        for c in ['a', 'Z', '0', '-', 'é'] {
            assert!(!is_emoji(c), "{c}");
        }
    }
}
