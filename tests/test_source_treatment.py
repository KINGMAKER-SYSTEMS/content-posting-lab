import pytest

from services.source_treatment import derived_source_treatment, normalized_visual_treatment, source_treatment_receipt


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
