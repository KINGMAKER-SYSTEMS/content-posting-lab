"""Unit tests for the CORE production pipeline in services/abn_factory.py.

The episode factory's production functions (script -> VO -> align -> caption -> assemble)
had zero coverage; only the garbage-collector had tests (test_abn_factory_gc.py). These pin
the load-bearing behaviour of each stage WITHOUT touching the network, an LLM, Pocket-TTS,
Whisper, or ffmpeg — every external call is monkeypatched. The hard gates covered here:

  * VO command is Pocket-TTS built-in English voice ONLY (no clone, no cloud TTS).
  * `_voice` routes its write through the asset gateway and raises on TTS failure — and on an
    off-schema slug it raises the gateway's AssetPathError BEFORE ever shelling out to TTS.
  * Captions are SCRIPT-TRUE (script text wins; Whisper only supplies timing).
  * Whisper brand garbles (Thropic->Anthropic, open ai->OpenAI) are corrected WITHOUT disturbing
    the real word timestamps around them.
  * `_assemble_episode` refuses to emit an episode with no usable segment clips — whether every
    per-segment encode failed or the segment list was empty to begin with.
"""
import asyncio

import pytest

from services import abn_factory
from services import abn_assets


# ---------------- VO HARD GATE: Pocket-TTS built-in voice only ----------------

def test_pocket_tts_command_uses_pocket_tts_builtin_voice(monkeypatch, tmp_path):
    """The narrator command MUST be local pocket-tts — never a clone file or a cloud TTS engine.
    This is the locked-voice hard gate; a regression here re-narrates the whole channel."""
    monkeypatch.delenv("ABN_POCKET_LANGUAGE", raising=False)
    out = tmp_path / "voice.wav"
    cmd = abn_factory._pocket_tts_command("hello builders", out)

    assert cmd[0] == "pocket-tts"
    assert cmd[1] == "generate"
    assert "--text" in cmd and "hello builders" in cmd
    assert "--output-path" in cmd and str(out) in cmd
    # default built-in english language, no clone path, no cloud engine flags
    assert "--language" in cmd and "english_2026-04" in cmd
    joined = " ".join(cmd).lower()
    for banned in ("safetensors", "replicate", "chatterbox", "elevenlabs", "--voice-clone", "clone"):
        assert banned not in joined, f"VO command leaked a non-pocket-tts narrator: {banned!r}"


def test_pocket_tts_command_honours_language_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ABN_POCKET_LANGUAGE", "english_test")
    cmd = abn_factory._pocket_tts_command("hi", tmp_path / "v.wav")
    assert "english_test" in cmd
    assert "pocket-tts" == cmd[0]


def test_voice_routes_through_gateway_and_returns_url_and_duration(monkeypatch, tmp_path):
    """_voice must (a) build its output path via the asset gateway (per-episode schema, not a
    flat dump), (b) shell out to the pocket-tts command, (c) return the managed URL + measured
    duration. We stub the shell + ffprobe so the test never spawns a real TTS/ffprobe."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    captured = {}

    async def fake_sh(cmd, timeout=600):
        captured["cmd"] = cmd
        # simulate pocket-tts writing the wav the gateway told it to
        out = abn_assets.asset_path_from_slug("ep_a111111_s0", "voice")
        out.write_bytes(b"RIFF....fake wav")
        return 0, "ok"

    async def fake_dur(path):
        return 7.5

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    url, dur = asyncio.run(abn_factory._voice("hello world", "ep_a111111_s0"))

    assert dur == 7.5
    assert url.startswith("/agenticnews-assets/")
    assert "pocket-tts" in captured["cmd"], "VO did not go through pocket-tts"
    # the URL round-trips back to the file the gateway placed under the per-episode schema
    resolved = abn_factory._resolve_asset(url)
    assert resolved.exists()
    assert "ep_a111111" in str(resolved)


def test_voice_raises_when_tts_fails(monkeypatch, tmp_path):
    """If pocket-tts returns non-zero (or writes nothing), _voice must raise rather than hand the
    pipeline a silent/empty narration that would slip past the duration floor downstream."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def fake_sh(cmd, timeout=600):
        return 1, "pocket-tts: boom"

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    with pytest.raises(RuntimeError):
        asyncio.run(abn_factory._voice("hello", "ep_a111111_s0"))


def test_voice_raises_on_gateway_error_before_shelling_out(monkeypatch, tmp_path):
    """A malformed slug (no 'ep_<hex>' prefix) must be rejected by the asset gateway BEFORE _voice
    ever shells out to TTS — the write path itself is the enforcement point (asset-schema epic).
    The error surfaces as the gateway's AssetPathError, and pocket-tts is never invoked."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def boom_sh(cmd, timeout=600):  # gateway must reject before we get here
        raise AssertionError("shelled out to TTS despite an off-schema asset slug")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    with pytest.raises(abn_assets.AssetPathError):
        asyncio.run(abn_factory._voice("hello", "no_episode_prefix"))


# ---------------- SCRIPT: fallback + length clamp ----------------

def test_script_segment_falls_back_when_llm_returns_nothing(monkeypatch):
    """When the scriptwriter expert is unavailable / returns junk, _script_segment must still
    yield a usable spoken beat (built from the title + research) — never None / empty."""
    monkeypatch.setattr(abn_factory, "_llm_script_sync", lambda *a, **k: None)
    txt = asyncio.run(
        abn_factory._script_segment("Agents ship faster", "https://x", 0, True, research="the mechanism", deep=False)
    )
    assert txt and len(txt) > 10
    assert "Agents ship faster" in txt


def test_script_segment_clamps_overlong_llm_output(monkeypatch):
    """A runaway LLM script is truncated near the word budget so the segment can't balloon the
    episode length past the format target."""
    monkeypatch.setattr(abn_factory, "_llm_script_sync", lambda *a, **k: "word " * 500)
    txt = asyncio.run(
        abn_factory._script_segment("t", "https://x", 1, False, research="", deep=False)
    )
    # budget for a non-deep segment is SEG_WORDS; clamp is budget + 40
    assert len(txt.split()) <= abn_factory.SEG_WORDS + 40


def test_script_segment_deep_uses_larger_budget(monkeypatch):
    captured = {}

    def fake_llm(title, is_hook, research="", words=0, deep=False):
        captured["words"] = words
        captured["deep"] = deep
        return "a real script of sufficient length to pass the floor check easily here"

    monkeypatch.setattr(abn_factory, "_llm_script_sync", fake_llm)
    asyncio.run(abn_factory._script_segment("t", "u", 0, False, research="", deep=True))
    assert captured["deep"] is True
    assert captured["words"] == 200  # deep-dive facets run longer


# ---------------- ALIGN: whisper word-timestamps + brand correction ----------------

def test_fix_brand_words_merges_split_brand_pair():
    """Whisper splits 'OpenAI' into 'open ai'; the fixer must merge the pair into one token while
    keeping the combined timespan, so the karaoke caption reads the brand correctly."""
    words = [{"w": "open", "s": 0.0, "e": 0.5}, {"w": "ai", "s": 0.5, "e": 1.0}]
    fixed = abn_factory._fix_brand_words(words)
    assert len(fixed) == 1
    assert fixed[0]["w"] == "OpenAI"
    assert fixed[0]["s"] == 0.0 and fixed[0]["e"] == 1.0  # spans both original tokens


def test_fix_brand_words_corrects_asr_garble():
    """The Anthropic ASR garble (Thropic/Thropix...) caught 25x across real episodes must be
    corrected; possessive 's is carried across the fix."""
    words = [{"w": "Thropic's", "s": 1.0, "e": 1.4}, {"w": "openai", "s": 1.4, "e": 1.8}]
    fixed = abn_factory._fix_brand_words(words)
    assert fixed[0]["w"] == "Anthropic's"
    assert fixed[1]["w"] == "OpenAI"  # plain casing fix on a single token


def test_fix_brand_words_preserves_timestamps_across_garble_in_a_real_sentence():
    """The garble fix must NOT disturb timing: a bare 'Thropic' (the actual Whisper garble, no
    leading 'a') embedded mid-sentence is rewritten to 'Anthropic' while keeping its own start/end,
    and a following 'open ai' split merges to 'OpenAI' spanning both source tokens. Verifying with
    the surrounding words proves the corrector doesn't shift neighbour timestamps (ticket: garble
    correction was never checked against actual bad timestamps)."""
    words = [
        {"w": "And", "s": 0.00, "e": 0.20},
        {"w": "Thropic", "s": 0.20, "e": 0.65},   # bare-Thropic garble that slipped onto a real caption
        {"w": "just", "s": 0.65, "e": 0.90},
        {"w": "open", "s": 0.90, "e": 1.10},
        {"w": "ai", "s": 1.10, "e": 1.35},
    ]
    fixed = abn_factory._fix_brand_words(words)
    assert [w["w"] for w in fixed] == ["And", "Anthropic", "just", "OpenAI"]
    # the corrected brand keeps the EXACT timestamps Whisper emitted for the garble
    anthropic = fixed[1]
    assert anthropic["s"] == 0.20 and anthropic["e"] == 0.65
    # the merged OpenAI spans both original 'open' + 'ai' tokens; neighbours are untouched
    assert fixed[-1]["s"] == 0.90 and fixed[-1]["e"] == 1.35
    assert fixed[0] == {"w": "And", "s": 0.00, "e": 0.20}
    assert fixed[2] == {"w": "just", "s": 0.65, "e": 0.90}


def test_fix_brand_words_leaves_real_words_untouched():
    """Lowercase real words that merely resemble a garble (tropics/tropical) must NEVER be rewritten."""
    words = [{"w": "tropical", "s": 0.0, "e": 0.4}, {"w": "storms", "s": 0.4, "e": 0.8}]
    fixed = abn_factory._fix_brand_words(words)
    assert [w["w"] for w in fixed] == ["tropical", "storms"]


def test_script_align_takes_text_from_script_keeps_whisper_timing():
    """Captions are SCRIPT-TRUE: when Whisper mis-transcribes our own TTS, the on-screen text comes
    from the script we WROTE while timestamps come from Whisper. (John: 'render captions from the
    actual script, not the voiceover'.)"""
    script = "OpenAI shipped Codex today"
    whisper_words = [
        {"w": "open", "s": 0.0, "e": 0.3},
        {"w": "ai", "s": 0.3, "e": 0.6},
        {"w": "shipped", "s": 0.6, "e": 1.0},
        {"w": "codecs", "s": 1.0, "e": 1.4},   # mis-transcribed 'Codex'
        {"w": "today", "s": 1.4, "e": 1.8},
    ]
    out = abn_factory._script_align(whisper_words, script)
    assert " ".join(w["w"] for w in out) == script  # text is exactly the script
    # timing is preserved from whisper (first token starts at 0, last ends at 1.8)
    assert out[0]["s"] == 0.0
    assert out[-1]["e"] == pytest.approx(1.8, abs=0.01)


def test_script_align_returns_input_when_either_missing():
    assert abn_factory._script_align([], "script") == []
    words = [{"w": "x", "s": 0, "e": 1}]
    assert abn_factory._script_align(words, "") == words


def test_align_returns_whisper_words_without_cli_fallback(monkeypatch, tmp_path):
    """_align resolves the segment slug to its gateway voice wav, runs faster-whisper, and returns
    its words. When faster-whisper yields words, the slow whisper-CLI fallback must NOT be invoked."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    wav = abn_assets.asset_path_from_slug("ep_a111111_s0", "voice")
    wav.write_bytes(b"fake wav")

    monkeypatch.setattr(abn_factory, "_align_sync", lambda p: [{"w": "hi", "s": 0.0, "e": 0.5}])

    async def boom_sh(*a, **k):  # CLI fallback must never run on the happy path
        raise AssertionError("whisper-CLI fallback ran despite faster-whisper success")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    words = asyncio.run(abn_factory._align("ep_a111111_s0"))
    assert words == [{"w": "hi", "s": 0.0, "e": 0.5}]


# ---------------- ASSEMBLE: refuse an empty episode ----------------

def test_assemble_episode_raises_when_no_clips_render(monkeypatch, tmp_path):
    """If ffmpeg fails to produce ANY per-segment clip, _assemble_episode must raise 'no segment
    clips' rather than silently emit a 0-length or partial episode."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def failing_sh(cmd, timeout=600):
        return 1, "ffmpeg: nope"  # every per-segment encode fails

    monkeypatch.setattr(abn_factory, "_sh", failing_sh)

    segments = [{"script": "hi", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None}]
    with pytest.raises(RuntimeError, match="no segment clips"):
        asyncio.run(abn_factory._assemble_episode("ep_a111111", segments))


def test_assemble_episode_raises_on_zero_segments(monkeypatch, tmp_path):
    """The <1-usable-segment gate also covers the degenerate case: an empty segment list (nothing
    survived upstream filtering) must raise 'no segment clips', not silently emit a 0-length episode.
    ffmpeg is never even reached here, so the gate is the segment-count check itself."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def boom_sh(cmd, timeout=600):  # no segments means no encode should be attempted
        raise AssertionError("ran ffmpeg despite zero segments")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    with pytest.raises(RuntimeError, match="no segment clips"):
        asyncio.run(abn_factory._assemble_episode("ep_a111111", []))


def test_assemble_episode_uses_reencode_fallback_when_copy_concat_fails(monkeypatch, tmp_path):
    """`_assemble_episode` has two concat paths: a fast stream-copy (`-c copy`) and a slow
    re-encode fallback (`libx264`). When the copy-concat fails (heterogeneous segment streams —
    the normal case for ABN's mixed asset layers), the function MUST fall back to re-encoding
    rather than raising. This pins that fallback branch (services/abn_factory.py ~2467-2472).

    The stub `_sh` dispatches on the command: per-segment encodes and the ffprobe duration probe
    succeed; the `-c copy` concat fails (no output file written) so `final.exists()` is False and
    the fallback fires; the `libx264` concat succeeds and writes the final episode. We assert the
    fallback actually ran and the function returns the managed URL + measured duration."""
    import shlex

    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    calls = {"copy_concat": 0, "reencode_concat": 0}

    async def routed_sh(cmd, timeout=600):
        if cmd.startswith("ffprobe"):
            return 0, "12.5\n"  # _dur probe on the final episode
        # the ffmpeg output path is always the last shell-quoted token of the command
        out = shlex.split(cmd)[-1]
        if "-f concat" in cmd and "-c copy" in cmd:
            calls["copy_concat"] += 1
            return 1, "ffmpeg: do not output non monotonically increasing dts / copy failed"
        if "-f concat" in cmd and "libx264" in cmd:
            calls["reencode_concat"] += 1
            from pathlib import Path
            Path(out).write_bytes(b"\x00final-mp4")  # re-encode succeeds → final exists
            return 0, ""
        # per-segment encode: succeeds and writes the intermediate clip
        from pathlib import Path
        Path(out).write_bytes(b"\x00seg-mp4")
        return 0, ""

    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    segments = [{"script": "hi", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None}]
    url, dur = asyncio.run(abn_factory._assemble_episode("ep_a111111", segments))

    assert calls["copy_concat"] == 1, "fast copy-concat should have been attempted first"
    assert calls["reencode_concat"] == 1, "re-encode fallback must run after copy-concat fails"
    assert url and isinstance(url, str)
    assert dur == 12.5  # duration came from the ffprobe stub on the fallback-produced final


# ---------------- _render_remotion: error recovery + fallback re-encode ----------------
#
# _render_remotion shells out to the Remotion CLI to produce the full episode mp4, then runs a
# normalize (yuv420p + -14 LUFS) and an optional duck post-pass. Two resilience behaviours are
# load-bearing and were previously untested — a regression in either silently DROPS whole episodes:
#
#   1. A failed Remotion render (non-zero exit / no output file) MUST raise RuntimeError. The caller
#      (_produce_episode) only re-renders-from-the-same-timeline / falls back to ffmpeg when this
#      raises; if it stopped raising, a broken/0-byte render would ship.
#   2. A failed POST pass (normalize / duck) must be NON-FATAL: it logs an error event but still
#      returns the episode, so a loudnorm/ffmpeg hiccup doesn't throw away a good Remotion render.

def _stub_remotion_dir(monkeypatch, tmp_path):
    """Point REMOTION_DIR at a tmp dir whose node_modules exists so the install-guard passes."""
    rdir = tmp_path / "remotion"
    (rdir / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(abn_factory, "REMOTION_DIR", rdir)
    return rdir


def test_render_remotion_raises_when_not_installed(monkeypatch, tmp_path):
    """No node_modules → raise 'remotion not installed' BEFORE shelling out. This is the first
    thing the caller's retry/fallback path relies on raising."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "REMOTION_DIR", tmp_path / "no-remotion-here")

    async def boom_sh(*a, **k):
        raise AssertionError("shelled out despite remotion not installed")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    with pytest.raises(RuntimeError, match="remotion not installed"):
        asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": None}))


def test_render_remotion_raises_on_render_failure(monkeypatch, tmp_path):
    """A non-zero Remotion exit (and no output mp4) MUST raise RuntimeError carrying the exit code +
    log tail. This is THE error path the caller's re-render-from-same-timeline retry hinges on; if
    it silently swallowed the failure the episode would ship empty."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    async def failing_sh(cmd, timeout=600):
        return 1, "Error: composition crashed at frame 0"

    monkeypatch.setattr(abn_factory, "_sh", failing_sh)
    with pytest.raises(RuntimeError, match=r"remotion: exit 1"):
        asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": None}))


def test_render_remotion_survives_failed_normalize_pass(monkeypatch, tmp_path):
    """If the Remotion render SUCCEEDS but the normalize post-pass fails, _render_remotion must NOT
    raise — it keeps the rendered episode, emits a non-fatal error event, and still returns
    (url, duration). A loudnorm/ffmpeg hiccup must never throw away a good render."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")

    calls = {"n": 0}

    async def fake_sh(cmd, timeout=600):
        calls["n"] += 1
        if "npx remotion render" in cmd:
            out.write_bytes(b"\x00fake-mp4")     # remotion succeeds: writes the episode mp4
            return 0, "rendered"
        return 1, "ffmpeg normalize: boom"        # the normalize re-encode fails

    async def fake_dur(path):
        return 640.0

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    seen_before = {e["id"] for e in abn_factory.BUS.replay()}

    url, dur = asyncio.run(
        abn_factory._render_remotion("ep_a111111", {"musicBed": None}))

    assert dur == 640.0
    assert url.startswith("/agenticnews-assets/")
    assert out.exists(), "a failed post-pass must not delete the rendered episode"
    # the failure was reported but swallowed: a non-fatal 'normalize pass failed' error event fired.
    new_events = [e for e in abn_factory.BUS.replay() if e["id"] not in seen_before]
    assert any(e["action"] == "error" and "normalize pass failed" in e["detail"] for e in new_events), \
        "a failed normalize pass must emit a non-fatal error event, not raise"


def test_render_remotion_reuses_existing_complete_render(monkeypatch, tmp_path):
    """The re-render GUARD: a pre-existing complete render (>= 10min AND already yuv420p) is REUSED —
    _render_remotion returns its (url, duration) 2-tuple WITHOUT ever invoking the Remotion CLI.
    Re-entry for an already-rendered episode must be cheap, not a double-render."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")
    out.write_bytes(b"\x00already-rendered")

    async def fake_sh(cmd, timeout=600):
        if "npx remotion render" in cmd:
            raise AssertionError("re-rendered despite a complete existing render")
        return 0, "yuv420p"                       # the ffprobe pix_fmt probe

    async def fake_dur(path):
        return 900.0

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    url, dur = asyncio.run(
        abn_factory._render_remotion("ep_a111111", {"musicBed": None}, force=False))
    assert dur == 900.0
    assert url.startswith("/agenticnews-assets/")


# ---------------- _plan_shots: segment mixing, rhythm variation, Ken-Burns ----------------
#
# _plan_shots is the shot-composition engine (≈130 lines, previously untested). It:
#   * seeds per-segment RHYTHM variation by `seg_index % 3` (the UI/demo split, the card slot),
#     so no two segments are templated;
#   * shares ONE _kb_picker per segment so consecutive Ken-Burns moves never repeat their
#     direction (the "every shot is the same slow push-in" defect John flagged);
#   * drops UI/demo beats whose source asset is missing/corrupt, and falls back card→screenshot;
#   * mixes live UI broll → Ken-Burns artifact middle → live demo broll.
# Every existence check goes through _resolve_asset, so the only thing we stub is which asset
# paths "exist" — no disk, no ffmpeg, no Remotion.

def _present_assets(monkeypatch, present):
    """Make _resolve_asset(url).exists() return True iff `url` is in `present` (a set of strings)."""
    present = set(present)

    class _P:
        def __init__(self, s):
            self._s = s

        def exists(self):
            return self._s in present

    monkeypatch.setattr(abn_factory, "_resolve_asset", lambda x: _P(str(x)))


def _full_segment(monkeypatch, seg_index, duration=60.0):
    """Plan one segment with all four asset types present (ui + screenshot + card + demo)."""
    paths = {
        "screenshot": "/agenticnews-assets/ep_a111111/shot.png",
        "card": "/agenticnews-assets/ep_a111111/card.png",
        "ui": "/agenticnews-assets/ep_a111111/ui.mp4",
        "demo": "/agenticnews-assets/ep_a111111/demo.mp4",
    }
    _present_assets(monkeypatch, paths.values())
    return abn_factory._plan_shots(
        duration, paths["screenshot"], paths["card"], words=[], keywords=[],
        source_url="https://github.com/foo/bar", demo=paths["demo"], ui=paths["ui"],
        seg_index=seg_index,
    )


def test_plan_shots_mixes_ui_artifact_demo_in_order(monkeypatch):
    """A full segment composites three beats in order: live UI broll (open) → Ken-Burns artifact
    middle → live code-demo broll (close). The shot ids encode the beat (ui*/shot*/demo*)."""
    shots = _full_segment(monkeypatch, seg_index=0)
    kinds = []
    for s in shots:
        if s["id"].startswith("ui"):
            kinds.append("ui")
        elif s["id"].startswith("demo"):
            kinds.append("demo")
        elif s["type"] == "artifact":
            kinds.append("artifact")
    # all three beat kinds present, and they never interleave (UI block, then artifacts, then demo)
    assert set(kinds) == {"ui", "artifact", "demo"}
    assert kinds == sorted(kinds, key=["ui", "artifact", "demo"].index)
    # shots are time-ordered and non-overlapping; every beat is capped at the editing ceiling (~8s)
    for a, b in zip(shots, shots[1:]):
        assert a["endSec"] <= b["startSec"] + 0.01
    for s in shots:
        assert s["endSec"] - s["startSec"] <= 8.0 + 0.01


def test_plan_shots_rhythm_variation_differs_per_segment(monkeypatch):
    """REGRESSION (seg_index % 3 seeding): the UI/demo structural split must differ across the three
    consecutive segments so the episode isn't templated. We pin that ui_end AND demo_start each take
    distinct values for seg 0/1/2 — if the (0.26,0.30,0.22)/(0.68,0.72,0.70) tuples ever collapse to a
    constant, this fails."""
    ui_ends, demo_starts = [], []
    for seg in (0, 1, 2):
        shots = _full_segment(monkeypatch, seg_index=seg)
        ui_ends.append(max(s["endSec"] for s in shots if s["id"].startswith("ui")))
        demo_starts.append(min(s["startSec"] for s in shots if s["id"].startswith("demo")))
    # no two segments share a UI split, and no two share a demo start → nothing is templated
    assert len(set(ui_ends)) == 3, f"UI split repeated across segments: {ui_ends}"
    assert len(set(demo_starts)) == 3, f"demo start repeated across segments: {demo_starts}"


def test_plan_shots_card_placement_varies_by_segment_parity(monkeypatch):
    """The designed card gets a gentle still-hold (startScale 1.0 → 1.04) and is placed at a DIFFERENT
    artifact slot depending on segment parity: center-ish (n//2) on even segments, near-end (n-1) on
    odd. This is the 'card position shifts between segments' rule — pin that even vs odd land the hold
    in different positions within the artifact run."""
    def card_slot(seg_index):
        shots = _full_segment(monkeypatch, seg_index=seg_index)
        arts = [s for s in shots if s["type"] == "artifact"]
        held = [i for i, s in enumerate(arts)
                if s["kenBurns"]["startScale"] == 1.0 and s["kenBurns"]["endScale"] == 1.04]
        assert len(held) == 1, f"expected exactly one still-held card, got {held}"
        return held[0], len(arts)

    even_slot, even_n = card_slot(0)   # even seg → n//2
    odd_slot, odd_n = card_slot(1)     # odd seg  → n-1 (end)
    assert even_slot == even_n // 2
    assert odd_slot == odd_n - 1
    assert even_slot != odd_slot       # parity genuinely moves the card


def test_plan_shots_ken_burns_moves_never_repeat_direction_in_a_segment(monkeypatch):
    """The shared per-segment picker guarantees consecutive shots never reuse the same camera GESTURE.
    Designed cards are deliberately exempt (they take a fixed still-hold), so we check the LIVE moves:
    no two adjacent live Ken-Burns shots share an identical move (which would mean a repeated zoom)."""
    shots = _full_segment(monkeypatch, seg_index=0)
    live = []
    for s in shots:
        kb = s.get("kenBurns")
        if not kb:
            continue
        # skip the still-held designed card (the one intentionally-static hold)
        if kb.get("startScale") == 1.0 and kb.get("endScale") == 1.04 and kb.get("startX") == 0.5:
            continue
        live.append((kb["startScale"], kb["endScale"], kb["startX"], kb["startY"], kb["endX"], kb["endY"]))
    assert len(live) >= 4
    for a, b in zip(live, live[1:]):
        assert a != b, "two consecutive Ken-Burns shots used an identical move (no variation)"


def test_plan_shots_drops_missing_ui_and_demo_and_falls_back_to_card(monkeypatch):
    """Missing/corrupt assets (existence check fails): a screenshot/ui/demo that doesn't resolve on
    disk must be dropped — NO ui*/demo* broll shots are emitted — and the artifact middle still renders,
    sourcing the one surviving card (screenshot was nulled, so each artifact falls back to the card)."""
    card = "/agenticnews-assets/ep_a111111/card.png"
    _present_assets(monkeypatch, {card})  # only the card exists; screenshot/ui/demo are all missing
    shots = abn_factory._plan_shots(
        40.0, "/agenticnews-assets/ep_a111111/missing_shot.png", card, words=[], keywords=[],
        source_url="https://x", demo="/agenticnews-assets/ep_a111111/missing_demo.mp4",
        ui="/agenticnews-assets/ep_a111111/missing_ui.mp4", seg_index=0,
    )
    assert shots, "a segment with a valid card must still produce shots"
    assert all(s["type"] == "artifact" for s in shots), "no UI/demo broll should survive missing assets"
    assert not any(s["id"].startswith(("ui", "demo")) for s in shots)
    # screenshot was missing → nulled → every artifact sources the surviving card (not a dead path)
    assert {s["src"] for s in shots} == {card}


def test_plan_shots_survives_all_assets_missing(monkeypatch):
    """Degenerate: even with NOTHING on disk (card→screenshot fallback both None), _plan_shots must not
    crash — it emits Ken-Burns artifact beats with a null src for the compiler to skip, rather than
    raising mid-pipeline."""
    _present_assets(monkeypatch, set())  # nothing exists
    shots = abn_factory._plan_shots(
        20.0, None, None, words=[], keywords=[], source_url="https://x", seg_index=1,
    )
    assert shots and all(s["type"] == "artifact" for s in shots)
    assert {s.get("src") for s in shots} == {None}


def test_kb_picker_alternates_direction_no_two_consecutive_same(monkeypatch):
    """Unit-pin the heart of Ken-Burns variation: the picker must never hand out two consecutive moves
    with the SAME direction tag (in/out/pan). We map each returned move back to its _KB_MOVES direction
    and assert adjacency never collides — over a long run that exercises the rotation wrap-around."""
    pick = abn_factory._kb_picker(seed=0)
    # build a reverse lookup from a move's geometry → its direction tag (field index 7)
    dir_of = {}
    for m in abn_factory._KB_MOVES:
        dir_of[(m[0], m[1], m[2], m[3], m[4], m[5], m[6])] = m[7]

    dirs = []
    for _ in range(40):
        kb = pick()
        key = (kb["startScale"], kb["endScale"], kb["startX"], kb["startY"], kb["endX"], kb["endY"], kb["easing"])
        dirs.append(dir_of[key])
    for a, b in zip(dirs, dirs[1:]):
        assert a != b, f"picker served two consecutive {a!r} moves — directions must alternate"
    # and it genuinely uses more than one direction (not stuck on a single gesture)
    assert len(set(dirs)) >= 2


def test_kb_picker_seed_changes_first_move(monkeypatch):
    """The seed (segment index) rotates the picker's starting point so different segments don't all
    open on the identical first move."""
    first0 = abn_factory._kb_picker(seed=0)()
    first1 = abn_factory._kb_picker(seed=1)()
    assert first0 != first1


# ---------------- TIMELINE WRITE: must be atomic (crash-safe), not a truncating write ----------------
#
# timeline.json is the ground-truth render props. It used to be written with
# `props.write_text(json.dumps(timeline))` — a raw, non-atomic sync write. A crash mid-write
# (kill/OOM/ENOSPC/power-loss) truncates the file, leaving the timeline unreadable for the next
# render. The write now routes through services.json_store.atomic_save (tmp + fsync + rename), so a
# reader never sees a partial file and a prior good timeline survives a failed write.

def test_render_remotion_writes_timeline_atomically(monkeypatch, tmp_path):
    """_render_remotion must persist timeline.json via atomic_save: after the call the file is valid
    JSON (never truncated) and NO `.tmp` sidecar is left behind. A partial write here would corrupt
    the render's ground-truth props."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    # _render_remotion bails unless remotion's node_modules exists; fake that check without installing
    # remotion, and make the render shell out fail so we exercise ONLY the timeline write.
    monkeypatch.setattr(abn_factory, "REMOTION_DIR", tmp_path)
    (tmp_path / "node_modules").mkdir()

    async def fail_sh(*a, **k):
        return 1, "render skipped in test"

    async def fake_dur(path):
        return 0.0

    monkeypatch.setattr(abn_factory, "_sh", fail_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    ep_id = "ep_a111111"
    timeline = {"fps": 30, "segments": [{"segmentId": "s0", "durationSec": 7.5}], "title": "t"}
    # the stubbed render fails after the write → RuntimeError; the WRITE is what we're pinning
    with pytest.raises(RuntimeError):
        asyncio.run(abn_factory._render_remotion(ep_id, timeline, force=True))

    props = abn_assets.asset_path(ep_id, "timeline")
    assert props.exists(), "timeline.json was not written"
    import json as _json
    assert _json.loads(props.read_text()) == timeline, "timeline.json is not the exact payload"
    # the atomic write must leave no tmp sidecar (a leftover means a non-atomic/raw write)
    assert not list(props.parent.glob("*.tmp")), "atomic_save left a .tmp sidecar behind"


def test_timeline_write_does_not_route_through_raw_write_text():
    """Guard rail: the timeline persistence in abn_factory must go through atomic_save, NOT a raw
    `write_text(json.dumps(...))`. This pins the fix so the non-atomic write can't silently return."""
    import inspect
    src = inspect.getsource(abn_factory._render_remotion)
    assert "atomic_save" in src, "_render_remotion must persist the timeline via atomic_save"
    assert "write_text(json.dumps" not in src, "_render_remotion still does a raw truncating write"


# ---------------- _chop WINDOWING: caps, micro-beat merge, contiguity ----------------
#
# _chop(t0, t1, target, max_n, lead) splits a span into N sub-shots, each ~target seconds,
# with two hard editing rules baked in:
#   * NO sub-shot may run past ~8s (the held-on-one-frame rule) — N is recomputed upward when needed.
#   * NO tail micro-beat under 3.5s — it is merged back into the previous window (a 0.7s shot reads
#     as a glitch, not a beat).
# These pin those rules plus the contiguity/coverage invariant: the windows must tile [t0,t1] with
# no gap and no dropped beat, and clipStartSec must track the continuous source offset (+lead).

# How much tolerance to allow on the per-shot cap: shots are rounded to 2dp, so allow a hair over 8.0.
_CAP = 8.0 + 0.02


def _assert_contiguous(out, t0, t1):
    """Every window abuts the next with no gap/overlap, and together they cover exactly [t0,t1]."""
    assert out, "expected at least one window for a positive span"
    assert out[0][0] == pytest.approx(t0, abs=0.01), "first window must start at t0"
    assert out[-1][1] == pytest.approx(t1, abs=0.01), "last window must end at t1"
    for (s, e, _), (ns, _ns_e, _off) in zip(out, out[1:]):
        assert e == ns, f"gap/overlap between windows: {e} != {ns}"
        assert e > s, "each window must have positive length"


def test_chop_zero_span_returns_empty():
    """A zero-length span (t0 == t1) yields no shots — must not emit a degenerate 0-length window."""
    assert abn_factory._chop(10.0, 10.0) == []


def test_chop_negative_span_returns_empty():
    """An inverted/negative span (t1 < t0) is clamped to empty, never a backwards window."""
    assert abn_factory._chop(10.0, 4.0) == []


def test_chop_unit_duration_single_shot():
    """A short unit-duration span fits in one shot — and a lone shot is NEVER treated as a
    micro-beat to merge (there is nothing before it to merge into)."""
    out = abn_factory._chop(0.0, 2.0)
    assert len(out) == 1
    assert out == [(0.0, 2.0, 0.0)]
    _assert_contiguous(out, 0.0, 2.0)


def test_chop_no_shot_exceeds_max_length():
    """The hard cap: across a sweep of spans (incl. ones whose naive N would exceed 8s/shot, like a
    50s span where round(50/6)=8 is clamped to max_n=6 → 8.3s/shot), NO emitted sub-shot runs past ~8s."""
    for span in (8.3, 12.0, 16.0, 24.0, 30.0, 40.0, 50.0, 60.0):
        out = abn_factory._chop(0.0, span)
        for s, e, _ in out:
            assert (e - s) <= _CAP, f"span={span}: sub-shot {(s, e)} = {round(e - s, 2)}s exceeds 8s cap"
        _assert_contiguous(out, 0.0, span)


def test_chop_caps_uncapped_max_n_when_needed():
    """Regression guard for the cap recompute: a 50s span with target=6 gives round(50/6)=8, clamped
    to max_n=6 → 8.33s/shot which trips the >8s rule, so N must be bumped via ceil(50/7)=8.
    With a roomier max_n the recompute is free to take effect and every shot lands under the cap."""
    out = abn_factory._chop(0.0, 50.0, target=6.0, max_n=8)
    assert len(out) == 8  # ceil(50/7) = 8, since the clamped-at-6 plan blew the cap
    for s, e, _ in out:
        assert (e - s) <= _CAP
    _assert_contiguous(out, 0.0, 50.0)


def test_chop_merges_tail_micro_beat():
    """A trailing fragment under 3.5s must be MERGED into the previous window, never emitted as a
    standalone micro-beat. 13s @ target=6 → round(13/6)=2 shots of 6.5s each (no micro-beat there),
    so we force the case with max_n high enough to produce an undersized tail and assert no window
    in the result is under 3.5s while coverage stays exact."""
    # 10s into 3 slots = 3.33s each → the last 3.33s slot is a micro-beat and must merge up.
    out = abn_factory._chop(0.0, 10.0, target=3.3, max_n=3)
    assert len(out) == 2, "the sub-3.5s tail slot must have merged into the previous window"
    assert (out[-1][1] - out[-1][0]) >= 3.5, "no surviving window may be a sub-3.5s micro-beat"
    _assert_contiguous(out, 0.0, 10.0)


def test_chop_does_not_drop_or_duplicate_a_beat():
    """The merge must rewrite the previous window's END (extending it), never silently drop the tail
    or leave a gap. Total covered duration after any merge equals the full span exactly."""
    for span, target, max_n in ((10.0, 3.3, 3), (9.0, 4.0, 3), (7.4, 3.6, 2), (11.0, 4.0, 3)):
        out = abn_factory._chop(0.0, span, target=target, max_n=max_n)
        covered = sum(e - s for s, e, _ in out)
        assert covered == pytest.approx(span, abs=0.02), f"span={span}: coverage {covered} != {span}"
        _assert_contiguous(out, 0.0, span)


def test_chop_lead_offset_tracks_continuous_source():
    """clipStartSec (3rd tuple element) carries the continuous source offset + lead-in, advancing one
    slot per shot from `lead` — so warm-up frames are skipped without desyncing later sub-shots."""
    out = abn_factory._chop(0.0, 12.0, target=6.0, max_n=2, lead=1.5)
    assert len(out) == 2
    assert out[0][2] == 1.5  # first shot offset == lead
    slot = 12.0 / 2
    assert out[1][2] == pytest.approx(1.5 + slot, abs=0.01)  # next shot advances one slot


# ------------- _best_effort_update: swallow DB contention, SURFACE real bugs -------------
# These pin the contract of the board-state helper that replaced three bare
# `try: db.update_video(...) except Exception: pass` swallow blocks. The point of the
# refactor: tolerable sqlite contention stays silent, but a real bug (AttributeError,
# NameError, TypeError…) must no longer hide — it gets logged, not swallowed in silence.
import sqlite3


def test_best_effort_update_calls_db_on_happy_path(monkeypatch):
    seen = {}

    async def fake_update(vid, patch):
        seen["args"] = (vid, patch)
        return {"id": vid}

    monkeypatch.setattr(abn_factory.db, "update_video", fake_update)
    asyncio.run(abn_factory._best_effort_update("s1", {"stage": "scripting"}))
    assert seen["args"] == ("s1", {"stage": "scripting"})


def test_best_effort_update_swallows_sqlite_error_silently(monkeypatch):
    async def boom(vid, patch):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(abn_factory.db, "update_video", boom)
    asyncio.run(abn_factory._best_effort_update("s1", {"stage": "assets"}))


def test_best_effort_update_logs_unexpected_error_instead_of_hiding_it(monkeypatch, caplog):
    async def real_bug(vid, patch):
        raise AttributeError("'NoneType' object has no attribute 'execute'")  # a genuine bug

    monkeypatch.setattr(abn_factory.db, "update_video", real_bug)
    with caplog.at_level("ERROR", logger=abn_factory.__name__):
        asyncio.run(abn_factory._best_effort_update("s1", {"stage": "voicing"}))
    assert any("unexpected error updating video" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)  # logged with traceback (logger.exception)


# ---------------- CARD BACKGROUNDS: writes must flow through the abn_assets gateway ----------------
#
# _ensure_card_backgrounds() used to promote a generated background straight into
# `ASSETS / "card_backgrounds"` via `src.replace(bgdir / f"bg_{idx:02d}.png")`, bypassing the
# services/abn_assets gateway. That let a broken/modified function dump an off-schema file under the
# ASSETS root with no _SLUG_RE validation. The write now routes through shared_path("card_backgrounds",
# ...) (lands in _shared/card_backgrounds/, name-validated), and the cards reader (_v2cards._ASSETS_DIR)
# is pointed at that same dir so reads and writes can't drift apart.

def test_cards_assets_dir_matches_gateway_write_location(monkeypatch, tmp_path):
    """The cards reader base (_cards_assets_dir) + '/card_backgrounds' MUST equal the dir shared_path
    writes the bg pool into. If these drift, cards read an empty pool and silently fall back to the
    flat gradient even though backgrounds were generated."""
    import pathlib
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    write_dir = abn_assets.shared_path("card_backgrounds", "bg_00.png").parent
    # the cards reader resolves <_cards_assets_dir()>/card_backgrounds — must equal the write dir.
    read_dir = pathlib.Path(abn_factory._cards_assets_dir()) / "card_backgrounds"
    assert read_dir == write_dir
    assert write_dir.parent.name == "_shared"
    assert write_dir != tmp_path / "card_backgrounds"  # NOT the old off-schema flat location


def test_ensure_card_backgrounds_promotes_through_gateway(monkeypatch, tmp_path):
    """_ensure_card_backgrounds must promote a generated keeper into the GATEWAY dir
    (_shared/card_backgrounds/bg_NN.png), not the off-schema flat `ASSETS/card_backgrounds/`.
    A direct write would skip the gateway's _SLUG_RE validation (the whole point of the ticket)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "_V2_VISUALS", True)

    calls = {"n": 0}

    def fake_codex_image(prompt, out_name, size="1536x1024"):
        # mimic the real return: a /agenticnews-assets/ URL whose file already exists on disk
        # (codex drops it under _scratch/). The promoter resolves + .replace()s it into the pool.
        rel = f"_scratch/{out_name}.png"
        src = tmp_path / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x89PNG\r\n")  # plausible PNG bytes; content is irrelevant to the test
        calls["n"] += 1
        return "/agenticnews-assets/" + rel

    monkeypatch.setattr(abn_factory, "_codex_image", fake_codex_image)

    abn_factory._ensure_card_backgrounds(want=2)

    gateway_dir = tmp_path / "_shared" / "card_backgrounds"
    flat_dir = tmp_path / "card_backgrounds"
    promoted = sorted(p.name for p in gateway_dir.glob("bg_*.png"))
    assert promoted == ["bg_00.png", "bg_01.png"], "keepers must land in the gateway dir"
    # the off-schema flat location must NOT be written to at all
    assert not flat_dir.exists() or not list(flat_dir.glob("bg_*.png")), \
        "_ensure_card_backgrounds wrote to the off-schema flat card_backgrounds/ (gateway bypassed)"
    # and the cards reader was pointed at the gateway's base, not the flat ASSETS root
    assert abn_factory._cards_assets_dir() == str(tmp_path / "_shared")


# ---------------- _build_timeline: schema-aware ABN episode assembly ----------------
#
# _build_timeline composes segments → a Remotion timeline. It is the routine commit cb5c98f5
# touched to add (a) v2 DESIGNED-CARD swapping (replace blog-screenshot 'artifact' shots with
# designed cards), (b) FIRST-5-SECONDS hook reordering (the hook card owns 0:00 on seg 0), and
# (c) hook-window pop/highlight suppression. None of that had a regression test. These pin it
# WITHOUT touching disk, an LLM, the v2 card renderer, or Remotion: _extract_keywords and
# _v2_scene_cards are stubbed, and _resolve_asset is faked so the swapped-in card urls "exist"
# on disk with a non-trivial size (the >1024-byte guard in the swap).


class _FakeAsset:
    """Stand-in for _resolve_asset(url): .exists() and .stat().st_size are driven by `present`,
    a {url: size} map. The v2-card swap gates on BOTH cf.exists() and cf.stat().st_size > 1024."""

    def __init__(self, s, present):
        self._s = s
        self._present = present

    def exists(self):
        return self._s in self._present

    def stat(self):
        size = self._present.get(self._s, 0)
        return type("st", (), {"st_size": size})()


def _stub_build_timeline_env(monkeypatch, present, v2_cards, keywords=None):
    """Wire up the four external touch-points _build_timeline reaches into so it runs offline:
      * _resolve_asset → existence + size driven by `present` ({url: size_bytes});
      * _v2_scene_cards → returns the fixed `v2_cards` url list (no scene-tagging / card render);
      * _extract_keywords → returns `keywords` (no LLM / heuristic keyword pass)."""
    monkeypatch.setattr(abn_factory, "_resolve_asset", lambda x: _FakeAsset(str(x), present))
    monkeypatch.setattr(abn_factory, "_v2_scene_cards",
                        lambda ep_id, seg_index, seg, ep_budget=None: list(v2_cards))
    monkeypatch.setattr(abn_factory, "_extract_keywords",
                        lambda *a, **k: list(keywords or []))


def _seg(idx, duration=60.0, card="/agenticnews-assets/ep_t/card.png"):
    return {
        "segment_id": f"seg{idx}", "title": f"Tool {idx} — the headline", "script": "body text " * 20,
        "words": [{"text": "w", "s": 0.0, "e": 0.3}], "source_url": "https://github.com/foo/bar",
        "screenshot": None, "card": card, "vo_path": f"/agenticnews-assets/ep_t/vo{idx}.wav",
        "duration": duration,
    }


def test_build_timeline_returns_well_formed_episode_envelope(monkeypatch):
    """The top-level shape: 30fps 1920x1080, one tseg per input segment (in order), totalSec is the
    sum of segment durations, and each tseg carries its VO + ALWAYS an empty keywordPops list
    (the floating-label feature was deaded — a regression that re-populates it would be caught here)."""
    _stub_build_timeline_env(monkeypatch, present={}, v2_cards=[])
    segs = [_seg(0, 60.0), _seg(1, 50.0), _seg(2, 40.0)]
    tl = abn_factory._build_timeline("ep_t", 7, segs, animated_bg=None)

    assert tl["fps"] == 30 and tl["width"] == 1920 and tl["height"] == 1080
    assert [s["segmentId"] for s in tl["segments"]] == ["seg0", "seg1", "seg2"]
    assert tl["totalSec"] == pytest.approx(150.0, abs=0.01)
    for ts in tl["segments"]:
        assert ts["keywordPops"] == [], "keywordPops must stay deaded — floating labels were removed"
        assert ts["audio"]["vo"]["duration"] > 0 and ts["audio"]["vo"]["src"]


def test_build_timeline_swaps_v2_designed_cards_into_artifact_shots(monkeypatch):
    """cb5c98f5 anti-slop swap: 'artifact' shots (blog-screenshot slop) are replaced by DESIGNED
    cards from the v2 catalog when the card file exists on disk with real bytes. The swapped shot
    points at the designed-card url and takes the gentle still-hold (1.0→1.05), NOT a hard Ken-Burns."""
    card = "/agenticnews-assets/ep_t/card.png"
    v2 = "/agenticnews-assets/ep_t/css/s1_v2sc0.png"
    # the card the planner needs AND the designed card both 'exist' with > 1KB so the swap gate passes
    present = {card: 4096, v2: 8192}
    _stub_build_timeline_env(monkeypatch, present=present, v2_cards=[v2])
    # use a NON-zero segment so the hook-reorder branch (seg 0 only) doesn't interfere
    tl = abn_factory._build_timeline("ep_t", 7, [_seg(0), _seg(1, card=card)], animated_bg=None)

    shots = tl["segments"][1]["shots"]
    swapped = [s for s in shots if s.get("src") == v2]
    assert swapped, "no artifact shot was swapped for a v2 designed card"
    for s in swapped:
        kb = s["kenBurns"]
        assert (kb["startScale"], kb["endScale"]) == (1.0, 1.05), "designed card must get the gentle hold"


def test_build_timeline_skips_v2_swap_when_card_file_too_small(monkeypatch):
    """DEFENSE-IN-DEPTH: the swap is gated on cf.stat().st_size > 1024 — a 0/under-size designed-card
    file (a render that produced a stub) must NOT be injected, or the timeline references a broken
    asset. The original artifact card src survives untouched."""
    card = "/agenticnews-assets/ep_t/card.png"
    v2 = "/agenticnews-assets/ep_t/css/s1_v2sc0.png"
    present = {card: 4096, v2: 512}  # designed card exists but is below the 1024-byte floor
    _stub_build_timeline_env(monkeypatch, present=present, v2_cards=[v2])
    tl = abn_factory._build_timeline("ep_t", 7, [_seg(0), _seg(1, card=card)], animated_bg=None)

    srcs = {s.get("src") for s in tl["segments"][1]["shots"]}
    assert v2 not in srcs, "an under-1KB designed card must never be swapped into the timeline"
    assert card in srcs, "the original artifact card must survive when the v2 swap is rejected"


def test_build_timeline_hook_card_owns_zero_on_opening_segment(monkeypatch):
    """FIRST-5-SECONDS (cb5c98f5): on seg 0 the HOOK card must be the very first thing on screen —
    moved to the front and retimed so startSec == 0.0 — not 3s of webpage scroll first."""
    card = "/agenticnews-assets/ep_t/card.png"
    hook = "/agenticnews-assets/ep_t/css/s0_v2sc0_hook.png"
    present = {card: 4096, hook: 8192}
    _stub_build_timeline_env(monkeypatch, present=present, v2_cards=[hook])
    tl = abn_factory._build_timeline("ep_t", 7, [_seg(0, card=card)], animated_bg=None)

    shots = tl["segments"][0]["shots"]
    hook_shots = [s for s in shots if "hook" in (s.get("src") or "")]
    assert hook_shots, "expected a hook shot on the opening segment"
    assert shots[0] is hook_shots[0], "the hook must be the FIRST shot on seg 0"
    assert shots[0]["startSec"] == 0.0, "the hook must own 0:00 on the opening segment"


def test_build_timeline_keeps_the_hook_window_clean(monkeypatch):
    """KEEP THE HOOK CLEAN (cb5c98f5): on seg 0, keyword-pops in the first ~12s are suppressed and
    per-shot highlight boxes inside the hook window are stripped — the opening statement stands alone.
    keywordPops is emitted empty regardless (deaded), so we assert via the suppression's side effects:
    no shot in the hook window keeps a highlight."""
    card = "/agenticnews-assets/ep_t/card.png"
    hook = "/agenticnews-assets/ep_t/css/s0_v2sc0_hook.png"
    present = {card: 4096, hook: 8192}
    # a keyword that lands at 1.0s — inside the 12s hook window — would have popped pre-fix
    kws = [{"text": "agents", "s": 1.0, "e": 1.4, "color": "#0ff"}]
    _stub_build_timeline_env(monkeypatch, present=present, v2_cards=[hook], keywords=kws)
    tl = abn_factory._build_timeline("ep_t", 7, [_seg(0, card=card)], animated_bg=None)

    shots = tl["segments"][0]["shots"]
    for s in shots:
        if s.get("startSec", 0) < 12.0:
            assert "highlight" not in s, "a highlight box survived inside the clean hook window"
    assert tl["segments"][0]["keywordPops"] == []


def test_build_timeline_suppresses_lower_third_on_hook_segment_only(monkeypatch):
    """CAPTION best-practice (cb5c98f5): seg 0 opens clean (no lower-third over the hook), while later
    segments DO get a lower-third headline sourced from the segment title + source url."""
    _stub_build_timeline_env(monkeypatch, present={}, v2_cards=[])
    tl = abn_factory._build_timeline("ep_t", 7, [_seg(0), _seg(1)], animated_bg=None)

    assert tl["segments"][0]["lowerThirds"] == [], "seg 0 must open clean — no lower-third over the hook"
    lt = tl["segments"][1]["lowerThirds"]
    assert lt and lt[0]["headline"] and lt[0]["sourceUrl"] == "https://github.com/foo/bar"
