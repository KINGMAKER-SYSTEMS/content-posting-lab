"""Narrow original-master slot-source repair for one prepared-post job.

An existing prepared-post job whose source bytes carry a different visual
treatment than its immutable request asks for cannot be repaired by regrading
the already treated parent cut: the parent arc is graded twice. This module
cuts the *same exact time window* from the *original registered master* and
applies the *stored requested* visual treatment once.

It is not an arbitrary visual override and it never registers a new recipe:
the parent's frozen registration stays the only source authority, and the
result is evidence (a `content-lab.slot-source-treatment.v1` receipt bound to
the parent recipe identity) rather than a claim that the old recipe produced a
new visual.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Callable, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from services.source_treatment import NEUTRAL_CROP, normalized_visual_treatment

REQUEST_SCHEMA = "content-lab.source-regeneration-request.v1"
COMMAND_SCHEMA = "content-lab.slot-source-regeneration-command.v1"
ENVELOPE_SCHEMA = "content-lab.slot-source-regeneration.v1"
RECEIPT_SCHEMA = "content-lab.slot-source-treatment.v1"
SOURCE_ACK_SCHEMA = "content-lab.slot-source-ack.v1"
ARTIFACT_MIME_TYPE = "video/mp4"
ARTIFACT_NAME = "source.mp4"
OBJECT_KEY_PREFIX = "post-renders"
# One delivered frame at the sourced format's 30 fps delivery rate.
FRAME_TOLERANCE_MS = 34
PROBE_TIMEOUT_S = 120
PROBE_OUTPUT_LIMIT = 1024 * 1024
# run_color_correct serializes encodes on one process-wide asyncio semaphore.
# Production workers dispatch onto the shared ASGI loop (run_coroutine_threadsafe),
# but keep at most one repair thread inside the renderer at a time as a further
# bound on concurrent master copies and encode setup.
_RENDER_LOCK = threading.Lock()

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(min_length=1, max_length=512)]


class SourceRegenerationError(ValueError):
    """Stable, closed failure reason for the source-repair lane."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class SourceRegenerationRequest(BaseModel):
    """The whole wire body: identity only, never a URL, window or visual."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_: Literal[REQUEST_SCHEMA] = Field(alias="schema")
    parent_generation_job_id: Identity


class SourceAckRequest(BaseModel):
    """Optional closed ACK body naming the exact completed source attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_: Literal[SOURCE_ACK_SCHEMA] = Field(alias="schema")
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    source_sha256: Hash
    slot_payload_sha256: Hash


def canonical_json(value: Any) -> str:
    """Compact, key-sorted, NaN-free JSON. Never claims another language's encoding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def object_key(job_id: str, attempt_id: str) -> str:
    return f"{OBJECT_KEY_PREFIX}/{job_id}/source-regeneration/{attempt_id}/{ARTIFACT_NAME}"


def _object_key_parts(job_id: str, attempt_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", job_id)) and bool(
        re.fullmatch(r"[0-9a-f]{32}", attempt_id)
    )


def pending_command(row) -> dict | None:
    """The typed queued-command variant stored in the existing authority column."""
    encoded = row["output_authority_json"]
    if not encoded:
        return None
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and value.get("schema") == COMMAND_SCHEMA:
        return value
    return None


def completed_envelope(row) -> dict | None:
    """The typed completed-envelope variant stored in the existing authority column."""
    encoded = row["output_authority_json"]
    if not encoded:
        return None
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and value.get("schema") == ENVELOPE_SCHEMA:
        return value
    return None


def malformed_authority(row) -> bool:
    """A nonblank authority column that is none of the typed variants.

    ``pending_command``/``completed_envelope`` return None for unparseable
    bytes, which is indistinguishable from a truly absent column. A queued
    or running row whose authority is corrupted out-of-band must fail closed
    instead of falling through to the ordinary prepared renderer.
    """
    encoded = row["output_authority_json"]
    if not encoded:
        return False
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        return True
    if not isinstance(value, dict):
        return True
    # OUTPUT_AUTHORITY_SCHEMA is the normal prepared-final tombstone and is
    # never processed by run_one (those rows are state='succeeded'), but it
    # remains a valid typed variant, not corruption.
    return value.get("schema") not in {
        COMMAND_SCHEMA, ENVELOPE_SCHEMA, "content-lab.post-render-output-authority.v1",
    }


def _color_correction(filters: dict) -> dict:
    """Reuse the exact recipe filter mapping for the stored requested visual."""
    from services.control_plane_generation import dossier_filters_to_color_correction

    return dossier_filters_to_color_correction(
        SimpleNamespace(recipe_spec={"renderTreatment": {"filters": filters}})
    )


def _frame_treatment(recipe_spec: dict) -> dict | None:
    treatment = recipe_spec.get("renderTreatment")
    if not isinstance(treatment, dict):
        return None
    frame = treatment.get("frame")
    if frame is None or frame == "9:16":
        return None
    declared = {"frame": frame}
    if treatment.get("frameFit") is not None:
        declared["frameFit"] = treatment["frameFit"]
    return declared


def _equal_dict(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


# A subsequent human edit can point a repair at the *previous* prepared-post
# render (`render_<id>`), whose delivered bytes came from an earlier repair and
# not directly from the Control Plane producer. Walk that bounded chain back to
# the terminal Control Plane job, validating each hop's retained completed
# envelope, then resolve the terminal job exactly as a first repair does.
MAX_ANCESTRY_HOPS = 8


def _load_prior_row(prior_row_loader, job_id: str):
    """One durable post-render row, or None when the id is not a post-render job."""
    if prior_row_loader is None:
        return None
    return prior_row_loader(job_id)


def _prior_submission_request(row) -> dict:
    try:
        submission = json.loads(row["submission_json"])
        return submission["request"]
    except (TypeError, ValueError, KeyError) as error:
        raise SourceRegenerationError("source_regeneration_ancestor_invalid") from error


def _validated_ancestor_receipt(row, *, page_id: str, account: str, expected_source_sha: str) -> dict:
    """Validate one prior hop's retained envelope against its immutable row.

    No media IO: the retained receipt/output-authority bytes are the authority;
    the current Control Plane source hash is trusted and the download lane
    re-hashes bytes independently, so the old source binary need not still exist.
    """
    envelope = completed_envelope(row)
    if envelope is None:
        raise SourceRegenerationError("source_regeneration_ancestor_invalid")
    receipt, artifact = envelope.get("receipt"), envelope.get("artifact")
    if not isinstance(receipt, dict) or not isinstance(artifact, dict):
        raise SourceRegenerationError("source_regeneration_ancestor_invalid")
    if receipt.get("schema") != RECEIPT_SCHEMA or envelope.get("schema") != ENVELOPE_SCHEMA:
        raise SourceRegenerationError("source_regeneration_ancestor_invalid")
    # Canonical identity: the envelope must name its own immutable parent row.
    if (
        receipt.get("regeneration_job_id") != row["id"]
        or receipt.get("attempt_id") != row["attempt_id"]
        or receipt.get("slot_id") != row["slot_id"]
        or receipt.get("slot_payload_sha256") != row["slot_hash"]
        or artifact.get("object_key") != object_key(row["id"], row["attempt_id"])
        or artifact.get("mime_type") != ARTIFACT_MIME_TYPE
    ):
        raise SourceRegenerationError("source_regeneration_ancestor_invalid")
    # Source identity: this hop's delivered bytes are exactly the expected parent.
    if (
        not isinstance(artifact.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        or type(artifact.get("byte_length")) is not int
        or artifact["byte_length"] <= 0
        or receipt.get("source_sha256") != artifact["sha256"]
        or artifact["sha256"] != expected_source_sha
    ):
        raise SourceRegenerationError("source_regeneration_ancestor_source_mismatch")
    # Same page and account as the requesting prepared job.
    prior_request = _prior_submission_request(row)
    if prior_request.get("page_id") != page_id:
        raise SourceRegenerationError("source_regeneration_ancestor_foreign_page")
    if prior_request.get("account") != account:
        raise SourceRegenerationError("source_regeneration_ancestor_account_mismatch")
    parent_id = receipt.get("parent_generation_job_id")
    parent_source_sha = receipt.get("parent_source_sha256")
    if (
        not isinstance(parent_id, str)
        or not isinstance(parent_source_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", parent_source_sha)
    ):
        raise SourceRegenerationError("source_regeneration_ancestor_invalid")
    return receipt


def _walk_ancestry(*, page_id: str, parent_generation_job_id: str, request, prior_row_loader):
    """Resolve any prior prepared-post hops to the terminal Control Plane job."""
    receipts = []
    parent_id = parent_generation_job_id
    source_sha = request.source_sha256
    visited: set[str] = set()
    hops = 0
    while True:
        row = _load_prior_row(prior_row_loader, parent_id)
        if row is None:
            return parent_id, source_sha, receipts
        hops += 1
        if hops > MAX_ANCESTRY_HOPS:
            raise SourceRegenerationError("source_regeneration_ancestor_too_deep")
        if parent_id in visited:
            raise SourceRegenerationError("source_regeneration_ancestor_cycle")
        visited.add(parent_id)
        receipt = _validated_ancestor_receipt(
            row, page_id=page_id, account=request.account, expected_source_sha=source_sha,
        )
        receipts.append(receipt)
        parent_id = receipt["parent_generation_job_id"]
        source_sha = receipt["parent_source_sha256"]


def _validate_ancestry_invariants(receipts: list[dict], command: dict) -> None:
    """Every hop must keep the same original recipe/library/master/local cut."""
    for receipt in receipts:
        cut = receipt.get("cut")
        if not isinstance(cut, dict) or not _equal_dict(cut, command["cut"]):
            raise SourceRegenerationError("source_regeneration_ancestor_cut_mismatch")
        if (
            receipt.get("parent_recipe_id") != command["parentRecipeId"]
            or receipt.get("parent_recipe_version") != command["parentRecipeVersion"]
            or receipt.get("source_library_id") != command["sourceLibraryId"]
            or receipt.get("source_library_version") != command["sourceLibraryVersion"]
            or receipt.get("master_sha256") != command["master"]["sha256"]
        ):
            raise SourceRegenerationError("source_regeneration_ancestor_invariant_mismatch")


def revalidate_parent_authority(command: dict) -> None:
    """Re-check the terminal parent's intent and immutable identity at execute.

    Proven at queue time, then re-checked here before any media IO and before
    completion: a strategy flip or recipe/library change after queueing must
    refuse rather than render stale authority. The comparison uses only the
    queued command's immutable identity fields; the command's own createdAtMs
    is a local timestamp and is deliberately excluded.
    """
    from routers import control_plane as cp

    authority_id = command.get("parentAuthorityJobId") or command.get("parentGenerationJobId")
    try:
        job = cp._get_job_or_404(authority_id)
    except Exception as error:
        raise SourceRegenerationError("source_regeneration_parent_unavailable") from error
    if not cp._job_matches_current_master_pages(job):
        raise SourceRegenerationError("source_regeneration_parent_strategy_changed")
    if (
        job.get("pageId") != command.get("pageId")
        or job.get("status") != "completed"
        or job.get("recipeId") != command.get("parentRecipeId")
        or job.get("recipeVersion") != command.get("parentRecipeVersion")
        or job.get("sourceLibraryId") != command.get("sourceLibraryId")
        or job.get("sourceLibraryHash") != str(command.get("sourceLibraryVersion", "")).removeprefix("sha256:")
    ):
        raise SourceRegenerationError("source_regeneration_parent_changed")


def resolve_parent_authority(
    *,
    page_id: str,
    parent_generation_job_id: str,
    request,
    prior_row_loader=None,
) -> dict:
    """Verify the parent generation job and derive the exact master-cut authority.

    Reads only durable Control Plane job/recipe state and retained post-render
    envelopes: no media IO happens here, so every authority failure refuses
    before a byte is fetched or cut.
    """
    from routers import control_plane as cp

    terminal_parent_id, terminal_source_sha, ancestry = _walk_ancestry(
        page_id=page_id,
        parent_generation_job_id=parent_generation_job_id,
        request=request,
        prior_row_loader=prior_row_loader,
    )
    try:
        job = cp._get_job_or_404(terminal_parent_id)
    except Exception as error:  # FastAPI 404 or a malformed identifier
        raise SourceRegenerationError("source_regeneration_parent_unavailable") from error
    if job.get("pageId") != page_id:
        raise SourceRegenerationError("source_regeneration_parent_foreign_page")
    if job.get("status") != "completed":
        raise SourceRegenerationError("source_regeneration_parent_not_completed")
    if not cp._job_matches_current_master_pages(job):
        raise SourceRegenerationError("source_regeneration_parent_strategy_changed")

    publication = cp.load_registered_recipe(
        job.get("recipePublicationPageId") or job["pageId"],
        job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe = cp._dossier_source_recipe(publication)
    if (
        recipe is None
        or job.get("sourceLibraryId") != recipe.source_library_id
        or job.get("sourceLibraryHash") != recipe.source_library_hash
        or not cp.job_profile_authority_matches(job, recipe.engine_profile_hash, {
            "engine": recipe.engine,
            "sourceLibraryId": recipe.source_library_id,
            "sourceLibraryHash": recipe.source_library_hash,
            "formatContractVersion": recipe.format_contract_version,
            "executorVersion": recipe.executor_version,
            "materialSource": recipe.material_source,
            "assetType": recipe.asset_type,
        })
        or job.get("engine") != recipe.engine
        or job.get("formatContractVersion") != recipe.format_contract_version
        or job.get("executorVersion") != recipe.executor_version
        or job.get("materialSource") != recipe.material_source
        or job.get("assetType") != recipe.asset_type
    ):
        raise SourceRegenerationError("source_regeneration_recipe_unavailable")

    # A parent job may deliver several cuts; the prepared request names exactly
    # one of them by its delivered source SHA, and that clip alone is authority.
    clips = [clip for clip in job.get("clips") or []
             if isinstance(clip, dict) and clip.get("sha256") == terminal_source_sha]
    if not clips:
        raise SourceRegenerationError("source_regeneration_parent_source_mismatch")
    if len(clips) != 1:
        raise SourceRegenerationError("source_regeneration_parent_clip_ambiguous")
    clip = clips[0]
    lineage = clip.get("source")
    if not isinstance(lineage, dict):
        raise SourceRegenerationError("source_regeneration_parent_provenance_missing")
    if (
        lineage.get("recipeId") != recipe.recipe_id
        or lineage.get("sourceLibraryId") != recipe.source_library_id
        or lineage.get("sourceLibraryVersion") != f"sha256:{recipe.source_library_hash}"
    ):
        raise SourceRegenerationError("source_regeneration_parent_provenance_mismatch")
    master_lineage = lineage.get("master")
    cut_window = lineage.get("cutWindow")
    if not isinstance(master_lineage, dict) or not isinstance(cut_window, dict):
        raise SourceRegenerationError("source_regeneration_parent_provenance_missing")

    master_sha256 = master_lineage.get("sha256")
    master = next(
        (candidate for candidate in recipe.masters if candidate.sha256 == master_sha256),
        None,
    )
    if master is None:
        raise SourceRegenerationError("source_regeneration_master_unknown")
    if not _equal_dict(master_lineage, {
        "sourceId": master.source_id,
        "sha256": master.sha256,
        "bytes": master.bytes,
        "filename": master.filename,
        "mimeType": master.mime_type,
        "storageKey": master.storage_key,
        "durationMs": master.duration_ms,
        "sourceOffsetMs": master.source_offset_ms,
        "provenance": master.provenance,
    }):
        raise SourceRegenerationError("source_regeneration_master_mismatch")

    start_ms = cut_window.get("libraryStartMs")
    duration_ms = cut_window.get("durationMs")
    if (
        type(start_ms) is not int or type(duration_ms) is not int
        or start_ms < 0 or duration_ms <= 0
        or start_ms + duration_ms > master.duration_ms
        or cut_window.get("libraryEndMs") != start_ms + duration_ms
        or cut_window.get("originalStartMs") != master.source_offset_ms + start_ms
        or cut_window.get("originalEndMs") != master.source_offset_ms + start_ms + duration_ms
    ):
        raise SourceRegenerationError("source_regeneration_cut_invalid")

    cuts = job.get("sourceCuts")
    matching = [
        cut for cut in cuts or []
        if isinstance(cut, dict)
        and cut.get("masterSha256") == master.sha256
        and cut.get("startMs") == start_ms
        and cut.get("libraryStartMs") == start_ms
        and cut.get("durationMs") == duration_ms
    ]
    from services.control_plane_sources import source_cut_is_planned

    if len(matching) != 1:
        raise SourceRegenerationError("source_regeneration_cut_unplanned")
    cut_slot_id = matching[0].get("slotId")
    if not source_cut_is_planned(recipe, master, start_ms, duration_ms, cut_slot_id):
        raise SourceRegenerationError("source_regeneration_cut_unplanned")

    try:
        desired = normalized_visual_treatment(json.loads(request.render_treatment_json))
    except (TypeError, ValueError) as error:
        raise SourceRegenerationError("source_regeneration_requested_visual_invalid") from error
    actual = None
    if request.source_visual_treatment_json is not None:
        try:
            actual = normalized_visual_treatment(json.loads(request.source_visual_treatment_json))
        except (TypeError, ValueError) as error:
            raise SourceRegenerationError("source_regeneration_source_visual_invalid") from error
    if actual is not None and actual == desired:
        raise SourceRegenerationError("source_regeneration_not_needed")

    render_treatment = recipe.recipe_spec.get("renderTreatment")
    if not isinstance(render_treatment, dict):
        raise SourceRegenerationError("source_regeneration_recipe_unavailable")
    desired_json = canonical_json(desired)
    command = {
        "schema": COMMAND_SCHEMA,
        "parentGenerationJobId": parent_generation_job_id,
        "parentAuthorityJobId": terminal_parent_id,
        "cutSlotId": cut_slot_id,
        "masterSha256": master_sha256,
        "pageId": page_id,
        "slotId": request.slot_id,
        "slotPayloadSha256": request.slot_payload_sha256,
        "parentSourceSha256": request.source_sha256,
        "parentRecipeId": recipe.recipe_id,
        "parentRecipeVersion": job["recipeVersion"],
        "sourceLibraryId": recipe.source_library_id,
        "sourceLibraryVersion": f"sha256:{recipe.source_library_hash}",
        "requestedVisualJson": desired_json,
        "requestedVisualSha256": _sha256_text(desired_json),
        "requestedSourceVisualSha256": (
            request.source_visual_treatment_sha256
        ),
        "master": {
            "sha256": master.sha256,
            "bytes": master.bytes,
            "storageKey": master.storage_key,
            "filename": master.filename,
            "mimeType": master.mime_type,
            "durationMs": master.duration_ms,
            "sourceOffsetMs": master.source_offset_ms,
        },
        "cut": {
            "start_ms": start_ms,
            "end_ms": start_ms + duration_ms,
            "duration_ms": duration_ms,
        },
        "render": {
            "playbackSpeed": desired["clipSpeed"],
            "clipCrop": desired["clipCrop"],
            "filters": desired["filters"],
            "encodePreset": recipe.encode_preset,
            "outputWidth": recipe.output_width,
            "outputHeight": recipe.output_height,
            "frame": _frame_treatment(recipe.recipe_spec),
        },
        "createdAtMs": int(time.time_ns() // 1_000_000),
    }
    _validate_ancestry_invariants(ancestry, command)
    return command


# --------------------------------------------------------------------------
# Source bytes: the one fixed original-master route, bounded by registered bytes
# --------------------------------------------------------------------------


OUTPUT_HEADROOM_BYTES = 128 * 1024 * 1024


def _verified_cache_master(sha256: str, byte_length: int) -> Path | None:
    """The verified `_source_dna` master, if one exists. Read-only, never mutated."""
    from routers import control_plane as cp

    cache_root = (cp._generation_root() / "_source_dna").resolve()
    target = cache_root / f"{sha256}.mp4"
    if not target.is_file():
        return None
    try:
        if target.stat().st_size != byte_length:
            return None
    except OSError:
        return None
    return target


def _copy_verified_master(source: Path, destination: Path, sha256: str, byte_length: int) -> None:
    """Copy one already-hashed master into owned scratch from a single held fd.

    The cache is evidence, not our file, so it is never read from the shared
    path during a long encode: one copy, verified against the registered bytes,
    is what the cut sees. A concurrent eviction cannot change those bytes.
    """
    digest, total = hashlib.sha256(), 0
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise SourceRegenerationError("source_regeneration_master_unavailable") from error
    try:
        with os.fdopen(fd, "rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                total += len(chunk)
                if total > byte_length:
                    raise SourceRegenerationError("source_regeneration_master_size_mismatch")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if total != byte_length or digest.hexdigest() != sha256:
        destination.unlink(missing_ok=True)
        raise SourceRegenerationError("source_regeneration_master_hash_mismatch")


def fetch_master(master: dict, destination: Path, *, page_id: str) -> Path:
    """Return original master bytes: the verified local cache, else one bounded fetch."""
    sha256, byte_length = master["sha256"], master["bytes"]
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or "") or type(byte_length) is not int or byte_length <= 0:
        raise SourceRegenerationError("source_regeneration_master_invalid")
    try:
        free = shutil.disk_usage(destination.parent).free
    except OSError as error:
        raise SourceRegenerationError("source_regeneration_workspace_unavailable") from error
    # A master may be far larger than the cut-sized prepared-source bound, so
    # reserve its exact registered bytes plus output headroom before fetching.
    if free < byte_length + OUTPUT_HEADROOM_BYTES:
        raise SourceRegenerationError("source_regeneration_workspace_insufficient")
    cached = _verified_cache_master(sha256, byte_length)
    if cached is not None:
        _copy_verified_master(cached, destination, sha256, byte_length)
        return destination
    from routers import control_plane as cp

    origin = cp._source_media_origin()
    url = (
        f"{origin}/api/control-plane/v1/pages/"
        f"{quote(page_id, safe='')}/dossier-ingredient-media/source/{sha256}"
    )
    digest, total = hashlib.sha256(), 0
    deadline = time.monotonic() + 120
    headers = {"Accept": "video/mp4", "Accept-Encoding": "identity"}
    try:
        with httpx.Client(trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(10, connect=5), headers=headers) as client:
            with client.stream("GET", url) as response:
                if response.status_code in {403, 404}:
                    raise SourceRegenerationError("source_regeneration_master_rejected")
                if response.status_code != 200:
                    raise SourceRegenerationError("source_unavailable")
                declared = response.headers.get("content-length", "")
                if (not declared.isdigit() or int(declared) != byte_length
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise SourceRegenerationError("source_regeneration_master_binding_mismatch")
                with destination.open("xb") as handle:
                    for chunk in response.iter_raw(1024 * 1024):
                        if time.monotonic() > deadline:
                            raise SourceRegenerationError("source_timeout")
                        total += len(chunk)
                        if total > byte_length:
                            raise SourceRegenerationError("source_regeneration_master_size_mismatch")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
    except httpx.TimeoutException as error:
        raise SourceRegenerationError("source_timeout") from error
    except httpx.HTTPError as error:
        raise SourceRegenerationError("source_unavailable") from error
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if total != byte_length or digest.hexdigest() != sha256:
        destination.unlink(missing_ok=True)
        raise SourceRegenerationError("source_regeneration_master_hash_mismatch")
    return destination


# --------------------------------------------------------------------------
# Render: one grade of the original master's exact window, no caption or audio
# --------------------------------------------------------------------------


def _probe_artifact(path: Path) -> dict:
    """Prove the delivered facts this receipt binds: 1080x1920, silent, one video."""
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov",
             "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, timeout=PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceRegenerationError("source_regeneration_decode_unavailable") from error
    if completed.returncode != 0 or len(completed.stdout) > PROBE_OUTPUT_LIMIT:
        raise SourceRegenerationError("source_regeneration_decode_unavailable")
    try:
        data = json.loads(completed.stdout)
        videos = [row for row in data["streams"] if row.get("codec_type") == "video"]
        audios = [row for row in data["streams"] if row.get("codec_type") == "audio"]
        if len(videos) != 1 or audios:
            raise ValueError("one silent video stream required")
        video = videos[0]
        duration = float(video.get("duration", data["format"]["duration"]))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        return {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "duration_ms": round(duration * 1000),
            "video_codec": video["codec_name"],
            "pixel_format": video["pix_fmt"],
            "frame_rate": _frame_rate(video),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise SourceRegenerationError("source_regeneration_decode_unavailable") from error


DELIVERY_FPS = 30


def _frame_rate(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if isinstance(raw, str) and "/" in raw:
            numerator, _, denominator = raw.partition("/")
            if numerator.isdigit() and denominator.isdigit() and int(denominator) > 0:
                value = int(numerator) / int(denominator)
                if math.isfinite(value) and value > 0:
                    return value
    raise SourceRegenerationError("source_regeneration_decode_unavailable")


def _hash_file(path: Path, limit: int) -> tuple[str, int]:
    if not path.is_file():
        raise SourceRegenerationError("source_regeneration_artifact_missing")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise SourceRegenerationError("source_regeneration_artifact_too_large")
            digest.update(chunk)
    if total <= 0:
        raise SourceRegenerationError("source_regeneration_artifact_missing")
    return digest.hexdigest(), total


def _reload_planned_cut(command: dict):
    """Reload the registered recipe and master the command was planned against."""
    from routers import control_plane as cp

    authority_id = command.get("parentAuthorityJobId") or command.get("parentGenerationJobId")
    try:
        job = cp._get_job_or_404(authority_id)
    except Exception as error:
        raise SourceRegenerationError("source_regeneration_parent_unavailable") from error
    publication = cp.load_registered_recipe(
        job.get("recipePublicationPageId") or job["pageId"],
        job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe = cp._dossier_source_recipe(publication)
    if recipe is None:
        raise SourceRegenerationError("source_regeneration_recipe_unavailable")
    master_sha = command.get("masterSha256") or command["master"]["sha256"]
    master = next((m for m in recipe.masters if m.sha256 == master_sha), None)
    if master is None:
        raise SourceRegenerationError("source_regeneration_master_unavailable")
    return recipe, master


async def _render(
    command: dict, master_path: Path, output_path: Path,
) -> None:
    from services.control_plane_sources import source_cut_is_planned
    from services.ffmpeg import delivery_encode_args, run_color_correct

    render = command["render"]
    cut = command["cut"]
    # Re-derive the exact planned cut before rendering: source_cut_is_planned
    # is the planner's own vocabulary check, the same one _run_dossier_source
    # applies per cut. A stored window the planner would not emit is refused.
    recipe, master = _reload_planned_cut(command)
    slot_id = command.get("cutSlotId") or f"{master.sha256}:{cut["start_ms"]}:{cut["duration_ms"]}"
    if not source_cut_is_planned(
        recipe, master, cut["start_ms"], cut["duration_ms"], slot_id,
    ):
        raise SourceRegenerationError("source_regeneration_cut_unplanned")
    frame_kwargs: dict[str, Any] = {}
    if render.get("frame"):
        from routers import control_plane as cp

        frame_kwargs = await cp._page_frame_cut_kwargs(render["frame"], master_path)
    await run_color_correct(
        str(master_path), str(output_path),
        _color_correction(render["filters"]),
        scale=None,
        encode_args=delivery_encode_args(render["encodePreset"]),
        playback_speed=render["playbackSpeed"],
        clip_crop=render["clipCrop"],
        clip_crop_size=(render["outputWidth"], render["outputHeight"]),
        clip_start_ms=cut["start_ms"],
        clip_duration_ms=cut["duration_ms"],
        **frame_kwargs,
    )


def regenerate(
    command: dict, work_dir: Path, *, job_id: str, attempt_id: str,
    clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    loop=None,
) -> dict:
    """Produce the repaired original-master clip and its closed receipt envelope."""
    if command.get("schema") != COMMAND_SCHEMA:
        raise SourceRegenerationError("source_regeneration_command_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(command.get("submissionSha256", ""))):
        # The command is bound to the immutable submission it was derived from.
        raise SourceRegenerationError("source_regeneration_command_invalid")
    if not _object_key_parts(job_id, attempt_id):
        raise SourceRegenerationError("source_regeneration_command_invalid")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    master = command["master"]
    cut = command["cut"]
    render = command["render"]
    scratch = work_dir / "master.mp4"
    output = work_dir / ARTIFACT_NAME
    owned_scratch = False
    try:
        master_path = fetch_master(master, scratch, page_id=command["pageId"])
        owned_scratch = master_path == scratch
        with _RENDER_LOCK:
            if loop is not None:
                # Production: dispatch onto the one ASGI loop so the process-wide
                # color-correction semaphore is shared with normal generation.
                future = asyncio.run_coroutine_threadsafe(
                    _render(command, master_path, output), loop,
                )
                future.result()
            else:
                # Direct/test invocation without a shared loop.
                asyncio.run(_render(command, master_path, output))
        probe = _probe_artifact(output)
        if (probe["width"], probe["height"]) != (render["outputWidth"], render["outputHeight"]):
            raise SourceRegenerationError("source_regeneration_output_geometry_invalid")
        if abs(probe["frame_rate"] - DELIVERY_FPS) > 0.01:
            raise SourceRegenerationError("source_regeneration_output_rate_invalid")
        expected_ms = round(cut["duration_ms"] / render["playbackSpeed"])
        if abs(probe["duration_ms"] - expected_ms) > FRAME_TOLERANCE_MS:
            raise SourceRegenerationError("source_regeneration_output_duration_invalid")
        sha256, byte_length = _hash_file(output, master["bytes"] + 64 * 1024 * 1024)
        if sha256 == master["sha256"]:
            raise SourceRegenerationError("source_regeneration_output_unchanged")
        recorded = int(clock_ms())
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "regeneration_job_id": job_id,
            "attempt_id": attempt_id,
            "page_id": command["pageId"],
            "slot_id": command["slotId"],
            "slot_payload_sha256": command["slotPayloadSha256"],
            "parent_generation_job_id": command["parentGenerationJobId"],
            "parent_source_sha256": command["parentSourceSha256"],
            "parent_recipe_id": command["parentRecipeId"],
            "parent_recipe_version": command["parentRecipeVersion"],
            "master_sha256": master["sha256"],
            "source_library_id": command["sourceLibraryId"],
            "source_library_version": command["sourceLibraryVersion"],
            "cut": {
                "start_ms": cut["start_ms"],
                "end_ms": cut["end_ms"],
                "duration_ms": cut["duration_ms"],
            },
            "visual_treatment_json": command["requestedVisualJson"],
            "visual_treatment_sha256": command["requestedVisualSha256"],
            "source_sha256": sha256,
            "completed_at_ms": recorded,
        }
        return {
            "schema": ENVELOPE_SCHEMA,
            "receipt": receipt,
            "artifact": {
                "sha256": sha256,
                "byte_length": byte_length,
                "object_key": object_key(job_id, attempt_id),
                "mime_type": ARTIFACT_MIME_TYPE,
                "width": probe["width"],
                "height": probe["height"],
                "duration_ms": probe["duration_ms"],
            },
        }
    finally:
        if owned_scratch:
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass


def artifact_path(root: Path, job_id: str, attempt_id: str) -> Path:
    if not _object_key_parts(job_id, attempt_id):
        raise SourceRegenerationError("source_regeneration_command_invalid")
    return Path(root) / "attempts" / job_id / attempt_id / "source-regeneration" / ARTIFACT_NAME


def verify_envelope(envelope: dict, *, root: Path, job_id: str, attempt_id: str,
                    not_before_ms: int | None = None,
                    not_after_ms: int | None = None) -> Path:
    """Re-validate a completed envelope against its exact bytes before reuse."""
    if not isinstance(envelope, dict) or envelope.get("schema") != ENVELOPE_SCHEMA:
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    if set(envelope) != {"schema", "receipt", "artifact"}:
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    receipt, artifact = envelope.get("receipt"), envelope.get("artifact")
    if not isinstance(receipt, dict) or not isinstance(artifact, dict):
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    if (
        receipt.get("regeneration_job_id") != job_id
        or receipt.get("attempt_id") != attempt_id
        or artifact.get("object_key") != object_key(job_id, attempt_id)
        or artifact.get("mime_type") != ARTIFACT_MIME_TYPE
        or receipt.get("source_sha256") != artifact.get("sha256")
    ):
        raise SourceRegenerationError("source_regeneration_envelope_mismatch")
    if not isinstance(artifact.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    if type(artifact.get("byte_length")) is not int or artifact["byte_length"] <= 0:
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    completed = receipt.get("completed_at_ms")
    if type(completed) is not int or completed <= 0:
        raise SourceRegenerationError("source_regeneration_envelope_invalid")
    if not_before_ms is not None and completed < not_before_ms:
        raise SourceRegenerationError("source_regeneration_envelope_mismatch")
    if not_after_ms is not None and completed > not_after_ms:
        raise SourceRegenerationError("source_regeneration_envelope_mismatch")
    path = artifact_path(root, job_id, attempt_id)
    sha256, byte_length = _hash_file(path, artifact["byte_length"])
    if sha256 != artifact["sha256"] or byte_length != artifact["byte_length"]:
        raise SourceRegenerationError("source_regeneration_artifact_changed")
    return path


__all__ = [
    "ARTIFACT_NAME", "COMMAND_SCHEMA", "ENVELOPE_SCHEMA", "OBJECT_KEY_PREFIX",
    "RECEIPT_SCHEMA", "REQUEST_SCHEMA", "SOURCE_ACK_SCHEMA", "SourceAckRequest",
    "SourceRegenerationError", "SourceRegenerationRequest", "artifact_path",
    "canonical_json", "completed_envelope", "fetch_master", "malformed_authority",
    "object_key", "pending_command", "regenerate", "revalidate_parent_authority",
    "resolve_parent_authority", "verify_envelope",
]
