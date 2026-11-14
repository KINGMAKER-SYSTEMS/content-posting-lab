"""Independent regressions from Kimi's pinned reader review."""
import pytest
from services import scrambler_originality as o
from tests.test_scrambler_originality_boundaries import build, show, SHA, NOW


def test_lab_id_is_not_an_implicit_handle_alias(tmp_path):
    index = build(tmp_path, [show(page="@labpage", days=8)])
    allowed, reason = index.window_allowed("labpage", SHA, 1200000, 1203000,
        page_handles=frozenset({"@different.page"}), now_epoch=NOW.timestamp())
    assert not allowed
    assert reason.startswith("cross_page_90d")


@pytest.mark.parametrize(("start", "end", "duration", "expected_start", "expected_end"), [
    (1.001, 2.002, 1000, 1001, 2001),
    (0, 1.001, 1001, 0, 1001),
])
def test_decimal_millisecond_mapping_does_not_gain_binary_rounding_error(
    tmp_path, start, end, duration, expected_start, expected_end,
):
    index = build(tmp_path, mapping={"page": "@pilot", "origin_kind": "drive",
        "origin_id": "CaseSensitiveID", "origin_start_s": start, "origin_end_s": end})
    try:
        actual = index.canonical_origin_window(SHA, 0, duration)
    except o.ScramblerOriginalityUnavailable:
        actual = None  # refusing this valid mapping is a behavioral failure
    assert actual == ("drive", "CaseSensitiveID", expected_start, expected_end)
    assert index.window_allowed("acct:pilot", SHA, expected_start, expected_end,
        now_epoch=NOW.timestamp())[0]


def test_true_fractional_millisecond_offset_still_refuses(tmp_path):
    index = build(tmp_path, mapping={"page": "@pilot", "origin_kind": "drive",
        "origin_id": "CaseSensitiveID", "origin_start_s": 1.0015, "origin_end_s": 2.0025})
    with pytest.raises(o.ScramblerOriginalityUnavailable):
        index.canonical_origin_window(SHA, 0, 1000)


def test_nonhex_retirement_digest_refuses_whole_feed(tmp_path):
    with pytest.raises(o.ScramblerOriginalityUnavailable, match="retired_list_invalid"):
        build(tmp_path, retired=[{"master_sha256s": ["z" * 64]}])


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_query_clock_never_unlocks_recent_cross_page_history(tmp_path, clock):
    index = build(tmp_path, [show(days=1)])
    try:
        allowed, _ = index.window_allowed("acct:pilot", SHA, 1200000, 1203000,
            now_epoch=clock)
    except o.ScramblerOriginalityUnavailable:
        return
    assert not allowed


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_loader_clock_refuses(tmp_path, clock):
    build(tmp_path, [show(days=1)])
    with pytest.raises(o.ScramblerOriginalityUnavailable):
        o.load_originality_index(tmp_path / "usage.jsonl",
            retired_path=tmp_path / "retired.json", now_epoch=clock)


def test_conflicting_normalized_master_mapping_refuses_whole_feed(tmp_path):
    import json
    build(tmp_path)
    path = tmp_path / "origins.json"
    origins = json.loads(path.read_text())
    origins["sha256:" + SHA.upper()] = {
        **origins[SHA], "origin_id": "ConflictingCanonicalOrigin",
    }
    path.write_text(json.dumps(origins))
    with pytest.raises(o.ScramblerOriginalityUnavailable, match="origins_map_invalid"):
        o.load_originality_index(tmp_path / "usage.jsonl",
            retired_path=tmp_path / "retired.json", now_epoch=NOW.timestamp())


@pytest.mark.parametrize("point", [1200, 1200.5])
def test_empty_known_origin_posted_window_refuses_feed(tmp_path, point):
    # Confirmed posting + known identity with an impossible empty window
    # cannot disappear as zero indexed seconds or vary with second alignment.
    with pytest.raises(o.ScramblerOriginalityUnavailable, match="usage_row_invalid"):
        build(tmp_path, [show(page="@other", days=1, start=point, end=point)])


@pytest.mark.parametrize("page", ["", "   ", "\t", "@pilot\n", "@pilot\x1b", "@pilot\x85", "x" * 257, "@pilot\ud800"])
def test_noncopyable_producer_page_refuses_typed_feed(tmp_path, page):
    # A producer observation must remain valid when copied into a durable
    # private hold; do not defer invalid page text to JSON/UTF-8 persistence.
    with pytest.raises(o.ScramblerOriginalityUnavailable, match="usage_row_invalid.*page"):
        build(tmp_path, [show(page=page, days=1)])


@pytest.mark.parametrize("page", ["acct:pilot", "  valid raw label  ", "@PiLoT", "x" * 256])
def test_copyable_producer_page_preserves_verbatim_observation(tmp_path, page):
    asset = "c" * 64
    row = {**show(page=page, days=1), "asset_sha": asset}
    index = build(tmp_path, [row])
    observation = index.posted_asset_observation(
        asset, "drive", "CaseSensitiveID", 1200000, 1203000)
    assert observation is not None
    assert observation["page"] == page
