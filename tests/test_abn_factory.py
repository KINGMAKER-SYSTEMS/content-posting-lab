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
import json
from pathlib import Path

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


@pytest.mark.parametrize("evil", [
    "/path/to/evil.safetensors",            # absolute path to a clone weights file
    "../../etc/passwd",                      # path traversal
    "english_2026-04/../evil.safetensors",   # smuggle a path behind a valid-looking prefix
    "model.safetensors",                     # a bare filename (has a '.' extension)
    "~/john_voice.safetensors",              # home-relative clone file
    "english 2026-04; rm -rf /",             # shell-ish junk with a space
    "C:\\models\\evil.bin",                  # windows path
    "replicate:chatterbox",                 # cloud-engine handle
])
def test_pocket_tts_command_rejects_pathlike_language_env(monkeypatch, tmp_path, evil):
    """LOCKED-VOICE HARD GATE: ABN_POCKET_LANGUAGE is operator-controlled but NEVER trusted
    verbatim. A path / filename / shell-junk value must be rejected and replaced by the
    built-in default — it must NOT reach the `--language` flag, which could feed pocket-tts a
    clone weights file (.safetensors) or a cloud handle and re-narrate the channel."""
    monkeypatch.setenv("ABN_POCKET_LANGUAGE", evil)
    cmd = abn_factory._pocket_tts_command("hi", tmp_path / "v.wav")

    # the poisoned value never lands in the command at all
    assert evil not in cmd
    # --language falls back to the built-in default, never the attacker value
    assert cmd[cmd.index("--language") + 1] == "english_2026-04"
    # belt-and-suspenders: no banned narrator artifact leaked into the joined command
    joined = " ".join(cmd).lower()
    for banned in ("safetensors", "replicate", "chatterbox", "elevenlabs", "..", "/etc", "rm -rf"):
        assert banned not in joined, f"VO command leaked a poisoned language value: {banned!r}"


@pytest.mark.parametrize("evil", [
    "english_x\n",                          # bare trailing newline (Python `$` tolerates this)
    "english_x\n/path/to/evil.safetensors", # trailing-newline smuggles a clone path
    "english_2026-04\nmodel.safetensors",   # valid-looking code, then newline + clone file
])
def test_pocket_language_regex_rejects_trailing_newline_bypass(evil):
    """REGEX-LEVEL DEFENSE-IN-DEPTH for the locked-voice gate: Python's `re.match` + `$` treats
    a trailing newline as the end of the string, so a value like "english_x\\n/evil.safetensors"
    can pass `match()` even though it carries an embedded path. `_POCKET_LANG_RE` must use
    `fullmatch` semantics so it rejects these regardless of whether the caller's .strip() is
    present — guarding against a future refactor dropping the strip and reopening the bypass."""
    assert abn_factory._POCKET_LANG_RE.fullmatch(evil) is None


def test_pocket_language_resolves_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("ABN_POCKET_LANGUAGE", raising=False)
    assert abn_factory._pocket_language() == "english_2026-04"


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


def test_assemble_episode_raises_when_reencode_fallback_also_fails(monkeypatch, tmp_path):
    """The fallback's OWN failure must surface. When BOTH concat passes fail (copy AND the libx264
    re-encode), `_assemble_episode` must raise `RuntimeError("concat: ...")` carrying the ffmpeg log
    tail — NOT silently return a URL to a non-existent / 0-byte episode. Pins the error branch at
    services/abn_factory.py ~2470-2471 that the existing happy-path fallback test does not reach."""
    import shlex

    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def routed_sh(cmd, timeout=600):
        if cmd.startswith("ffprobe"):
            raise AssertionError("probed duration despite both concat passes failing")
        if "-f concat" in cmd:
            # neither concat writes an output file → final.exists() stays False on both passes
            return 1, "ffmpeg: concat blew up — non monotonic dts, then re-encode also failed"
        # per-segment encode succeeds so we actually reach the concat stage
        from pathlib import Path
        Path(shlex.split(cmd)[-1]).write_bytes(b"\x00seg-mp4")
        return 0, ""

    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    segments = [{"script": "hi", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None}]
    with pytest.raises(RuntimeError, match="concat:"):
        asyncio.run(abn_factory._assemble_episode("ep_a111111", segments))


def test_assemble_episode_per_segment_encode_includes_karaoke_drawtext(monkeypatch, tmp_path):
    """Each per-segment encode must carry the karaoke caption filter chain — the `drawbox` strip +
    `drawtext` overlay with the segment's script text. The ticket flags that recent ffmpeg command
    edits (drawbox/drawtext) could break SILENTLY: if either filter is dropped or the script text
    stops reaching drawtext, every episode ships without burned captions and nothing else notices.
    This captures the actual encode command and asserts the filter chain + caption text survive."""
    import shlex

    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    seg_cmds = []

    async def routed_sh(cmd, timeout=600):
        if cmd.startswith("ffprobe"):
            return 0, "9.0\n"
        out = shlex.split(cmd)[-1]
        from pathlib import Path
        if "-f concat" in cmd:  # let the fast copy-concat succeed
            Path(out).write_bytes(b"\x00final-mp4")
            return 0, ""
        seg_cmds.append(cmd)  # per-segment encode
        Path(out).write_bytes(b"\x00seg-mp4")
        return 0, ""

    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    segments = [{"script": "Anthropic shipped a coding agent", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None}]
    asyncio.run(abn_factory._assemble_episode("ep_a111111", segments))

    assert len(seg_cmds) == 1, "exactly one per-segment encode should have run"
    cmd = seg_cmds[0]
    assert "drawbox=" in cmd, "karaoke caption background strip (drawbox) was dropped from the encode"
    assert "drawtext=" in cmd, "karaoke caption overlay (drawtext) was dropped from the encode"
    # the segment's script text must actually reach drawtext (shlex-quoted somewhere in the cmd)
    assert "Anthropic shipped a coding agent" in cmd, "segment script text never reached drawtext"


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


def test_render_remotion_force_rerenders_despite_complete_render(monkeypatch, tmp_path):
    """force=True (operator edit from the editor bay) MUST bypass the reuse guard: even though a
    pre-existing complete render is on disk (>= 10min AND yuv420p — the SAME shape the reuse path
    accepts), the Remotion CLI MUST be re-invoked so the freshly-edited timeline actually ships.
    Reusing the stale mp4 here would silently re-publish the unedited video."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")
    out.write_bytes(b"\x00stale-render")            # a complete, reuse-eligible render already exists

    rendered = {"n": 0}

    async def fake_sh(cmd, timeout=600):
        if "npx remotion render" in cmd:
            rendered["n"] += 1
            out.write_bytes(b"\x00fresh-render")     # remotion re-renders the edited timeline
            return 0, "rendered"
        return 0, "yuv420p"                          # ffprobe pix_fmt + normalize/duck passes

    async def fake_dur(path):
        return 900.0                                 # long + would otherwise satisfy the reuse guard

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    url, dur = asyncio.run(
        abn_factory._render_remotion("ep_a111111", {"musicBed": None}, force=True))
    assert rendered["n"] == 1, "force=True must re-invoke Remotion, not reuse the existing render"
    assert url.startswith("/agenticnews-assets/")


def test_render_remotion_does_not_reuse_unnormalized_render(monkeypatch, tmp_path):
    """Reuse-guard NEGATIVE case: a leftover mp4 that is long enough but still yuvj420p (the raw,
    pre-normalize FIRST-render output) MUST NOT be reused — reusing it would skip the loudnorm/duck
    normalize pass. The guard falls through and renders fresh."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")
    out.write_bytes(b"\x00raw-unnormalized")        # long enough but NOT yet normalized to yuv420p

    rendered = {"n": 0}

    async def fake_sh(cmd, timeout=600):
        if "npx remotion render" in cmd:
            rendered["n"] += 1
            out.write_bytes(b"\x00fresh-render")
            return 0, "rendered"
        if "pix_fmt" in cmd:
            return 0, "yuvj420p"                     # raw JPEG-range fmt → must NOT be reused
        return 0, "ok"                               # normalize/duck passes

    async def fake_dur(path):
        return 900.0                                 # plenty long; only the pix_fmt fails the guard

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": None}, force=False))
    assert rendered["n"] == 1, "a yuvj420p (un-normalized) leftover must be re-rendered, not reused"


def test_render_remotion_does_not_reuse_short_render(monkeypatch, tmp_path):
    """Reuse-guard NEGATIVE case: a leftover mp4 that is already yuv420p but UNDER the 10-min
    MIN_EPISODE_SEC floor (a partial/aborted render) MUST NOT be reused — it would ship a short
    episode that fails the RPM/mid-roll floor. The guard falls through and renders fresh."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")
    out.write_bytes(b"\x00short-partial")           # normalized fmt but too short

    rendered = {"n": 0}

    async def fake_sh(cmd, timeout=600):
        if "npx remotion render" in cmd:
            rendered["n"] += 1
            out.write_bytes(b"\x00fresh-render")
            return 0, "rendered"
        return 0, "yuv420p"                          # correct fmt; only the duration fails the guard

    async def fake_dur(path):
        return abn_factory.MIN_EPISODE_SEC - 1.0     # just under the 10-min floor

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": None}, force=False))
    assert rendered["n"] == 1, "a sub-floor (short) leftover must be re-rendered, not reused"


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


# ---------------- _build_timeline: choreographed shot-boundary crossfades ----------------
#
# _plan_shots cuts a new visual every 4-7s but bare shot boundaries hard-cut. _build_timeline
# tags every shot AFTER the first with a short `transitionSec`, which editor_timeline promotes to
# a `crossfade` effect → OpenShot dissolves the boundary. Kinetic inserts (own full-bleed motion)
# are exempt. We stub the heavy collaborators so this exercises only the tagging step.

def test_build_timeline_tags_shot_boundaries_with_crossfade_transitions(monkeypatch):
    planned = [
        {"id": "a", "type": "artifact", "src": "/x/a.png", "startSec": 0.0, "endSec": 3.0},
        {"id": "kin0", "type": "kinetic", "src": "/x/k.mp4", "startSec": 3.0, "endSec": 13.0},
        {"id": "b", "type": "artifact", "src": "/x/b.png", "startSec": 13.0, "endSec": 16.0},
    ]
    monkeypatch.setattr(abn_factory, "_plan_shots", lambda *a, **k: [dict(s) for s in planned])
    monkeypatch.setattr(abn_factory, "_extract_keywords", lambda *a, **k: [])
    monkeypatch.setattr(abn_factory, "_v2_scene_cards", lambda *a, **k: [])

    seg = {
        "segment_id": "s0", "title": "Seg", "source_url": "https://x", "duration": 16.0,
        "screenshot": None, "card": None, "ui": None, "demo": None,
        "script": "", "words": [], "vo_path": "/x/vo.wav",
    }
    tl = abn_factory._build_timeline("ep_xf", 1, [seg])
    shots = tl["segments"][0]["shots"]

    # first shot hard-cuts in (nothing to dissolve from)
    assert "transitionSec" not in shots[0]
    # the kinetic insert carries its own in/out — never gets a crossfade tag
    kin = next(s for s in shots if s["type"] == "kinetic")
    assert "transitionSec" not in kin
    # the subsequent artifact boundary gets a real crossfade duration
    b = next(s for s in shots if s["id"] == "b")
    assert b["transitionSec"] == 0.4
    # and the import path turns that into an OpenShot-bound crossfade effect
    from services import editor_timeline
    fx = editor_timeline._shot_effects(b)
    assert any(e["type"] == "crossfade" and e["params"]["duration"] == 0.4 for e in fx)


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


# ---------------- produce_one_episode: orchestration EARLY-EXIT / back-off paths ----------------
#
# produce_one_episode (the factory entry point) opens with two news-supply gates BEFORE it ever
# spends a render: (1) an empty scrape idles the factory, (2) a thin-news day tops up with evergreen
# deep-dives and, only if STILL below the segment floor, backs off rather than ship a stub episode.
# These early-exit paths drive the freshness ledger, the evergreen progressive-relaxation loop, and
# the live STATE/event side-effects, yet had zero coverage. Each test stubs the scrape + evergreen +
# freshness leaves so it never hits the network, an LLM, a render, or the SQLite DB (the early
# returns happen before db.create_video is reached).


def _stub_memory_fresh(monkeypatch):
    """Neutralize the freshness ledger so it never touches the real DB: nothing is 'recently used'."""
    import services.abn_memory as mem
    monkeypatch.setattr(mem, "is_recently_used", lambda title, *a, **k: False, raising=False)


def test_produce_one_episode_idles_when_no_stories_scraped(monkeypatch):
    """The very first gate: an empty scrape must idle the factory and return None — no episode card
    is created, no render is attempted. (services/abn_factory.py ~2487.) A regression here would let
    the loop spin on a thin-news cycle instead of backing off cleanly."""
    monkeypatch.setattr(abn_factory, "_scrape_sync", lambda: [])

    async def boom_create(*a, **k):  # the idle return fires BEFORE any episode row is created
        raise AssertionError("created an episode despite zero scraped stories")

    monkeypatch.setattr(abn_factory.db, "create_video", boom_create)

    seen = {e["id"] for e in abn_factory.BUS.replay()}
    result = asyncio.run(abn_factory.produce_one_episode())

    assert result is None
    assert abn_factory.STATE["stage"] == "idle"
    # the idle transition was announced on the bus (stage.idle from the scraper agent)
    new = [e for e in abn_factory.BUS.replay() if e["id"] not in seen]
    assert any(e["stage"] == "idle" for e in new), "an empty scrape must emit an idle stage event"


def test_produce_one_episode_backs_off_when_below_floor_after_evergreen(monkeypatch):
    """Thin-news back-off: when the scrape returns fewer than the segment floor (MIN_SEGMENTS) AND
    the evergreen top-up can't make up the gap, produce_one_episode must idle + back off (return
    None) rather than ship a too-short stub episode. This drives the evergreen progressive-relaxation
    loop (12h → 3h → 0 windows) AND the floor gate (services/abn_factory.py ~2510-2534). We give it 2
    fresh stories, an empty evergreen pool, and a default roundup (no force) so the floor stays at
    MIN_SEGMENTS — 2 < floor → back off. db.create_video must never be reached."""
    _stub_memory_fresh(monkeypatch)
    thin = [
        {"title": "Anthropic ships a thing", "url": "https://example.com/a", "pts": 120, "source_signal": "lab"},
        {"title": "OpenAI ships another", "url": "https://example.com/b", "pts": 90, "source_signal": "lab"},
    ]
    monkeypatch.setattr(abn_factory, "_scrape_sync", lambda: list(thin))
    monkeypatch.setattr(abn_factory, "_evergreen_topics", lambda n: [])  # nothing to top up with

    async def boom_create(*a, **k):  # the floor back-off fires BEFORE the episode row is created
        raise AssertionError("created an episode despite news below the segment floor")

    monkeypatch.setattr(abn_factory.db, "create_video", boom_create)

    assert len(thin) < abn_factory.MIN_SEGMENTS, "fixture must sit below the floor for this gate to fire"

    seen = {e["id"] for e in abn_factory.BUS.replay()}
    result = asyncio.run(abn_factory.produce_one_episode())

    assert result is None
    assert abn_factory.STATE["stage"] == "idle"
    # the thin-news back-off announces itself (news.thin) so the gap is observable, not silent
    new = [e for e in abn_factory.BUS.replay() if e["id"] not in seen]
    assert any(e["action"] == "news.thin" for e in new), \
        "a sub-floor news day (post-evergreen) must emit a news.thin back-off event"


# ---------------- _github_repo: clone-URL validation (security boundary) ----------------
#
# _github_repo(url) is the network-trust + path-traversal gate in front of `git clone` in
# _real_demo: it pins clones to github.com, normalizes to an https `.git` URL, and rejects any
# owner/repo slug that is '.'/'..'/empty or otherwise lacks an alphanumeric char (defense in depth
# against path traversal / shell-meta even though the value is later shlex.quote'd). The validation
# exists but had ZERO coverage — any change to _GH_REPO_RE / the slug regex (abn_factory.py ~1100-1120)
# could silently reopen the hole. These pin the security boundary.

def test_github_repo_normalizes_plain_url():
    """A bare github URL is normalized to an https `.git` clone URL (https forced even from http)."""
    assert abn_factory._github_repo("https://github.com/foo/bar") == "https://github.com/foo/bar.git"
    assert abn_factory._github_repo("http://github.com/Foo/Bar") == "https://github.com/Foo/Bar.git"


def test_github_repo_strips_then_readds_git_suffix():
    """An already-`.git` URL isn't doubled — the suffix is stripped then re-added exactly once."""
    assert abn_factory._github_repo("https://github.com/foo/bar.git") == "https://github.com/foo/bar.git"


def test_github_repo_extracts_repo_from_subpath_and_surrounding_text():
    """The owner/repo are extracted even when followed by a sub-path (…/tree/main) or embedded in prose,
    and always re-emitted as the canonical clone URL — never the raw input."""
    assert abn_factory._github_repo("https://github.com/foo/bar/tree/main") == "https://github.com/foo/bar.git"
    assert abn_factory._github_repo("see https://github.com/foo/bar for more") == "https://github.com/foo/bar.git"


def test_github_repo_allows_legitimate_dotted_and_dashed_slugs():
    """Real owner/repo names contain dots/dashes/underscores (e.g. `a-b_c.d`); these are valid as long
    as the segment has at least one alphanumeric char, so the path-traversal guard isn't over-broad."""
    assert abn_factory._github_repo("https://github.com/a-b_c.d/x.y_z") == "https://github.com/a-b_c.d/x.y_z.git"


@pytest.mark.parametrize("url", [
    "https://github.com/../etc",      # traversal in the owner slug
    "https://github.com/foo/..",      # traversal in the repo slug
    "https://github.com/./bar",       # current-dir in the owner slug
    "https://github.com/.../bar",     # all-dots owner (no alphanumeric)
    "https://github.com/foo/.git",    # repo becomes empty after stripping `.git`
])
def test_github_repo_rejects_path_traversal_slugs(url):
    """THE security regression guard: a '.'/'..'/all-dots owner or repo slug — the path-traversal
    vector — must be rejected (None), never turned into a clone URL. If the slug regex on line ~1115
    is loosened, one of these starts returning a URL and this fails."""
    assert abn_factory._github_repo(url) is None


@pytest.mark.parametrize("url", [
    "https://gitlab.com/foo/bar",            # different host entirely
    "https://evil.com/github.com/foo/bar",   # github.com only in the path, not the host
    "https://github.com.evil.com/foo/bar",   # host-spoof: github.com is a subdomain prefix
    "git@github.com:foo/bar.git",            # ssh scheme — not an https github.com URL
])
def test_github_repo_pins_to_github_host(url):
    """The network-trust boundary: only real `https?://github.com/` URLs clone — a different host, a
    spoofed subdomain, github.com appearing only in the path, or an ssh URL must all return None."""
    assert abn_factory._github_repo(url) is None


@pytest.mark.parametrize("url", ["", None, "not a url", "https://github.com/foo"])
def test_github_repo_rejects_empty_and_incomplete(url):
    """Empty/None/garbage input and an owner-only URL (no repo segment) all return None — the caller
    (_real_demo) then cleanly falls back to the scripted demo instead of cloning junk."""
    assert abn_factory._github_repo(url) is None


# ---------------- _real_demo: finally-block cleanup (workdir + tape) ----------------
#
# _real_demo clones a repo into a tempdir, records VHS footage, and ALWAYS runs a finally block
# that safe_rmtree's the workdir and safe_unlink's the .tape. That cleanup is load-bearing — a
# leak here fills the temp volume across a whole episode's segments — but was never tested. These
# pin: (1) an exception inside the try still cleans both up and returns None; (2) the early
# clone-failure return still cleans up; (3) a partial state (workdir exists but the tape was never
# written) doesn't crash the finally; (4) the cleanup helpers are never-raise by contract, so the
# finally itself can never re-raise and mask the real fallback-to-scripted-demo path.

def _patch_demo_workdir(monkeypatch, tmp_path):
    """Redirect _real_demo's tempfile.mkdtemp + asset gateway into tmp_path and hand back a dict
    that will hold the workdir/tape paths the function actually used, so a test can assert on
    whether the finally block removed them."""
    import tempfile
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    seen = {}
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*a, **k):
        k.pop("dir", None)
        d = real_mkdtemp(*a, dir=str(tmp_path), **k)
        seen["workdir"] = Path(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    return seen


def test_real_demo_cleans_workdir_and_tape_when_an_exception_is_raised(monkeypatch, tmp_path):
    """An exception AFTER the clone (here: the shell raises on the VHS pass) must be swallowed
    (_real_demo returns None so the caller falls back to the scripted demo) AND the finally block
    must still safe_rmtree the workdir and safe_unlink the tape — no temp leak on the error path."""
    seen = _patch_demo_workdir(monkeypatch, tmp_path)

    async def sh(cmd, timeout=60):
        if "git clone" in cmd:
            # simulate a successful clone: create the repo dir + a README so the body builds
            repo = seen["workdir"] / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "README.md").write_text("# hi\nreal repo\n")
            return 0, "cloned"
        raise RuntimeError("vhs blew up mid-render")  # the VHS pass explodes

    monkeypatch.setattr(abn_factory, "_sh", sh)

    result = asyncio.run(abn_factory._real_demo("https://github.com/foo/bar", "ep_a111111_s0"))
    assert result is None, "an exception must fall back to None (scripted demo), not propagate"
    assert "workdir" in seen, "the demo never created its workdir"
    assert not seen["workdir"].exists(), "finally must safe_rmtree the workdir even on the error path"
    tape = abn_assets.scratch_path("ep_a111111_s0", "ep_a111111_s0_real.tape")
    assert not tape.exists(), "finally must safe_unlink the tape even on the error path"


def test_real_demo_cleans_up_on_clone_failure_early_return(monkeypatch, tmp_path):
    """The early `return None` on a clone failure (404/private/timeout) still flows through the
    finally — the workdir created before the clone attempt must be removed, not leaked."""
    seen = _patch_demo_workdir(monkeypatch, tmp_path)

    async def sh(cmd, timeout=60):
        return 128, "fatal: repository not found"  # clone fails; repo_dir never created

    monkeypatch.setattr(abn_factory, "_sh", sh)

    result = asyncio.run(abn_factory._real_demo("https://github.com/foo/bar", "ep_a111111_s0"))
    assert result is None
    assert "workdir" in seen
    assert not seen["workdir"].exists(), "finally must remove the workdir on the clone-failure return"


def test_real_demo_finally_survives_partial_state_without_tape(monkeypatch, tmp_path):
    """Partial state: the workdir exists but the .tape was NEVER written (clone failed before the
    tape build). safe_unlink on a missing tape must NOT crash the finally — it returns False and the
    workdir is still removed. Pins that the cleanup is robust to a half-built demo."""
    seen = _patch_demo_workdir(monkeypatch, tmp_path)

    async def sh(cmd, timeout=60):
        return 1, "clone died"  # nothing past the clone runs → no tape is ever written

    monkeypatch.setattr(abn_factory, "_sh", sh)

    tape = abn_assets.scratch_path("ep_a111111_s0", "ep_a111111_s0_real.tape")
    assert not tape.exists(), "precondition: tape must not exist for the partial-state case"

    result = asyncio.run(abn_factory._real_demo("https://github.com/foo/bar", "ep_a111111_s0"))
    assert result is None  # did not raise despite the missing tape in the finally
    assert not seen["workdir"].exists()


def test_real_demo_cleanup_helpers_are_never_raise():
    """The finally block's safety rests on safe_rmtree/safe_unlink NEVER raising on a normal
    OSError (missing/locked/permission). If a refactor makes either propagate, _real_demo's finally
    could re-raise and mask the clean fallback-to-scripted-demo. Pin the never-raise contract."""
    from services.fsutil import safe_rmtree, safe_unlink
    # missing paths: both report 'nothing removed' rather than raising
    assert safe_rmtree("/nonexistent/abn/demo/workdir") is False
    assert safe_unlink("/nonexistent/abn/demo/some.tape") is False


# ---------------- _codex_image: subprocess timeout + error recovery ----------------
#
# _codex_image() shells to `codex exec` (180s timeout) to generate a card background, then grabs
# the newest png that appeared in ~/.codex/generated_images and copies it into _scratch/. Every
# error path (timeout, no image produced, copy failure) is swallowed and returns None — which is
# the CORRECT contract (a failed background must not crash episode assembly; the caller falls back
# to flux/gradients). These tests pin that contract so a future refactor can't let an exception
# escape into the pipeline, and confirm each silent path is now logged (no more invisible masking).

import subprocess as _subprocess

# conftest's autouse `no_live_codex_image` fixture replaces abn_factory._codex_image with a no-op
# lambda for hermeticity (so the app-lifespan warmup never shells to the real Codex). Capture the
# REAL function here at import time — before any fixture runs — so these tests exercise the actual
# subprocess/timeout/copy logic, not the stub. (Its closure refs the module globals we monkeypatch.)
_REAL_CODEX_IMAGE = abn_factory._codex_image


def _codex_env(monkeypatch, tmp_path):
    """Point _codex_image at an isolated CODEX_HOME + ASSETS so it never touches the real
    ~/.codex or the asset volume. Returns the generated_images subdir the function globs."""
    codex_home = tmp_path / "codex"
    gen_dir = codex_home / "generated_images" / "session"
    gen_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # _codex_image routes its _scratch write through the asset gateway (_cross_scratch_path ->
    # is_managed), which resolves against abn_assets.ASSETS_DIR — patch BOTH the factory ASSETS and
    # the gateway ASSETS_DIR at the same throwaway store so the scratch path is recognised as managed.
    assets = tmp_path / "assets"
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    return gen_dir


def test_codex_image_returns_none_on_timeout(monkeypatch, tmp_path, caplog):
    """A 180s timeout (codex hung) must be swallowed -> None, and logged so the failure is visible."""
    _codex_env(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise _subprocess.TimeoutExpired(cmd="codex", timeout=180)

    monkeypatch.setattr(_subprocess, "run", boom)
    with caplog.at_level("WARNING"):
        assert _REAL_CODEX_IMAGE("a cinematic background", "_tmp_bg_0") is None
    assert any("_codex_image" in r.message for r in caplog.records), "timeout must be logged"


def test_codex_image_returns_none_on_subprocess_error(monkeypatch, tmp_path):
    """Any other subprocess explosion (codex binary missing, OSError) is swallowed -> None."""
    _codex_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("codex")))
    assert _REAL_CODEX_IMAGE("bg", "_tmp_bg_1") is None


def test_codex_image_returns_none_when_no_image_produced(monkeypatch, tmp_path, caplog):
    """codex exec returned cleanly but wrote no new ig_*.png -> None (nothing to copy)."""
    gen_dir = _codex_env(monkeypatch, tmp_path)
    # subprocess "succeeds" but leaves the generated_images dir empty
    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        assert _REAL_CODEX_IMAGE("bg", "_tmp_bg_2") is None
    assert gen_dir.exists()  # dir untouched
    assert any("no new image" in r.message for r in caplog.records), "empty result must be logged"


def test_codex_image_returns_none_on_copy_failure(monkeypatch, tmp_path, caplog):
    """A new image WAS produced but the copy into _scratch/ fails -> None (not a crash), logged."""
    gen_dir = _codex_env(monkeypatch, tmp_path)

    def make_image(*a, **k):
        (gen_dir / "ig_new.png").write_bytes(b"\x89PNG fake")

    monkeypatch.setattr(_subprocess, "run", make_image)
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "copy",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("readonly volume")))
    with caplog.at_level("WARNING"):
        assert _REAL_CODEX_IMAGE("bg", "_tmp_bg_3") is None
    assert any("failed to copy" in r.message for r in caplog.records), "copy failure must be logged"


def test_codex_image_happy_path_copies_newest_into_scratch(monkeypatch, tmp_path):
    """When codex writes a new png, it is copied into _scratch/<out_name>.png and the managed
    /agenticnews-assets URL is returned. Pins that the success path actually lands the asset."""
    gen_dir = _codex_env(monkeypatch, tmp_path)

    def make_image(*a, **k):
        (gen_dir / "ig_pick.png").write_bytes(b"\x89PNG real")

    monkeypatch.setattr(_subprocess, "run", make_image)
    url = _REAL_CODEX_IMAGE("a deep blue gradient", "_tmp_bg_9")

    assert url == "/agenticnews-assets/_scratch/_tmp_bg_9.png"
    dest = abn_factory.ASSETS / "_scratch" / "_tmp_bg_9.png"
    assert dest.exists() and dest.read_bytes() == b"\x89PNG real"


def test_codex_image_ignores_preexisting_images(monkeypatch, tmp_path):
    """The before/after snapshot must ignore images that existed BEFORE the run — only a newly
    written png counts. If codex produces nothing new, a stale image is not falsely returned."""
    gen_dir = _codex_env(monkeypatch, tmp_path)
    (gen_dir / "ig_old.png").write_bytes(b"stale")  # present before the run

    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: None)  # writes nothing new
    assert _REAL_CODEX_IMAGE("bg", "_tmp_bg_4") is None


# ---------------- OFFLOAD: R2 upload failure must ALERT, not silently no-op ----------------

def _offload_env(monkeypatch, tmp_path, *, configured=True, upload=None):
    """Wire _offload_episode for testing: point ASSETS at tmp, create a real episode mp4, and
    stub services.r2 (is_configured + upload_from_path). Returns the list that BUS.emit appends to.
    `upload` is the upload_from_path body (default no-op success); pass a raiser to simulate failure."""
    import services.r2 as r2
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    # a real on-disk episode mp4 at the schema path so the file-exists guard passes
    mp4 = abn_factory.asset_path("ep_a11111", "episode")
    mp4.parent.mkdir(parents=True, exist_ok=True)
    mp4.write_bytes(b"\x00" * (3 * 1024 * 1024))

    monkeypatch.setattr(r2, "is_configured", lambda: configured)
    monkeypatch.setattr(r2, "upload_from_path", upload or (lambda *a, **k: None))
    monkeypatch.setattr(r2, "public_url", lambda key: f"https://cdn.example/{key}", raising=False)

    events = []
    monkeypatch.setattr(abn_factory.BUS, "emit",
                        lambda *a, **k: events.append((a, k)))
    return events


def test_offload_emits_system_error_on_r2_upload_failure(monkeypatch, tmp_path):
    """The whole point of offload is the durable fix for the recurring disk wall. If R2 is
    configured and the mp4 exists but the upload throws (bad creds / denied bucket / network),
    the old code returned None silently and disk filled with NO signal. It must now fire a
    'system'/'error' alert so the failure is visible."""
    def boom(*a, **k):
        raise PermissionError("AccessDenied: bucket policy forbids PutObject")
    events = _offload_env(monkeypatch, tmp_path, configured=True, upload=boom)

    result = abn_factory._offload_episode("ep_a11111")

    assert result is None  # still graceful to the caller
    errors = [(a, k) for (a, k) in events if a[:2] == ("system", "error")]
    assert errors, "a persistent R2 upload failure must emit a system/error alert, not be swallowed"
    actor_action, kwargs = errors[0]
    assert "ep_a11111" in actor_action[2], "alert should name the episode that failed to offload"
    assert kwargs.get("episode_id") == "ep_a11111"


def test_offload_no_alert_when_r2_not_configured(monkeypatch, tmp_path):
    """When R2 simply isn't configured, offload is a graceful no-op — that is NOT a failure and
    must NOT raise a false alarm. Returns None with no system/error event."""
    events = _offload_env(monkeypatch, tmp_path, configured=False)

    assert abn_factory._offload_episode("ep_a11111") is None
    assert not [e for (a, k) in events for e in [None] if a[:2] == ("system", "error")], \
        "unconfigured R2 is expected, not an error"


def test_offload_happy_path_emits_offload_not_error(monkeypatch, tmp_path):
    """Successful upload emits the 'offload' breadcrumb and returns a URL — and crucially does NOT
    emit a system/error (guards against the alert firing on the success path)."""
    events = _offload_env(monkeypatch, tmp_path, configured=True)

    result = abn_factory._offload_episode("ep_a11111")

    assert result == "https://cdn.example/agenticnews/episodes/ep_a11111_episode.mp4"
    assert any(a[:2] == ("system", "offload") for (a, k) in events), "success must log the offload"
    assert not any(a[:2] == ("system", "error") for (a, k) in events), "no error on the success path"


# ---------------- AUTO-PUBLISH: missing abn_youtube must alert ONCE, not spam ----------------

async def _drive_loop_cycles(monkeypatch, n_cycles, pending_eps):
    """Run run_factory_loop for ~n_cycles with every heavy collaborator stubbed, capturing every
    BUS.emit. produce_one_episode and the GC are no-ops; sleeps are skipped; list_videos always
    returns the given pending episodes (in 'review', kind 'episode'). The loop is cancelled after
    n_cycles real iterations so it can't run forever."""
    events = []
    monkeypatch.setattr(abn_factory.BUS, "emit",
                        lambda *a, **k: events.append((a, k)) or {"id": len(events)})

    # keep STATE hermetic across tests (the once-only warn flag lives here)
    abn_factory.STATE.pop("_ytmod_warned", None)

    async def fake_list_videos(stage=None):
        return list(pending_eps) if stage == "review" else []
    monkeypatch.setattr(abn_factory.db, "list_videos", fake_list_videos)

    async def fake_gc(*a, **k):
        return None
    monkeypatch.setattr(abn_factory, "_gc_segments", fake_gc)

    calls = {"n": 0}

    async def fake_produce(*a, **k):
        calls["n"] += 1
        if calls["n"] >= n_cycles:
            raise asyncio.CancelledError  # break out of the while True after n cycles
        return None
    monkeypatch.setattr(abn_factory, "produce_one_episode", fake_produce)

    async def fake_sleep(*a, **k):
        return None
    monkeypatch.setattr(abn_factory.asyncio, "sleep", fake_sleep)

    # abn_memory.stats is touched for the lore-rotation branch; keep it cheap + deterministic
    import services.abn_memory as _mm
    monkeypatch.setattr(_mm, "stats", lambda: {"episodes": 0})

    await abn_factory.run_factory_loop()
    return events


@pytest.mark.asyncio
async def test_missing_abn_youtube_alerts_once_not_per_cycle(monkeypatch):
    """The auto-publish loop tries `import services.abn_youtube`, which does not exist. With an
    episode waiting in review the operator MUST be told the feature is broken — but exactly once,
    as a distinct 'unavailable' alert, not a generic per-cycle 'error' that buries the signal in
    the 600-entry ring buffer.

    services/abn_youtube.py now ships (the publish endpoints need it), so the loop's
    ModuleNotFoundError branch can no longer fire on a genuinely-absent module. Force
    the absence at import time instead — `sys.modules[name] = None` makes
    `import services.abn_youtube` raise ModuleNotFoundError — so this still pins the
    once-only 'unavailable' alert behavior of the missing-module code path."""
    import sys
    monkeypatch.setitem(sys.modules, "services.abn_youtube", None)

    pending = [{"id": "ep_pub01", "kind": "episode", "stage": "review"}]
    events = await _drive_loop_cycles(monkeypatch, n_cycles=3, pending_eps=pending)

    unavailable = [(a, k) for (a, k) in events if a[:2] == ("publisher", "unavailable")]
    assert len(unavailable) == 1, "must alert exactly once across multiple cycles, not every cycle"
    assert "abn_youtube" in unavailable[0][0][2], "alert must name the missing module"

    publish_errors = [(a, k) for (a, k) in events if a[:2] == ("publisher", "error")]
    assert not publish_errors, "missing module is a config state, not a swallowed runtime error"


@pytest.mark.asyncio
async def test_ytmod_warned_guard_survives_many_cycles(monkeypatch):
    """The _ytmod_warned guard is the ONLY thing standing between the missing-module branch and
    per-cycle log spam. Pin it across many cycles (not just the 3 the happy-path test runs): the
    'unavailable' alert must fire exactly once no matter how long the loop runs, and STATE must
    carry the flag forward. A regression that drops/clears the flag would flood the bus here."""
    import sys
    monkeypatch.setitem(sys.modules, "services.abn_youtube", None)

    pending = [{"id": "ep_pubXX", "kind": "episode", "stage": "review"}]
    events = await _drive_loop_cycles(monkeypatch, n_cycles=25, pending_eps=pending)

    unavailable = [(a, k) for (a, k) in events if a[:2] == ("publisher", "unavailable")]
    assert len(unavailable) == 1, "guard must suppress all but the first alert across 25 cycles"
    assert abn_factory.STATE.get("_ytmod_warned") is True, "the suppression flag must persist in STATE"


@pytest.mark.asyncio
async def test_is_configured_raising_is_caught_not_crashing_loop(monkeypatch):
    """services/abn_youtube.py exists but ytmod.is_configured() raises (corrupt creds, OAuth lib
    blowup, etc.). The loop must NOT die: the exception is caught at the inner try/except and
    surfaced as a 'publisher'/'error' event, while episode production keeps running every cycle.
    This pins the untested exception path at the is_configured() call."""
    import types
    fake_yt = types.ModuleType("services.abn_youtube")

    def boom():
        raise RuntimeError("creds corrupt")
    fake_yt.is_configured = boom

    import sys
    monkeypatch.setitem(sys.modules, "services.abn_youtube", fake_yt)
    # `import services.abn_youtube as ytmod` binds from the PARENT package attribute when the
    # submodule is already imported (which earlier tests in the suite do), not from sys.modules —
    # so patch the attribute too, otherwise the loop sees the real module and is_configured() never
    # raises. (In isolation services.abn_youtube isn't pre-imported, so this is a harmless no-op.)
    import services as _services_pkg
    monkeypatch.setattr(_services_pkg, "abn_youtube", fake_yt, raising=False)

    pending = [{"id": "ep_pubER", "kind": "episode", "stage": "review"}]
    events = await _drive_loop_cycles(monkeypatch, n_cycles=2, pending_eps=pending)

    # is_configured() blowing up is a runtime error → caught and surfaced, not a crash
    errors = [(a, k) for (a, k) in events if a[:2] == ("publisher", "error")]
    assert errors, "is_configured() raising must be caught and emitted as a publisher error"
    assert "creds corrupt" in errors[0][0][2], "the original exception must be surfaced in the alert"
    # the module IS present, so the missing-module path must NOT fire
    assert not [(a, k) for (a, k) in events if a[:2] == ("publisher", "unavailable")], \
        "module is present — the missing-module 'unavailable' branch must not fire"
    # and the loop kept producing episodes despite the publisher fault
    assert any(a[:2] == ("factory", "boot") for (a, k) in events)


@pytest.mark.asyncio
async def test_no_publisher_noise_when_no_pending_episodes(monkeypatch):
    """When nothing is waiting in review, the loop must not touch the publisher at all — no import
    attempt, hence no 'unavailable' alert and no 'error'. (Guards the regression where the import
    fired every cycle even with an empty board.)"""
    events = await _drive_loop_cycles(monkeypatch, n_cycles=3, pending_eps=[])

    assert not [(a, k) for (a, k) in events if a[0] == "publisher"], \
        "an empty review board must produce zero publisher events"


# ---------------- OpenShot-as-compiler: factory episode assembly ----------------

@pytest.mark.asyncio
async def test_assemble_episode_openshot_bridges_timeline_to_compiler(monkeypatch, tmp_path):
    """The factory's final assembly must flow through the sanctioned compiler
    (editor_render.choose_renderer) — not the raw-ffmpeg bypass. This pins the bridge:
    a factory _build_timeline shape -> project_from_abn_timeline -> renderer.render(),
    output routed to the gateway {ep}/renders/episode.mp4, returning (url, duration).
    choose_renderer is mocked so CI doesn't need libopenshot; the WIRING is what's pinned."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    ep_id = "ep_c0ffee01"
    # a minimal but real factory timeline (same shape _build_timeline emits)
    timeline = {
        "fps": 30, "width": 1920, "height": 1080, "episodeId": ep_id,
        "title": "T", "totalSec": 4.0, "musicBed": None,
        "segments": [{
            "segmentId": "s0", "title": "Seg", "sourceUrl": "https://x",
            "shots": [{"id": "a", "src": "/agenticnews-assets/x.png", "startSec": 0.0,
                       "durationSec": 4.0, "type": "artifact"}],
            "wordTimestamps": [], "keywordPops": [], "lowerThirds": [],
            "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}},
            "durationSec": 4.0,
        }],
    }

    captured = {}

    class FakeRenderer:
        backend = "openshot"
        def render(self, project, *, output_path=None, **kw):
            captured["projectId"] = project.get("projectId")
            captured["has_clips"] = bool(project.get("clips"))
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"mp4")
            return {"backend": "openshot", "video": str(output_path), "duration": 4.0,
                    "missingAssets": []}

    import services.editor_render as er
    monkeypatch.setattr(er, "choose_renderer", lambda out, asset_root=None: FakeRenderer())

    url, dur = await abn_factory._assemble_episode_openshot(ep_id, timeline)

    # bridge actually converted the timeline into an editor-bay project with clips
    assert captured["projectId"] == ep_id
    assert captured["has_clips"] is True
    # output landed at the gateway episode path and returns a usable (url, duration)
    assert url.startswith("/agenticnews-assets/") and url.endswith("episode.mp4")
    assert dur == 4.0
    assert (tmp_path / ep_id / "renders" / "episode.mp4").exists()


@pytest.mark.asyncio
async def test_assemble_episode_openshot_raises_so_factory_falls_back(monkeypatch, tmp_path):
    """If the compiler produces no file, the OpenShot path must RAISE — so produce_one_episode
    falls through to Remotion / ffmpeg instead of shipping a phantom episode. Additive, no regression."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    timeline = {"fps": 30, "width": 1920, "height": 1080, "episodeId": "ep_dead01",
                "title": "T", "totalSec": 1.0, "segments": [{
                    "segmentId": "s0", "durationSec": 1.0, "shots": [],
                    "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 1.0}}}]}

    class NoFileRenderer:
        backend = "openshot"
        def render(self, project, *, output_path=None, **kw):
            return {"backend": "openshot", "video": str(output_path), "duration": 1.0}  # never writes the file

    import services.editor_render as er
    monkeypatch.setattr(er, "choose_renderer", lambda out, asset_root=None: NoFileRenderer())

    with pytest.raises(RuntimeError):
        await abn_factory._assemble_episode_openshot("ep_dead01", timeline)


# ---------------- revisualize_episode: re-skin keeping the original audio mix ----------------
#
# `revisualize_episode` rebuilds an already-rendered episode with the new visual grammar while
# muxing back the ENTIRE original audio mix (VO + bed + ducking). It has five error paths and a
# load-bearing piece of temporal arithmetic (front-trim the audio by d_orig - d_new = the dropped
# logo sting) that had zero coverage. These pin both the guards and the arithmetic WITHOUT touching
# ffmpeg, Remotion, or an LLM — every shell call and duration probe is monkeypatched.


def _stub_revis_timeline(monkeypatch):
    """_build_timeline is pure-ish but pulls in card/shot machinery; stub it to a minimal timeline
    so the test exercises revisualize_episode's own logic (audio drop, trim, mux), not the builder."""
    def fake_build(ep_id, ep_idx, segments, animated_bg=None):
        return {"episodeId": ep_id, "title": "built", "logo": {"sting": 1},
                "musicBed": {"src": "bed"}, "sfx": [{"x": 1}],
                "segments": [{"segmentId": "s0", "audio": {"vo": {"src": "v"}}}]}
    monkeypatch.setattr(abn_factory, "_build_timeline", fake_build)


def _write_revis_inputs(ep_id):
    """Lay down the two preconditions revisualize_episode checks for: a timeline.json and the
    original episode.mp4. Returns (timeline_path, original_mp4_path)."""
    tlf = abn_factory.asset_path(ep_id, "timeline")
    orig = abn_factory.asset_path(ep_id, "episode")
    tl = {"title": "Orig Title", "segments": [
        {"segmentId": "s0", "title": "Seg one", "sourceUrl": "https://x",
         "durationSec": 4.0, "wordTimestamps": [{"w": "hello"}, {"w": "world"}],
         "audio": {"vo": {"src": "/agenticnews-assets/ep_a111111_s0.wav"}}},
    ]}
    tlf.write_text(json.dumps(tl))
    orig.write_bytes(b"original-episode-with-audio")
    return tlf, orig


def test_revisualize_raises_when_timeline_or_mp4_missing(monkeypatch, tmp_path):
    """Guard #1: with no timeline.json and no episode.mp4 on disk, revisualize must refuse
    up front rather than shelling out to ffmpeg against nonexistent files."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def boom_sh(cmd, timeout=600):
        raise AssertionError("must not shell out when inputs are missing")
    monkeypatch.setattr(abn_factory, "_sh", boom_sh)

    with pytest.raises(RuntimeError, match="missing timeline or mp4"):
        asyncio.run(abn_factory.revisualize_episode("ep_a111111"))


def test_revisualize_raises_when_audio_extract_fails(monkeypatch, tmp_path):
    """Guard #2: if extracting the original audio (-vn -c:a copy) fails, revisualize must raise
    'audio extract failed' BEFORE rendering anything new — losing the original mix is unrecoverable."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _write_revis_inputs("ep_a111111")

    async def fake_dur(p):
        return 60.0
    async def fail_audio_sh(cmd, timeout=600):
        # the first shell call is the audio extract; fail it (and write nothing)
        return 1, "ffmpeg: cannot copy audio"
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    monkeypatch.setattr(abn_factory, "_sh", fail_audio_sh)

    with pytest.raises(RuntimeError, match="audio extract failed"):
        asyncio.run(abn_factory.revisualize_episode("ep_a111111"))


def test_revisualize_raises_when_remotion_render_fails(monkeypatch, tmp_path):
    """Guard #3: a non-zero remotion exit (or no output file) must surface as a RuntimeError
    naming the remotion exit code — not silently mux a stale/absent intermediate."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _write_revis_inputs("ep_a111111")
    _stub_revis_timeline(monkeypatch)

    async def fake_dur(p):
        return 60.0
    async def fake_bg(ep_id, n=4):
        return []
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    monkeypatch.setattr(abn_factory, "_animated_bg", fake_bg)

    async def routed_sh(cmd, timeout=600):
        if "-vn" in cmd:  # audio extract: succeed + create the m4a
            aud = abn_factory.asset_path("ep_a111111", "assembled", "origaudio", ext="m4a")
            aud.write_bytes(b"audio")
            return 0, ""
        if "remotion render" in cmd:  # the silent video render: fail
            return 1, "remotion blew up"
        raise AssertionError(f"unexpected shell call after remotion failure: {cmd[:60]}")
    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    with pytest.raises(RuntimeError, match="remotion exit 1"):
        asyncio.run(abn_factory.revisualize_episode("ep_a111111"))


def test_revisualize_raises_when_mux_fails(monkeypatch, tmp_path):
    """Guard #4: if the final mux (re-attaching the trimmed original audio) fails, revisualize
    must raise 'mux failed' AND clean up the intermediate revis mp4."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _write_revis_inputs("ep_a111111")
    _stub_revis_timeline(monkeypatch)

    async def fake_dur(p):
        return 60.0
    async def fake_bg(ep_id, n=4):
        return []
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    monkeypatch.setattr(abn_factory, "_animated_bg", fake_bg)

    vid_path = abn_factory.asset_path("ep_a111111", "scratch", "revis", ext="mp4")

    async def routed_sh(cmd, timeout=600):
        if "-vn" in cmd:
            abn_factory.asset_path("ep_a111111", "assembled", "origaudio", ext="m4a").write_bytes(b"a")
            return 0, ""
        if "remotion render" in cmd:
            vid_path.write_bytes(b"silent-video")
            return 0, ""
        if "-map 0:v" in cmd:  # the mux: fail
            return 1, "mux exploded"
        raise AssertionError(f"unexpected shell call: {cmd[:60]}")
    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    with pytest.raises(RuntimeError, match="mux failed"):
        asyncio.run(abn_factory.revisualize_episode("ep_a111111"))
    # intermediate is unlinked even on mux failure (it's removed before the failure check)
    assert not vid_path.exists()


def test_revisualize_happy_path_trims_audio_by_duration_delta_and_drops_sting(monkeypatch, tmp_path):
    """The load-bearing arithmetic + audio handling, end to end:

      * the new silent render's timeline DROPS the logo sting, music bed, and sfx, and zeroes every
        segment's audio (the original mix carries all sound) — the locked-audio contract;
      * the original audio is front-trimmed by EXACTLY d_orig - d_new (the dropped sting length),
        so VO/caption sync is arithmetic, not guesswork;
      * the pristine original timeline is backed up once (idempotent re-runs);
      * it returns (asset_url, final_duration) and marks the video back to 'review'."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    ep_id = "ep_a111111"
    _write_revis_inputs(ep_id)

    captured = {}

    def fake_build(ep_id, ep_idx, segments, animated_bg=None):
        captured["segments_in"] = segments
        return {"episodeId": ep_id, "title": "built", "logo": {"sting": 1},
                "musicBed": {"src": "bed"}, "sfx": [{"x": 1}],
                "segments": [{"segmentId": "s0", "audio": {"vo": {"src": "v"}}},
                             {"segmentId": "s1", "audio": {"vo": {"src": "v2"}}}]}
    monkeypatch.setattr(abn_factory, "_build_timeline", fake_build)

    async def fake_bg(ep_id, n=4):
        return []
    monkeypatch.setattr(abn_factory, "_animated_bg", fake_bg)

    # d_orig = 62.5 (includes a 2.5s logo sting), d_new = 60.0 (silent render, sting dropped),
    # d_final = the muxed output. _dur is called on orig, then new vid, then final out — in order.
    durs = iter([62.5, 60.0, 60.0])
    async def fake_dur(p):
        return next(durs)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    vid_path = abn_factory.asset_path(ep_id, "scratch", "revis", ext="mp4")
    out_path = abn_factory.asset_path(ep_id, "episode")

    async def routed_sh(cmd, timeout=600):
        if "-vn" in cmd:
            abn_factory.asset_path(ep_id, "assembled", "origaudio", ext="m4a").write_bytes(b"a")
            return 0, ""
        if "remotion render" in cmd:
            vid_path.write_bytes(b"silent-video")
            return 0, ""
        if "-map 0:v" in cmd:
            captured["mux_cmd"] = cmd
            out_path.write_bytes(b"final-episode")
            return 0, ""
        raise AssertionError(f"unexpected shell call: {cmd[:60]}")
    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    updates = []
    async def fake_update(vid, patch):
        updates.append((vid, patch))
        return {}
    monkeypatch.setattr(abn_factory.db, "update_video", fake_update)

    # silence the timeline mutation that revisualize applies to fake_build's output; capture it
    real_atomic = abn_factory.atomic_save
    def spy_atomic(path, data):
        captured["silent_tl"] = data
        real_atomic(path, data)
    monkeypatch.setattr(abn_factory, "atomic_save", spy_atomic)

    url, d_final = asyncio.run(abn_factory.revisualize_episode(ep_id))

    # --- locked-audio contract: silent render carries NO sound of its own ---
    stl = captured["silent_tl"]
    assert stl["logo"] is None and stl["musicBed"] is None and stl["sfx"] is None
    assert all(s["audio"] == {} for s in stl["segments"]), "every segment must be muted in the silent render"
    # original title is preserved onto the rebuilt timeline
    assert stl["title"] == "Orig Title"

    # --- the trim arithmetic: front-trim = d_orig - d_new = 62.5 - 60.0 = 2.5 (the dropped sting) ---
    assert "-ss 2.5 " in captured["mux_cmd"], captured["mux_cmd"]
    # the original (extracted) audio is the trimmed input and is stream-COPIED, not re-encoded
    assert "-c:a copy" in captured["mux_cmd"]

    # --- pristine original timeline backed up once for idempotent re-runs ---
    bak = abn_factory.asset_path(ep_id, "assembled", "timeline.orig", ext="json")
    assert bak.exists() and json.loads(bak.read_text())["title"] == "Orig Title"

    # --- return contract + stage flip back to review ---
    assert url.endswith("episode.mp4")
    assert d_final == 60.0
    assert updates and updates[0][1]["stage"] == "review"


def test_revisualize_trim_never_negative_when_new_render_is_longer(monkeypatch, tmp_path):
    """Defensive arithmetic: if the new (silent) render somehow measures LONGER than the original,
    the front-trim must clamp to 0.0 — never a negative -ss that ffmpeg would reject or misread."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    ep_id = "ep_a111111"
    _write_revis_inputs(ep_id)
    _stub_revis_timeline(monkeypatch)

    async def fake_bg(ep_id, n=4):
        return []
    monkeypatch.setattr(abn_factory, "_animated_bg", fake_bg)

    durs = iter([58.0, 60.0, 60.0])  # d_orig < d_new -> raw delta is negative
    async def fake_dur(p):
        return next(durs)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    vid_path = abn_factory.asset_path(ep_id, "scratch", "revis", ext="mp4")
    out_path = abn_factory.asset_path(ep_id, "episode")
    captured = {}

    async def routed_sh(cmd, timeout=600):
        if "-vn" in cmd:
            abn_factory.asset_path(ep_id, "assembled", "origaudio", ext="m4a").write_bytes(b"a")
            return 0, ""
        if "remotion render" in cmd:
            vid_path.write_bytes(b"v")
            return 0, ""
        if "-map 0:v" in cmd:
            captured["mux_cmd"] = cmd
            out_path.write_bytes(b"f")
            return 0, ""
        raise AssertionError(cmd[:60])
    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    async def fake_update(vid, patch):
        return {}
    monkeypatch.setattr(abn_factory.db, "update_video", fake_update)

    asyncio.run(abn_factory.revisualize_episode(ep_id))
    assert "-ss 0.0 " in captured["mux_cmd"], captured["mux_cmd"]


# ---------------- purge_disk: FAIL-SAFE protection guard on the low-disk render trim ----------------
# purge_disk (services/abn_factory.py) tombstones spent scratch unconditionally, but only trims the
# oldest EPISODE RENDERS when BOTH (a) the Editor-Bay protection scan was COMPLETE and (b) free disk
# is below low_disk_gb. The guard logic itself (`protection_complete` + the threshold) was untested;
# a regression there could premature-delete live renders under load, breaking the recovery guarantee.

def _stub_purge_disk(monkeypatch, *, protection_complete, free_gb, renders):
    """Wire purge_disk's collaborators so only the guard logic under test runs. Returns a list that
    records every render passed to tombstone_render() (i.e. every render actually trimmed)."""
    import shutil
    # No scratch to reap → isolate the render-trim branch entirely.
    monkeypatch.setattr(abn_factory, "reapable_scratch", lambda: [])
    monkeypatch.setattr(abn_factory, "tombstone", lambda f: 0)
    monkeypatch.setattr(abn_factory, "_editor_timeline_asset_paths_checked",
                        lambda: (set(), protection_complete))
    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset",
                        lambda path, protected: False)
    monkeypatch.setattr(abn_factory, "_old_episode_renders", lambda: list(renders))

    # The consume-site hardening guard (purge_disk: `if not old.is_file() or
    # old.is_symlink(): continue`) only hands tombstone_render() a real regular
    # file. These stub renders are synthetic Paths that don't exist on disk, so
    # make them present as plain files for the guard — without this they'd all be
    # skipped and the trim logic under test would never run. We do NOT weaken the
    # guard; we satisfy it so the protection/threshold branch is what's exercised.
    _render_set = {str(r) for r in renders}
    _real_is_file = Path.is_file
    _real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_file",
                        lambda self: True if str(self) in _render_set else _real_is_file(self))
    monkeypatch.setattr(Path, "is_symlink",
                        lambda self: False if str(self) in _render_set else _real_is_symlink(self))

    trimmed = []
    def fake_tombstone_render(old):
        trimmed.append(old)
        return 1024 * 1024  # 1 MB freed per render
    monkeypatch.setattr(abn_factory, "tombstone_render", fake_tombstone_render)

    class _Usage:
        free = int(free_gb * 1e9)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: _Usage())
    return trimmed


def test_purge_disk_skips_render_trim_when_protection_scan_incomplete(monkeypatch):
    """FAIL SAFE: even with disk critically low, an INCOMPLETE protection scan must skip the
    destructive render trim — a live render we couldn't prove safe is never tombstoned."""
    renders = [Path(f"/ep{i}/renders/episode.mp4") for i in range(10)]
    trimmed = _stub_purge_disk(monkeypatch, protection_complete=False, free_gb=0.1, renders=renders)

    abn_factory.purge_disk(keep_episodes=4, low_disk_gb=2.0)

    assert trimmed == [], "incomplete protection scan must not trim ANY render, even under low disk"


def test_purge_disk_trims_old_renders_when_protection_complete_and_low_disk(monkeypatch):
    """When the scan is COMPLETE and disk is below the threshold, trim only renders past keep_episodes
    (the newest keep_episodes are retained)."""
    renders = [Path(f"/ep{i}/renders/episode.mp4") for i in range(10)]
    trimmed = _stub_purge_disk(monkeypatch, protection_complete=True, free_gb=0.5, renders=renders)

    abn_factory.purge_disk(keep_episodes=4, low_disk_gb=2.0)

    assert trimmed == renders[4:], "only renders past the keep_episodes window should be trimmed"


def test_purge_disk_does_not_trim_when_disk_above_threshold(monkeypatch):
    """Disk above low_disk_gb → no render trim even with a complete scan (threshold guard)."""
    renders = [Path(f"/ep{i}/renders/episode.mp4") for i in range(10)]
    trimmed = _stub_purge_disk(monkeypatch, protection_complete=True, free_gb=50.0, renders=renders)

    abn_factory.purge_disk(keep_episodes=4, low_disk_gb=2.0)

    assert trimmed == [], "ample free disk must not trigger any destructive render trim"
# ---------------- _compile_episode: the OpenShot → Remotion → ffmpeg fallback cascade ----------------
#
# produce_one_episode's compiler order (CLAUDE.md) tries the sanctioned OpenShot compiler first,
# then Remotion (a live layer source, retried once), then the ffmpeg slideshow as last resort.
# That cascade was inline in the 500-line produce_one_episode and effectively untestable; it now
# lives in _compile_episode(ep_id, timeline, segments) so each fall-through branch — and the
# diagnostic BUS events that make the fall-through observable — can be pinned directly. The three
# tests below cover: (1) OpenShot raises but Remotion succeeds, (2) OpenShot + Remotion fail then
# ffmpeg succeeds, (3) all three fail (raises). A regression in the ORDER (e.g. Remotion before
# OpenShot, or skipping the ffmpeg backstop) flips at least one of these.

def _bus_actions_since(seen):
    return [e["action"] for e in abn_factory.BUS.replay() if e["id"] not in seen]


@pytest.mark.asyncio
async def test_compile_episode_falls_back_to_remotion_when_openshot_raises(monkeypatch):
    """OpenShot raises → cascade falls through to Remotion, which succeeds. The OpenShot result is
    NOT used, Remotion's (url, duration) is returned, and the openshot.fallback + remotion.done
    diagnostic events are both emitted (Remotion is never even reached if OpenShot order breaks)."""
    async def openshot_boom(ep_id, timeline):
        raise RuntimeError("libopenshot bindings missing")

    async def remotion_ok(ep_id, timeline):
        return "/agenticnews-assets/remotion.mp4", 612.0

    async def ffmpeg_boom(ep_id, segments):
        raise AssertionError("ffmpeg fallback must NOT run once Remotion succeeds")

    monkeypatch.setattr(abn_factory, "_assemble_episode_openshot", openshot_boom)
    monkeypatch.setattr(abn_factory, "_render_remotion", remotion_ok)
    monkeypatch.setattr(abn_factory, "_assemble_episode", ffmpeg_boom)

    seen = {e["id"] for e in abn_factory.BUS.replay()}
    url, dur = await abn_factory._compile_episode("ep_fb0001", {"musicBed": None}, [])

    assert (url, dur) == ("/agenticnews-assets/remotion.mp4", 612.0)
    actions = _bus_actions_since(seen)
    assert "openshot.fallback" in actions, "OpenShot failure must announce the fall-through to Remotion"
    assert "remotion.done" in actions, "a successful Remotion render must emit remotion.done"
    assert "remotion.fallback" not in actions, "ffmpeg backstop must not be announced when Remotion wins"


@pytest.mark.asyncio
async def test_compile_episode_falls_back_to_ffmpeg_when_openshot_and_remotion_fail(monkeypatch):
    """OpenShot raises AND Remotion fails both attempts → cascade lands on the ffmpeg slideshow,
    which succeeds. Remotion is retried exactly twice (two 'error' events), the remotion.fallback
    backstop event fires, and ffmpeg's (url, duration) is returned."""
    calls = {"remotion": 0}

    async def openshot_boom(ep_id, timeline):
        raise RuntimeError("openshot down")

    async def remotion_boom(ep_id, timeline):
        calls["remotion"] += 1
        raise RuntimeError("remotion asset fetch timeout")

    async def ffmpeg_ok(ep_id, segments):
        return "/agenticnews-assets/ffmpeg.mp4", 605.0

    monkeypatch.setattr(abn_factory, "_assemble_episode_openshot", openshot_boom)
    monkeypatch.setattr(abn_factory, "_render_remotion", remotion_boom)
    monkeypatch.setattr(abn_factory, "_assemble_episode", ffmpeg_ok)

    seen = {e["id"] for e in abn_factory.BUS.replay()}
    url, dur = await abn_factory._compile_episode("ep_fb0002", {"musicBed": None}, [{"script": "x"}])

    assert (url, dur) == ("/agenticnews-assets/ffmpeg.mp4", 605.0)
    assert calls["remotion"] == 2, "Remotion must be retried exactly once (two attempts) before ffmpeg"
    actions = _bus_actions_since(seen)
    assert "openshot.fallback" in actions
    assert actions.count("error") >= 2, "each failed Remotion attempt must emit a diagnostic error event"
    assert "remotion.fallback" in actions, "the ffmpeg last-resort backstop must announce itself"


@pytest.mark.asyncio
async def test_compile_episode_raises_when_all_three_compilers_fail(monkeypatch):
    """OpenShot, Remotion (both attempts), and ffmpeg all fail → _compile_episode propagates the
    ffmpeg error (produce_one_episode maps that to idle + abort). The full diagnostic trail —
    openshot.fallback, two remotion errors, remotion.fallback — is emitted before the raise."""
    async def openshot_boom(ep_id, timeline):
        raise RuntimeError("openshot down")

    async def remotion_boom(ep_id, timeline):
        raise RuntimeError("remotion down")

    async def ffmpeg_boom(ep_id, segments):
        raise RuntimeError("no segment clips")

    monkeypatch.setattr(abn_factory, "_assemble_episode_openshot", openshot_boom)
    monkeypatch.setattr(abn_factory, "_render_remotion", remotion_boom)
    monkeypatch.setattr(abn_factory, "_assemble_episode", ffmpeg_boom)

    seen = {e["id"] for e in abn_factory.BUS.replay()}
    with pytest.raises(RuntimeError, match="no segment clips"):
        await abn_factory._compile_episode("ep_fb0003", {"musicBed": None}, [{"script": "x"}])

    actions = _bus_actions_since(seen)
    assert "openshot.fallback" in actions
    assert "remotion.fallback" in actions, "the cascade must reach the ffmpeg backstop before giving up"


# ---------------- _cross_scratch_path: traversal guard + GC-reapability ----------------

@pytest.fixture
def scratch_store(tmp_path, monkeypatch):
    """Point BOTH the factory's bound ASSETS and the gateway's ASSETS_DIR at a throwaway store.
    _cross_scratch_path builds under abn_factory.ASSETS but checks membership against
    abn_assets.scratch_dirs() (which reads abn_assets.ASSETS_DIR) — both must move together
    or the guard would compare against the real on-disk store."""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    return assets


def test_cross_scratch_path_accepts_flat_name_and_lands_in_reapable_root(scratch_store):
    """Happy path: a flat basename routes to ASSETS/_scratch/<name>, the parent dir gets created,
    and it lives in a GC-reapable root (scratch_dirs() membership)."""
    dest = abn_factory._cross_scratch_path("_tmp_bg_0.png")
    assert dest == scratch_store / "_scratch" / "_tmp_bg_0.png"
    assert dest.parent.is_dir(), "the chokepoint must create the _scratch parent"
    reapable = {d.resolve() for d in abn_assets.scratch_dirs()}
    assert dest.parent.resolve() in reapable, "write must land in a GC-reapable root"


@pytest.mark.parametrize("bad", [
    "../escape.png",          # parent traversal
    "sub/dir/file.png",       # forward-slash subpath
    "sub\\dir.png",           # backslash subpath
    ".hidden",                # leading dot
    "..",                     # bare traversal
    "/abs/path.png",          # absolute-ish leading slash
    "",                       # empty after strip
    "   ",                    # whitespace-only -> empty after strip
    "bad name!.png",          # disallowed punctuation
])
def test_cross_scratch_path_rejects_bad_names_before_any_write(scratch_store, bad):
    """Off-schema names (traversal, slashes, leading dot, junk) RAISE before bytes are written,
    so the GC can never be handed an unreapable stray. The _scratch dir must not be created as a
    side effect of a rejected call."""
    with pytest.raises(ValueError, match="bad cross-scratch filename"):
        abn_factory._cross_scratch_path(bad)


def test_cross_scratch_path_rejects_non_reapable_root(scratch_store, monkeypatch):
    """Even a name that passes the regex must RAISE if its parent isn't a GC-reapable root.
    Simulate a store whose _scratch is NOT in scratch_dirs() (e.g. the dir doesn't exist yet so
    scratch_dirs() omits it) — the membership check is the second, independent guard."""
    # scratch_dirs() only includes _scratch when it's an existing dir; force it empty so the
    # membership assertion is the thing that fires, not the regex. _cross_scratch_path imported
    # scratch_dirs into the factory namespace, so patch the name the function actually resolves.
    monkeypatch.setattr(abn_factory, "scratch_dirs", lambda: [])
    with pytest.raises(ValueError, match="refusing non-reapable cross-scratch write"):
        abn_factory._cross_scratch_path("probe.png")


# ---- adhoc_scratch_path: name validation + extension format (services/abn_assets.py L279-313) ----


@pytest.mark.parametrize(
    "bad_name",
    [
        "..",
        "../evil",
        ".hidden",          # leading dot rejected by _SLUG_RE
        "a/b",              # slash
        "a\\b",             # backslash
        "evil\x00",         # null byte
        "",                 # empty
        "  ",               # whitespace-only -> empty after strip
    ],
)
def test_adhoc_scratch_path_rejects_bad_names(monkeypatch, tmp_path, bad_name):
    """A GC-unsafe / traversing ad-hoc name must RAISE before any path is built —
    it must never land a write at the ASSETS_DIR root or escape the store."""
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    with pytest.raises(abn_assets.AssetPathError):
        abn_assets.adhoc_scratch_path(bad_name, "wav")


@pytest.mark.parametrize("bad_ext", ["w av", "wav/x", "wav.", "../x", "wa-v", "wav!"])
def test_adhoc_scratch_path_rejects_bad_extensions(monkeypatch, tmp_path, bad_ext):
    """The extension must be a plain alphanumeric token — special chars RAISE."""
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    with pytest.raises(abn_assets.AssetPathError):
        abn_assets.adhoc_scratch_path("vo", bad_ext)


def test_adhoc_scratch_path_lands_valid_name_under_scratch(monkeypatch, tmp_path):
    """A valid name+ext lands under the reapable cross-episode _scratch/ dir,
    NOT the flat ASSETS_DIR root (the glob-GC hazard the gateway exists to kill)."""
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    p = abn_assets.adhoc_scratch_path("vo", "wav")
    assert p == tmp_path / "_scratch" / "vo.wav"
    assert p.parent.is_dir()
    # leading dot on ext is normalized; empty ext yields a bare name
    assert abn_assets.adhoc_scratch_path("card", ".png") == tmp_path / "_scratch" / "card.png"
    assert abn_assets.adhoc_scratch_path("clip", "") == tmp_path / "_scratch" / "clip"
