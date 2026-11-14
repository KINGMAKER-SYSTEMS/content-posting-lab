"""The relaxed image-video treatment gate (F-LAB-IMAGE-VIDEO-GRADE-1104).

The closed plan's ``brightnessGainFromOriginals`` is provenance for the later
managed source-treatment boundary, never the one grade a Dossier may save. This
suite pins the gate: any filter values the visual normalizer accepts are
admitted once clipSpeed is 1 and clipCrop is neutral; malformed treatment,
non-1 speed, non-neutral crop, out-of-range filters and unknown pages stay
refused; and the legacy 0.85 recipe/plan bytes and hashes are unchanged.
"""

import hashlib
import json

import pytest

import services.control_plane_image_video as iv
import services.control_plane_slideshows as ss
from services.source_treatment import NEUTRAL_CROP, normalized_visual_treatment
from tests.test_control_plane_image_video_execution import image_lab

PENPAL = "acct:pen-pal3"
TWIN = "acct:twin47996"
UNKNOWN = "acct:someone-else"

# Recorded at base (origin/main 291fc5c) before any change: the approved photo
# plan authority is deterministic and must stay byte-identical.
BASE_PLAN_SIGNATURES = {
    PENPAL: "9b2b68c3a69e99cbbf1e5bb8f1f263e43d8590e93052c9fc68586f326e0e7aa5",
    TWIN: "d65c9f465a6af985195ebf25462405a2bae8665b0fa5f65470ce5d1cbde322ac",
}

# Mon Rovia targets: the exact Dossier grades Eric wants iterated.
ADDENDUM_GRADES = {
    PENPAL: {"brightness": 0.55, "saturation": 1.25, "grain": 0.10, "fade": 0.05},
    TWIN: {"brightness": 0.60, "saturation": 1.20, "grain": 0.10, "fade": 0.05},
}


def _treatment(filters, *, clip_speed=1.0, clip_crop=None):
    return {
        "stylePreset": None,
        "filters": dict(filters),
        "captionStyle": {},
        "clipSpeed": clip_speed,
        "clipCrop": dict(clip_crop) if clip_crop is not None else dict(NEUTRAL_CROP),
    }


def _spec(treatment):
    return {"renderTreatment": treatment}


# --- direct gate unit tests -------------------------------------------------

def test_penpal_lower_brightness_higher_saturation_accepted():
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 0.6, "saturation": 1.15})), PENPAL,
    ) is True


def test_twin_accepted_at_neutral_and_below_neutral_brightness():
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 1.0})), TWIN,
    ) is True
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 0.9})), TWIN,
    ) is True


def test_addendum_grades_normalize_and_are_accepted():
    for page, filters in ADDENDUM_GRADES.items():
        assert iv.required_image_video_treatment(_spec(_treatment(filters)), page) is True
        normalized = normalized_visual_treatment(_treatment(filters))
        assert normalized["filters"]["brightness"] == filters["brightness"]
        assert normalized["filters"]["saturation"] == filters["saturation"]
        assert normalized["filters"]["grain"] == filters["grain"]
        assert normalized["filters"]["fade"] == filters["fade"]


def test_non_neutral_speed_refused():
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 0.85}, clip_speed=1.2)), PENPAL,
    ) is False


def test_non_neutral_crop_refused():
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 0.85},
                         clip_crop={"zoom": 1.2, "focusX": 0.5, "focusY": 0.5})),
        PENPAL,
    ) is False


def test_unknown_page_refused():
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 0.85})), UNKNOWN,
    ) is False


def test_out_of_range_brightness_refused_by_normalizer():
    with pytest.raises(ValueError):
        normalized_visual_treatment(_treatment({"brightness": 3.5}))
    assert iv.required_image_video_treatment(
        _spec(_treatment({"brightness": 3.5})), PENPAL,
    ) is False


def test_malformed_treatment_refused():
    assert iv.required_image_video_treatment(
        _spec({"filters": {"unknown": 1.0}, "clipSpeed": 1.0}), PENPAL,
    ) is False
    assert iv.required_image_video_treatment(
        {"renderTreatment": None}, PENPAL,
    ) is False
    assert iv.required_image_video_treatment({}, PENPAL) is False


def test_plan_authority_bytes_unchanged_from_base():
    for page, signature in BASE_PLAN_SIGNATURES.items():
        assert iv.image_video_plan(page)["signature"] == signature


# --- admission-path integration tests --------------------------------------

def _recanonicalize(payload, spec):
    """Rebuild recipeSpecCanonical/hash for a mutated spec dict."""
    payload = dict(payload)
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return payload


def test_085_recipe_and_plan_bytes_identical_both_pages(image_lab):
    client, library, payload, photo = image_lab
    page = photo["pageId"]
    # Plan authority (bytes + signature) is untouched by the gate relaxation.
    assert iv.image_video_plan(page)["signature"] == BASE_PLAN_SIGNATURES[page]
    spec = json.loads(payload["recipeSpecCanonical"])
    assert spec["renderTreatment"]["filters"] == {"brightness": 0.85}
    # The 0.85 recipe is admitted with its exact bytes and hash round-tripping.
    recipe = ss.resolve_slideshow_recipe(payload, require_runtime=False)
    assert recipe is not None
    assert recipe.recipe_spec == spec
    assert "sha256:" + hashlib.sha256(payload["recipeSpecCanonical"].encode()).hexdigest() \
        == payload["recipeSpecHash"]


def test_addendum_recipe_carries_exact_grade_values(image_lab):
    client, library, payload, photo = image_lab
    page = photo["pageId"]
    filters = ADDENDUM_GRADES[page]
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"] = filters
    payload = _recanonicalize(payload, spec)
    recipe = ss.resolve_slideshow_recipe(payload, require_runtime=False)
    assert recipe is not None
    # The Dossier grade is preserved exactly on the accepted recipe.
    assert recipe.recipe_spec["renderTreatment"]["filters"] == filters
    # Both are reachable only at speed 1 / neutral crop (already enforced).
    assert iv.required_image_video_treatment(spec, page) is True


def test_admission_refuses_non_neutral_speed_and_crop(image_lab):
    client, library, payload, photo = image_lab
    page = photo["pageId"]
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["clipSpeed"] = 1.2
    assert ss.resolve_slideshow_recipe(_recanonicalize(payload, spec), require_runtime=False) is None
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["clipCrop"] = {"zoom": 1.2, "focusX": 0.5, "focusY": 0.5}
    assert ss.resolve_slideshow_recipe(_recanonicalize(payload, spec), require_runtime=False) is None


def test_admission_refuses_out_of_range_brightness(image_lab):
    client, library, payload, photo = image_lab
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"] = {"brightness": 3.5}
    assert ss.resolve_slideshow_recipe(_recanonicalize(payload, spec), require_runtime=False) is None
