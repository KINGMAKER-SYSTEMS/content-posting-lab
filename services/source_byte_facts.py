"""Exact-byte raw inventory facts; no scan, final-audio or native-sound authority."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone

SCHEMA = "content-lab.source-byte-facts.v1"
PHASE = "source_blank_inventory"
SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 128 * 1024 * 1024
PROBE_SECONDS = 3.0
PROBE_STDOUT_BYTES = 65536
PROBE_STDERR_BYTES = 8192
SOURCE_KINDS = frozenset({"generated", "dossier_source_dna", "syzygy_slideshow", "truck_master_recovery"})
_PROBE_LOCK = threading.Lock()
POLICY = {"version": 1, "phase": PHASE, "maxBytes": MAX_BYTES,
          "probeSeconds": PROBE_SECONDS, "probeStdoutBytes": PROBE_STDOUT_BYTES,
          "probeStderrBytes": PROBE_STDERR_BYTES, "maxStreams": 128,
          "protocols": "file,pipe", "newVisualScan": False,
          "agePolicy": "immutable_bytes_and_current_policy", "positiveVisual": "clean"}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def producer_identity():
    from services import visual_admission as visual
    root = Path(__file__).resolve().parent.parent
    tool = shutil.which("ffprobe")
    return {"sourceSha256": digest(Path(__file__)),
            "policySha256": hashlib.sha256(_canonical({**POLICY,
                "visualAlgorithm": visual.ALGORITHM,
                "visualSourceSha256": digest(Path(visual.__file__)),
                "serializerSha256": digest(root / "routers/control_plane.py"),
                "authSha256": digest(root / "routers/control_plane_recipes.py"),
                "probeToolSha256": digest(Path(tool)) if tool else None})).hexdigest()}


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    except (TypeError, ValueError):
        return False


def enabled_for(job):
    """Only server-owned, exact page/recipe cohort configuration selects this phase."""
    if os.environ.get("CONTENT_LAB_SOURCE_BYTE_FACTS") != "on":
        return False
    try:
        rows = json.loads(os.environ.get("CONTENT_LAB_SOURCE_BYTE_FACTS_COHORT", "[]"))
        if not isinstance(rows, list) or not 0 < len(rows) <= 100:
            return False
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"pageId", "recipeSpecHash"}:
                return False
            if (not isinstance(row["pageId"], str) or not 0 < len(row["pageId"]) <= 200
                    or any(ord(c) < 32 for c in row["pageId"])
                    or not isinstance(row["recipeSpecHash"], str)
                    or not SHA.fullmatch(row["recipeSpecHash"].removeprefix("sha256:"))):
                return False
        return any(row["pageId"] == job.get("pageId")
                   and row["recipeSpecHash"] == job.get("recipeSpecHash") for row in rows)
    except (ValueError, TypeError):
        return False


def binding(job, job_id, index, clip):
    return {"pageId": job.get("pageId"), "jobId": job_id, "outputIndex": index,
            "sha256": clip.get("sha256"), "bytes": clip.get("bytes"),
            "recipeSpecHash": job.get("recipeSpecHash"), "policyHash": job.get("policyHash")}


def valid_binding(value):
    return (isinstance(value.get("pageId"), str) and bool(value["pageId"])
            and isinstance(value.get("jobId"), str) and bool(value["jobId"])
            and type(value.get("outputIndex")) is int and 0 <= value["outputIndex"] < 100
            and isinstance(value.get("sha256"), str) and bool(SHA.fullmatch(value["sha256"]))
            and type(value.get("bytes")) is int and 0 < value["bytes"] <= MAX_BYTES
            and isinstance(value.get("recipeSpecHash"), str)
            and bool(SHA.fullmatch(value["recipeSpecHash"].removeprefix("sha256:")))
            and isinstance(value.get("policyHash"), str) and 0 < len(value["policyHash"]) <= 200
            and not any(ord(c) < 32 for c in value["policyHash"]))


def reusable_audio(previous, identity, producer, evidence=None):
    if not isinstance(previous, dict):
        return False
    custody, audio = previous.get("custody"), previous.get("audio")
    if not isinstance(custody, dict) or not isinstance(audio, dict):
        return False
    if (previous.get("schema") != SCHEMA or previous.get("phase") != PHASE
            or any(previous.get(k) != v for k, v in identity.items())
            or previous.get("producer") != producer
            or custody != {"beforeSha256": identity["sha256"], "beforeBytes": identity["bytes"],
                          "afterSha256": identity["sha256"], "afterBytes": identity["bytes"]}
            or audio.get("status") != "MEASURED" or type(audio.get("audioStreams")) is not int
            or not 0 <= audio["audioStreams"] <= 128
            or not isinstance(audio.get("probeSha256"), str)
            or not SHA.fullmatch(audio["probeSha256"])):
        return False
    if (not isinstance(evidence, dict) or not isinstance(evidence.get("probeJson"), str)
            or len(evidence["probeJson"].encode()) > PROBE_STDOUT_BYTES
            or hashlib.sha256(evidence["probeJson"].encode()).hexdigest() != audio["probeSha256"]
            or evidence.get("probeSha256") != audio["probeSha256"]):
        return False
    try:
        if _audio_count(evidence["probeJson"].encode()) != audio["audioStreams"]:
            return False
    except (TypeError, ValueError):
        return False
    return _timestamp(previous.get("measuredAt"))


def _source_matches_identity(path, identity):
    """Confirm the current regular file still matches the bound artifact bytes."""
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            return False
        size = path.stat().st_size
        return (0 < size <= MAX_BYTES and size == identity["bytes"]
                and digest(path) == identity["sha256"])
    except OSError:
        return False


def _audio_count(raw):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("audio_probe_invalid")
            result[k] = v
        return result
    data = json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("audio_probe_invalid")))
    streams = data.get("streams") if isinstance(data, dict) else None
    if (not isinstance(streams, list) or not 1 <= len(streams) <= 128
            or any(not isinstance(s, dict) or type(s.get("index")) is not int
                   or s["index"] < 0 or s.get("codec_type") not in
                   {"video", "audio", "subtitle", "data", "attachment"} for s in streams)
            or len({s["index"] for s in streams}) != len(streams)
            or sum(s["codec_type"] == "video" for s in streams) != 1
            or not isinstance(data.get("format"), dict)
            or not isinstance(data["format"].get("format_name"), str)
            or "mp4" not in data["format"]["format_name"].split(",")):
        raise ValueError("audio_probe_invalid")
    return sum(s["codec_type"] == "audio" for s in streams)


def _probe_unlocked(path):
    """Bounded real all-stream inventory; never invoke a visual/provider scan."""
    tool = shutil.which("ffprobe")
    if tool is None:
        raise ValueError("audio_probe_unavailable")
    argv = [tool, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
            "stream=index,codec_type:format=format_name", "-of", "json", str(path)]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True,
                            env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"})
    sel = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    drained = False
    try:
        for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ, label)
        deadline = time.monotonic() + PROBE_SECONDS
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("audio_probe_timeout")
            for key, _ in sel.select(min(.1, remaining)):
                chunk = os.read(key.fd, 16384)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                limit = PROBE_STDOUT_BYTES if key.data == "stdout" else PROBE_STDERR_BYTES
                if len(output[key.data]) > limit:
                    raise ValueError("audio_probe_output_limit")
        drained = True
        if proc.wait(timeout=max(.001, deadline-time.monotonic())) != 0 or output["stderr"]:
            raise ValueError("audio_probe_failed")
        raw = bytes(output["stdout"])
        return _audio_count(raw), raw
    finally:
        sel.close()
        if not drained or proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _probe(path):
    if not _PROBE_LOCK.acquire(blocking=False):
        raise ValueError("audio_probe_busy")
    try:
        return _probe_unlocked(path)
    finally:
        _PROBE_LOCK.release()


def cached_visual(job, job_id, index, clip):
    from services import visual_admission as visual
    records = job.get("visualAdmission")
    decision = records.get(str(index)) if isinstance(records, dict) else None
    expected = {"schema": visual.SCHEMA, "pageId": job.get("pageId"), "jobId": job_id,
                "outputIndex": index, "sha256": clip.get("sha256"), "bytes": clip.get("bytes")}
    if (not isinstance(decision, dict) or any(decision.get(k) != v for k, v in expected.items())
            or type(decision.get("outputIndex")) is not int or type(decision.get("bytes")) is not int
            or not isinstance(decision.get("sampling"), dict)
            or not isinstance(decision.get("ocr"), dict) or not isinstance(decision.get("model"), dict)
            or decision["sampling"].get("algorithm") != visual.ALGORITHM
            or not _timestamp(decision.get("scannedAt"))
            or not visual.is_final_decision(decision)):
        return {"status": "UNKNOWN", "decision": None}
    return {"status": "CACHED_FINAL", "decision": copy.deepcopy(decision)}


def build(job, job_id, index, clip, path, *, allow_probe=False, _producer=None):
    """Return wire facts plus private raw probe evidence, without writing any state."""
    identity = binding(job, job_id, index, clip)
    producer = _producer if _producer is not None else producer_identity()
    result = {"schema": SCHEMA, "phase": PHASE, "status": "UNKNOWN", **identity,
              "measuredAt": datetime.now(timezone.utc).isoformat(), "producer": producer,
              "custody": {"beforeSha256": None, "beforeBytes": None,
                          "afterSha256": None, "afterBytes": None},
              "audio": {"status": "UNKNOWN", "audioStreams": None, "probeSha256": None},
              "visual": {"status": "UNKNOWN", "decision": None},
              "native": {"sound": "NOT_ASSESSED", "end": "NOT_ASSESSED"},
              "reason": "facts_pending"}
    if (not valid_binding(identity) or job.get("jobId", job_id) != job_id
            or job.get("sourceKind") not in SOURCE_KINDS
            or job.get("recipeId") == "lyric-video:master" or clip.get("lyricTiming") is not None):
        result["reason"] = "unsupported_source_binding_or_phase"
        return result, None
    source_matches = _source_matches_identity(path, identity)
    if source_matches:
        result["visual"] = cached_visual(job, job_id, index, clip)
    records = job.get("sourceByteFacts")
    previous = records.get(str(index)) if isinstance(records, dict) else None
    private_records = job.get("sourceByteFactsEvidence")
    old_evidence = private_records.get(str(index)) if isinstance(private_records, dict) else None
    private = None
    if source_matches and reusable_audio(previous, identity, producer, old_evidence):
        result.update(audio=copy.deepcopy(previous["audio"]), custody=copy.deepcopy(previous["custody"]),
                      measuredAt=previous["measuredAt"])
    elif allow_probe:
        try:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("source_unreadable")
            size = path.stat().st_size
            if not 0 < size <= MAX_BYTES:
                raise ValueError("source_size_invalid")
            result["custody"].update(beforeSha256=digest(path), beforeBytes=size)
            if result["custody"]["beforeSha256"] != identity["sha256"] or size != identity["bytes"]:
                raise ValueError("source_identity_mismatch")
            count, raw = _probe(path)
            result["custody"].update(afterSha256=digest(path), afterBytes=path.stat().st_size)
            if (result["custody"]["afterSha256"] != identity["sha256"]
                    or result["custody"]["afterBytes"] != identity["bytes"]):
                raise ValueError("source_changed_during_probe")
            if producer_identity() != producer:
                raise ValueError("producer_changed_during_probe")
            result["audio"] = {"status": "MEASURED", "audioStreams": count,
                               "probeSha256": hashlib.sha256(raw).hexdigest()}
            private = {"probeJson": raw.decode("utf-8"), "probeSha256": hashlib.sha256(raw).hexdigest()}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            codes = {"source_unreadable", "source_size_invalid", "source_identity_mismatch",
                     "source_changed_during_probe", "audio_probe_unavailable", "audio_probe_timeout",
                     "audio_probe_output_limit", "audio_probe_failed", "audio_probe_invalid",
                     "audio_probe_busy", "producer_changed_during_probe"}
            result["reason"] = str(error) if str(error) in codes else "audio_probe_unavailable"
            return result, None
    if result["audio"]["status"] != "MEASURED":
        return result, None
    if result["audio"]["audioStreams"] > 0:
        result.update(status="MEASURED", reason="audio_streams_present")
        return result, private
    decision = result["visual"]["decision"]
    if result["visual"]["status"] != "CACHED_FINAL":
        result["reason"] = "visual_not_final"
    elif decision.get("verdict") == "text":
        result.update(status="MEASURED", reason="visual_text_detected")
    elif (decision.get("verdict") == "clean" and decision.get("ocr", {}).get("status") == "clean"
          and decision.get("model", {}).get("status") == "clean"
          and decision.get("reason") == "full_frame_ocr_and_vision_clean"
          and decision.get("sampling", {}).get("mode") == "all_frames"
          and type(decision["sampling"].get("frameCount")) is int
          and type(decision["sampling"].get("expectedFrameCount")) is int
          and decision["sampling"]["frameCount"] > 0
          and decision["sampling"]["frameCount"] == decision["sampling"].get("expectedFrameCount")
          and isinstance(decision["model"].get("sampledFrames"), list)
          and len(decision["model"]["sampledFrames"]) > 0
          and all(type(n) is int and 0 <= n < decision["sampling"]["frameCount"]
                  for n in decision["model"]["sampledFrames"])):
        result.update(status="MEASURED", reason="raw_source_facts_measured")
    else:
        result["reason"] = "visual_unavailable"
    return result, private
