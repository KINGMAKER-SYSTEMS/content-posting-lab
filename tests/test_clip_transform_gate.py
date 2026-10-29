"""Gate tests for the Layer B transform allow-list (services/clip_transforms.py).

Layer A (services/clip_variation.py CLIP_VARIATION_PAGES) cuts a page's clips
at their own speed/crop/grade; Layer B (mirror / motion / reverse) is wider and
turned on for every sourced source-video cut format, unless a page is on
TRANSFORM_DENY_PAGES so its clips stay exactly as delivered. Reverse can be
dropped with one constant so a cost call stays mirror+motion only.
"""
import pytest
from pathlib import Path

import services.clip_transforms as ct
from services.clip_transforms import (
    CutTransforms,
    TRANSFORM_DENY_PAGES,
    TRANSFORMS_REVERSE_ENABLED,
    transforms_enabled,
)

BTL = "acct:between-the-lines66"
SOURCED_PAGE = "tt-chase-miles-4l"


def test_transforms_are_enabled_for_sourced_source_video_cut_formats():
    for slug in ("meme", "pov-club", "pov-dirt-bike", "pov-dusk-core",
                 "pov-night-core", "pov-scenic"):
        assert transforms_enabled(SOURCED_PAGE, slug) is True, slug


def test_transforms_are_off_for_a_deny_listed_page(monkeypatch):
    monkeypatch.setattr(ct, "TRANSFORM_DENY_PAGES", frozenset({SOURCED_PAGE}))
    assert transforms_enabled(SOURCED_PAGE, "pov-club") is False
    # A different page on the same format is unaffected.
    assert transforms_enabled("acct:other", "pov-club") is True


def test_transforms_are_off_for_non_source_cut_formats():
    for slug in ("boat-lake", "coffee-tok", "image-video", "lyric-video",
                 "meme-slideshow", "single-image-video", "truck-scenic",
                 "ugc-slideshow", "not-a-format"):
        assert transforms_enabled(SOURCED_PAGE, slug) is False, slug
    assert transforms_enabled(BTL, "boat-lake") is False


def test_transforms_deny_list_starts_empty():
    # No client-floor / campaign-final lane exists in this build, so nothing is
    # deny-listed yet; the set is empty until evidence names a page.
    assert TRANSFORM_DENY_PAGES == frozenset()


def test_reverse_enabled_constant_defaults_true():
    assert TRANSFORMS_REVERSE_ENABLED is True


def _draws(seeds):
    return [ct._draw(seed) for seed in seeds]


def test_reverse_off_drops_reverse_and_keeps_mirror_and_motion(monkeypatch):
    seeds = [f"seed-{index}" for index in range(2000)]
    # With reverse on, some seed draws reverse (share 1/4 over 2000 seeds).
    monkeypatch.setattr(ct, "TRANSFORMS_REVERSE_ENABLED", True)
    with_reverse = _draws(seeds)
    assert any(drawn.reverse for drawn in with_reverse)
    assert any(drawn.mirror for drawn in with_reverse)
    assert any(drawn.motion is not None for drawn in with_reverse)

    # With reverse off, no seed ever draws reverse; mirror and motion are the
    # same draws they were before (the seed scheme is untouched).
    monkeypatch.setattr(ct, "TRANSFORMS_REVERSE_ENABLED", False)
    without_reverse = _draws(seeds)
    assert all(not drawn.reverse for drawn in without_reverse)
    assert [drawn.mirror for drawn in without_reverse] == [
        drawn.mirror for drawn in with_reverse
    ]
    assert [drawn.motion for drawn in without_reverse] == [
        drawn.motion for drawn in with_reverse
    ]
    assert any(drawn.mirror for drawn in without_reverse)
    assert any(drawn.motion is not None for drawn in without_reverse)


def test_reverse_off_never_builds_a_reverse_transform(monkeypatch):
    # cut_transforms() honours the same flag: reverse stays off in every drawn
    # combination, while mirror and motion remain reachable.
    monkeypatch.setattr(ct, "TRANSFORMS_REVERSE_ENABLED", False)
    drawn = []
    for index in range(2000):
        value = ct.cut_transforms(SOURCED_PAGE, f"cut-{index}", None)
        drawn.append(value)
        assert value.reverse is False
    assert any(value.mirror for value in drawn)
    assert any(value.motion is not None for value in drawn)


def test_reverse_on_can_draw_a_reverse_combination():
    assert TRANSFORMS_REVERSE_ENABLED is True
    seen = [ct.cut_transforms(SOURCED_PAGE, f"cut-{index}", None)
            for index in range(2000)]
    assert any(value.reverse for value in seen)
    assert all(set(value.manifest()) == {"mirror", "motion", "reverse", "loop_swap"}
               for value in seen)


def test_transforms_deny_pages_are_read_from_env_and_merged_with_code(monkeypatch):
    monkeypatch.setenv("TRANSFORM_DENY_PAGES", " acct:one ,acct:two, ")
    assert ct._transforms_deny_pages() == frozenset({"acct:one", "acct:two"})
    monkeypatch.setattr(ct, "CODE_TRANSFORM_DENY_PAGES", frozenset({"acct:code"}))
    assert ct._transforms_deny_pages() == frozenset({"acct:one", "acct:two", "acct:code"})


def test_transforms_all_defaults_on_and_reads_env(monkeypatch):
    monkeypatch.delenv("TRANSFORMS_ALL", raising=False)
    assert ct._transforms_all() is True
    # Every off spelling (case-insensitive, stripped) turns Layer B off.
    for off in ("0", "false", "off", "no", " FALSE ", "OFF", "No"):
        monkeypatch.setenv("TRANSFORMS_ALL", off)
        assert ct._transforms_all() is False, off
    # Anything else — including an empty value or unset — keeps Layer B on.
    for on in ("1", "true", "yes", "on", "", "garbage"):
        monkeypatch.setenv("TRANSFORMS_ALL", on)
        assert ct._transforms_all() is True, on
    monkeypatch.delenv("TRANSFORMS_ALL", raising=False)
    assert ct._transforms_all() is True


def test_transforms_all_off_disables_the_layer_b_gate(monkeypatch):
    slots = [f"{'c' * 64}:{index * 8000}:6000" for index in range(200)]
    # With the switch on (the default), a non-vary_look sourced page draws a
    # transform for at least one seed, so the gate is what keeps it off.
    assert ct.TRANSFORMS_ALL is True
    assert any(ct.cut_transforms(SOURCED_PAGE, slot, None).any() for slot in slots)
    monkeypatch.setattr(ct, "TRANSFORMS_ALL", False)
    for slug in ct.SOURCE_CUT_FORMATS:
        assert transforms_enabled(SOURCED_PAGE, slug) is False, slug
    assert transforms_enabled(SOURCED_PAGE, "boat-lake") is False
    # Restoring the switch restores Layer B.
    monkeypatch.setattr(ct, "TRANSFORMS_ALL", True)
    assert transforms_enabled(SOURCED_PAGE, "pov-club") is True


def test_env_switch_wording_names_the_railway_restart_not_a_redeploy():
    # The switches are read once at service start, so a variable change takes
    # effect on the Railway restart that follows it — never "without a
    # redeploy". This pins the comments stay honest.
    source = Path(ct.__file__).read_text()
    assert "without a redeploy" not in source
    assert "without a code deploy" not in source
    assert "Railway service restart" in source
