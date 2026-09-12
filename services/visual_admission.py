"""Bounded, pre-caption visual evidence. Detection is evidence, not a perfect oracle.

Every decoded frame is decoded at native resolution and OCR'd at the configured
working long edge. GLM sees up to 16 evenly spaced native frames. Clean requires
both independent detectors and complete coverage. Missing evidence, resource
exhaustion and malformed replies fail closed.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
import hashlib
import fcntl
import io
import json
import os
import selectors
import shutil
import signal
import subprocess
import threading
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

SCHEMA = "content-lab.visual-admission.v1"
MODEL = "glm-4.6v-flash"
VISION_URL = "https://api.z.ai/api/paas/v4/chat/completions"
MAX_BYTES = 128 * 1024 * 1024
MAX_FRAMES = 600
MAX_PIXELS = 4096 * 2160
MAX_VISION_BYTES = 32 * 1024 * 1024
TIMEOUT = 420
# 0 preserves the historical native OCR default. A positive value is the OCR
# working long edge; decode and GLM inputs remain native regardless.
OCR_LONG_EDGE = int(os.environ.get("CONTENT_LAB_OCR_LONG_EDGE", "0") or 0)
_GATE = threading.BoundedSemaphore(1)


def _lock_path() -> Path:
    return Path(tempfile.gettempdir()) / "content-lab-visual-admission.lock"


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _run(args, *, timeout, input=None):
    # No shell, network protocols or inherited process lifetime.
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    try:
        out, _ = proc.communicate(input=input, timeout=timeout)
        if proc.returncode:
            raise RuntimeError("detector_process_failed")
        return out
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def _probe(path, deadline):
    data = json.loads(_run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-select_streams", "v:0", "-count_frames", "-show_entries",
        "stream=width,height,nb_read_frames,duration", "-of", "json", str(path)],
        timeout=min(30, max(.1, deadline-time.monotonic()))))
    stream = data["streams"][0]
    width, height, frames = (int(stream[k]) for k in ("width", "height", "nb_read_frames"))
    if min(width, height, frames) < 1 or width * height > MAX_PIXELS or frames > MAX_FRAMES:
        raise RuntimeError("frame_budget_exceeded")
    return width, height, frames, float(stream["duration"])


def _frames(path, width, height, deadline):
    # Stream exactly one native RGB frame at a time: bounded RAM, no frame cache.
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-threads", "1",
        "-protocol_whitelist", "file,pipe", "-noautorotate", "-i", str(path), "-map", "0:v:0",
        "-fps_mode", "passthrough", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            frame = bytearray()
            while len(frame) < width * height * 3:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RuntimeError("scan_timeout")
                chunk = os.read(proc.stdout.fileno(), min(65536, width * height * 3-len(frame)))
                if not chunk:
                    if frame or proc.wait(timeout=2) != 0:
                        raise RuntimeError("incomplete_decode")
                    return
                frame.extend(chunk)
            yield bytes(frame)
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()


def _ocr(frame, width, height, deadline, frame_budget):
    # Decode stays native; only the OCR working image may be resized.
    ocr_width, ocr_height = width, height
    if OCR_LONG_EDGE > 0 and max(width, height) > OCR_LONG_EDGE:
        scale = OCR_LONG_EDGE / max(width, height)
        ocr_width, ocr_height = round(width * scale), round(height * scale)
        frame = Image.frombytes("RGB", (width, height), frame).resize((ocr_width, ocr_height)).tobytes()
    ppm = f"P6\n{ocr_width} {ocr_height}\n255\n".encode() + frame
    # The cap is a share of the whole scan budget, derived from the probed frame
    # count; it is never a fixed wall-clock allowance per frame.
    pixel_ratio = (width * height) / max(1, (OCR_LONG_EDGE or max(width, height)) ** 2)
    per_frame = (TIMEOUT * 4 * pixel_ratio) / max(1, frame_budget)
    text = _run(["tesseract", "stdin", "stdout", "--psm", "12", "-l", "eng"], input=ppm,
                timeout=min(max(.1, deadline-time.monotonic()), max(.1, per_frame))).decode("utf-8", "strict")
    # Any recognized glyph rejects; orientation gibberish is still text evidence.
    return text.strip()[:500]


def _scanned_frames(path, width, height, deadline, expected):
    frames = _frames(path, width, height, deadline)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            number = 0
            while batch := list(islice(frames, 2)):
                pending = [pool.submit(_ocr, frame, width, height, deadline, expected) for frame in batch]
                for frame, result in zip(batch, pending):
                    yield number, frame, result.result()
                    number += 1
    finally:
        frames.close() if hasattr(frames, "close") else None


def _vision(samples, deadline):
    key = os.environ.get("CONTENT_LAB_VISION_API_KEY", "").strip()
    if not key:
        raise RuntimeError("vision_credentials_unavailable")
    content = [{"type": "text", "text": 'Inspect every supplied frame for any existing writing, captions, logos with letters, numbers or watermarks. Return ONLY JSON {"verdict":"clean"|"text"|"unavailable","reason":"describe the visible scene and any text actually observed"}. Use unavailable when unreadable or uncertain. Any text, even brief or tiny, means text. These frames precede our caption stage.'}]
    for sample in samples:
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + sample}})
    with httpx.Client(timeout=min(60, max(.1, deadline-time.monotonic())), follow_redirects=False) as client:
        with client.stream("POST", VISION_URL, headers={"Authorization": f"Bearer {key}"}, json={
            "model": MODEL, "messages": [{"role": "user", "content": content}],
            "temperature": 0, "max_tokens": 512, "thinking": {"type": "disabled"}}) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=4096):
                if time.monotonic() > deadline:
                    raise RuntimeError("scan_timeout")
                if len(body) + len(chunk) > 65536:
                    raise RuntimeError("vision_response_oversized")
                body.extend(chunk)
            try:
                result = json.loads(json.loads(body)["choices"][0]["message"]["content"])
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise RuntimeError("vision_response_invalid") from error
    if result.get("verdict") not in {"clean", "text", "unavailable"} or not isinstance(result.get("reason"), str) or not result["reason"].strip():
        raise RuntimeError("vision_response_invalid")
    return result


ALGORITHM = "tesseract-psm12-configured-long-edge-rgb-v2"


def pending_decision(*, page_id, job_id, index, sha256, byte_count):
    return {"schema": SCHEMA, "verdict": "unavailable", "jobId": job_id, "outputIndex": index,
        "pageId": page_id, "sha256": sha256, "bytes": byte_count,
        "scannedAt": datetime.now(timezone.utc).isoformat(), "reason": "scan_pending",
        "sampling": {"mode": "all_frames", "algorithm": ALGORITHM, "frameCount": 0, "expectedFrameCount": 0},
        "model": {"name": MODEL, "status": "unavailable", "sampledFrames": []},
        "ocr": {"engine": "tesseract", "status": "unavailable", "psm": 12, "languages": ["eng", "osd"], "workingLongEdge": OCR_LONG_EDGE or "native"}}


def scan_artifact(path: Path, *, page_id: str, job_id: str, index: int, sha256: str, byte_count: int):
    decision = pending_decision(page_id=page_id, job_id=job_id, index=index, sha256=sha256, byte_count=byte_count)
    decision["reason"] = "scan_unavailable"
    if not _GATE.acquire(blocking=False):
        decision["reason"] = "scanner_busy_retry"
        return decision
    deadline = time.monotonic() + TIMEOUT
    lock_file = None
    try:
        lock_fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock_file = os.fdopen(lock_fd, "a+b")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            decision["reason"] = "scanner_busy_retry"
            return decision
        if byte_count < 1 or byte_count > MAX_BYTES or path.stat().st_size != byte_count or _hash(path) != sha256:
            raise RuntimeError("artifact_identity_mismatch")
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "tesseract")):
            raise RuntimeError("ocr_or_decoder_unavailable")
        width, height, expected, duration = _probe(path, deadline)
        decision["sampling"].update(expectedFrameCount=expected, width=width, height=height,
                                     durationSeconds=duration, durationMs=round(duration*1000))
        selected = {round(i*(expected-1)/min(15, expected-1)) for i in range(min(16, expected))} if expected > 1 else {0}
        samples = []
        for number, frame, text in _scanned_frames(path, width, height, deadline, expected):
            if number >= expected or number >= MAX_FRAMES:
                raise RuntimeError("frame_count_mismatch")
            decision["sampling"]["frameCount"] = number + 1
            if text:
                decision["ocr"].update(status="text", frame=number, text=text)
                decision.update(verdict="text", reason="pre_existing_text_ocr")
                return decision
            if number in selected:
                image = Image.frombytes("RGB", (width, height), frame)
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                samples.append(base64.b64encode(encoded.getvalue()).decode())
                if sum(len(sample) for sample in samples) > MAX_VISION_BYTES:
                    raise RuntimeError("vision_input_budget_exceeded")
                decision["model"]["sampledFrames"].append(number)
        if decision["sampling"]["frameCount"] != expected or _hash(path) != sha256:
            raise RuntimeError("incomplete_or_changed_artifact")
        decision["ocr"]["status"] = "clean"
        model = _vision(samples, deadline)
        decision["model"].update(status=model["verdict"], reason=model["reason"][:500])
        decision.update(verdict=model["verdict"], reason="full_frame_ocr_and_glm_clean" if model["verdict"] == "clean" else "pre_existing_text_glm" if model["verdict"] == "text" else "vision_uncertain")
    except subprocess.TimeoutExpired:
        decision["reason"] = "scan_timeout"
    except httpx.TimeoutException:
        decision["reason"] = "vision_timeout"
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        decision["reason"] = "vision_rate_limited" if code == 429 else "vision_auth_unavailable" if code in {401, 403} else "vision_service_unavailable"
        decision["model"]["reason"] = decision["reason"]
    except Exception as exc:
        # No remote body, credentials, or local filenames enter operator evidence.
        allowed = {"artifact_identity_mismatch", "ocr_or_decoder_unavailable", "frame_budget_exceeded",
            "scan_timeout", "incomplete_decode", "frame_count_mismatch", "incomplete_or_changed_artifact",
            "vision_input_budget_exceeded", "vision_credentials_unavailable", "vision_response_invalid", "vision_response_oversized", "detector_process_failed"}
        decision["reason"] = str(exc) if str(exc) in allowed else "visual_evidence_unavailable"
    finally:
        decision["scannedAt"] = datetime.now(timezone.utc).isoformat()
        if lock_file is not None:
            lock_file.close()
        _GATE.release()
    return decision
