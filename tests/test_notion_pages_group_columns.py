"""parse_page must read the group taxonomy from the columns the live DB
actually has.

The Master Pages DB names them "Label" (ATLANTIC / WARNER / INTERNAL) and
"Account Group" ("Warner UGC", "Mon Rovia", …). An earlier parse read only
the legacy "Group" / "Group " names, so a sync could report success
(added/updated counts, no errors) while writing an empty group for every
row — observed in production 2026-08-18, when the roster snapshot's groups
collapsed from 30 pages to 3 immediately after a "successful" sync.
"""

from services.notion_pages import parse_page


def _page(**props):
    properties = {"Account Username": {"title": [{"plain_text": "some.page"}]}}
    properties.update(props)
    return {"id": "page-1", "properties": properties}


def test_group_comes_from_label_and_group_label_from_account_group():
    row = parse_page(_page(
        **{
            "Label": {"select": {"name": "WARNER"}},
            "Account Group": {"select": {"name": "Warner UGC"}},
        }
    ))
    assert row["group"] == "WARNER"
    assert row["group_label"] == "Warner UGC"


def test_legacy_column_names_still_parse():
    row = parse_page(_page(
        **{
            "Group": {"select": {"name": "ATLANTIC"}},
            "Group ": {"select": {"name": "Sam Barber"}},
        }
    ))
    assert row["group"] == "ATLANTIC"
    assert row["group_label"] == "Sam Barber"


def test_canonical_wins_when_both_exist():
    row = parse_page(_page(
        **{
            "Label": {"select": {"name": "WARNER"}},
            "Group": {"select": {"name": "INTERNAL"}},
        }
    ))
    assert row["group"] == "WARNER"


def test_content_niche_reads_the_trailing_space_column():
    row = parse_page(_page(**{"Content Niche ": {"select": {"name": "TRUCK"}}}))
    assert row["content_niche"] == "TRUCK"


def test_content_niche_absent_is_empty_not_missing():
    row = parse_page(_page())
    assert "content_niche" in row
    assert row["content_niche"] == ""
