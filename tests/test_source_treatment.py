import copy

import pytest

from services.source_treatment import (
    derived_source_treatment, normalized_visual_treatment,
    recovery_treatment_matches, source_treatment_receipt,
)


def test_actual_video_receipt_resolves_neutral_defaults_without_claiming_caption_application():
    recipe = {"filters": {"brightness": 0.85}, "captionStyle": {"position": "top"}, "clipSpeed": 1.5}
    result = source_treatment_receipt({"jobId": "generation-1", "recipeSpecHash": "sha256:" + "a" * 64},
        recipe, "b" * 64, clip_speed=1.5, clip_crop=None)
    assert result["sourceSha256"] == "b" * 64
    assert result["visualTreatment"] == {"filters": {"brightness": 0.85, "contrast": 1.0, "saturation": 1.0,
        "warmth": 0.0, "fade": 0.0, "grain": 0.0, "vignette": 0.0}, "clipSpeed": 1.5,
        "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}
    assert "captionStyle" not in result["visualTreatment"]
    assert result["sourceRecipeTreatment"]["captionStyle"] == {"position": "top"}
    recipe["captionStyle"]["position"] = "bottom"
    assert result["sourceRecipeTreatment"]["captionStyle"]["position"] == "top"


def test_recovery_inherits_proven_actual_treatment_and_cannot_invent_missing_history():
    assert derived_source_treatment(None, "a" * 64, "b" * 64, "recovery") is None
    original = source_treatment_receipt({"jobId": "generation-1", "recipeSpecHash": "sha256:" + "c" * 64},
        {"filters": {"saturation": 0.5}}, "a" * 64, clip_speed=0.75, clip_crop=None)
    derived = derived_source_treatment(original, "a" * 64, "b" * 64, "recovery-2")
    assert derived["visualTreatment"] == original["visualTreatment"]
    assert derived["sourceSha256"] == "b" * 64
    assert derived["generationJobId"] == "recovery-2"
    assert derived["derivedFrom"] == {"sourceSha256": "a" * 64, "generationJobId": "generation-1"}
    assert original["sourceSha256"] == "a" * 64
    with pytest.raises(ValueError):
        derived_source_treatment(original, "0" * 64, "b" * 64, "recovery-2")


@pytest.mark.parametrize("treatment", [
    {"filters": {"unsupported": 1}}, {"filters": {"brightness": True}},
    {"filters": {}, "clipSpeed": float("nan")}, {"filters": {}, "clipCrop": {}},
])
def test_visual_provenance_rejects_unknown_and_invalid_applied_values(treatment):
    with pytest.raises(ValueError):
        normalized_visual_treatment(treatment)


def recovery_evidence():
    job = {"jobId": "generation-1", "recipeSpecHash": "sha256:" + "a" * 64}
    treatment = {"filters": {"brightness": 0.85}, "clipSpeed": 0.75,
                 "clipCrop": {"zoom": 1.5, "focusX": 0.2, "focusY": 0.8},
                 "captionStyle": {"position": "top"}}
    receipt = source_treatment_receipt(job, treatment, "b" * 64,
        clip_speed=treatment["clipSpeed"], clip_crop=treatment["clipCrop"])
    return job, treatment, receipt


def test_recovery_accepts_exact_applied_video_and_caption_only_change():
    job, treatment, receipt = recovery_evidence()
    original = copy.deepcopy(receipt)
    assert recovery_treatment_matches(receipt, job, "b" * 64, treatment)
    treatment["captionStyle"] = {"position": "bottom"}
    assert recovery_treatment_matches(receipt, job, "b" * 64, treatment)
    assert receipt == original


@pytest.mark.parametrize("change", [
    "missing", "not_object", "schema", "source_sha", "job_id", "recipe_hash",
    "producer_recipe_hash", "missing_visual", "unknown_field", "incomplete_visual",
    "invalid_number", "inconsistent_recipe", "requested_grade", "requested_speed",
    "requested_crop",
])
def test_recovery_skips_unpreparable_master_without_inventing_treatment(change):
    job, treatment, receipt = recovery_evidence()
    if change == "missing":
        receipt = None
    elif change == "not_object":
        receipt = []
    elif change == "schema":
        receipt["schema"] = "unknown"
    elif change == "source_sha":
        receipt["sourceSha256"] = "c" * 64
    elif change == "job_id":
        receipt["generationJobId"] = "another-job"
    elif change == "recipe_hash":
        receipt["recipeSpecHash"] = "invalid"
    elif change == "producer_recipe_hash":
        job["recipeSpecHash"] = "c" * 64
    elif change == "missing_visual":
        del receipt["visualTreatment"]
    elif change == "unknown_field":
        receipt["pretend"] = True
    elif change == "incomplete_visual":
        del receipt["visualTreatment"]["filters"]["warmth"]
    elif change == "invalid_number":
        receipt["visualTreatment"]["clipSpeed"] = True
    elif change == "inconsistent_recipe":
        receipt["sourceRecipeTreatment"]["clipSpeed"] = 1
    elif change == "requested_grade":
        treatment["filters"]["brightness"] = 1
    elif change == "requested_speed":
        treatment["clipSpeed"] = 1
    elif change == "requested_crop":
        treatment["clipCrop"]["zoom"] = 1
    assert not recovery_treatment_matches(receipt, job, "b" * 64, treatment)
