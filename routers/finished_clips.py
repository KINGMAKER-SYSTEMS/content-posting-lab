"""Exact, immutable finished MP4 custody for Control Plane admission."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from routers.control_plane_recipes import require_control_plane_bearer
from services.generation_recovery import store_lock as lock_for
from services.json_store import atomic_load, atomic_save
from services.source_treatment import normalized_visual_treatment, source_treatment_receipt

router = APIRouter()
SHA = re.compile(r"[0-9a-f]{64}\Z")
PAGE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
MAX_BYTES = 32 * 1024 * 1024
# Exact bodies from custody 002125ae. Burnt custody records date/ordinal, not clock time.
BOOKED = {
    'acct:culture-core-1': {
        '34d449d33eba2aa42dd85f03973927eec7fb21c24e498ba6bc2cb928217e7429',
        '1be6ec6be64302cc085ebb9c3138c7c58b1b1804c662bb40f00a28555840ed83',
        '3fdefd5a8bab937f8f41138035455d416b99255147dcc3312979febf50069217',
        '600ccf3f8c951f48830842b7563d09da647630e2e3a7609ca2e1260675eba218',
        'd96e589271004c0ff6e15c1cb7725931cb93c661533a132b3fca598ef8b89bc6',
        '9733ca618dcc2cfdd2ae4c71d56f5a19e7b923adb21fc943b81fec16c84d6685',
        'a26822f74dd78d255935095df2cedfd2967fa77914d0cf21e8154c2a380bf746',
        'de25eca2f106edeac9f3a9853fe3a26f8fcce8033b4e0ff635196fe2033cbbbe',
        'f519cd7b73016edb68e9aed3489430c3318ec7d9b001928243d5c0708271fc98',
        '6560b8211ec2cc93a4ee6225b4f67fe1b3acc78722dc6f885c99444fffcd27bc',
        'a7cb21edc33f6828af2797357d6c94c3ea7d85c88902f21b2706902183a5dd67',
        '23abb89dc26bc65c2d3782b40e0f18aed6275bafb469de977ff67050de2b97bd',
        '3e2ac10bd470840b73884389cc3b78ca735b743a2a778b7931877300ed07a405',
        'e2570b3c32921ae5908f71991f389335099c1e75c374eab3825f1fc88d6d7442',
        'b0119ef93f076478c07662699d3d3d09cf2d33f0df312ef32f66b9ec71fdb3d2',
    },
    'acct:burnt-mill-rd': {
            '3fc6a5414216274fc6bc0d8eb78757516782f453e4120356379fbb0344779a43',
            '4b26d5f20f3d79b89a8a1cb2b916acf663fa8c6c535affafc894712e2827e91e',
            '352bac94a5d3cb4c5d31ddfa538988b69343c6f33a4ec8ba6af86d71489b9d33',
            '4d6289f5a4c27f8b6c8c544545f44a7bf322b3bafe984da6625b76a661018ddf',
            'eb0539ff7cb0e2c47945fa737055fc8c183200b8c5de769751bb71aeed107d92',
    },
}


# Joy (pad #342) is an AI video page (Eric 11-04) and stays off the pool. Its
# nine alternate bodies were cut outside the Lab from raws of Lab ai_video job
# cpl-18882c4ced3c15ba: Sun 11-22 ord 1-5 and Mon 11-23 ord 1-3 (book
# JOY-CUT-SUNMON-G6-1120.tsv, raw per body from each RESULT.json), and the Sat
# CFR-30 heal c5a992bf of 9d1e37d0. Each SHA was re-hashed from its file. Each
# body maps to its Lab parent raw. No generation runs on this path.
JOY_PAGE = 'acct:joy-to-the-world709'
JOY_SOURCE_JOB = 'cpl-18882c4ced3c15ba'
JOY_AI_REUSE = {
    'b621efc54d9b0000b9d8208522e89166943b8636302730413874a07513ceaf5c': '1e7d874165236ca3c8933fa8921860a1fcecc3b45812591160ba6629599e6e55',
    'd148b55469279ba82972b1cbbc2aaca079fedb079e293df15763627673e1450f': '13397af4c834eeda694d2234552c6e190324dc327fe2d205ba1e276864c78238',
    '311bfcc849367647cf635dc4bc1487a06e5a37cec93ce80233b83ab27278ae1c': '0de0437f74999a1c37964cd6d70b5baaab502c4e4019291aff75f3fd925a9618',
    '059039a46e895d9b11853eb1226c1f32279bed5e52571eb830aa93f61eac7750': '2fef2b38f75304575c85d93524101903a2bb55b0e31b54b51d7d4ea97b37d3e3',
    '5167df7fd0b96c5a065fb1218b4d0ee2513dff57ce912735dcec98b839a43430': '419d6d15f1aa9d49b174fbe0e58c579357ff70a800cdbf246047bcf0debee550',
    '09bafd988b06d9c4091a4a69d5eb205d1e0a2d7f65f1495bc3ec4691cdc32115': 'd8bb2b81093eb3e7b1fb072ca80192254d1ff827ef9b6438b4351d52bdcc06ec',
    '9d1e37d0a77c51c460196f03777ab9838959fa436f8c843b05058a6a66aa2841': 'd635d3cbf7efb777bc6ef5e6c5adb99a078dabc70553a1050f118a729295dd51',
    '310034c0814394ce95fb4463d974882195d831ce4f71421881c123c97efa5f2a': 'f03b8acfee596d048342c9f0ad62b10165d3691e3747b6751f85456495633a42',
    'c5a992bfea9f02d4a26c9bc4e8c24af8838a09a3aa4c2cb6ee6d7184f688786d': 'd635d3cbf7efb777bc6ef5e6c5adb99a078dabc70553a1050f118a729295dd51',
}
# What those cuts applied to the raw (RESULT.json: speed 0.85, luma x0.9).
JOY_APPLIED_TREATMENT = {"filters": {"brightness": 0.9}, "clipSpeed": 0.85,
                         "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}


def _booked(page: str, sha: str) -> bool:
    if page == JOY_PAGE:
        return sha in JOY_AI_REUSE
    return sha in BOOKED.get(page, set())


def _joy_source_treatment(booking: dict, sha: str) -> dict:
    """Issue Joy's receipt only for a listed body under a recipe that asks for the applied treatment."""
    parent = JOY_AI_REUSE.get(sha)
    if parent is None:
        raise HTTPException(400, 'clip_not_booked')
    spec_hash = booking.get("recipeSpecHash")
    if not isinstance(spec_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", spec_hash):
        raise HTTPException(400, "recipe_spec_hash_invalid")
    recipe_treatment = booking.get("renderTreatment")
    applied = normalized_visual_treatment(JOY_APPLIED_TREATMENT)
    try:
        requested = normalized_visual_treatment(recipe_treatment)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "render_treatment_invalid") from exc
    if requested != applied:
        raise HTTPException(400, "joy_recipe_treatment_mismatch")
    receipt = source_treatment_receipt({"jobId": JOY_SOURCE_JOB, "recipeSpecHash": spec_hash}, recipe_treatment, sha,
                                       clip_speed=applied["clipSpeed"], clip_crop=applied["clipCrop"],
                                       filters=applied["filters"])
    receipt["derivedFrom"] = {"sourceSha256": parent, "generationJobId": JOY_SOURCE_JOB}
    return receipt


def _root() -> Path:
    from routers.control_plane import _generation_root
    path = _generation_root() / "finished-clips"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _receipt_path(page: str, sha: str) -> Path:
    return _root() / hashlib.sha256(page.encode()).hexdigest() / f"{sha}.json"


def _required(value: object, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value or not value.strip() or len(value) > limit or value != value.strip():
        raise HTTPException(400, f"{name} is required")
    return value


def _probe(path: Path, declared_audio: bool) -> dict:
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                capture_output=True, timeout=20, check=True)
        data = json.loads(result.stdout)
        video = [s for s in data["streams"] if s.get("codec_type") == "video"]
        audio = [s for s in data["streams"] if s.get("codec_type") == "audio"]
        duration = float(data["format"]["duration"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "media_probe_failed") from exc
    if "mp4" not in data["format"].get("format_name", "").split(","):
        raise HTTPException(422, "media_container_mismatch")
    if len(video) != 1 or video[0].get("codec_name") != "h264" or video[0].get("width") != 1080 or video[0].get("height") != 1920:
        raise HTTPException(422, "media_video_mismatch")
    if not 5 <= duration <= 30 or bool(audio) != declared_audio:
        raise HTTPException(422, "media_duration_or_audio_mismatch")
    return {"width": 1080, "height": 1920, "codec": "h264", "durationSeconds": duration, "hasAudio": bool(audio)}


def _commit(page: str, sha: str, total: int, chunks: list[bytes], booking: dict, idempotency_key: str,
            source_treatment: dict | None = None) -> dict:
    path = _receipt_path(page, sha)
    video = path.with_suffix('.mp4')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_for(path):
        existing = atomic_load(path, default=None)
        if existing is not None:
            if not video.is_file() or video.stat().st_size != total or hashlib.sha256(video.read_bytes()).hexdigest() != sha:
                raise HTTPException(409, 'finished_clip_storage_mismatch')
            return existing
        # A prior atomic receipt write may have failed after the video rename.
        # Verify that exact orphan before completing its immutable receipt.
        orphan = video.exists()
        if orphan and (not video.is_file() or video.stat().st_size != total or hashlib.sha256(video.read_bytes()).hexdigest() != sha):
            raise HTTPException(409, 'finished_clip_storage_mismatch')
        partial = path.with_name(f'{sha}.{uuid4().hex}.part')
        try:
            if not orphan:
                with partial.open('xb') as handle:
                    for chunk in chunks:
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            media = _probe(video if orphan else partial, False)
            receipt = {'schema': 'content-lab.finished-clip.v1', 'provenance': 'finished_clip_booked',
                       'pageId': page, 'sha256': sha, 'bytes': total, 'booking': booking,
                       'media': media, 'idempotencyKey': idempotency_key,
                       'createdAt': datetime.now(timezone.utc).isoformat()}
            if source_treatment is not None:
                receipt['provenance'] = 'joy_ai_input_reuse'
                receipt['sourceTreatment'] = source_treatment
            if not orphan:
                os.replace(partial, video)
            atomic_save(path, receipt)
            return receipt
        finally:
            partial.unlink(missing_ok=True)


@router.post("/v1/finished-clips")
async def create_finished_clip(request: Request, authorization: str | None = Header(None),
                               x_rt_page_id: str | None = Header(None), idempotency_key: str | None = Header(None)) -> dict:
    require_control_plane_bearer(authorization)
    page = _required(x_rt_page_id, "X-RT-Page-Id", 128)
    if not PAGE.fullmatch(page):
        raise HTTPException(400, "invalid page")
    _required(idempotency_key, "Idempotency-Key", 200)
    try:
        booking = json.loads(request.headers.get("x-rt-finished-clip-booking", ""))
    except ValueError as exc:
        raise HTTPException(400, "booking_json_required") from exc
    if not isinstance(booking, dict) or booking.get("pageId") != page:
        raise HTTPException(400, "booking_page_mismatch")
    sha = booking.get("bookedSha256")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise HTTPException(400, "booked_sha256_invalid")
    for field in ("publicationId", "masterPagesHash"):
        _required(booking.get(field), field)
    for field in ("qaReceiptPath", "qaReceiptSha256", "clearance914Ref", "clearanceEmRef"):
        if field in booking:
            _required(booking[field], field, 500)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", booking["masterPagesHash"]):
        raise HTTPException(400, "master_pages_hash_invalid")
    if "bookedDateTime" in booking:
        value = booking["bookedDateTime"]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise HTTPException(400, "booked_datetime_invalid")
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise HTTPException(400, "booked_datetime_invalid") from exc
    if not _booked(page, sha):
        raise HTTPException(400, 'clip_not_booked')
    if (booking.get("hasAudio") is not False or not isinstance(booking.get("treatment"), dict)
            or booking["treatment"].get("textMode") != "blank"):
        raise HTTPException(400, "audio_and_treatment_required")
    if page == "acct:burnt-mill-rd" and (booking["hasAudio"] or booking["treatment"].get("textMode") != "blank" or booking["treatment"].get("captionMode") != "in_app"):
        raise HTTPException(400, "burnt_treatment_mismatch")
    source_treatment = _joy_source_treatment(booking, sha) if page == JOY_PAGE else None
    declared_bytes = booking.get("bytes")
    if type(declared_bytes) is not int or not 0 < declared_bytes <= MAX_BYTES:
        raise HTTPException(413, "clip_size_invalid")
    try:
        if request.headers.get("content-length") and int(request.headers["content-length"]) > MAX_BYTES:
            raise HTTPException(413, "clip_too_large")
    except ValueError as exc:
        raise HTTPException(400, "content_length_invalid") from exc
    chunks, total, digest = [], 0, hashlib.sha256()
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BYTES:
            raise HTTPException(413, "clip_too_large")
        digest.update(chunk)
        chunks.append(chunk)
    if total != declared_bytes or digest.hexdigest() != sha:
        raise HTTPException(422, "booked_bytes_mismatch")
    return await run_in_threadpool(_commit, page, sha, total, chunks, booking, idempotency_key, source_treatment)


@router.get("/v1/finished-clips/{sha}")
def read_finished_clip(sha: str, authorization: str | None = Header(None), x_rt_page_id: str | None = Header(None)) -> dict:
    require_control_plane_bearer(authorization)
    if not x_rt_page_id or not PAGE.fullmatch(x_rt_page_id) or not SHA.fullmatch(sha):
        raise HTTPException(404, "finished_clip_not_found")
    receipt = atomic_load(_receipt_path(x_rt_page_id, sha), default=None)
    if not isinstance(receipt, dict) or receipt.get("pageId") != x_rt_page_id or receipt.get("sha256") != sha:
        raise HTTPException(404, "finished_clip_not_found")
    return receipt


@router.get("/v1/finished-clips/{sha}/download")
def download_finished_clip(sha: str, authorization: str | None = Header(None), x_rt_page_id: str | None = Header(None)) -> FileResponse:
    read_finished_clip(sha, authorization, x_rt_page_id)
    video = _receipt_path(x_rt_page_id, sha).with_suffix(".mp4")
    if not video.is_file() or video.stat().st_size > MAX_BYTES or hashlib.sha256(video.read_bytes()).hexdigest() != sha:
        raise HTTPException(409, "finished_clip_storage_mismatch")
    return FileResponse(video, media_type="video/mp4")
