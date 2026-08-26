"""The live Master Pages ontology must survive Notion parsing verbatim."""
from services.notion_pages import parse_page


def test_live_generation_and_bucket_ontology_is_parsed():
    row = parse_page({
        "id": "notion-page-1",
        "properties": {
            "Account Username": {"title": [{"plain_text": "backroaddriver"}]},
            "Label": {"select": {"name": "INTERNAL"}},
            "Account Group": {"select": {"name": "Internal Page"}},
            "Content Niche ": {"select": {"name": "TRUCK"}},
            "ContentEngine": {"select": {"name": "ai_video"}},
            "Automation vs Operator": {"select": {"name": "Automation"}},
            "Account Status": {"select": {"name": "active"}},
            "Vault Link": {"url": "https://shipstream.risingtidesviral.com/vault/backroaddriver"},
            "Files & media": {"files": [{"name": "OPEN VIDEO BUCKET", "type": "external", "external": {"url": "https://fallback.invalid/vault"}}]},
            "Poster": {"multi_select": [{"name": "Eric"}, {"name": "Sam"}]},
            "fwd address": {"multi_select": [{"name": "forwarding-label"}]},
            "Archived": {"checkbox": False},
        },
    })
    assert row is not None
    assert row["integration_id"] == "acct:backroaddriver"
    assert row["content_niche"] == "TRUCK"
    assert row["content_engine"] == "ai_video"
    assert row["automation_mode"] == "Automation"
    assert row["status"] == "active"
    assert row["account_status"] == "active"
    assert row["vault_url"] == "https://shipstream.risingtidesviral.com/vault/backroaddriver"
    assert row["poster_name"] == "Eric, Sam"
    assert row["fwd_address"] == "forwarding-label"
    assert row["archived"] is False


def test_files_media_external_url_is_bucket_fallback():
    row = parse_page({
        "id": "notion-page-2",
        "properties": {
            "Account Username": {"title": [{"plain_text": "page.two"}]},
            "Files & media": {"files": [{"name": "OPEN VIDEO BUCKET", "type": "external", "external": {"url": "https://shipstream.risingtidesviral.com/vault/page.two"}}]},
        },
    })
    assert row is not None
    assert row["vault_url"] == "https://shipstream.risingtidesviral.com/vault/page.two"
