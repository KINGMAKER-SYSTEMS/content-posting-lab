from services.clip_variation import (
    CLIP_VARIATION_PAGES, FAST_SPEEDS, SLOW_SPEEDS, cut_variant, varies_clips,
)

BTL = "acct:between-the-lines66"
CROP = {"zoom": 1.63, "focusX": 0.5, "focusY": 0.5}
MASTER = "a" * 64


def test_only_the_listed_page_varies():
    assert CLIP_VARIATION_PAGES == {BTL}
    assert varies_clips(BTL)
    assert not varies_clips("acct:codyjames6-7")


def test_variant_is_deterministic_per_cut_and_varies_across_cuts():
    cuts = [f"{MASTER}:{start}:8000" for start in range(0, 400_000, 8_000)]
    variants = [cut_variant(BTL, cut, 1.0, CROP) for cut in cuts]
    assert variants == [cut_variant(BTL, cut, 1.0, CROP) for cut in cuts]
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
    first = cut_variant(BTL, cut, 1.0, CROP)
    assert first != (1.0, CROP)
    again = cut_variant(BTL, cut, 1.0, CROP, previous=first)
    assert again != first
    # The recipe's own look is avoided even when the recipe is itself slow.
    slow_recipe = cut_variant(BTL, cut, first[0], first[1])
    assert slow_recipe != first
