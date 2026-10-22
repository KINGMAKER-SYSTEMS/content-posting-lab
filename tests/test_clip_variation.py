from services.clip_variation import (
    CLIP_VARIATION_PAGES, FAST_SPEEDS, OUTPUT_MAX_MS, OUTPUT_MIN_MS, SLOW_SPEEDS, cut_variant, varies_clips,
)
from services.control_plane_sources import CUT_LENGTH_MAX_MS, CUT_LENGTH_MIN_MS, CUT_LENGTH_STEP_MS

BTL = "acct:between-the-lines66"
CROP = {"zoom": 1.63, "focusX": 0.5, "focusY": 0.5}
MASTER = "a" * 64


def test_only_the_listed_page_varies():
    assert CLIP_VARIATION_PAGES == {BTL}
    assert varies_clips(BTL)
    assert not varies_clips("acct:codyjames6-7")


def test_variant_is_deterministic_per_cut_and_varies_across_cuts():
    cuts = [f"{MASTER}:{start}:8000" for start in range(0, 400_000, 8_000)]
    variants = [cut_variant(BTL, cut, 1.0, CROP, duration_ms=8000) for cut in cuts]
    assert variants == [cut_variant(BTL, cut, 1.0, CROP, duration_ms=8000) for cut in cuts]
    speeds = [speed for speed, _ in variants]
    assert all(speed in SLOW_SPEEDS + FAST_SPEEDS for speed in speeds)
    assert sum(speed < 1 for speed in speeds) > len(speeds) * 0.6
    assert any(speed > 1 for speed in speeds)
    assert len({(speed, tuple(crop.values())) for speed, crop in variants}) == len(variants)
    for _, crop in variants:
        assert 1.43 <= crop["zoom"] <= 1.83
        assert 0.42 <= crop["focusX"] <= 0.58 and 0.42 <= crop["focusY"] <= 0.58


def test_variant_never_repeats_the_recipe_look_or_the_previous_cut():
    cut = f"{MASTER}:0:8000"
    first = cut_variant(BTL, cut, 1.0, CROP, duration_ms=8000)
    assert first != (1.0, CROP)
    again = cut_variant(BTL, cut, 1.0, CROP, previous=first, duration_ms=8000)
    assert again != first
    # The recipe's own look is avoided even when the recipe is itself slow.
    slow_recipe = cut_variant(BTL, cut, first[0], first[1], duration_ms=8000)
    assert slow_recipe != first


def test_every_planned_window_length_delivers_a_clip_the_worker_admits():
    # Worker sourceVideoBounds.js admits 5-10 s. Every window length the planner
    # can choose, at every speed a varied cut can draw, stays inside it.
    lengths = range(CUT_LENGTH_MIN_MS, CUT_LENGTH_MAX_MS + 1, CUT_LENGTH_STEP_MS)
    for duration_ms in lengths:
        speeds = set()
        for start in range(0, 600_000, 3_000):
            speed, _ = cut_variant(BTL, f"{MASTER}:{start}:{duration_ms}", 1.0, CROP, duration_ms=duration_ms)
            speeds.add(speed)
            assert OUTPUT_MIN_MS <= duration_ms / speed <= OUTPUT_MAX_MS, (duration_ms, speed)
            assert 5_000 <= duration_ms / speed <= 10_000
        assert len(speeds) > 1, duration_ms


def test_cut_grade_stays_inside_the_bands_around_the_recipe():
    from services.clip_variation import GRADE_BANDS, cut_grade
    recipe = {"brightness": 1.1, "contrast": 0.95, "fade": 0.1}
    moved = set()
    for start in range(0, 600_000, 2_000):
        cut = f"{MASTER}:{start}:8000"
        graded = cut_grade(BTL, cut, recipe)
        assert graded == cut_grade(BTL, cut, recipe)
        assert graded["fade"] == 0.1, "a filter outside the bands is never touched"
        for name, band in GRADE_BANDS.items():
            centre = recipe.get(name, 0.0 if name == "warmth" else 1.0)
            assert abs(graded[name] - centre) <= band, (name, graded[name])
        moved.add(graded["brightness"])
    assert len(moved) > 20, "the grade actually varies"


def test_the_receipt_records_the_grade_actually_applied():
    from services.source_treatment import source_treatment_receipt
    job = {"jobId": "generation-1", "recipeSpecHash": "sha256:" + "a" * 64}
    recipe = {"filters": {"brightness": 1.1}, "clipSpeed": 1.0}
    receipt = source_treatment_receipt(job, recipe, "b" * 64, clip_speed=0.9, clip_crop=None,
                                       filters={"brightness": 1.12, "warmth": -0.02})
    assert receipt["visualTreatment"]["filters"]["brightness"] == 1.12
    assert receipt["visualTreatment"]["filters"]["warmth"] == -0.02
    assert receipt["sourceRecipeTreatment"]["filters"] == {"brightness": 1.1}, "the recipe stays the recipe"
