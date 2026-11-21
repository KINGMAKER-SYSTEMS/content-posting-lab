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

router = APIRouter()
SHA = re.compile(r"[0-9a-f]{64}\Z")
PAGE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
MAX_BYTES = 32 * 1024 * 1024
# Exact bodies from custody 002125ae. Burnt custody records date/ordinal, not clock time.
BOOKED = {
    'acct:culture-core-1': {
        'e13d51e241fb2f730f225a818dc3081e6215b2e5f64d598898af8881ba15e30f',
        '7f80b4c43d474bb00b6444b64409a37c783ed204f2c7f1dc4a1622e8760b24c6',
        'f26488829851cb078cfb7515545b4a951c82790249430f775526a4488da9a8c0',
        '7f5d93ac64b442f32dd75f3e11d55c34d700923c40b0ae874cff6f44fba143d5',
        '3d263f8a3b72094ad03f6d89a7146d138add571fcb73886ef063b991e1761881',
        '67ee38f39b6988dc67af9ae5b364f2609f0983ac50c31adfdec5ca9a99b30a6d',
        '14e85e1b672e68e8d417fb6fbe6f47a0cc24f76f1e9c607a6889a2b5615886c6',
        '393865529d5374773b706124aedd965b1a1db3cd5593761d87b0412196497ff6',
        'cdfcdf0b9f9a2a0b30d80311482fa98a05b430a6a06b0d2d09637febc3450f8f',
        '02a1b415a06a9c75f5bb0828c9c70b4778a82c3065d2b9780622d5e68134b80d',
        'b462e975158302058bec0f7bd9ba042a0a6243221d447b634033281a34bd54cd',
        '77a6324f5666ed9635506c25c21cbf8bc44b45515fb79727b17571bda2e317ad',
        'db205375984a05dcf91d5247f10d58bafc184cc263b999e6b998dabb81f4b29f',
        'a3a22d208d722a7d48efebbc30e2008e393befc1fe537da81a01aa04b132ca70',
        '6dce01452d9390f3a26ecb647f92e71272c63e611d40927eb17e893fe22557e4',
        'bc90246031eaf63dc4e34453bc3fdb253a37a86b837ecae42155c2c55ef33999',
        '44b97f3abbd020e06e9e54465373888926bc1dfab08abfc8c0100f54a24c42b1',
        '922c204d18c7d5ad4ea43714710c69141a57146ea321910e5bd63da5daed0035',
        'a0d03109197eb7798abf58949bed30cbad46b0bb4d9699d01c719cc90099cf9d',
        '9c5ac0761f811239c06e40404e657f432a6ef490703401911ff535b7e6712a8b',
        '97e2513518eaca62e922c1b8a4c8741c0110bfba76f8a0d570dd5f62f2e32fe8',
        '29f056146be27d32d6402aa1713b6856bf16045f76be08a987225d0f939afff2',
        'c73f54b73427076b0898c9cc6e4aaf138e435bb5d285f1cf9a4c4c6d01846a3d',
        'bd0624e8365ea04aaf2f7ea8663bf5eb58ac4648a6391cdbd736a1fcd186ae67',
        '0ff1425b0ac33331eae683aad9e22b377b88cd253b0d5ae6dedf3ba5dbc4a143',
        '344157980bf78472316a2fc67cab8b3726c4b8f19e9d98a212994322b90078ad',
        '6604767ef4786e416cdb7b7e53eb621bc9379fdfae8d3bfce7059fed7c864e61',
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
            'e6c45da296d017a47a8dc7da9a07fc650eb8187ef8daea7f14d54df67ad57d56',
    },
}


def _booked(page: str, sha: str) -> bool:
    return sha in BOOKED.get(page, set())


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


def _commit(page: str, sha: str, total: int, chunks: list[bytes], booking: dict, idempotency_key: str) -> dict:
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
    return await run_in_threadpool(_commit, page, sha, total, chunks, booking, idempotency_key)


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
