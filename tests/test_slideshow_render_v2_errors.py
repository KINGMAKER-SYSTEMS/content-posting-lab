"""Failure-path tests for the slideshow V2 render pipeline (_run_render_v2).

The /slideshow/render-v2 endpoint runs a 3-phase FFmpeg pipeline
(Block 1 shuffle, Block 2 static, Assembly) in a thread pool. Each phase
shells out via subprocess.run and raises RuntimeError on a non-zero return
code; the synchronous worker records the failure in the shared `jobs` dict
and ALWAYS removes its tmp dir in a finally block.

These tests cover the gaps the unit tests left open:
  - intermediate FFmpeg failures at each of the 3 phases
  - overlay PNG base64 decode errors (and data-URI prefix stripping)
  - audio path resolution edge cases (missing vs present)
  - tmp directory cleanup on exception
"""

import base64
import subprocess
from pathlib import Path

import pytest

from routers import slideshow
from routers.slideshow import (
    BlockOneConfig,
    BlockTwoConfig,
    RenderV2Request,
    _run_render_v2,
    jobs,
)


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr


def _make_body(project="p", overlay_png=None, audio=None):
    return RenderV2Request(
        project=project,
        block1=BlockOneConfig(images=["a.jpg"], duration=2.0, overlay_png=overlay_png),
        block2=BlockTwoConfig(source="b.jpg", source_type="image", duration=2.0),
        audio=audio,
    )


@pytest.fixture
def patched_dirs(tmp_path, monkeypatch):
    """Point every project dir helper at isolated tmp subdirs."""
    images = tmp_path / "images"
    output = tmp_path / "output"
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    for d in (images, output, audio, video):
        d.mkdir(parents=True, exist_ok=True)
    # Source files must exist on disk: _validate_image checks existence, and
    # Block 2's image source resolves under the images dir as well.
    (images / "a.jpg").write_bytes(b"jpg-fake")
    (images / "b.jpg").write_bytes(b"jpg-fake")
    monkeypatch.setattr(slideshow, "get_project_slideshow_images_dir", lambda p: images)
    monkeypatch.setattr(slideshow, "get_project_slideshow_dir", lambda p: output)
    monkeypatch.setattr(slideshow, "get_project_slideshow_audio_dir", lambda p: audio)
    monkeypatch.setattr(slideshow, "get_project_video_dir", lambda p: video)
    return {"images": images, "output": output, "audio": audio, "video": video}


@pytest.fixture
def track_tmp(monkeypatch):
    """Capture the tmp dir created by the worker and whether it was cleaned up."""
    state = {"created": [], "removed": []}
    real_mkdtemp = slideshow.tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        state["created"].append(d)
        return d

    real_rmtree = slideshow.safe_rmtree

    def spy_rmtree(path, *a, **k):
        state["removed"].append(str(path))
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(slideshow.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(slideshow, "safe_rmtree", spy_rmtree)
    return state


def _assert_tmp_cleaned(track_tmp):
    assert track_tmp["created"], "worker never created a tmp dir"
    tmp = track_tmp["created"][0]
    assert tmp in track_tmp["removed"], "tmp dir was not passed to safe_rmtree"
    assert not Path(tmp).exists(), "tmp dir still on disk after worker finished"


# ── FFmpeg phase failures ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fail_call,expected_msg",
    [
        (1, "Block 1 FFmpeg failed"),
        (2, "Block 2 FFmpeg failed"),
        (3, "Assembly FFmpeg failed"),
    ],
)
def test_ffmpeg_failure_at_each_phase(
    patched_dirs, track_tmp, monkeypatch, fail_call, expected_msg
):
    """A non-zero return at phase N records that phase's error and cleans tmp."""
    calls = {"n": 0}

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        if calls["n"] == fail_call:
            return _FakeCompleted(1, b"boom-stderr")
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    job_id = "job-phase"
    _run_render_v2(job_id, _make_body())

    assert jobs[job_id]["status"] == "error"
    assert expected_msg in jobs[job_id]["message"]
    assert "boom-stderr" in jobs[job_id]["message"]
    assert calls["n"] == fail_call  # short-circuited at the failing phase
    _assert_tmp_cleaned(track_tmp)


def test_block1_failure_cascades_skip_block2_and_assembly(
    patched_dirs, track_tmp, monkeypatch
):
    """Block 1 failure must short-circuit: Block 2 and Assembly never shell out."""
    seen = {"block1": False, "block2": False, "assembly": False}

    def fake_run(cmd, *a, **k):
        joined = " ".join(str(c) for c in cmd)
        if joined.endswith("block1.mp4"):
            seen["block1"] = True
            return _FakeCompleted(1, b"block1-boom")
        if joined.endswith("block2.mp4"):
            seen["block2"] = True
        else:
            seen["assembly"] = True
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    job_id = "job-cascade"
    _run_render_v2(job_id, _make_body())

    assert jobs[job_id]["status"] == "error"
    assert "Block 1 FFmpeg failed" in jobs[job_id]["message"]
    assert seen["block1"] is True
    assert seen["block2"] is False, "Block 2 ran after Block 1 failed"
    assert seen["assembly"] is False, "Assembly ran after Block 1 failed"
    _assert_tmp_cleaned(track_tmp)


def test_assembly_failure_removes_partial_output(
    patched_dirs, track_tmp, monkeypatch
):
    """A failed assembly pass must not leave a partial mp4 in the output dir."""
    output_file = patched_dirs["output"] / "slideshow_job-orph.mp4"

    def fake_run(cmd, *a, **k):
        joined = " ".join(str(c) for c in cmd)
        if "concat=n=2" in joined:  # the assembly pass
            output_file.write_bytes(b"partial-corrupt-mp4")  # ffmpeg half-wrote it
            return _FakeCompleted(1, b"assembly-boom")
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    job_id = "job-orph"
    _run_render_v2(job_id, _make_body())

    assert jobs[job_id]["status"] == "error"
    assert "Assembly FFmpeg failed" in jobs[job_id]["message"]
    assert not output_file.exists(), "partial output mp4 left orphaned on disk"
    _assert_tmp_cleaned(track_tmp)


def test_success_path_records_complete_and_cleans_tmp(
    patched_dirs, track_tmp, monkeypatch
):
    """All three phases succeed → status complete, tmp still cleaned up."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    job_id = "job-ok"
    _run_render_v2(job_id, _make_body())

    assert jobs[job_id]["status"] == "complete"
    assert jobs[job_id]["progress"] == 100
    _assert_tmp_cleaned(track_tmp)


# ── Overlay PNG decoding ─────────────────────────────────────────────────


def test_overlay_bad_base64_errors_and_cleans_tmp(
    patched_dirs, track_tmp, monkeypatch
):
    """Undecodable overlay base64 fails before any FFmpeg call, tmp cleaned."""
    ran = {"n": 0}

    def fake_run(*a, **k):
        ran["n"] += 1
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    job_id = "job-badpng"
    _run_render_v2(job_id, _make_body(overlay_png="!!!not-base64!!!"))

    assert jobs[job_id]["status"] == "error"
    assert ran["n"] == 0  # decode blew up before Pass 1
    _assert_tmp_cleaned(track_tmp)


def test_overlay_data_uri_prefix_is_stripped(patched_dirs, track_tmp, monkeypatch):
    """A data: URI prefix is dropped and the rest is decoded to overlay.png."""
    raw = b"\x89PNG\r\n\x1a\n-fake"
    b64 = base64.b64encode(raw).decode()
    written = {"overlay": None}

    def fake_run(cmd, *a, **k):
        # By Pass 1 the overlay file must exist with the decoded bytes.
        for tok in cmd:
            if str(tok).endswith("overlay.png"):
                written["overlay"] = Path(tok).read_bytes()
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    job_id = "job-datauri"
    _run_render_v2(
        job_id, _make_body(overlay_png=f"data:image/png;base64,{b64}")
    )

    assert jobs[job_id]["status"] == "complete"
    assert written["overlay"] == raw


# ── Audio path resolution ────────────────────────────────────────────────


def test_missing_audio_resolves_to_none(patched_dirs, track_tmp, monkeypatch):
    """A named-but-absent audio file leaves audio_path None (no audio in assembly)."""
    captured = {}

    def fake_assemble(b1, b2, audio_path, out, dur):
        captured["audio_path"] = audio_path
        return ["ffmpeg", "-y"]

    monkeypatch.setattr(slideshow, "_assemble_final_cmd", fake_assemble)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    job_id = "job-noaudio"
    _run_render_v2(job_id, _make_body(audio="ghost.mp3"))

    assert captured["audio_path"] is None
    assert jobs[job_id]["status"] == "complete"


def test_present_audio_resolves_to_path(patched_dirs, track_tmp, monkeypatch):
    """An existing audio file under the audio dir is resolved and passed through."""
    audio_file = patched_dirs["audio"] / "song.mp3"
    audio_file.write_bytes(b"id3-fake")
    captured = {}

    def fake_assemble(b1, b2, audio_path, out, dur):
        captured["audio_path"] = audio_path
        return ["ffmpeg", "-y"]

    monkeypatch.setattr(slideshow, "_assemble_final_cmd", fake_assemble)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0))

    job_id = "job-audio"
    _run_render_v2(job_id, _make_body(audio="song.mp3"))

    assert captured["audio_path"] == audio_file
    assert jobs[job_id]["status"] == "complete"
