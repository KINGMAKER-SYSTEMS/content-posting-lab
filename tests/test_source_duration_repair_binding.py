"""A repair command is authority only as the derivation of its own slot.

``rt.source-duration-repair.v1`` names its own slot, page, source and evidence,
so a schema-valid command proves nothing by itself. The immutable slot carries a
hash-free ``source_trim`` policy; the command must be exactly what that slot
asks for, and the delivered final must be exactly the commanded cut. These
tests pin that binding and the exact-output rule through the real submission
model, the real durable runner and real ffmpeg.

The Joy shape: a silent 768x1344, 24 fps, 226-frame treated parent (9.416667 s)
cut to [0, 7500) ms must deliver exactly 225 frames, 7500 ms, with no audio.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from services import post_render_jobs as jobs
from services.post_render import sha256
from services.source_duration_repair import command_digest
from services.source_trim_policy import (
    REQUEST_IDENTITY_KEYS, SOURCE_TRIM_POLICY_SCHEMA, TRIM_POLICY_KEYS, font_file_sha256, trim_policy_of,
)
from tests.test_post_render import NOW
from tests.test_source_duration_repair_lab import ENV, _bound, _make_source, _service, _timed_submission

FFMPEG = shutil.which("ffmpeg")


def _body(**trim) -> dict:
    return _bound(**trim)[2].model_dump(by_alias=True)


def _rehash(body: dict, slot: dict) -> dict:
    """Store a changed slot and carry its new hash everywhere the old one was."""
    body["slot_payload_json"] = json.dumps(slot, separators=(",", ":"))
    slot_hash = sha256(body["slot_payload_json"].encode())
    body["request"]["slot_payload_sha256"] = slot_hash
    if body["request"].get("source_duration_repair") is not None:
        body["request"]["source_duration_repair"]["slot_payload_sha256"] = slot_hash
    return body


def _consistent(body: dict, **changes) -> dict:
    """Change the command AND the slot's own policy the same way (a self-consistent forgery)."""
    command = body["request"]["source_duration_repair"]
    command.update(changes)
    slot = json.loads(body["slot_payload_json"])
    slot["source_trim"] = trim_policy_of(command)
    return _rehash(body, slot)


def _refused(body: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        jobs.RenderJobSubmission.model_validate(body)


# --- the binding ---------------------------------------------------------------------------

def test_the_worker_shaped_submission_is_accepted_and_its_slot_policy_names_no_slot():
    command, request, body = _bound()
    slot = json.loads(body.slot_payload_json)
    assert slot["source_trim"] == trim_policy_of(command)
    assert slot["source_trim"]["schema"] == SOURCE_TRIM_POLICY_SCHEMA
    # Hash-free: the policy repeats nothing that identifies the slot or its bytes.
    assert set(slot["source_trim"]) == {"schema", *TRIM_POLICY_KEYS}
    assert not set(slot["source_trim"]) & set(REQUEST_IDENTITY_KEYS)
    assert request.source_duration_repair == command
    assert command["slot_payload_sha256"] == sha256(body.slot_payload_json.encode())


@pytest.mark.parametrize("key", [key for key in REQUEST_IDENTITY_KEYS if key != "slot_payload_sha256"])
def test_command_identity_must_be_the_render_requests(key):
    body = _body()
    command = body["request"]["source_duration_repair"]
    command[key] = "f" * 64 if key == "source_sha256" else command[key] + "x"
    _refused(body, f"repair command {key} does not match the render request")


def test_command_slot_hash_must_be_the_hashed_slot():
    body = _body()
    body["request"]["source_duration_repair"]["slot_payload_sha256"] = "0" * 64
    _refused(body, "repair command slot_payload_sha256 does not match the render request")


@pytest.mark.parametrize("field,change", [
    ("sound", lambda slot: slot["sound"].update(music_id="7000000000000000001")),
    ("sound", lambda slot: slot.pop("sound")),
    ("format", lambda slot: slot.update(format="truck-night")),
    ("format", lambda slot: slot.pop("format")),
])
def test_command_sound_and_format_must_be_the_slots(field, change):
    body = _body()
    slot = json.loads(body["slot_payload_json"])
    change(slot)
    _refused(_rehash(body, slot), f"repair command {field} does not match the immutable slot")


@pytest.mark.parametrize("key,value", [
    ("measured_quantization_ms", 2), ("sample_clock_hz", 44100), ("native_official_start_ms", 1),
    ("official_start_ms", 1), ("compressed_reference_sha256", "1" * 64), ("tail_evidence_id", "ev-tail-2"),
    ("expected_output_frames", 59), ("retimed_lyric_cue_sha256", "2" * 64),
    ("recipe_id", "recipe-2"), ("recipe_version", "v2"),
])
def test_command_trim_evidence_and_recipe_must_be_the_slots_source_trim(key, value):
    # A schema-valid command that differs from what the slot asked for in one field.
    body = _body()
    body["request"]["source_duration_repair"][key] = value
    _refused(body, "repair command trim, evidence or recipe does not match the immutable slot source trim")


def test_a_shorter_cut_than_the_slot_asked_for_is_refused():
    body = _body()
    body["request"]["source_duration_repair"].update(
        source_trim_end_ms=1900, official_end_ms=1900, tail_evidence_end_ms=1900,
        expected_output_duration_ms=1900, expected_output_frames=57)
    _refused(body, "repair command trim, evidence or recipe does not match the immutable slot source trim")


def test_a_command_without_a_slot_source_trim_is_not_authority():
    body = _body()
    slot = json.loads(body["slot_payload_json"])
    slot.pop("source_trim")
    _refused(_rehash(body, slot), "repair command is not backed by a source trim in the immutable slot")
    slot["source_trim"] = None
    _refused(_rehash(body, slot), "repair command is not backed by a source trim in the immutable slot")


def test_a_slot_that_asks_for_a_cut_is_never_rendered_uncut():
    body = _body()
    body["request"].pop("source_duration_repair")
    _refused(body, "slot carries a source trim but the request has no repair command")


def test_command_font_must_be_the_slot_caption_font_and_its_file_digest():
    font = json.loads(_bound()[1].render_treatment_json)["captionStyle"]["font"]
    assert font_file_sha256(font) is not None, "fixture control: the Lab has this font file"
    body = _body()
    body["request"]["source_duration_repair"]["font_sha256"] = "3" * 64
    _refused(body, "repair command font is not the slot caption font and its file digest")
    body = _body()
    body["request"]["source_duration_repair"]["font_id"] = "TikTokSans-Regular.ttf"
    _refused(body, "repair command font is not the slot caption font and its file digest")
    for unsafe in ("../services/post_render.py", ".hidden", "", "missing-font.ttf"):
        assert font_file_sha256(unsafe) is None


def test_frames_must_be_exactly_the_duration_on_the_delivery_clock():
    # Self-consistent (slot and command agree) but 59 frames is not 2000 ms at 30 fps.
    _refused(_consistent(_body(), expected_output_frames=59),
             "repair command frames are not its duration on the 30 fps delivery clock")
    jobs.RenderJobSubmission.model_validate(_consistent(_body(), expected_output_frames=60))  # control


def test_timed_render_refuses_a_slot_that_asks_for_a_cut():
    body = _timed_submission(repair=None).model_dump(by_alias=True)
    slot = json.loads(body["slot_payload_json"])
    slot["source_trim"] = trim_policy_of(_bound()[0])
    _refused(_rehash(body, slot), "timed render does not support a source trim")


def test_an_ordinary_submission_is_unchanged():
    from tests.test_post_render_jobs import submission
    body = submission().model_dump(by_alias=True)
    assert "source_trim" not in json.loads(body["slot_payload_json"])
    assert jobs.RenderJobSubmission.model_validate(body).model_dump(by_alias=True) == body


# --- the delivered final is exactly the commanded cut ---------------------------------------

def _silent_joy_parent(path: Path) -> Path:
    """The Joy treated-parent shape: 768x1344, 24 fps, 226 frames (9.416667 s), no audio."""
    if not FFMPEG:
        pytest.fail("ffmpeg required for repair evidence")
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=768x1344:rate=24",
                    "-frames:v", "226", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)
    return path


def _ffprobe(path: Path) -> dict:
    raw = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,nb_read_frames,r_frame_rate,duration", "-of", "json", str(path)],
                         check=True, capture_output=True, timeout=120).stdout
    return json.loads(raw)


def test_joy_silent_parent_is_cut_to_exactly_225_frames_through_the_durable_runner(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    parent = _silent_joy_parent(tmp_path / "joy-parent.mp4")
    facts = _ffprobe(parent)["streams"]
    assert [s["codec_type"] for s in facts] == ["video"] and facts[0]["nb_read_frames"] == "226"
    command, _request, body = _bound(sha256(parent.read_bytes()), end_ms=7500)
    assert (command["expected_output_frames"], command["expected_output_duration_ms"]) == (225, 7500)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(parent, path))
    job = service.enqueue(body, "joy-silent-225")
    assert service.run_one()
    status = service.status(job["id"])
    assert status["state"] == "succeeded", status
    receipt = json.loads(service.artifact(job["id"], "receipt").read_bytes())
    assert receipt["duration_ms"] == 7500
    assert receipt["source_sha256"] == command["source_sha256"], "the receipt still names the 9.4 s parent"
    assert receipt["source_duration_repair_sha256"] == command_digest(command)
    streams = _ffprobe(service.artifact(job["id"], "final"))["streams"]
    assert [s["codec_type"] for s in streams] == ["video"], "a silent parent delivers no audio stream"
    assert streams[0]["nb_read_frames"] == "225" and streams[0]["r_frame_rate"] == "30/1"
    assert abs(float(streams[0]["duration"]) - 7.5) < 0.0005
    row = service._row(job["id"])
    authority = service._load_output_authority(row)
    assert authority["decode"]["decoded_video_frames"] == 225

    def refused(mutate):
        changed = copy.deepcopy(authority)
        mutate(changed)
        with pytest.raises(jobs.RenderJobError) as error:
            service._validate_output_authority(row, changed)
        return error.value.code
    # Durable acceptance requires the exact cut: one frame short is not "at most 225".
    assert refused(lambda a: a["decode"].update(decoded_video_frames=224)) == "receipt_binding_mismatch"
    assert refused(lambda a: a["decode"].update(decoded_video_frames=226)) == "receipt_binding_mismatch"

    def shorter(a):
        a["receipt"]["duration_ms"] = 7467
        a["decode"]["final_probe"]["duration_ms"] = 7467
    assert refused(shorter) == "receipt_binding_mismatch"
    service._validate_output_authority(row, copy.deepcopy(authority))  # control: untouched, it validates


def test_a_final_one_frame_short_of_the_command_never_becomes_ready(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src)
    command, _request, body = _bound(sha256(src.read_bytes()), end_ms=2000)
    assert command["expected_output_frames"] == 60

    def one_frame_short(source_path, output_directory, repair, **_kwargs):
        # A repair encoder that honours "at most" instead of "exactly": 59 frames.
        output_directory = Path(output_directory)
        output_directory.mkdir()
        subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
                        "-frames:v", "59", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                        "-pix_fmt", "yuv420p", str(output_directory / "final.mp4")], check=True, timeout=120)
        return {}
    import services.source_duration_repair as module
    monkeypatch.setattr(module, "render_shorter", one_frame_short)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    job = service.enqueue(body, "one-frame-short")
    assert service.run_one()
    status = service.status(job["id"])
    assert status["state"] != "succeeded", status
    assert status["error_code"] == "repair_output_mismatch"
