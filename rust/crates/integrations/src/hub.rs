//! Campaign Hub client.
//!
//! The Hub is the source of truth for **which campaigns are active** — a
//! campaign is active while `completion_status != "completed"`. The sound
//! sync pairs this with Notion (which owns the TikTok Sound Links).

use serde_json::Value;

use crate::http::client;

/// Base URL, env-overridable (defaults to the deployed Hub — same as the old
/// service, which meant `is_configured()` was effectively always true).
fn base_url() -> String {
    std::env::var("CAMPAIGN_HUB_URL")
        .unwrap_or_else(|_| {
            "https://risingtides-campaign-hub-production.up.railway.app".into()
        })
        .trim_end_matches('/')
        .to_string()
}

/// Whether the Hub integration is available.
pub fn configured() -> bool {
    !base_url().is_empty()
}

/// One Hub campaign, in the fields the sound sync cares about. Missing fields
/// decode as empty strings (the old code used `.get(..., "")` everywhere).
#[derive(Debug, Clone, PartialEq)]
pub struct HubCampaign {
    pub slug: String,
    pub title: String,
    pub artist: String,
    pub song: String,
    pub completion_status: String,
}

impl HubCampaign {
    /// Active = not explicitly completed (unknown statuses count as active).
    pub fn is_active(&self) -> bool {
        self.completion_status != "completed"
    }
}

fn field(v: &Value, key: &str) -> String {
    v[key].as_str().unwrap_or("").to_string()
}

/// Parse the `GET /api/campaigns` payload (a JSON array of campaign objects).
pub fn parse_campaigns(v: &Value) -> Vec<HubCampaign> {
    v.as_array()
        .map(|items| {
            items
                .iter()
                .map(|c| HubCampaign {
                    slug: field(c, "slug"),
                    title: field(c, "title"),
                    artist: field(c, "artist"),
                    song: field(c, "song"),
                    completion_status: field(c, "completion_status"),
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Fetch every campaign from the Hub.
pub async fn fetch_all_campaigns() -> anyhow::Result<Vec<HubCampaign>> {
    let resp = client()
        .get(format!("{}/api/campaigns", base_url()))
        .send()
        .await?;
    let status = resp.status();
    let body: Value = resp.json().await?;
    if !status.is_success() {
        anyhow::bail!("campaign hub {status}");
    }
    Ok(parse_campaigns(&body))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_campaign_list_with_missing_fields() {
        let fixture = json!([
            { "slug": "alex-nicol-waiting", "title": "Alex Nicol - Waiting",
              "artist": "Alex Nicol", "song": "Waiting", "completion_status": "active" },
            { "slug": "done-one", "artist": "Done", "song": "One",
              "completion_status": "completed" },
            { "slug": "sparse" }
        ]);
        let campaigns = parse_campaigns(&fixture);
        assert_eq!(campaigns.len(), 3);

        assert!(campaigns[0].is_active());
        assert_eq!(campaigns[0].title, "Alex Nicol - Waiting");

        assert!(!campaigns[1].is_active());
        assert_eq!(campaigns[1].title, "", "missing title → empty");

        assert!(campaigns[2].is_active(), "missing status counts as active");
        assert_eq!(campaigns[2].artist, "");
    }

    #[test]
    fn non_array_payload_parses_empty() {
        assert!(parse_campaigns(&json!({"error": "nope"})).is_empty());
    }
}
