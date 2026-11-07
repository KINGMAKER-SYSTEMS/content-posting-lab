import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from services.source_duration_repair import (
    RepairError,
    canonical_json,
    command_digest,
    encode_shorter_final,
    probe,
    render_shorter,
    trim_source,
    validate_command,
)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


def S(ch):
    return ch * 64


def good_command(overrides=None):
    command = {
        "schema": "rt.source-duration-repair.v1",
        "owner_id": "owner-1",
        "slot_id": "slot-test-1",
        "slot_payload_sha256": S("e"),
        "page_id": "tt-tucker-reeves",
        "program_id": "program-1",
        "device_serial": "SER-0001",
        "account": "tucker.reeves",
        "source_sha256": S("f"),
        "approved_music_id": "7647631384072850190",
        "compressed_reference_sha256": S("b"),
        "decoded_reference_sha256": S("c"),
        "sample_clock_hz": 48000,
        "source_trim_start_ms": 0,
        "source_trim_end_ms": 11546,
        "official_start_ms": 0,
        "official_end_ms": 11546,
        "native_official_start_ms": 0,
        "measured_quantization_ms": 1,
        "opening_evidence_id": None,
        "opening_evidence_sha256": None,
        "opening_evidence_end_ms": None,
        "tail_evidence_id": "ev-tail-1",
        "tail_evidence_sha256": S("c"),
        "tail_evidence_end_ms": 11546,
        "cue_evidence_id": None,
        "cue_evidence_sha256": None,
        "cue_evidence_end_ms": None,
        "recipe_id": "recipe-1",
        "recipe_version": "v1",
        "format_id": "truck-scenic",
        "font_id": "font-17pt",
        "font_sha256": S("d"),
        "retimed_lyric_cue_sha256": None,
        "expected_output_frames": 346,
        "expected_output_duration_ms": 11546,
        "renderer_id": "content-lab.prepared-post",
        "renderer_version": "2",
    }
    if overrides:
        command.update(overrides)
    return command


def test_cross_language_canonical_and_digest():
    command = good_command()
    expected = (
        '{"account":"tucker.reeves","approved_music_id":"7647631384072850190",'
        '"compressed_reference_sha256":"' + S("b") + '",'
        '"cue_evidence_end_ms":null,"cue_evidence_id":null,"cue_evidence_sha256":null,'
        '"decoded_reference_sha256":"' + S("c") + '",'
        '"device_serial":"SER-0001","expected_output_duration_ms":11546,'
        '"expected_output_frames":346,"font_id":"font-17pt",'
        '"font_sha256":"' + S("d") + '","format_id":"truck-scenic",'
        '"measured_quantization_ms":1,"native_official_start_ms":0,'
        '"official_end_ms":11546,"official_start_ms":0,'
        '"opening_evidence_end_ms":null,"opening_evidence_id":null,'
        '"opening_evidence_sha256":null,"owner_id":"owner-1","page_id":"tt-tucker-reeves",'
        '"program_id":"program-1","recipe_id":"recipe-1","recipe_version":"v1",'
        '"renderer_id":"content-lab.prepared-post","renderer_version":"2",'
        '"retimed_lyric_cue_sha256":null,"sample_clock_hz":48000,'
        '"schema":"rt.source-duration-repair.v1","slot_id":"slot-test-1",'
        '"slot_payload_sha256":"' + S("e") + '",'
        '"source_sha256":"' + S("f") + '",'
        '"source_trim_end_ms":11546,"source_trim_start_ms":0,'
        '"tail_evidence_end_ms":11546,"tail_evidence_id":"ev-tail-1",'
        '"tail_evidence_sha256":"' + S("c") + '"}'
    )
    assert canonical_json(command) == expected
    # The exact digest minted by src/scheduler/sourceDurationRepair.js for this
    # canonical command (cross-language vector).
    assert command_digest(command) == "de4d3f35f19132f624e2c7866baee86b3f1de6e7e443a92de814737504bea1fe"
    # Independent sha256 over the canonical bytes.
    assert command_digest(command) == hashlib.sha256(canonical_json(command).encode()).hexdigest()


def test_validate_command_accepts_and_rejects():
    command = validate_command(good_command())
    assert command["source_trim_end_ms"] == 11546
    bad = [
        None, "x", [], 0,
        {**good_command(), "schema": "wrong.v1"},
        {**good_command(), "extra": 1},
        {k: v for k, v in good_command().items() if k != "slot_id"},
        {**good_command(), "slot_payload_sha256": "XYZ"},
        {**good_command(), "approved_music_id": "12345"},
        {**good_command(), "approved_music_id": 7647631384072850190},
        {**good_command(), "source_trim_start_ms": 1.5},
        {**good_command(), "source_trim_end_ms": 0},
        {**good_command(), "official_start_ms": -1},
        {**good_command(), "sample_clock_hz": 0},
        {**good_command(), "expected_output_frames": 0},
        {**good_command(), "expected_output_duration_ms": 999},
        {**good_command(), "tail_evidence_id": "ev", "tail_evidence_sha256": None},
        {**good_command(), "tail_evidence_sha256": S("9")},  # made-up sha != decoded reference
        {**good_command(), "cue_evidence_id": "ev-cue", "cue_evidence_sha256": S("8")},  # mismatched cue sha
        {**good_command(), "tail_evidence_end_ms": None},  # bound evidence with no measured end
        {**good_command(), "tail_evidence_end_ms": 11000},  # cut != evidence measured end
        {**good_command(), "tail_evidence_end_ms": "11546"},  # string end
        {**good_command(), "cue_evidence_end_ms": 5},  # end without bound cue
        {**good_command(), "retimed_lyric_cue_sha256": "not-a-sha"},
        {**good_command(), "page_id": "  leading-space"},
    ]
    for value in bad:
        with pytest.raises(RepairError):
            validate_command(value)


def _has_ffmpeg():
    try:
        subprocess.run([FFMPEG, "-version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=True)
        subprocess.run([FFPROBE, "-version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_trim_and_encode_real_clip(tmp_path):
    src = tmp_path / "source.mp4"
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True, timeout=60)
    facts = probe(src)
    assert facts["duration_ms"] in (3000, 3001)
    trimmed = tmp_path / "trimmed.mp4"
    trimmed_facts = trim_source(src, trimmed, 0, 2000)
    assert abs(trimmed_facts["duration_ms"] - 2000) <= 40
    final = tmp_path / "final.mp4"
    final_facts = encode_shorter_final(trimmed, final)
    assert (final_facts["width"], final_facts["height"], final_facts["codec_name"], final_facts["pix_fmt"]) \
        == (1080, 1920, "h264", "yuv420p")
    assert abs(final_facts["duration_ms"] - trimmed_facts["duration_ms"]) <= 80
    assert final.stat().st_size > 0


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_render_shorter_real_clip(tmp_path):
    src = tmp_path / "source.mp4"
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True, timeout=60)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    command = good_command({
        "source_sha256": src_sha,
        "source_trim_start_ms": 0,
        "source_trim_end_ms": 2000,
        "official_end_ms": 2000,
        "tail_evidence_end_ms": 2000,
        "expected_output_duration_ms": 2000,
        "expected_output_frames": 60,
    })
    out = tmp_path / "out"
    receipt = render_shorter(src, out, command)
    assert receipt["final_sha256"] not in (src_sha, receipt["trimmed_source_sha256"])
    assert abs(receipt["final_duration_ms"] - 2000) <= 80
    assert (out / "final.mp4").exists()
    assert (out / "receipt.json").exists()


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_render_shorter_refuses_mismatched_source_sha(tmp_path):
    """The hash-bound source control: the source bytes must equal the immutable
    command's source_sha256. A stale/wrong-sha source is refused BEFORE any trim
    or encode; dropping the check must make this test fail (mutant M9)."""
    src = tmp_path / "source.mp4"
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True, timeout=60)
    # A self-consistent command whose source_sha256 does NOT match the real
    # bytes on disk (stale CAS / swapped source). The sha is still well-formed.
    wrong_sha = S("0")
    assert wrong_sha != hashlib.sha256(src.read_bytes()).hexdigest()
    command = good_command({
        "source_sha256": wrong_sha,
        "source_trim_start_ms": 0,
        "source_trim_end_ms": 2000,
        "official_end_ms": 2000,
        "tail_evidence_end_ms": 2000,
        "expected_output_duration_ms": 2000,
        "expected_output_frames": 60,
    })
    out = tmp_path / "out"
    with pytest.raises(RepairError) as exc:
        render_shorter(src, out, command)
    assert exc.value.code == "source_sha256_mismatch"
    assert not (out / "final.mp4").exists()


def _make_source(path: Path, fps: str, seconds: int = 12) -> None:
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", f"testsrc2=size=1080x1920:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, timeout=120)


def _command_for(source_sha: str, end_ms: int) -> dict:
    return good_command({
        "source_sha256": source_sha,
        "source_trim_start_ms": 0,
        "source_trim_end_ms": end_ms,
        "official_end_ms": end_ms,
        "tail_evidence_end_ms": end_ms,
        "expected_output_duration_ms": end_ms,
        "expected_output_frames": int(end_ms * 30 / 1000),
    })


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_clean_end_11167_at_30fps_never_overshoots(tmp_path):
    """RED test: the old `-t` code emitted 336 frames (11.200 s), one frame past
    the 11.167 s cut. The fixed encoder must end both streams at or before it."""
    src = tmp_path / "src30.mp4"
    _make_source(src, "30")
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "out"
    receipt = render_shorter(src, out, _command_for(src_sha, 11167))
    assert receipt["final_packet_end_ms"]["video"] <= 11167.0
    assert receipt["final_packet_end_ms"]["audio"] <= 11167.0
    # floor(11167 * 30 / 1000) = 335 frames; one more frame would be 33 ms over.
    assert receipt["final_packet_counts"]["video"] == 335
    # The container and the audio stream must also end at/before the command:
    # AAC priming/padding can report a few ms longer than the video stream.
    assert receipt["final_container_duration_ms"] <= 11167
    assert receipt["final_audio_duration_ms"] <= 11167


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
@pytest.mark.parametrize("fps,expected_frames", [
    ("24", 268),   # floor(11167*24/1000)
    ("25", 279),   # floor(11167*25/1000)
    ("30000/1001", 334),  # floor(11167*30000/(1000*1001))
])
def test_clean_end_11167_other_frame_rates(tmp_path, fps, expected_frames):
    src = tmp_path / f"src{fps.replace('/', '_')}.mp4"
    _make_source(src, fps)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "out"
    receipt = render_shorter(src, out, _command_for(src_sha, 11167))
    assert receipt["final_packet_end_ms"]["video"] <= 11167.0
    assert receipt["final_packet_end_ms"]["audio"] <= 11167.0
    # The delivered final is 30 fps; after downsampling the frame count may be
    # one less, never one more. The invariant is the end bound, not the exact
    # source frame count, so assert the bound and non-overshoot here.
    assert receipt["final_duration_ms"] <= 11167


def _decode_mono(path: Path, ffmpeg_bin: str = FFMPEG) -> list:
    proc = subprocess.run(
        [ffmpeg_bin, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:].decode(errors="replace")
    import array
    return list(array.array("h", proc.stdout))


def _goertzel_power(samples, sample_rate, target_hz):
    import math
    w = 2 * math.pi * target_hz / sample_rate
    coeff = 2 * math.cos(w)
    s_prev = s_prev2 = 0.0
    for s in samples:
        s_cur = s + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s_cur
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_attack_marker_at_11180_outside_corrected_final(tmp_path):
    """Symbolic boundary control: an official-reference attack marker at 11.180 s
    sits inside the OLD 11.2 s video but must be outside the <=11.167 s final."""
    src = tmp_path / "marker.mp4"
    # 440 Hz until 11.18 s, then a 2000 Hz "next word" marker from 11.18 s on.
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "color=c=black:s=1080x1920:r=30:d=15",
        "-f", "lavfi",
        "-i", "aevalsrc=if(lt(t\\,11.18)\\,0.5*sin(2*PI*440*t)\\,0.5*sin(2*PI*2000*t)):s=48000:d=15",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-shortest", str(src),
    ], check=True, timeout=120)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "out"
    receipt = render_shorter(src, out, _command_for(src_sha, 11167))
    assert receipt["final_packet_end_ms"]["video"] <= 11167.0
    assert receipt["final_packet_end_ms"]["audio"] <= 11167.0
    # The decoded final audio must not contain the 2000 Hz marker, while the
    # full source (which extends to 15 s) does.
    final_samples = _decode_mono(out / "final.mp4")
    src_samples = _decode_mono(src)
    final_power = _goertzel_power(final_samples, 48000, 2000.0)
    src_power = _goertzel_power(src_samples, 48000, 2000.0)
    assert src_power > 0
    assert final_power < src_power * 0.01


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_impossible_interval_emits_regeneration_remedy(tmp_path):
    """Minimum-duration case: the clean interval [word end, next onset] is 8 ms
    (11167..11175), so no 30 fps frame boundary lies inside it. The helper must
    refuse with the same-sound regeneration remedy, not clamp or fade."""
    src = tmp_path / "src.mp4"
    _make_source(src, "30", seconds=12)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    command = good_command({
        "source_sha256": src_sha,
        "source_trim_start_ms": 0,
        "source_trim_end_ms": 11167,
        "official_start_ms": 11167,   # complete final word end
        "official_end_ms": 11175,     # next word onset (8 ms later, no frame fits)
        "tail_evidence_end_ms": 11167,
        "expected_output_duration_ms": 11167,
        "expected_output_frames": 335,
    })
    out = tmp_path / "out"
    with pytest.raises(RepairError) as exc:
        render_shorter(src, out, command)
    assert exc.value.code == "boundary_regeneration_needed"
    assert "regenerate" in str(exc.value)


def _make_black_source(path: Path, fps: str = "30", seconds: int = 12) -> None:
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", f"color=c=black:s=1080x1920:r={fps}:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True, timeout=120)


def _make_caption_overlay(path: Path) -> None:
    # Symbolic caption: a white band across the lower caption area, transparent
    # elsewhere, so the test can prove the overlay pixels are actually burned.
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "color=c=white:s=1080x400:d=1",
        "-vf", "format=rgba,pad=1080:1920:0:1520:color=0x00000000",
        "-frames:v", "1", str(path),
    ], check=True, timeout=60)


def _mean_luma(path: Path, x: int, y: int, w: int, h: int,
               ffmpeg_bin: str = FFMPEG) -> float:
    proc = subprocess.run(
        [ffmpeg_bin, "-nostdin", "-v", "error", "-i", str(path),
         "-vf", f"crop={w}:{h}:{x}:{y}", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 0, proc.stderr[-800:].decode(errors="replace")
    data = proc.stdout
    assert len(data) == w * h, f"expected {w * h} luma bytes, got {len(data)}"
    return sum(data) / len(data)


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_caption_overlay_is_burned_into_shorter_final(tmp_path):
    """The real _encode_final burns caption overlay.png; render_shorter must take
    the same overlay through the delivery encode or an ordinary post loses its
    words. Prove on a real file that the caption pixels are present."""
    src = tmp_path / "src.mp4"
    _make_black_source(src)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    overlay = tmp_path / "overlay.png"
    _make_caption_overlay(overlay)
    out = tmp_path / "out"
    render_shorter(src, out, _command_for(src_sha, 11167), overlay_path=overlay)
    final = out / "final.mp4"
    # Caption band is white in the output; the empty top stays black.
    assert _mean_luma(final, 0, 1700, 1080, 200) > 200.0
    assert _mean_luma(final, 0, 200, 1080, 200) < 30.0
    # Without the overlay the same region stays black (delivery-only path).
    out2 = tmp_path / "out2"
    render_shorter(src, out2, _command_for(src_sha, 11167))
    assert _mean_luma(out2 / "final.mp4", 0, 1700, 1080, 200) < 30.0


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_near_vertical_source_is_scaled_by_real_encode_path(tmp_path):
    """Finding #3: the vendored encode path hard-required 1080x1920, so a native
    near-9:16 source (e.g. 704x1280) was refused and the real scale/crop branch
    was dead. The REAL _encode_final must scale it to 1080x1920 (page look cannot
    drift). A mutant that hard-refuses non-1080x1920 must fail this test."""
    src = tmp_path / "near720.mp4"
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=size=704x1280:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True, timeout=60)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "out"
    receipt = render_shorter(src, out, _command_for(src_sha, 3000))
    assert (receipt["width"], receipt["height"]) == (1080, 1920)
    assert (out / "final.mp4").exists()
