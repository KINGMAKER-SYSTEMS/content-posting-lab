"""Capture the OFF-Scrambler serialized surface from a checkout, base or head.

Run inside the checkout to certify (repo root on sys.path), e.g. from a
worktree of the base (re-captured at Lab main 036bb75f when PR #281 was
rebased onto it; main #279 moved the fixture page to tt-fixture-dirt-bike on
an isolated source-DNA registry, and #284 added Layer B transforms):

    SCRAMBLER_GOLDEN_REPO_ROOT=/isolated/base python /candidate/tests/golden/scrambler/generate_base_golden.py out.json

It reproduces the lab fixture from tests/test_control_plane_source_execution.py
(offline vault, bound intent, registered publication), then serializes the RAW
enqueue response, queued/completed stored jobs, capabilities and the actual
artifacts endpoint response after running the real executor with fixed media IO, normalizing
only volatile timestamps/runtime ids and replacing the temporary root prefix
(paths, artifactRoot and complete thumbnail objects remain material) - the same
normalization tests/test_scrambler_executor_behavior.py applies to the
candidate. Byte-identity of the two documents is the OFF guarantee.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Override selects the application checkout, never the driver location.
REPO = Path(os.environ.get("SCRAMBLER_GOLDEN_REPO_ROOT") or
            Path(__file__).resolve().parents[3]).resolve()
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

TOKEN = "test-control-plane-token"
VOLATILE_KEYS = {
    "createdAt", "completedAt", "updatedAt", "startedAt", "runtimeId",
    "lastCheckedAt",
}


def _normalize(value, tmp: str):
    if isinstance(value, dict):
        return {key: _normalize(item, tmp)
                for key, item in sorted(value.items()) if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_normalize(item, tmp) for item in value]
    if isinstance(value, str):
        # macOS /var temporary roots may resolve under /private/var.
        # Replace only the run-specific prefix, preserving every path suffix.
        prefixes = {tmp, str(Path(tmp).resolve())} if tmp else set()
        for prefix in sorted(prefixes, key=len, reverse=True):
            value = value.replace(prefix, "<TMP>")
        return value
    return value


def complete_off_job(cp, tmp: Path, job_id: str, patch) -> dict:
    """Run the real executor; replace only media retrieval/encoding IO.

    Paths, thumbnail manifests, provenance, receipts and serialized artifact
    claims remain application outputs. Fixed fake bytes allow exact hashing
    comparisons without a network request or ffmpeg process.
    """
    source = tmp / "master.mp4"
    source.write_bytes(b"off-golden-source-master")

    async def cached_source(*_args):
        return source

    async def render(src, dst, correction, **kwargs):
        # Output hashes also defend the real executor render arguments:
        # fixed bytes independent of those arguments would hide OFF drift.
        correction_value = (correction.model_dump()
                            if hasattr(correction, "model_dump") else correction)
        body = {"name": Path(dst).name, "correction": correction_value,
                "render": kwargs}
        Path(dst).write_bytes(json.dumps(body, sort_keys=True, default=str).encode())

    async def thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg-thumbnail")
        return cp._generated_manifest(job_root, target)

    patch.setattr(cp, "_cached_source_master", cached_source)
    patch.setattr(cp, "run_color_correct", render)
    patch.setattr(cp, "_thumbnail_manifest", thumbnail)
    # Layer B (#284) runs on every OFF source-cut page. Fold the drawn
    # transform set into the fixed bytes (so Layer B drift changes the
    # hashes) and answer the post-transform duration probe with a fixed
    # in-bounds value instead of running ffmpeg on fake bytes.
    def transforms(path, cut_transforms, encode_args):
        data = Path(path).read_bytes()
        Path(path).write_bytes(data + json.dumps(cut_transforms.manifest(), sort_keys=True).encode())

    patch.setattr(cp, "apply_transforms", transforms)
    patch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 8.0)
    asyncio.run(cp._run_dossier_source(job_id))
    completed = cp._load_jobs()["jobs"][job_id]
    assert completed["status"] == "completed", completed.get("error")
    assert len(completed["clips"]) == 2
    return completed


def main(out_path: str) -> None:
    import os

    import routers.control_plane as cp
    import routers.control_plane_recipes as recipes
    import services.shipstream_source_manifest as source_manifest
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from master_pages_fixtures import bind_current_intent, master_pages
    from test_control_plane_source_execution import PAGE_ID, publication
    from source_dna_fixture import isolate_source_dna_registry

    assert Path(cp.__file__).resolve() == REPO / "routers" / "control_plane.py"
    print("application root", REPO, "control_plane", cp.__file__, flush=True)

    tmp = Path(tempfile.mkdtemp())
    # Same isolated registry the lab fixture's autouse _isolated_source_dna uses.
    isolate_source_dna_registry(_Monkey(), tmp)
    os.environ["CONTROL_PLANE_TOKEN"] = TOKEN
    os.environ["CONTENT_LAB_RECIPE_ROOT"] = str(tmp / "recipes")
    for key in ("SCRAMBLER_ENABLED", "SCRAMBLER_PAGES", "SCRAMBLER_DENY_PAGES",
                "SCRAMBLER_FAST_SHARE", "SCRAMBLER_PAGE_FAST_SHARE",
                "SCRAMBLER_TREATMENT_WEIGHTS", "SCRAMBLER_HISTORY_PATH",
                "CONTENT_LAB_ORIGINALITY_USAGE_PATH",
                "CONTENT_LAB_ORIGINALITY_ORIGINS_PATH",
                "CONTENT_LAB_ORIGINALITY_RETIRED_PATH"):
        os.environ.pop(key, None)
    cp._jobs_path = lambda: tmp / "jobs.json"
    cp._generation_root = lambda: tmp / "generated"
    # The job id is normally random (secrets.token_hex) and seeds the cut
    # plan, so two runs of the SAME code pick different windows. Pin it so
    # the golden compares like with like: only real base-vs-head behavior
    # differences may show up, never per-run randomness.
    cp._secrets.token_hex = lambda n: "f" * (2 * n)
    cp._secrets.token_urlsafe = lambda n: "g" * n
    intent, revision = master_pages(
        PAGE_ID,
        handle="chase.miles.4l",
        content_niche="POV - Dirtbike",
        content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/chase.miles.4l",
    )
    bind_current_intent(_Monkey(), cp, intent, revision)
    cp._start_dossier_source = lambda job_id: None

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg-thumbnail")
        return cp._generated_manifest(job_root, target)

    cp._thumbnail_manifest = fake_thumbnail
    source_manifest._fetch_manifest = _unreachable_manifest(source_manifest)

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    client = TestClient(app)
    auth = {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": "content-bucket-control-plane",
        "X-RT-Page-Id": PAGE_ID,
    }
    registered = client.post(
        "/api/control-plane/v1/recipes", json=publication(),
        headers={**auth, "Idempotency-Key": "source-register-0001"},
    )
    assert registered.status_code == 200, registered.text
    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    body = {
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "engine": payload["engine"],
        "lockedRecipeId": payload["recipeId"],
        "recipeVersion": payload["recipeVersion"],
        "quantity": 2,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy",
        "masterPages": spec["masterPages"],
        "masterPagesHash": spec["masterPagesHash"],
    }
    response = client.post(
        "/api/control-plane/v1/jobs", json=body,
        headers={**auth, "Idempotency-Key": "scrambler-off-golden"},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["jobId"]
    stored = cp._load_jobs()["jobs"][job_id]
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).json()
    completed = complete_off_job(cp, tmp, job_id, _Monkey())
    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=auth,
    )
    assert artifacts.status_code == 200, artifacts.text
    assert len(artifacts.json()["artifacts"]) == 2
    completed_response = client.get(
        f"/api/control-plane/v1/jobs/{job_id}", headers=auth,
    )
    assert completed_response.status_code == 200, completed_response.text
    assert completed_response.json()["status"] == "completed"
    document = {
        "response": _normalize(response.json(), str(tmp)),
        "job": _normalize(stored, str(tmp)),
        "capabilities": _normalize(capabilities, str(tmp)),
        "completedJob": _normalize(completed, str(tmp)),
        "completedResponse": _normalize(completed_response.json(), str(tmp)),
        "artifacts": _normalize(artifacts.json(), str(tmp)),
    }
    Path(out_path).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    print("wrote", out_path, "jobId", job_id)


class _Monkey:
    """The two attribute patches bind_current_intent needs, sans pytest."""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def _unreachable_manifest(source_manifest):
    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable(
            "ShipStream vault is offline in tests")
    return unreachable


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "off-enqueue-base.json")
