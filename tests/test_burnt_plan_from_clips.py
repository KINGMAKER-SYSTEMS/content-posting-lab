"""The Burnt cutter-manifest handoff must remain isolated and loadable."""
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from services import control_plane_slideshows as slides

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/burnt_plan_from_clips.py"
FIXTURE = ROOT / "tests/fixtures/burnt-clips-40.json"
UGC = ROOT / "recipes/executors/syzygy-slideshow-ugc.v1.json"
LYRIC = ROOT / "recipes/executors/syzygy-slideshow-lyric-video.v1.json"
REGISTRY = ROOT / "recipes/content-engine-registry.v1.json"
CONTRACTS = ROOT / "recipes/content-format-contracts.v1.json"
BURNT = "acct:burnt-mill-rd"


def run(manifest, *args):
    return subprocess.run([sys.executable, str(TOOL), str(manifest), *args], text=True, capture_output=True)


def test_write_requires_pins_and_leaves_every_authority_file_unchanged():
    before = {path: path.read_bytes() for path in (UGC, LYRIC, REGISTRY, CONTRACTS)}
    result = run(FIXTURE, "--write")
    assert result.returncode == 2
    assert "must be used together" in result.stderr
    assert before == {path: path.read_bytes() for path in before}


def test_candidate_uses_only_each_variant_window_keys_and_requires_all_sources():
    before = {path: path.read_bytes() for path in (UGC, REGISTRY, CONTRACTS)}
    try:
        result = run(FIXTURE, "--write", "--write-pins")
        assert result.returncode == 0, result.stderr
        slides._curated_variant_table.cache_clear()
        curated = slides.ugc_page_treatment(BURNT)["curatedVideoPlan"]
        variants = [plan for plan in slides.curated_variant_plans(curated) if plan]
        assert len(variants) == 200
        for plan in variants:
            assert set(plan["mediaKeys"]) == {window["key"] for window in plan["windows"]}
        raw = slides.UGC_EXECUTOR_PATH.read_bytes()
        objects = tuple(slides.SyzygyObject(
            f"slideshow-webweaving/burnt-mill-rd/clips/{source['file']}",
            source["bytes"], "https://example.invalid/x",
        ) for source in curated["sources"])
        recipe = slides.SlideshowRecipe(
            "ugc-slideshow:master", "ugc-slideshow", "sourced_slideshow", "e" * 64,
            "sha256:" + "f" * 64, "image_library", "video/mp4",
            "sha256:" + hashlib.sha256(raw).hexdigest(), "syzygy:slideshow-webweaving:burnt-mill-rd",
            "slideshow-webweaving", "burnt-mill-rd", 3,
            (slides.SyzygyTemplate("curated-video-montage", 3),), 1, 1080, 1920,
            "tiktok_delivery_v1", {"masterPages": {"pageId": BURNT}},
        )
        first = {window["source"] for window in curated["windows"]}
        thin = replace(slides.SyzygyLibrary(recipe.library_id, recipe.lane, recipe.subject, objects, "e" * 64),
                       objects=tuple(item for item in objects if item.key.rsplit("/", 1)[-1] in first))
        assert slides.curated_plans_for_recipe(recipe, thin) == []
    finally:
        for path, raw in before.items():
            path.write_bytes(raw)
        slides._curated_variant_table.cache_clear()


def test_invalid_long_name_refuses_before_writing_any_authority_file(tmp_path):
    manifest = json.loads(FIXTURE.read_text())
    manifest["clips"][0]["file"] = "a" * 201 + ".mp4"
    path = tmp_path / "long.json"
    path.write_text(json.dumps(manifest))
    before = {item: item.read_bytes() for item in (UGC, LYRIC, REGISTRY, CONTRACTS)}
    result = run(path, "--write", "--write-pins")
    assert result.returncode == 2
    assert "invalid_file_name" in result.stderr
    assert before == {item: item.read_bytes() for item in before}


def test_every_executor_page_pair_guard_is_green_after_reviewed_write(tmp_path):
    plans = []
    for path in sorted((ROOT / "recipes/executors").glob("*.json")):
        value = json.loads(path.read_text())
        for page, treatment in value.get("pageTreatments", {}).items():
            curated = treatment.get("curatedVideoPlan") if isinstance(treatment, dict) else None
            if curated:
                plans.append((path.name, page, {source["sha256"] for source in curated["sources"]}))
    for index, (left_file, left_page, left_shas) in enumerate(plans):
        for right_file, right_page, right_shas in plans[index + 1:]:
            if left_page == right_page:
                continue
            assert not (left_shas & right_shas), (left_file, left_page, right_file, right_page)
    culture = next(shas for _, page, shas in plans if page == "acct:culture-core-1")
    assert culture
    # Mutant: a culture SHA planted in any executor becomes a shared pair.
    mutant = plans + [("source-dna-recut.v1.json", "acct:planted", {next(iter(culture))})]
    assert any(left & right for _, _, left in mutant for _, _, right in mutant if left is not right)
