//! Notion CRM client — two databases, two read paths:
//!   - **Campaigns DB** (`NOTION_CAMPAIGNS_DB`): rows with a TikTok Sound Link,
//!     consumed by the sound-library sync (`services/notion.py` port).
//!   - **Master Pages DB** (`NOTION_PAGES_DB`): the canonical account roster,
//!     consumed by the roster sync (`services/notion_pages.py` port).
//!
//! Parsing is split from fetching so the wire-shape logic is testable against
//! fixture JSON without a live Notion (fetch fns are thin paginated loops).

use serde_json::{json, Value};

use crate::http::client;

const NOTION_VERSION: &str = "2022-06-28";

/// Env override for tests / proxies (same pattern as OPENAI_BASE_URL).
fn base_url() -> String {
    std::env::var("NOTION_BASE_URL").unwrap_or_else(|_| "https://api.notion.com".into())
}

fn api_key() -> String {
    std::env::var("NOTION_API_KEY").unwrap_or_default()
}

fn campaigns_db() -> String {
    std::env::var("NOTION_CAMPAIGNS_DB").unwrap_or_default()
}

fn pages_db() -> String {
    std::env::var("NOTION_PAGES_DB").unwrap_or_default()
}

/// Only sync campaigns created on or after this date. Older campaigns predate
/// the Campaign Hub and would never get deactivated.
fn sound_cutoff() -> String {
    std::env::var("NOTION_SOUND_CUTOFF").unwrap_or_else(|_| "2026-03-01".into())
}

/// Whether the campaign-sounds sync can run.
pub fn campaigns_configured() -> bool {
    !api_key().is_empty() && !campaigns_db().is_empty()
}

/// Whether the Master Pages roster sync can run.
pub fn pages_configured() -> bool {
    !api_key().is_empty() && !pages_db().is_empty()
}

/// A Notion HTTP failure, carrying the upstream status so callers can pass it
/// through (the old roster route surfaced Notion's own status code).
#[derive(Debug)]
pub struct NotionApiError {
    pub status: u16,
    pub body: String,
}

impl std::fmt::Display for NotionApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "notion {}: {}", self.status, self.body)
    }
}

impl std::error::Error for NotionApiError {}

/// POST /v1/databases/{db}/query and collect every page across pagination.
async fn query_database(db_id: &str, filter: Option<Value>) -> anyhow::Result<Vec<Value>> {
    let mut results: Vec<Value> = Vec::new();
    let mut cursor: Option<String> = None;

    loop {
        let mut body = json!({ "page_size": 100 });
        if let Some(f) = &filter {
            body["filter"] = f.clone();
        }
        if let Some(c) = &cursor {
            body["start_cursor"] = json!(c);
        }

        let resp = client()
            .post(format!("{}/v1/databases/{}/query", base_url(), db_id))
            .bearer_auth(api_key())
            .header("Notion-Version", NOTION_VERSION)
            .json(&body)
            .send()
            .await?;

        let status = resp.status();
        let text = resp.text().await?;
        if !status.is_success() {
            return Err(NotionApiError {
                status: status.as_u16(),
                body: text.chars().take(500).collect(),
            }
            .into());
        }
        let data: Value = serde_json::from_str(&text)?;

        results.extend(data["results"].as_array().cloned().unwrap_or_default());
        if data["has_more"].as_bool().unwrap_or(false) {
            cursor = data["next_cursor"].as_str().map(str::to_string);
            if cursor.is_none() {
                break;
            }
        } else {
            break;
        }
    }

    Ok(results)
}

// ── Property extractors ──────────────────────────────────────────────────────

/// First-fragment plain text (campaigns DB used only the first rich-text run).
fn first_plain(prop: &Value, kind: &str) -> String {
    prop[kind][0]["plain_text"].as_str().unwrap_or("").to_string()
}

/// All fragments joined (the Master Pages parser concatenated runs).
fn joined_plain(prop: &Value, kind: &str) -> String {
    prop[kind]
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|p| p["plain_text"].as_str().unwrap_or(""))
                .collect::<String>()
        })
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn url_prop(prop: &Value) -> Option<String> {
    prop["url"].as_str().map(str::to_string)
}

fn url_prop_or_empty(prop: &Value) -> String {
    url_prop(prop).unwrap_or_default().trim().to_string()
}

fn status_prop(prop: &Value) -> String {
    prop["status"]["name"].as_str().unwrap_or("").to_string()
}

fn select_prop(prop: &Value) -> String {
    prop["select"]["name"].as_str().unwrap_or("").to_string()
}

fn date_prop(prop: &Value) -> String {
    prop["date"]["start"].as_str().unwrap_or("").to_string()
}

fn email_prop(prop: &Value) -> String {
    prop["email"].as_str().unwrap_or("").trim().to_string()
}

// ── Campaigns (sound sync) ───────────────────────────────────────────────────

/// One campaign row that has a TikTok Sound Link.
#[derive(Debug, Clone, PartialEq)]
pub struct CampaignSound {
    pub notion_page_id: String,
    pub artist: String,
    pub song: String,
    pub label: String,
    pub tiktok_url: String,
    pub insta_url: Option<String>,
    pub campaign_stage: String,
    pub pipeline_status: String,
}

fn build_sound_label(artist: &str, song: &str) -> String {
    let artist = artist.trim();
    let song = song.trim();
    if !artist.is_empty() && !song.is_empty() {
        format!("{artist} - {song}")
    } else if !artist.is_empty() {
        artist.to_string()
    } else if !song.is_empty() {
        song.to_string()
    } else {
        "Unknown Campaign".to_string()
    }
}

/// Parse a Notion campaigns-DB page. `None` when it has no TikTok Sound Link.
pub fn parse_campaign(page: &Value) -> Option<CampaignSound> {
    let props = &page["properties"];
    let tiktok_url = url_prop(&props["TikTok Sound Link"]).filter(|u| !u.is_empty())?;

    let artist = first_plain(&props["Artist Name"], "title");
    let song = first_plain(&props["Song Name"], "rich_text");

    Some(CampaignSound {
        notion_page_id: page["id"].as_str().unwrap_or("").to_string(),
        label: build_sound_label(&artist, &song),
        artist,
        song,
        tiktok_url,
        insta_url: url_prop(&props["Insta Sound Link"]),
        campaign_stage: status_prop(&props["Campaign Stage"]),
        pipeline_status: status_prop(&props["Pipeline Status"]),
    })
}

/// Query the campaigns DB for every row with a TikTok Sound Link created on or
/// after the cutoff. Paginates through all results.
pub async fn fetch_campaigns_with_sounds() -> anyhow::Result<Vec<CampaignSound>> {
    if !campaigns_configured() {
        anyhow::bail!("NOTION_API_KEY and NOTION_CAMPAIGNS_DB must be set in environment");
    }
    let filter = json!({
        "and": [
            { "property": "TikTok Sound Link", "url": { "is_not_empty": true } },
            { "timestamp": "created_time",
              "created_time": { "on_or_after": sound_cutoff() } },
        ]
    });
    let pages = query_database(&campaigns_db(), Some(filter)).await?;
    Ok(pages.iter().filter_map(parse_campaign).collect())
}

// ── Master Pages (roster sync) ───────────────────────────────────────────────

/// One account row from the Master Pages DB, in roster vocabulary. Field
/// values are empty strings (not None) when unset — parity with the old
/// parser, which the frontend relies on.
#[derive(Debug, Clone, PartialEq)]
pub struct MasterPage {
    pub integration_id: String,
    pub name: String,
    pub provider: String,
    pub tiktok_url: String,
    pub signup_email: String,
    pub fwd_address: String,
    pub password: String,
    pub poster_name: String,
    pub group: String,
    pub group_label: String,
    pub account_type: String,
    pub notes: String,
    pub notion_page_id: String,
    pub status: String,
    pub pipeline: String,
    pub page_type: String,
    pub sounds_reference: String,
    pub go_live_date: String,
    pub drive_folder_url: String,
}

/// Lowercase, non-alphanumeric runs → `-` (the old `slugify`).
pub fn slugify(s: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in s.to_lowercase().trim().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            dash = false;
        } else if !dash && !out.is_empty() {
            out.push('-');
            dash = true;
        }
    }
    let out = out.trim_end_matches('-').to_string();
    if out.is_empty() {
        "unknown".into()
    } else {
        out
    }
}

/// Stable, deterministic id for a Notion-sourced account.
pub fn mint_integration_id(username: &str) -> String {
    format!("acct:{}", slugify(username))
}

/// Parse a Master Pages DB row. `None` when the row has no username.
pub fn parse_master_page(page: &Value) -> Option<MasterPage> {
    let props = &page["properties"];
    let username = joined_plain(&props["Account Username"], "title");
    if username.is_empty() {
        return None;
    }

    Some(MasterPage {
        integration_id: mint_integration_id(&username),
        name: username,
        provider: "tiktok".into(), // all rows in this DB are TikTok
        tiktok_url: url_prop_or_empty(&props["Page URL"]),
        signup_email: email_prop(&props["email"]),
        fwd_address: joined_plain(&props["fwd address"], "rich_text"),
        password: joined_plain(&props["Password"], "rich_text"),
        poster_name: joined_plain(&props["Poster"], "rich_text"),
        // The DB has both "Group" and "Group " (trailing space) — distinct cols.
        group: select_prop(&props["Group"]),
        group_label: select_prop(&props["Group "]),
        account_type: select_prop(&props["Account Type"]),
        notes: joined_plain(&props["Notes"], "rich_text"),
        notion_page_id: page["id"].as_str().unwrap_or("").to_string(),
        status: select_prop(&props["Status"]),
        pipeline: select_prop(&props["Pipeline"]),
        page_type: select_prop(&props["Page Type"]),
        sounds_reference: url_prop_or_empty(&props["Sounds Reference"]),
        go_live_date: date_prop(&props["Go-Live Date"]),
        drive_folder_url: url_prop_or_empty(&props["Drive Folder URL"]),
    })
}

/// Query the Master Pages DB (no filter) and return every parseable row.
pub async fn fetch_all_pages() -> anyhow::Result<Vec<MasterPage>> {
    if !pages_configured() {
        anyhow::bail!("NOTION_API_KEY and NOTION_PAGES_DB must be set");
    }
    let pages = query_database(&pages_db(), None).await?;
    Ok(pages.iter().filter_map(parse_master_page).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn campaign_fixture() -> Value {
        json!({
            "id": "notion-123",
            "properties": {
                "TikTok Sound Link": { "url": "https://www.tiktok.com/music/x-123" },
                "Insta Sound Link": { "url": null },
                "Artist Name": { "title": [ { "plain_text": "Alex Nichol" } ] },
                "Song Name": { "rich_text": [ { "plain_text": "Waiting" } ] },
                "Campaign Stage": { "status": { "name": "Live" } },
                "Pipeline Status": { "status": { "name": "Posting" } }
            }
        })
    }

    #[test]
    fn parses_a_campaign_row() {
        let c = parse_campaign(&campaign_fixture()).unwrap();
        assert_eq!(c.notion_page_id, "notion-123");
        assert_eq!(c.artist, "Alex Nichol");
        assert_eq!(c.song, "Waiting");
        assert_eq!(c.label, "Alex Nichol - Waiting");
        assert_eq!(c.tiktok_url, "https://www.tiktok.com/music/x-123");
        assert_eq!(c.insta_url, None);
        assert_eq!(c.campaign_stage, "Live");
        assert_eq!(c.pipeline_status, "Posting");
    }

    #[test]
    fn campaign_without_sound_link_is_skipped() {
        let mut page = campaign_fixture();
        page["properties"]["TikTok Sound Link"] = json!({ "url": null });
        assert!(parse_campaign(&page).is_none());
    }

    #[test]
    fn sound_label_fallbacks() {
        assert_eq!(build_sound_label("A", "S"), "A - S");
        assert_eq!(build_sound_label("A", ""), "A");
        assert_eq!(build_sound_label("", "S"), "S");
        assert_eq!(build_sound_label(" ", ""), "Unknown Campaign");
    }

    fn master_page_fixture() -> Value {
        json!({
            "id": "notion-page-9",
            "properties": {
                "Account Username": { "title": [
                    { "plain_text": "cool" }, { "plain_text": ".page" }
                ] },
                "Page URL": { "url": "https://tiktok.com/@cool.page" },
                "email": { "email": "cool@pages.io" },
                "fwd address": { "rich_text": [ { "plain_text": "fwd@x.io" } ] },
                "Password": { "rich_text": [ { "plain_text": "hunter2" } ] },
                "Poster": { "rich_text": [ { "plain_text": "Seffra" } ] },
                "Group": { "select": { "name": "ATLANTIC" } },
                "Group ": { "select": { "name": "Sam Barber (Atlantic)" } },
                "Account Type": { "select": { "name": "UGC" } },
                "Notes": { "rich_text": [] },
                "Status": { "select": { "name": "Active" } },
                "Pipeline": { "select": null },
                "Page Type": { "select": { "name": "Fan Page" } },
                "Sounds Reference": { "url": null },
                "Go-Live Date": { "date": { "start": "2026-05-01" } },
                "Drive Folder URL": { "url": "https://drive.google.com/drive/folders/1AbC" }
            }
        })
    }

    #[test]
    fn parses_a_master_page_row() {
        let p = parse_master_page(&master_page_fixture()).unwrap();
        assert_eq!(p.integration_id, "acct:cool-page");
        assert_eq!(p.name, "cool.page", "title fragments joined");
        assert_eq!(p.provider, "tiktok");
        assert_eq!(p.poster_name, "Seffra");
        assert_eq!(p.group, "ATLANTIC");
        assert_eq!(p.group_label, "Sam Barber (Atlantic)");
        assert_eq!(p.signup_email, "cool@pages.io");
        assert_eq!(p.password, "hunter2");
        assert_eq!(p.status, "Active");
        assert_eq!(p.pipeline, "", "null select → empty string");
        assert_eq!(p.sounds_reference, "");
        assert_eq!(p.go_live_date, "2026-05-01");
        assert_eq!(p.drive_folder_url, "https://drive.google.com/drive/folders/1AbC");
    }

    #[test]
    fn master_page_without_username_is_skipped() {
        let mut page = master_page_fixture();
        page["properties"]["Account Username"] = json!({ "title": [] });
        assert!(parse_master_page(&page).is_none());
    }

    #[test]
    fn slugify_matches_the_old_rules() {
        assert_eq!(slugify("Cool.Page 99"), "cool-page-99");
        assert_eq!(slugify("  --  "), "unknown");
        assert_eq!(slugify("ALL_CAPS!"), "all-caps");
        assert_eq!(mint_integration_id("My Page"), "acct:my-page");
    }
}
