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
