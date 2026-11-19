"""services/qa_gate.py: the lead's post-cut checklist as code (standard item 3,
ruling L57062: hard faults BLOCK, look issues WARN with a score)."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import qa_gate
from tests import qa_gate_clips as clips


@pytest.fixture(autouse=True)
def _ffmpeg():
    clips.require_ffmpeg()


VIDEO_6S = qa_gate.QaPolicy(expected_frames=180)


def codes(verdict):
    return [item.code for item in verdict.findings]


# ── hard faults block ────────────────────────────────────────────────────────

def test_a_clean_body_passes_with_a_full_score(tmp_path):
    verdict = qa_gate.validate_clip(clips.clean(tmp_path / "clean.mp4"), VIDEO_6S)
    assert verdict.ok and verdict.findings == () and verdict.score == 100
    assert verdict.scan.frames == 180 and verdict.probe.sample_aspect_ratio == "1:1"
    record = verdict.manifest()
    assert record["schema"] == qa_gate.SCHEMA and record["ok"] and record["blocks"] == []


def test_non_square_sar_blocks_without_repair(tmp_path):
    bad = clips.non_square_sar(clips.clean(tmp_path / "clean.mp4"), tmp_path / "sar.mp4")
    assert qa_gate.probe_clip(bad).sample_aspect_ratio == "2272:2277"
    verdict = qa_gate.validate_clip(bad, VIDEO_6S)
    assert not verdict.ok and verdict.reason == "qa_sar_non_square"


def test_sar_only_fault_is_repaired_in_place_frame_identical(tmp_path):
    clean = clips.clean(tmp_path / "clean.mp4")
    bad = clips.non_square_sar(clean, tmp_path / "sar.mp4")
    before = clips.framemd5(bad)
    verdict = qa_gate.validate_and_repair(bad, VIDEO_6S)
    assert verdict.ok and verdict.repaired_sar and verdict.score == 100
    assert qa_gate.probe_clip(bad).sample_aspect_ratio == "1:1"
    assert clips.framemd5(bad) == before == clips.framemd5(clean)
    assert not (tmp_path / "sar.sar1.mp4").exists()
    assert verdict.manifest()["repairedSar"] is True


def test_sar_plus_another_hard_fault_is_not_repaired(tmp_path):
    bad = clips.non_square_sar(clips.clean(tmp_path / "audio.mp4", audio=True), tmp_path / "sar-audio.mp4")
    verdict = qa_gate.validate_and_repair(bad, VIDEO_6S)
    assert not verdict.ok and not verdict.repaired_sar
    assert {"qa_sar_non_square", "qa_audio_unexpected"} <= set(codes(verdict))
    assert qa_gate.probe_clip(bad).sample_aspect_ratio == "2272:2277"


def test_baked_audio_on_a_video_only_recipe_blocks(tmp_path):
    verdict = qa_gate.validate_clip(clips.clean(tmp_path / "audio.mp4", audio=True), VIDEO_6S)
    assert not verdict.ok and verdict.reason == "qa_audio_unexpected"
    allowed = qa_gate.validate_clip(tmp_path / "audio.mp4",
                                    qa_gate.QaPolicy(expected_frames=180, audio=qa_gate.AUDIO_ALLOWED))
    assert allowed.ok


def test_missing_audio_on_a_baked_audio_recipe_blocks(tmp_path):
    policy = qa_gate.QaPolicy(expected_frames=180, audio=qa_gate.AUDIO_REQUIRED)
    verdict = qa_gate.validate_clip(clips.clean(tmp_path / "clean.mp4"), policy)
    assert not verdict.ok and verdict.reason == "qa_audio_missing"


def test_wrong_length_blocks_beyond_one_frame(tmp_path):
    long = clips.clean(tmp_path / "long.mp4", seconds=7.0)
    verdict = qa_gate.validate_clip(long, VIDEO_6S)
    assert not verdict.ok and verdict.reason == "qa_duration_mismatch"
    assert "210 frames" in verdict.detail
    one_frame_over = qa_gate.QaPolicy(expected_frames=209)
    assert qa_gate.validate_clip(long, one_frame_over).ok
    two_frames_over = qa_gate.QaPolicy(expected_frames=208)
    assert qa_gate.validate_clip(long, two_frames_over).reason == "qa_duration_mismatch"


def test_wrong_frame_blocks(tmp_path):
    small = clips.clean(tmp_path / "small.mp4", size="720x1280")
    verdict = qa_gate.validate_clip(small, VIDEO_6S)
    assert not verdict.ok and verdict.reason == "qa_geometry_mismatch"


def test_a_broken_file_blocks(tmp_path):
    broken = clips.truncated(clips.clean(tmp_path / "clean.mp4"), tmp_path / "broken.mp4")
    verdict = qa_gate.validate_clip(broken, VIDEO_6S)
    assert not verdict.ok
    assert verdict.reason in {"qa_decode_failed", "qa_probe_unreadable", "qa_duration_mismatch"}
    missing = qa_gate.validate_clip(tmp_path / "missing.mp4", VIDEO_6S)
    assert missing.reason == "qa_probe_unreadable"


# ── look issues warn, score, never block ─────────────────────────────────────

def test_a_cut_two_frames_before_the_end_warns_and_scores(tmp_path):
    verdict = qa_gate.validate_clip(clips.with_tail_cut(tmp_path / "tail.mp4", frames_before_end=2), VIDEO_6S)
    assert verdict.ok, codes(verdict)
    assert codes(verdict) == ["qa_tail_cut"] and verdict.score == 60
    (frame, _time, score), = verdict.extra["tail_cuts"]
    assert frame == 178 and score > 3
    assert verdict.manifest()["warnings"] == ["qa_tail_cut"]


def test_a_cut_six_frames_before_the_end_is_inside_the_window(tmp_path):
    inside = qa_gate.validate_clip(clips.with_tail_cut(tmp_path / "six.mp4", frames_before_end=6), VIDEO_6S)
    assert codes(inside) == ["qa_tail_cut"]
    outside = qa_gate.validate_clip(clips.with_tail_cut(tmp_path / "seven.mp4", frames_before_end=7), VIDEO_6S)
    assert outside.findings == ()


def test_a_fast_montage_may_end_on_a_short_card(tmp_path):
    # Golden/Glossy/Fly: 0.2 s cards, a 3-frame last card. The look, not a jump.
    fast = clips.montage(tmp_path / "fast.mp4", cards=29, card_s=0.2, last_card_s=0.1)
    verdict = qa_gate.validate_clip(fast, qa_gate.QaPolicy(still=True))
    assert verdict.extra["tail_cuts"] and verdict.extra["fast_montage"]
    assert verdict.findings == () and verdict.score == 100


def test_slow_cards_ending_on_a_flash_is_a_tail_cut(tmp_path):
    # Tis 2825b619: 1 s cards, then a 0.2 s last card (a 6-frame flash).
    slow = clips.montage(tmp_path / "slow.mp4", cards=7, card_s=1.0, last_card_s=0.2)
    verdict = qa_gate.validate_clip(slow, qa_gate.QaPolicy(still=True))
    assert not verdict.extra["fast_montage"]
    assert codes(verdict) == ["qa_tail_cut"] and verdict.ok and verdict.score == 60


def test_a_black_run_over_a_tenth_warns_unless_the_recipe_fades(tmp_path):
    body = clips.with_black_run(tmp_path / "black.mp4", black_s=0.5)
    verdict = qa_gate.validate_clip(body, VIDEO_6S)
    assert verdict.ok and codes(verdict) == ["qa_black_run"] and verdict.score == 75
    (start, end), = verdict.scan.black_runs
    assert end - start == pytest.approx(0.5, abs=0.05)
    faded = qa_gate.validate_clip(body, qa_gate.QaPolicy(expected_frames=180, fade=True))
    assert faded.findings == ()


def test_a_short_black_blip_is_not_a_run(tmp_path):
    body = clips.with_black_run(tmp_path / "blip.mp4", black_s=0.1)
    assert qa_gate.validate_clip(body, VIDEO_6S).findings == ()


def test_a_freeze_over_half_a_second_warns_unless_the_format_is_still(tmp_path):
    body = clips.with_freeze(tmp_path / "freeze.mp4", freeze_s=1.0)
    verdict = qa_gate.validate_clip(body, VIDEO_6S)
    assert verdict.ok and codes(verdict) == ["qa_freeze"] and verdict.score == 85
    still = qa_gate.validate_clip(body, qa_gate.QaPolicy(expected_frames=180, still=True))
    assert still.findings == ()


def test_penalties_are_capped_and_floored():
    def warn(code, times):
        return tuple(qa_gate.QaFinding(code, "") for _ in range(times))
    probe = None
    assert qa_gate.QaVerdict(warn("qa_black_run", 5), probe, None).score == 50
    assert qa_gate.QaVerdict(warn("qa_freeze", 5), probe, None).score == 70
    everything = warn("qa_tail_cut", 1) + warn("qa_black_run", 3) + warn("qa_freeze", 3) + warn("qa_sound_window_miss", 1)
    assert qa_gate.QaVerdict(everything, probe, None).score == 0
    assert qa_gate.QaVerdict(warn("qa_sideways_picture", 1), probe, None).score == 60
    blocked = qa_gate.QaVerdict((qa_gate.QaFinding("qa_geometry_mismatch", "x"),), probe, None)
    assert not blocked.ok and blocked.score == 100  # a block is not a look score


# ── sound windows ────────────────────────────────────────────────────────────

def test_the_catalog_carries_the_em_windows():
    catalog = qa_gate.load_sound_windows()
    assert catalog["6704982590421469186"] == ((8.66, 9.29),)
    assert catalog["6753170535640729601"] == ((6.47, 7.62), (9.43, 9.58))
    assert catalog["7652818168142235664"] == ((7.35, 7.48),)
    assert catalog["7701226037748779806"] == ((5.82, 8.66),)
    assert qa_gate.clean_windows_for("7285696594120689666") is None  # Cher: no audio on file
    assert qa_gate.clean_windows_for(None) is None
    assert qa_gate.load_sound_windows(Path("/nonexistent/catalog.json")) == {}


def test_an_ending_outside_the_sounds_clean_window_warns(tmp_path):
    body = clips.clean(tmp_path / "six.mp4", seconds=6.0)
    holly = qa_gate.QaPolicy(expected_frames=180, sound_id="6704982590421469186")
    verdict = qa_gate.validate_clip(body, holly)
    assert verdict.ok and codes(verdict) == ["qa_sound_window_miss"] and verdict.score == 80
    assert verdict.sound_window["fits"] is False and verdict.manifest()["soundWindow"]["endS"] == 6.0
    nine = clips.clean(tmp_path / "nine.mp4", seconds=9.0)
    fits = qa_gate.validate_clip(nine, qa_gate.QaPolicy(expected_frames=270, sound_id="6704982590421469186"))
    assert fits.findings == () and fits.sound_window["fits"] is True
    unknown = qa_gate.validate_clip(nine, qa_gate.QaPolicy(expected_frames=270, sound_id="0000"))
    assert unknown.findings == () and unknown.sound_window is None


# ── policy from the recipe ───────────────────────────────────────────────────

def _recipe(**spec):
    return SimpleNamespace(format_slug="pov-dirt-bike", output_width=1080, output_height=1920,
                           encode_preset="tiktok_delivery_v1", recipe_spec=spec)


def test_policy_for_a_source_cut_is_the_window_at_the_effective_speed():
    policy = qa_gate.policy_for_source_cut(_recipe(), 6_000, 2.0)
    assert policy.expected_frames == 90 and policy.fps == 30.0
    # A Lab cut inherits its master's audio; the sound is bound downstream.
    assert policy.audio == qa_gate.AUDIO_ALLOWED and not policy.still and not policy.fade
    assert policy.sound_id is None and (policy.width, policy.height) == (1080, 1920)
    declared = qa_gate.policy_for_source_cut(
        _recipe(qaGate={"bakedAudio": True, "fade": True, "soundId": "6704982590421469186"}), 7_400, 1.0,
        job={"constraints": {}})
    assert declared.expected_frames == 222 and declared.audio == qa_gate.AUDIO_REQUIRED
    assert declared.fade and declared.sound_id == "6704982590421469186"
    video_only = qa_gate.policy_for_source_cut(_recipe(qaGate={"audio": "video_only"}), 7_400, 1.0)
    assert video_only.audio == qa_gate.AUDIO_FORBIDDEN
    assert qa_gate.QaPolicy().audio == qa_gate.AUDIO_FORBIDDEN  # intake/CLI default
    bound = qa_gate.policy_for_source_cut(_recipe(), 8_000, 1.0, job={"soundId": "7701226037748779806"})
    assert bound.sound_id == "7701226037748779806"
    still = qa_gate.policy_for_source_cut(SimpleNamespace(format_slug="image-video", output_width=1080,
                                                          output_height=1920, encode_preset="tiktok_delivery_v1",
                                                          recipe_spec={}), 9_000, 1.0)
    assert still.still


def test_gate_mode_reads_the_switch(monkeypatch):
    monkeypatch.delenv("CONTENT_LAB_QA_GATE", raising=False)
    assert qa_gate.gate_mode() == qa_gate.MODE_ENFORCE
    # L57135 (e): a typo during a shadow-first rollout means shadow, never enforce.
    for raw, mode in (("shadow", "shadow"), ("off", "off"), ("ON", "enforce"), ("bogus", "shadow"),
                      ("shaddow", "shadow"), ("observe", "shadow"), ("1", "enforce")):
        monkeypatch.setenv("CONTENT_LAB_QA_GATE", raw)
        assert qa_gate.gate_mode() == mode


def test_cli_prints_one_line_per_body_and_exits_nonzero_on_a_block(tmp_path, capsys):
    clean = clips.clean(tmp_path / "clean.mp4")
    audio = clips.clean(tmp_path / "audio.mp4", audio=True)
    assert qa_gate._main([str(clean), "--target-seconds", "6"]) == 0
    assert capsys.readouterr().out.startswith("PASS score=100")
    assert qa_gate._main([str(audio), str(clean), "--json"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert '"blocks": ["qa_audio_unexpected"]' in lines[0] and '"ok": true' in lines[1]


# ── sideways picture (L57098) ────────────────────────────────────────────────

def test_a_quarter_turned_picture_blocks_and_an_upright_scene_passes(tmp_path):
    # still=True: the synthetic scene does not move, and a freeze is a look
    # warning anyway; the question here is the picture's orientation.
    policy = qa_gate.QaPolicy(width=None, height=None, sideways_check=True, still=True)
    upright = qa_gate.validate_clip(clips.upright_scene(tmp_path / "up.mp4"), policy)
    assert upright.ok and upright.findings == (), codes(upright)
    frames = upright.extra["orientation"]["frames"]
    assert len(frames) == 3 and not any(frame["sideways"] for frame in frames)
    assert all(frame["top_bottom"] > frame["left_right"] for frame in frames)
    sideways = qa_gate.validate_clip(clips.sideways_scene(tmp_path / "side.mp4"), policy)
    # L57135 (a): an image cue warns and scores; it never blocks.
    assert sideways.ok and sideways.blocks == () and codes(sideways) == ["qa_sideways_picture"]
    assert sideways.score == 60 and "qa_sideways_picture" in qa_gate.WARNING_CODES
    assert "qa_sideways_picture" not in qa_gate.BLOCKING_CODES
    assert all(frame["sideways"] for frame in sideways.extra["orientation"]["frames"])
    assert sideways.manifest()["orientation"]["sideways"] is True
    # Off by default: a vetted master's cut never pays for the three reads.
    assert "orientation" not in qa_gate.validate_clip(tmp_path / "side.mp4", qa_gate.QaPolicy(width=None, height=None)).extra


def test_the_sideways_test_needs_all_three_frames_to_agree(tmp_path):
    # Half the clip upright, half turned: two of three frames disagree.
    mixed = tmp_path / "mixed.mp4"
    clips._encode(mixed, clips._concat(clips._scene_graph(1080, 1920, 3.0),
                                       clips._scene_graph(1920, 1080, 3.5) + ",transpose=1"),
                  frames=180)
    verdict = qa_gate.validate_clip(mixed, qa_gate.QaPolicy(width=None, height=None, sideways_check=True, still=True))
    frames = verdict.extra["orientation"]["frames"]
    assert [frame["sideways"] for frame in frames] == [False, True, True]
    assert "qa_sideways_picture" not in codes(verdict)


def test_a_rotate_tag_and_a_landscape_frame_are_param_blocks(tmp_path):
    upright = clips.upright_scene(tmp_path / "up.mp4")
    tagged = clips.with_display_rotation(upright, tmp_path / "tagged.mp4")
    assert qa_gate.probe_clip(tagged).rotation in (90, -90, 270)
    verdict = qa_gate.validate_clip(tagged, qa_gate.QaPolicy(width=None, height=None))
    assert {"qa_rotation", "qa_orientation_mismatch"} <= set(codes(verdict))
    landscape = clips.clean(tmp_path / "wide.mp4", size="1920x1080")
    wide = qa_gate.validate_clip(landscape, qa_gate.QaPolicy(width=None, height=None))
    assert wide.reason == "qa_orientation_mismatch"
    assert qa_gate.validate_clip(landscape, qa_gate.QaPolicy(width=None, height=None, expected_orientation=None)).ok


def test_policy_for_a_generated_clip_keeps_the_providers_frame_and_reads_the_picture():
    recipe = SimpleNamespace(format_slug="pov-seasonal", recipe_spec={})
    h3 = qa_gate.policy_for_generated_clip(recipe, {"strip_audio": True})
    assert h3.width is None and h3.height is None and h3.expected_frames is None
    assert h3.sideways_check and h3.expected_orientation == "portrait"
    assert h3.audio == qa_gate.AUDIO_FORBIDDEN and not h3.still
    other = qa_gate.policy_for_generated_clip(recipe, {})
    assert other.audio == qa_gate.AUDIO_ALLOWED
    still = qa_gate.policy_for_generated_clip(recipe, {"output_kind": "image"})
    assert still.still
    quiet = qa_gate.policy_for_generated_clip(SimpleNamespace(format_slug="x", recipe_spec={"qaGate": {"sidewaysCheck": False}}))
    assert not quiet.sideways_check


def test_a_sar_repair_can_land_beside_the_source(tmp_path):
    bad = clips.non_square_sar(clips.clean(tmp_path / "clean.mp4"), tmp_path / "provider.mp4")
    before = bad.read_bytes()
    verdict = qa_gate.validate_and_repair(bad, VIDEO_6S, repaired_destination=tmp_path / "treated" / "fixed.mp4")
    assert verdict.ok and verdict.repaired_sar and verdict.path == tmp_path / "treated" / "fixed.mp4"
    assert bad.read_bytes() == before  # the provider file is untouched evidence
    assert qa_gate.probe_clip(verdict.path).sample_aspect_ratio == "1:1"


# ── L57135 fix round: one RED/GREEN per item ─────────────────────────────────

def test_a_dark_room_with_a_bright_side_window_is_never_blocked(tmp_path):
    # (a) GLM's upright false positive: the image cue may warn, must not block.
    body = clips.dark_room_side_window(tmp_path / "room.mp4")
    verdict = qa_gate.validate_clip(body, qa_gate.QaPolicy(width=None, height=None, sideways_check=True, still=True))
    assert verdict.ok and verdict.blocks == ()
    frames = verdict.extra["orientation"]["frames"]
    assert all(frame["left_right"] > 30 for frame in frames)  # the cue really fires on it
    assert set(codes(verdict)) <= {"qa_sideways_picture"}
    if "qa_sideways_picture" in codes(verdict):
        assert verdict.score == 60


def test_a_missing_tool_passes_the_clip_and_says_so(tmp_path, monkeypatch):
    # (b) fail open: no ffprobe/ffmpeg on PATH is a tool outage, not a verdict.
    body = clips.clean(tmp_path / "clean.mp4")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    with pytest.raises(qa_gate.QaGateError):
        qa_gate.probe_clip(body)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.ok and verdict.findings == () and verdict.score == 100
    assert verdict.extra["tool_error"] == "ffprobe_unavailable"
    assert verdict.manifest()["toolError"] == "ffprobe_unavailable"


def test_an_unexpected_gate_exception_passes_the_clip(tmp_path, monkeypatch):
    body = clips.clean(tmp_path / "clean.mp4")

    def boom(*args, **kwargs):
        raise RuntimeError("gate bug")

    monkeypatch.setattr(qa_gate, "validate_and_repair", boom)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.ok and verdict.extra["tool_error"] == "RuntimeError: gate bug"


def test_a_probe_timeout_is_a_tool_error_not_a_broken_file(tmp_path, monkeypatch):
    import subprocess as sp
    body = clips.clean(tmp_path / "clean.mp4")

    def slow(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="ffprobe", timeout=1)

    monkeypatch.setattr(qa_gate.subprocess, "run", slow)
    with pytest.raises(qa_gate.QaGateError):
        qa_gate.probe_clip(body)
    assert qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE).extra["tool_error"] == "ffprobe_TimeoutExpired"


def test_shadow_never_repairs_and_leaves_the_bytes_alone(tmp_path):
    # (c) shadow changes nothing: same bytes, same sha, SAR fault recorded.
    bad = clips.non_square_sar(clips.clean(tmp_path / "clean.mp4"), tmp_path / "sar.mp4")
    before = bad.read_bytes()
    shadow = qa_gate.run_gate(bad, VIDEO_6S, qa_gate.MODE_SHADOW)
    assert not shadow.ok and shadow.reason == "qa_sar_non_square" and not shadow.repaired_sar
    assert bad.read_bytes() == before and not (tmp_path / "sar.sar1.mp4").exists()
    enforce = qa_gate.run_gate(bad, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert enforce.ok and enforce.repaired_sar and bad.read_bytes() != before


def test_a_24fps_provider_clip_is_sampled_at_its_own_times(tmp_path):
    # (d) the sampler must use the stream's rate, not the 30 fps policy default.
    body = clips.clean_24fps(tmp_path / "h3.mp4")  # 226 frames / 24 fps = 9.417 s
    policy = qa_gate.policy_for_generated_clip(SimpleNamespace(format_slug="pov-seasonal", recipe_spec={}),
                                               {"strip_audio": True})
    assert policy.fps == 30.0  # the policy cannot know the provider's rate in advance
    verdict = qa_gate.validate_clip(body, policy)
    assert verdict.probe.fps == 24.0 and verdict.scan.frames == 226
    at = [frame["atS"] for frame in verdict.extra["orientation"]["frames"]]
    assert at == pytest.approx([0.942, 4.708, 8.475], abs=0.01), at


def test_a_stream_duration_of_na_reads_the_format_duration(monkeypatch, tmp_path):
    # (f) HEVC/10-bit and streamed provider output print N/A on the stream.
    import json as _json
    import subprocess as sp
    reply = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 768, "height": 1344,
                          "pix_fmt": "yuv420p", "r_frame_rate": "24/1", "duration": "N/A",
                          "nb_frames": "N/A"}],
             "format": {"duration": "9.416667"}}

    def fake_run(args, **kwargs):
        return sp.CompletedProcess(args, 0, stdout=_json.dumps(reply), stderr="")

    monkeypatch.setattr(qa_gate.subprocess, "run", fake_run)
    probe = qa_gate.probe_clip(tmp_path / "any.mp4")
    assert probe.duration_s == pytest.approx(9.416667) and probe.nb_frames is None
    assert qa_gate._first_duration("N/A", None, "x", "0", "-1", "2.5") == 2.5
    assert qa_gate._first_duration("N/A", "N/A") is None
