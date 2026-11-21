"""Enforce heals re-encode a Lab cut with its delivery preset at its own rate
(control-plane-pad#327).

A format or edge heal on a Lab cut used the gate's own h264 args, so both
lacked the delivery preset's settings and its -minrate/-maxrate floor. The
format heal also passed no -r (ffmpeg then kept the input rate); the edge heal
already set -r to the clip's rate. Now both use the preset, and the format heal
sets -r explicitly to the rate the cut's encode picks (Lab #414:
services.ffmpeg.delivery_frame_rate / with_frame_rate): a 25 fps cut stays 25.
"""
from __future__ import annotations

import dataclasses
import subprocess
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import qa_gate
from services.ffmpeg import delivery_encode_args
from tests import qa_gate_clips as clips

pytestmark = pytest.mark.usefixtures("_ffmpeg")


@pytest.fixture
def _ffmpeg():
    clips.require_ffmpeg()


def _recipe():
    return SimpleNamespace(format_slug="pov-dirt-bike", output_width=1080, output_height=1920,
                           encode_preset="tiktok_delivery_v1", recipe_spec={})


def _cut_policy(**changes) -> qa_gate.QaPolicy:
    policy = qa_gate.policy_for_source_cut(_recipe(), 2_000, 1.0,
                                           encode_args=delivery_encode_args("tiktok_delivery_v1"))
    return dataclasses.replace(policy, **changes) if changes else policy


def _value(args: list[str], option: str) -> str:
    assert args.count(option) == 1, f"{option} appears {args.count(option)} times in {args}"
    return args[args.index(option) + 1]


def _spy(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = qa_gate._ffmpeg_heal

    def recording(args, what):
        calls.append(list(args))
        real(args, what)

    monkeypatch.setattr(qa_gate, "_ffmpeg_heal", recording)
    return calls


def _rate(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate,pix_fmt",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()


def test_a_source_cut_policy_carries_the_delivery_preset():
    assert qa_gate.policy_for_source_cut(_recipe(), 6_000, 1.0).encode_args == tuple(
        delivery_encode_args("tiktok_delivery_v1"))
    generated = qa_gate.policy_for_generated_clip(SimpleNamespace(format_slug="x", recipe_spec={}))
    assert generated.encode_args == ()


def test_a_format_heal_of_a_25fps_cut_keeps_25_and_the_bitrate_floor(tmp_path, monkeypatch):
    cut = tmp_path / "cut25.mp4"
    clips._encode(cut, clips._src(2.0, size="360x640", fps=25), frames=50, fps=25, audio=True,
                  extra=["-pix_fmt", "yuv444p"])
    calls = _spy(monkeypatch)
    healed = tmp_path / "healed.mp4"
    qa_gate.heal_format(cut, healed, _cut_policy())
    [args] = calls
    assert _value(args, "-r") == "25"
    assert _value(args, "-minrate") == "8M" and _value(args, "-maxrate") == "20M"
    assert _value(args, "-bufsize") == "20M" and _value(args, "-crf") == "18"
    # Audio is copied, never re-encoded by the preset's aac settings.
    assert _value(args, "-c:a") == "copy" and "-b:a" not in args
    assert _value(args, "-movflags") == "+faststart"
    assert _rate(healed) == "yuv420p,25/1"


def test_a_format_heal_on_a_video_only_cut_drops_audio_with_the_preset(tmp_path, monkeypatch):
    cut = tmp_path / "cut25.mp4"
    clips._encode(cut, clips._src(2.0, size="360x640", fps=25), frames=50, fps=25, audio=True,
                  extra=["-pix_fmt", "yuv444p"])
    calls = _spy(monkeypatch)
    qa_gate.heal_format(cut, tmp_path / "healed.mp4", _cut_policy(audio=qa_gate.AUDIO_FORBIDDEN))
    [args] = calls
    assert "-an" in args and "-c:a" not in args and "-b:a" not in args
    assert _value(args, "-r") == "25" and _value(args, "-minrate") == "8M"


def test_an_edge_heal_of_a_25fps_cut_keeps_25_and_the_bitrate_floor(tmp_path, monkeypatch):
    cut = tmp_path / "cut25.mp4"
    clips._encode(cut, clips._src(2.0, size="360x640", fps=25), frames=50, fps=25)
    calls = _spy(monkeypatch)
    healed = tmp_path / "healed.mp4"
    qa_gate.heal_edges(cut, healed, (0, 48, 50), 25.0, _cut_policy())
    [args] = calls
    assert _value(args, "-r") == "25" and _value(args, "-frames:v") == "50"
    assert _value(args, "-minrate") == "8M" and _value(args, "-maxrate") == "20M"
    assert _rate(healed) == "yuv420p,25/1"


def test_a_provider_clip_heal_keeps_the_gates_own_args(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(qa_gate, "_ffmpeg_heal", lambda args, what: calls.append(list(args)))
    policy = qa_gate.QaPolicy(width=None, height=None, expected_frames=None, audio=qa_gate.AUDIO_FORBIDDEN)
    qa_gate.heal_format(Path("in.mp4"), Path("out.mp4"), policy)
    assert calls == [["-i", "in.mp4", "-map", "0:v:0", "-an", "-vf", "setsar=1",
                      "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
                      "-movflags", "+faststart", "out.mp4"]]


def test_shadow_runs_no_re_encode_on_a_cut_with_a_preset(tmp_path, monkeypatch):
    cut = tmp_path / "cut25.mp4"
    clips._encode(cut, clips._src(2.0, size="1080x1920", fps=25), frames=50, fps=25,
                  extra=["-pix_fmt", "yuv444p"])
    before = cut.read_bytes()
    calls: list[list[str]] = []
    monkeypatch.setattr(qa_gate, "_ffmpeg_heal", lambda args, what: calls.append(list(args)))
    verdict = qa_gate.run_gate(cut, _cut_policy(fps=25.0, expected_frames=50), qa_gate.MODE_SHADOW)
    assert "qa_codec_mismatch" in [item.code for item in verdict.blocks]
    assert calls == [] and cut.read_bytes() == before and verdict.healed == ()


# ── the source-cut policy counts frames at the cut's own rate ────────────────

def test_a_25fps_cut_is_counted_in_25fps_frames(tmp_path):
    cut = clips.clean(tmp_path / "cut25.mp4", seconds=2.0, size="360x640", fps=25)
    policy = qa_gate.policy_for_source_cut(_recipe(), 6_000, 1.0, cut_path=cut)
    assert policy.fps == 25.0 and policy.expected_frames == 150
    # A 0.75x cut of the same 6 s window lasts 8 s: 200 frames at 25 fps.
    assert qa_gate.policy_for_source_cut(_recipe(), 6_000, 0.75, cut_path=cut).expected_frames == 200


def test_the_cut_rate_goes_through_the_delivery_rule(monkeypatch):
    import services.ffmpeg as ffmpeg

    for probed, fps, frames in [(Fraction(60), 30.0, 180), (Fraction(30000, 1001), 30000 / 1001, 180),
                                (Fraction(21), 21.0, 126), (Fraction(16), 32.0, 192)]:
        monkeypatch.setattr(ffmpeg, "probe_frame_rate", lambda path, rate=probed: rate)
        policy = qa_gate.policy_for_source_cut(_recipe(), 6_000, 1.0, cut_path="cut.mp4")
        assert (policy.fps, policy.expected_frames) == (fps, frames)


def test_an_unreadable_cut_rate_is_counted_at_30(tmp_path):
    unreadable = tmp_path / "not-a-video.mp4"
    unreadable.write_bytes(b"not a video")
    policy = qa_gate.policy_for_source_cut(_recipe(), 6_000, 1.0, cut_path=unreadable)
    assert policy.fps == 30.0 and policy.expected_frames == 180
    assert qa_gate.policy_for_source_cut(_recipe(), 6_000, 1.0).fps == 30.0
