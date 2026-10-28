"""Cross-repo contract: Lab image-video variant plans must pass the Worker validator.

The Worker validates every image-video / single-image-video artifact source with
``managedPhotoPlanValid`` (control-plane-worker/src/domain/managedSlideshowPages.js),
which requires varied plans to carry ``variantIndex`` (never the legacy ``variant``
key). A plan emitted with ``variant`` fails ``exactKeys`` and the Worker throws
``CONTENT_LAB_SCHEMA_MISMATCH``, wasting every varied render.

This test pins a byte-copy of the Worker validator to the Worker file's sha256
so a future key rename on either side fails CI, and runs the real validator
(node) over the Lab's real pen-pal3 (34) and twin47996 (24) variant sources.
"""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CONTRACT_DIR = HERE / "worker_contract"
FIXTURE = CONTRACT_DIR / "pentwin-variant-sources.json"
MIRROR = CONTRACT_DIR / "managedSlideshowPages.mjs"
RUNNER = CONTRACT_DIR / "run_contract.mjs"

# sha256 of control-plane-worker/src/domain/managedSlideshowPages.js, identical
# at both live refs (trainh-combined-1026-w base 6274210 and j6b-w ae9412cf8).
WORKER_VALIDATOR_SHA256 = "62633c95f28b1ca709a8c2930076e992c614ad23d5bcc84d9d8df03a8ff58802"


def _committed_sources() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_worker_validator_mirror_matches_worker_file_sha():
    assert hashlib.sha256(MIRROR.read_bytes()).hexdigest() == WORKER_VALIDATOR_SHA256


def test_fixture_matches_live_lab_generation():
    from tests.worker_contract import generate_fixture

    assert generate_fixture.build_sources() == _committed_sources()


def test_varied_plans_emit_variant_index_never_legacy_variant():
    for source in _committed_sources():
        plan = source["photoPlan"]
        if "variantIndex" in plan:
            assert "variant" not in plan, plan
            assert isinstance(plan["variantIndex"], int) and plan["variantIndex"] >= 1
        else:
            # Variant 0 carries no index key at all.
            assert "variant" not in plan and "variantIndex" not in plan


def test_worker_accepts_every_real_variant_source():
    sources = _committed_sources()
    assert len(sources) == 58
    result = subprocess.run(
        ["node", str(RUNNER)], capture_output=True, text=True, cwd=str(HERE.parent),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK: 58/58 sources valid"


def _legacy_stored_sources():
    """Pre-rename stored sources: varied plans spell the index as ``variant``."""
    import copy

    sources = copy.deepcopy(_committed_sources())
    for source in sources:
        plan = source["photoPlan"]
        if "variantIndex" in plan:
            plan["variant"] = plan.pop("variantIndex")
    return sources


def test_wire_translation_serializes_legacy_variant_as_variant_index():
    from services.control_plane_image_video import image_video_source_for_wire

    for stored in _legacy_stored_sources():
        plan = stored["photoPlan"]
        if "variant" in plan:
            wire = image_video_source_for_wire(stored)
            assert wire is not stored
            assert "variant" not in wire["photoPlan"]
            assert wire["photoPlan"]["variantIndex"] == plan["variant"]
            # The persisted record is copied, never mutated in place.
            assert stored["photoPlan"]["variant"] == plan["variant"]
        else:
            assert image_video_source_for_wire(stored) is stored


def test_worker_accepts_legacy_stored_plan_after_wire_translation():
    from services.control_plane_image_video import image_video_source_for_wire

    wire_sources = [image_video_source_for_wire(s) for s in _legacy_stored_sources()]
    tmp = CONTRACT_DIR / "legacy-wire-sources.json"
    tmp.write_text(json.dumps(wire_sources, indent=2), encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(RUNNER), str(tmp)],
            capture_output=True, text=True, cwd=str(HERE.parent),
        )
    finally:
        tmp.unlink()
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK: 58/58 sources valid"


def test_wire_translation_mutant_skips_translation_fails():
    """Skipping the translation leaves ``variant``, which the Worker rejects."""
    tmp = CONTRACT_DIR / "legacy-untranslated-sources.json"
    tmp.write_text(json.dumps(_legacy_stored_sources(), indent=2), encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(RUNNER), str(tmp)],
            capture_output=True, text=True, cwd=str(HERE.parent),
        )
    finally:
        tmp.unlink()
    assert result.returncode != 0
    assert "invalid" in result.stderr


@pytest.mark.parametrize("mutation", ["variant", "duration", "media_key"])
def test_contract_mutations_fail(mutation):
    """The three mutants from the acceptance brief each turn the contract RED."""
    import copy

    sources = _committed_sources()
    mutated = copy.deepcopy(sources)
    for source in mutated:
        plan = source["photoPlan"]
        if "variantIndex" not in plan:
            continue
        if mutation == "variant":
            plan["variant"] = plan.pop("variantIndex")
            # Re-sign is irrelevant: the Worker rejects the extra/legacy key
            # before it ever compares signatures.
        elif mutation == "duration":
            plan["totalDurationSeconds"] = 14.0
            for item in plan["media"]:
                item["duration"] = 14.0 / len(plan["media"])
        elif mutation == "media_key":
            plan["media"][0]["extra"] = 1
    tmp = CONTRACT_DIR / f"mutant-{mutation}.json"
    tmp.write_text(json.dumps(mutated, indent=2), encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(RUNNER), str(tmp)],
            capture_output=True, text=True, cwd=str(HERE.parent),
        )
    finally:
        tmp.unlink()
    assert result.returncode != 0, f"mutant {mutation} unexpectedly passed"
    assert "invalid" in result.stderr
