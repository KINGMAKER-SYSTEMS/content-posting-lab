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
import shlex
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


@pytest.mark.parametrize("evil", [
    "english_x\n/path/to/evil.safetensors", # newline smuggles a clone path past a `$`-anchored re
    "english_2026-04\nmodel.safetensors",   # valid-looking code, then newline + clone file
    "english_x\n/etc/passwd",               # newline + traversal target
])
def test_pocket_tts_command_rejects_trailing_newline_bypass_end_to_end(monkeypatch, tmp_path, evil):
    """END-TO-END locked-voice gate for the embedded-newline class: the regex test above pins the
    pattern, but this proves the WHOLE command path rejects it. `.strip()` only collapses a *bare*
    trailing newline; an embedded `\\n<path>` survives strip and would slip past a `$`-anchored
    `re.match`, so `_pocket_tts_command` must fall back to the built-in default and the poisoned
    value (plus any embedded clone file / path) must never reach the `--language` flag handed to
    the pocket-tts binary."""
    monkeypatch.setenv("ABN_POCKET_LANGUAGE", evil)
    cmd = abn_factory._pocket_tts_command("hi", tmp_path / "v.wav")

    assert cmd[cmd.index("--language") + 1] == "english_2026-04"
    joined = " ".join(cmd).lower()
    for banned in ("safetensors", "model.", "/path", "/etc", "passwd", "\n"):
        assert banned not in joined, f"trailing-newline bypass leaked into VO command: {banned!r}"


def test_pocket_language_fallback_logs_warning_not_security_anomaly(monkeypatch, caplog):
    """When a poisoned ABN_POCKET_LANGUAGE is rejected the fallback is QUIET: it emits a single
    WARNING (operator-visible, for debugging a misconfigured env) and never escalates to
    ERROR/CRITICAL or anything a SIEM would page on. A rejected env is an ops mistake, not a
    breach signal — over-logging it as an anomaly would drown the real signal."""
    monkeypatch.setenv("ABN_POCKET_LANGUAGE", "/path/to/evil.safetensors")
    with caplog.at_level("WARNING", logger="services.abn_factory"):
        assert abn_factory._pocket_language() == "english_2026-04"

    gate_records = [r for r in caplog.records if "ABN_POCKET_LANGUAGE" in r.getMessage()]
    assert len(gate_records) == 1, "fallback must log exactly once, not spam per call"
    rec = gate_records[0]
    assert rec.levelname == "WARNING", f"fallback must not escalate past WARNING, got {rec.levelname}"
    # the rejected value is named so ops can fix it, but it's a config warning, not a paged anomaly
    assert "evil.safetensors" in rec.getMessage()


def test_pocket_language_valid_env_logs_nothing(monkeypatch, caplog):
    """A VALID language code passes the gate silently — no warning noise on the happy path."""
    monkeypatch.setenv("ABN_POCKET_LANGUAGE", "english_test")
    with caplog.at_level("WARNING", logger="services.abn_factory"):
        assert abn_factory._pocket_language() == "english_test"
    assert not [r for r in caplog.records if "ABN_POCKET_LANGUAGE" in r.getMessage()]


def test_pocket_language_resolves_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("ABN_POCKET_LANGUAGE", raising=False)
    assert abn_factory._pocket_language() == "english_2026-04"


@pytest.mark.parametrize("poisoned", [
    "/path/to/evil.safetensors",            # clone weights file
    "model.safetensors",                     # bare clone filename
    "english_2026-04\nmodel.safetensors",   # embedded-newline path smuggle
    "replicate:chatterbox",                 # cloud-engine handle
    "../etc/passwd",                         # traversal
])
def test_pocket_tts_command_re_gates_even_if_language_resolver_bypassed(monkeypatch, tmp_path, poisoned):
    """LOCKED-VOICE HARD GATE, defense-in-depth: the env validation lives in _pocket_language(), but
    the audit concern is a CALL SITE that bypasses it (a future refactor sourcing the code elsewhere,
    or a new caller). _pocket_tts_command is the single chokepoint that emits `--language`, so even if
    the resolver is fully bypassed and hands back a poisoned value, the command MUST still fall back to
    the built-in default and never feed pocket-tts a clone file / cloud handle / path."""
    # simulate a bypass: the resolver itself returns an unvalidated, poisoned value
    monkeypatch.setattr(abn_factory, "_pocket_language", lambda: poisoned)
    cmd = abn_factory._pocket_tts_command("hi", tmp_path / "v.wav")

    assert poisoned not in cmd
    assert cmd[cmd.index("--language") + 1] == "english_2026-04"
    joined = " ".join(cmd).lower()
    for banned in ("safetensors", "replicate", "chatterbox", "passwd", "/path", "/etc", "\n"):
        assert banned not in joined, f"bypassed resolver leaked poisoned language into VO command: {banned!r}"


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


def test_voice_raises_when_tts_reports_ok_but_writes_no_file(monkeypatch, tmp_path):
    """Second half of the `code != 0 or not out.exists()` gate at services/abn_factory.py ~864:
    pocket-tts can exit 0 yet silently write NO wav (a binary that fails late, a filter that emits
    no audio). `_voice` must still raise rather than return a URL to a non-existent narration that
    would later read as 0-duration and slip past the episode duration floor. The existing
    `test_voice_raises_when_tts_fails` only exercises the `code != 0` half; this pins `not out.exists()`."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def ok_but_no_file_sh(cmd, timeout=600):
        return 0, "pocket-tts: done"  # success code, but the wav is never written -> out.exists() is False

    async def boom_dur(path):  # _dur must never run on an episode whose VO was never produced
        raise AssertionError("measured duration of a wav that was never written")

    monkeypatch.setattr(abn_factory, "_sh", ok_but_no_file_sh)
    monkeypatch.setattr(abn_factory, "_dur", boom_dur)
    with pytest.raises(RuntimeError, match="tts:"):
        asyncio.run(abn_factory._voice("hello", "ep_a111111_s0"))


def test_voice_raises_when_gateway_raises_after_tts_command_built(monkeypatch, tmp_path):
    """Failure mode the ticket calls out: asset_path_from_slug itself raises AssetPathError when
    resolving the slug. `_voice` resolves the path via the gateway FIRST (services/abn_factory.py
    ~861), so a gateway error must propagate as AssetPathError and the TTS command must NEVER run --
    proving the gateway is the enforcement chokepoint regardless of how the slug went bad. We make
    the gateway raise directly to cover any off-schema/parse failure surfacing from inside it."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    def raising_gateway(slug, kind, *, ext=None):
        raise abn_assets.AssetPathError(f"off-schema slug {slug!r}")

    async def boom_sh(cmd, timeout=600):
        raise AssertionError("shelled out to TTS despite the gateway raising on the slug")

    monkeypatch.setattr(abn_factory, "asset_path_from_slug", raising_gateway)
    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    with pytest.raises(abn_assets.AssetPathError):
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


# ---------------- TITLE CARD: ImageMagick failure modes ----------------

def test_card_raises_when_magick_returns_nonzero(monkeypatch, tmp_path):
    """If `magick` exits non-zero (e.g. ImageMagick missing / bad args), _card must raise
    rather than return a URL to a card that was never written."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def fail_sh(cmd, timeout=60):
        return 127, "magick: command not found"

    monkeypatch.setattr(abn_factory, "_sh", fail_sh)
    with pytest.raises(RuntimeError, match="card:"):
        asyncio.run(abn_factory._card("Headline", "subtitle", "ep_a111111_s0"))


def test_card_raises_when_magick_succeeds_but_no_output_file(monkeypatch, tmp_path):
    """Even on a zero exit code, _card must raise if the output PNG is missing — a silent
    render miss must not be handed downstream as a real card."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def ok_but_no_file_sh(cmd, timeout=60):
        return 0, ""  # exit 0 but never writes the out file

    monkeypatch.setattr(abn_factory, "_sh", ok_but_no_file_sh)
    with pytest.raises(RuntimeError, match="card:"):
        asyncio.run(abn_factory._card("Headline", "subtitle", "ep_a111111_s0"))


# ---------------- CARD + DEMO PRODUCERS: gateway routing (asset-schema epic) ----------------
# Same contract proven for _voice above: the card and code-demo producers must build their write
# path via the asset gateway (per-episode {ep_id}/css|broll/ schema, not a flat ASSETS-root dump),
# and an off-schema slug must be rejected by the gateway BEFORE the producer shells out to magick/
# VHS. These pin that the verification ticket's "card/demo producers route through
# asset_path_from_slug" guarantee can't silently regress to a hand-built flat path.

def test_card_routes_through_gateway_and_returns_managed_url(monkeypatch, tmp_path):
    """_card writes the title-card PNG to the gateway's per-episode css/ subdir and returns the
    managed /agenticnews-assets/ URL that round-trips back to that same file. magick is stubbed."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    captured = {}

    async def fake_sh(cmd, timeout=60):
        captured["cmd"] = cmd
        # simulate magick writing the png the gateway told it to
        out = abn_assets.asset_path_from_slug("ep_b222222_s0", "card")
        out.write_bytes(b"\x89PNG fake")
        return 0, "ok"

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)

    url = asyncio.run(abn_factory._card("Big Headline", "a subtitle", "ep_b222222_s0"))

    assert url.startswith("/agenticnews-assets/")
    assert "magick" in captured["cmd"], "card did not go through ImageMagick"
    resolved = abn_factory._resolve_asset(url)
    assert resolved.exists()
    # lands under the per-episode css/ layer, not a flat root dump
    assert resolved.parent == tmp_path / "ep_b222222" / "css"


def test_card_raises_on_gateway_error_before_shelling_out(monkeypatch, tmp_path):
    """An off-schema slug (no 'ep_<hex>' prefix) must be rejected by the gateway BEFORE _card ever
    shells out to magick — the write path itself is the enforcement point."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def boom_sh(cmd, timeout=60):
        raise AssertionError("shelled out to magick despite an off-schema card slug")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    with pytest.raises(abn_assets.AssetPathError):
        asyncio.run(abn_factory._card("h", "s", "no_episode_prefix"))


def test_code_demo_routes_through_gateway(monkeypatch, tmp_path):
    """_code_demo writes its VHS-rendered mp4 to the gateway's per-episode broll/ subdir and returns
    the managed URL. codegen + the VHS shell are stubbed (no real LLM / VHS / ffmpeg)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "_codegen_sync", lambda title, brief: "print('hi')\nx = 1\n")

    async def fake_sh(cmd, timeout=120):
        # VHS would write the relative Output into the gateway dir; simulate that
        out = abn_assets.asset_path_from_slug("ep_c333333_s1", "demo")
        out.write_bytes(b"fake mp4")
        return 0, "ok"

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)

    url = asyncio.run(abn_factory._code_demo("Title", "brief", "ep_c333333_s1"))

    assert url is not None and url.startswith("/agenticnews-assets/")
    resolved = abn_factory._resolve_asset(url)
    assert resolved.exists()
    # demo is a broll-layer asset under the per-episode schema, not a flat dump
    assert resolved.parent == tmp_path / "ep_c333333" / "broll"


def test_code_demo_raises_on_gateway_error_before_codegen_writes(monkeypatch, tmp_path):
    """An off-schema slug must be rejected by the gateway (asset_path_from_slug) before _code_demo
    builds any scratch tape/snippet — the producer cannot hand-build a flat path around it."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "_codegen_sync", lambda title, brief: "print('hi')\n")

    async def boom_sh(cmd, timeout=120):
        raise AssertionError("shelled out to VHS despite an off-schema demo slug")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)
    with pytest.raises(abn_assets.AssetPathError):
        asyncio.run(abn_factory._code_demo("Title", "brief", "no_episode_prefix"))


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


def test_align_legacy_fallback_is_read_only_and_writes_through_gateway(monkeypatch, tmp_path):
    """ASSET-GATEWAY GUARD: when _align hits its legacy flat-path read fallback (the
    back-compat symlink the migration left at the store root), that legacy path must stay
    READ-ONLY. The CLI-fallback's alignment JSON must be written under the GATEWAY
    audio/ dir (derived from asset_path_from_slug, never from the resolved read path), and
    the legacy file's bytes must be untouched. This pins the invariant the ticket audits:
    no VO code path can turn the legacy read fallback into a write that bypasses the
    services/abn_assets gateway."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    # The gateway voice path does NOT exist -> _align falls back to the legacy flat path.
    legacy = tmp_path / "ep_a111111_s0.wav"
    legacy.write_bytes(b"legacy vo bytes")
    gateway_wav = abn_assets.asset_path_from_slug("ep_a111111_s0", "voice")
    assert not gateway_wav.exists()

    # faster-whisper yields nothing -> the slow whisper-CLI fallback runs and writes JSON.
    monkeypatch.setattr(abn_factory, "_align_sync", lambda p: [])

    captured = {}

    async def fake_sh(cmd, timeout=300):
        captured["cmd"] = cmd
        # Emulate the whisper CLI writing its json into the --output_dir it was given,
        # named after the input wav stem (what _align expects to read back).
        out_json = abn_assets.asset_path_from_slug("ep_a111111_s0", "align")
        outdir = out_json.parent
        (outdir / f"{legacy.stem}.json").write_text(json.dumps(
            {"segments": [{"words": [{"word": "yo", "start": 0.0, "end": 0.4}]}]}
        ))
        return 0, ""

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    words = asyncio.run(abn_factory._align("ep_a111111_s0"))

    assert words == [{"w": "yo", "s": 0.0, "e": 0.4}]
    # The legacy flat file was read, never written through.
    assert legacy.read_bytes() == b"legacy vo bytes"
    # whisper's --output_dir is the GATEWAY audio/ dir, not the flat store root.
    gateway_audio_dir = abn_assets.asset_path_from_slug("ep_a111111_s0", "align").parent
    assert gateway_audio_dir.name == "audio"
    assert f"--output_dir {shlex.quote(str(gateway_audio_dir))}" in captured["cmd"]
    # Nothing was written next to the legacy file at the store root.
    assert not (tmp_path / f"{legacy.stem}.json").exists()


def test_align_feeds_legacy_flat_path_to_whisper_when_gateway_missing(monkeypatch, tmp_path):
    """Direct coverage for the legacy back-compat symlink read fallback (abn_factory._align,
    services/abn_factory.py:1010-1013): when the gateway voice wav is absent but a legacy flat
    `{slug}.wav` exists at the store root, _align must resolve to and TRANSCRIBE that legacy
    file — i.e. the resolved wav handed to faster-whisper is the legacy path, not the (missing)
    gateway path. Recent asset migrations could silently break this back-compat layer."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    legacy = tmp_path / "ep_a111111_s0.wav"
    legacy.write_bytes(b"legacy vo bytes")
    gateway_wav = abn_assets.asset_path_from_slug("ep_a111111_s0", "voice")
    assert not gateway_wav.exists()

    seen = {}

    def capture_align(p):
        seen["wav"] = Path(p)
        return [{"w": "hi", "s": 0.0, "e": 0.5}]

    monkeypatch.setattr(abn_factory, "_align_sync", capture_align)

    async def boom_sh(*a, **k):  # faster-whisper succeeded -> CLI must not run
        raise AssertionError("whisper-CLI fallback ran on legacy-path happy path")

    monkeypatch.setattr(abn_factory, "_sh", boom_sh)

    words = asyncio.run(abn_factory._align("ep_a111111_s0"))
    assert words == [{"w": "hi", "s": 0.0, "e": 0.5}]
    # The wav transcribed was the legacy flat path, NOT the missing gateway path.
    assert seen["wav"] == legacy


def test_align_swallows_malformed_cli_json_and_returns_empty(monkeypatch, tmp_path):
    """Direct coverage for the JSON-parse exception handler (abn_factory._align,
    services/abn_factory.py:1026-1032): if the whisper-CLI fallback writes a json file that
    is not valid JSON, _align must swallow the parse error and return [] rather than raise."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    wav = abn_assets.asset_path_from_slug("ep_a111111_s0", "voice")
    wav.write_bytes(b"fake wav")

    monkeypatch.setattr(abn_factory, "_align_sync", lambda p: [])  # force CLI fallback

    async def fake_sh(cmd, timeout=300):
        out_json = abn_assets.asset_path_from_slug("ep_a111111_s0", "align")
        # Write a file at the exact path _align reads back, but with garbage content.
        (out_json.parent / f"{wav.stem}.json").write_text("{not valid json")
        return 0, ""

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)

    words = asyncio.run(abn_factory._align("ep_a111111_s0"))
    assert words == []


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


def test_assemble_episode_raises_when_every_segment_encode_fails(monkeypatch, tmp_path):
    """The empty-`seg_clips` gate (services/abn_factory.py ~2741) is the LAST-RESORT ffmpeg
    fallback's own failure path: when EVERY per-segment encode fails it must raise 'no segment
    clips' and never reach concat. The existing single-segment failure test only proves one
    segment; this proves the gate holds when a MULTI-segment episode loses every clip — and that
    failure is detected via `code != 0` for each segment, so concat is never attempted."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    seg_attempts = {"n": 0}

    async def failing_sh(cmd, timeout=600):
        if "-f concat" in cmd or cmd.startswith("ffprobe"):
            raise AssertionError("reached concat/probe despite every segment encode failing")
        seg_attempts["n"] += 1
        return 1, "ffmpeg: encode failed for this segment"  # code != 0 for every segment

    monkeypatch.setattr(abn_factory, "_sh", failing_sh)

    segments = [
        {"script": "first", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None},
        {"script": "second", "vo_path": "/agenticnews-assets/ep_a111111_s1.wav", "screenshot": None},
        {"script": "third", "vo_path": "/agenticnews-assets/ep_a111111_s2.wav", "screenshot": None},
    ]
    with pytest.raises(RuntimeError, match="no segment clips"):
        asyncio.run(abn_factory._assemble_episode("ep_a111111", segments))
    assert seg_attempts["n"] == 3, "every segment should have been attempted before the gate fires"


def test_assemble_episode_raises_when_every_encode_reports_ok_but_writes_nothing(monkeypatch, tmp_path):
    """The gate at services/abn_factory.py ~2737 is `code == 0 AND clip.exists()` — a segment that
    returns success but silently writes NO output file must NOT count as a usable clip. When every
    segment hits that (ffmpeg exits 0 but produced nothing — e.g. a filter that emits no frames),
    `seg_clips` stays empty and 'no segment clips' must still raise. The other failure tests only
    exercise `code != 0`; this pins the second half of the AND."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    async def ok_but_empty_sh(cmd, timeout=600):
        if "-f concat" in cmd or cmd.startswith("ffprobe"):
            raise AssertionError("reached concat/probe despite no clip files being written")
        return 0, ""  # success code, but the clip file is never created → clip.exists() is False

    monkeypatch.setattr(abn_factory, "_sh", ok_but_empty_sh)

    segments = [
        {"script": "alpha", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None},
        {"script": "beta", "vo_path": "/agenticnews-assets/ep_a111111_s1.wav", "screenshot": None},
    ]
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


def test_assemble_episode_partial_encode_failure_concats_only_survivors(monkeypatch, tmp_path):
    """PARTIAL failure: of N segments, some encode and some don't. `_assemble_episode` skips the
    failed encodes (BUS-only, no raise) and concats ONLY the survivors — it must NOT abort the whole
    episode just because one segment's ffmpeg returned non-zero, and it must NOT feed a non-existent
    clip path into the concat manifest. Pins the silent-skip loop at services/abn_factory.py ~2644.

    Three segments; the MIDDLE one (seg1) fails to encode. We assert the concat manifest contains
    exactly seg0 + seg2 (in order), seg1 is absent, and the episode still renders."""
    import shlex

    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    manifest_lines = []

    async def routed_sh(cmd, timeout=600):
        if cmd.startswith("ffprobe"):
            return 0, "30.0\n"
        out = shlex.split(cmd)[-1]
        if "-f concat" in cmd:  # capture the manifest the concat is asked to stitch
            listf = shlex.split(cmd)[shlex.split(cmd).index("-i") + 1]
            manifest_lines.extend(
                l for l in Path(listf).read_text().splitlines() if l.strip())
            Path(out).write_bytes(b"\x00final-mp4")  # copy-concat succeeds
            return 0, ""
        # per-segment encode: seg1 fails (no output written), others succeed.
        # clip basenames are like "seg1_scratch.mp4" — match on the seg-index prefix.
        if Path(out).name.startswith("seg1"):
            return 1, "ffmpeg: seg1 encode blew up"
        Path(out).write_bytes(b"\x00seg-mp4")
        return 0, ""

    monkeypatch.setattr(abn_factory, "_sh", routed_sh)

    segments = [
        {"script": "one", "vo_path": "/agenticnews-assets/ep_a111111_s0.wav", "screenshot": None},
        {"script": "two", "vo_path": "/agenticnews-assets/ep_a111111_s1.wav", "screenshot": None},
        {"script": "three", "vo_path": "/agenticnews-assets/ep_a111111_s2.wav", "screenshot": None},
    ]
    url, dur = asyncio.run(abn_factory._assemble_episode("ep_a111111", segments))

    assert url and dur == 30.0, "episode must still render from the surviving segments"
    assert len(manifest_lines) == 2, f"concat manifest must hold ONLY the 2 survivors, got {manifest_lines}"
    assert any("/seg0" in l for l in manifest_lines), "surviving seg0 missing from concat"
    assert any("/seg2" in l for l in manifest_lines), "surviving seg2 missing from concat"
    assert not any("/seg1" in l for l in manifest_lines), "failed seg1 was fed into concat"
    # order preserved: seg0 before seg2
    assert next(i for i, l in enumerate(manifest_lines) if "/seg0" in l) < \
        next(i for i, l in enumerate(manifest_lines) if "/seg2" in l)


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


def test_render_remotion_rejects_failed_normalize_pass(monkeypatch, tmp_path):
    """ATOMIC, not "non-fatal": if the Remotion render SUCCEEDS but the normalize post-pass fails,
    the episode is still yuvj420p / unnormalized-loudness — shipping it would FAIL the hard yuv420p
    gate yet still reach 'review'. _render_remotion MUST raise so the broken render is rejected,
    and it MUST emit a FATAL error event (not the old swallowed 'non-fatal' one)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")

    async def fake_sh(cmd, timeout=600):
        if "npx remotion render" in cmd:
            out.write_bytes(b"\x00fake-mp4")     # remotion succeeds: writes the episode mp4
            return 0, "rendered"
        return 1, "ffmpeg normalize: boom"        # the normalize re-encode fails

    async def fake_dur(path):
        return 640.0

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    seen_before = {e["id"] for e in abn_factory.BUS.replay()}

    with pytest.raises(RuntimeError, match="normalize pass failed"):
        asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": None}))

    new_events = [e for e in abn_factory.BUS.replay() if e["id"] not in seen_before]
    assert any(e["action"] == "error" and "normalize pass failed (FATAL)" in e["detail"]
               for e in new_events), "a failed normalize pass must emit a FATAL error event"


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
        if cmd.startswith("ffmpeg"):                 # normalize/duck passes: write the intermediate
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
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
        if cmd.startswith("ffmpeg"):                 # normalize/duck passes: write the intermediate
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
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
        if cmd.startswith("ffmpeg"):                 # normalize/duck passes: write the intermediate
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
        return 0, "yuv420p"                          # correct fmt; only the duration fails the guard

    async def fake_dur(path):
        return abn_factory.MIN_EPISODE_SEC - 1.0     # just under the 10-min floor

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": None}, force=False))
    assert rendered["n"] == 1, "a sub-floor (short) leftover must be re-rendered, not reused"


def _run_duck_pass(monkeypatch, tmp_path, music_bytes=b"\x00music"):
    """Drive _render_remotion all the way to the duck post-pass with a real musicBed present,
    and return the captured duck ffmpeg command string. Render + normalize are stubbed to
    succeed; the music bed is written INSIDE the asset store so the containment check passes."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a111111", "episode")
    bed = tmp_path / "music_bed.mp3"          # under ASSETS -> passes the relative_to containment check
    bed.write_bytes(music_bytes)

    captured = {"duck": None}

    async def fake_sh(cmd, timeout=600):
        if "npx remotion render" in cmd:
            out.write_bytes(b"\x00fake-mp4")  # remotion render succeeds
            return 0, "rendered"
        if "sidechaincompress" in cmd:
            captured["duck"] = cmd            # this is the duck pass
        if cmd.startswith("ffmpeg"):          # normalize/duck: write the intermediate so .exists() holds
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
            return 0, "ok"
        return 0, "yuv420p"

    async def fake_dur(path):
        return 640.0

    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    asyncio.run(abn_factory._render_remotion("ep_a111111", {"musicBed": "music_bed.mp3"}))
    assert captured["duck"], "duck pass never ran despite a present musicBed"
    return captured["duck"]


def test_render_remotion_duck_pass_is_safe_for_mismatched_audio_durations(monkeypatch, tmp_path):
    """The duck post-pass mixes a separate music bed UNDER the baked VO via sidechaincompress. VO and
    music bed have NO guaranteed relationship in length, so the filter MUST stay anchored to the VO
    regardless of which side is longer:
      * `-stream_loop -1` on the music input -> a SHORTER bed is looped, never running out mid-episode
        (which would leave the tail un-ducked / silent music);
      * `amix=...:duration=first` + `-shortest` -> a LONGER bed is truncated to the VO, so the episode
        length tracks the narration and the ducking key never drifts past the VO.
    Without all three a mismatched bed silently produces wrong ducking / wrong episode length."""
    cmd = _run_duck_pass(monkeypatch, tmp_path)

    # the music bed is input [1] and must be looped so a SHORT bed covers the whole VO
    assert "-stream_loop -1" in cmd, "music bed must be looped so a shorter bed still covers the VO"
    # the mix length is pinned to the FIRST input (the VO at [0:a]); a LONGER bed is truncated, not extended
    assert "duration=first" in cmd, "amix must anchor output length to the VO (duration=first)"
    assert "-shortest" in cmd, "-shortest must clamp the output to the VO so a long bed can't extend it"
    # the VO ([0:a]) is the sidechain KEY for the compressor -- ducking is keyed off narration, not the bed
    assert "[m][0:a]sidechaincompress" in cmd, "VO must be the sidechain key for the ducking compressor"



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


def test_kb_picker_never_repeats_direction_for_any_seed(monkeypatch):
    """Harden the anti-templating guarantee across the FULL seed space, not just seed=0: for every
    legal starting seed the picker must still alternate direction back-to-back. A seed that happens to
    land the rotation on a run of same-direction moves must never leak two identical gestures in a row."""
    dir_of = {}
    for m in abn_factory._KB_MOVES:
        dir_of[(m[0], m[1], m[2], m[3], m[4], m[5], m[6])] = m[7]
    for seed in range(len(abn_factory._KB_MOVES)):
        pick = abn_factory._kb_picker(seed=seed)
        dirs = []
        for _ in range(25):
            kb = pick()
            key = (kb["startScale"], kb["endScale"], kb["startX"], kb["startY"],
                   kb["endX"], kb["endY"], kb["easing"])
            dirs.append(dir_of[key])
        for a, b in zip(dirs, dirs[1:]):
            assert a != b, f"seed={seed} served two consecutive {a!r} moves"


def test_plan_shots_card_and_screenshot_both_unresolvable_fall_through_to_none(monkeypatch):
    """The full fallback CHAIN under failure: card is provided but doesn't resolve on disk → it falls
    back to the screenshot → the screenshot ALSO doesn't resolve → it was already nulled → src is None.
    Distinct from passing literal None: here both paths are non-empty strings the guards must reject,
    proving card→screenshot→None degrades safely instead of emitting a dead on-disk path."""
    # Both real paths, but _present_assets reports neither exists, so every guard fails in sequence.
    _present_assets(monkeypatch, set())
    shots = abn_factory._plan_shots(
        30.0,
        "/agenticnews-assets/ep_a111111/gone_shot.png",   # screenshot: missing → nulled
        "/agenticnews-assets/ep_a111111/gone_card.png",   # card: missing → falls back to (nulled) screenshot → None
        words=[], keywords=[], source_url="https://x", seg_index=0,
    )
    assert shots and all(s["type"] == "artifact" for s in shots)
    # the card→screenshot→None chain collapsed cleanly; no surviving disk path leaked into src
    assert {s.get("src") for s in shots} == {None}


def test_plan_shots_short_duration_routes_to_legacy_ui_frac(monkeypatch):
    """DURATION-BASED routing: the wide v2 UI share (0.26/0.30/0.22) only applies when duration > 6.
    A short segment (≤6s) must fall back to the legacy ui_frac tuple (0.40/0.45/0.50) even with v2
    visuals on and UI present — pin that ui_end tracks the LEGACY fraction, not the v2 one."""
    monkeypatch.setattr(abn_factory, "_V2_VISUALS", True)
    monkeypatch.setattr(abn_factory, "_USE_V2_VISUALS", True)
    legacy = (0.40, 0.45, 0.50)
    for seg in (0, 1, 2):
        duration = 6.0  # not > 6 → legacy branch
        shots = _full_segment(monkeypatch, seg_index=seg, duration=duration)
        ui_shots = [s for s in shots if s["id"].startswith("ui")]
        assert ui_shots, "UI present + short duration should still open on a UI beat"
        ui_end = max(s["endSec"] for s in ui_shots)
        assert ui_end == round(duration * legacy[seg % 3], 2), (
            f"seg={seg} short-duration ui_end {ui_end} did not use legacy frac {legacy[seg % 3]}")


def test_plan_shots_demo_frac_routes_by_seg_index(monkeypatch):
    """DURATION-BASED routing #2: the demo block start is duration * demo_frac where demo_frac varies
    by seg_index % 3 (0.68/0.72/0.70). With a demo present, the first demo beat must start at exactly
    round(duration * demo_frac, 2) for each segment — locking the per-segment demo placement."""
    demo_frac = (0.68, 0.72, 0.70)
    duration = 60.0
    for seg in (0, 1, 2):
        shots = _full_segment(monkeypatch, seg_index=seg, duration=duration)
        demo_starts = [s["startSec"] for s in shots if s["id"].startswith("demo")]
        assert demo_starts, "demo present should produce a demo block"
        assert min(demo_starts) == round(duration * demo_frac[seg % 3], 2), (
            f"seg={seg} demo start {min(demo_starts)} != duration*{demo_frac[seg % 3]}")


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


def test_grow_bg_library_writes_through_gateway(monkeypatch, tmp_path):
    """_grow_bg_library used to write `ASSETS / "broll_library" / f"broll_NN.mp4"` directly via
    dest.write_bytes(), bypassing the services/abn_assets gateway (same class of off-schema write
    the card_backgrounds fix closed). The cached clip must now land in the GATEWAY dir
    (_shared/broll_library/), name-validated by shared_path — NOT the flat `ASSETS/broll_library/`."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    # _BG_LIB_DIR is computed at import time off the real store; point it at the runtime gateway dir.
    monkeypatch.setattr(abn_factory, "_BG_LIB_DIR",
                        abn_assets.shared_path("broll_library", "broll_00.mp4").parent)

    # stub the expensive gen chain: a clean Flux still, then a wan-i2v clip that already lives
    # on disk under _scratch/ (the local-path branch the promoter copies into the library).
    scratch_src = tmp_path / "_scratch" / "libgen0_clip.mp4"
    scratch_src.parent.mkdir(parents=True, exist_ok=True)
    scratch_src.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 8192)  # >4096 bytes so it's accepted

    monkeypatch.setattr(abn_factory, "_flux_sync", lambda prompt: "/agenticnews-assets/_scratch/still.png")
    monkeypatch.setattr(abn_factory, "_bg_is_clean", lambda still, tag: True)
    monkeypatch.setattr(abn_factory, "_wan_i2v_sync",
                        lambda still, tag: "/agenticnews-assets/_scratch/libgen0_clip.mp4")

    made = asyncio.run(abn_factory._grow_bg_library(want=1))

    gateway_dir = tmp_path / "_shared" / "broll_library"
    flat_dir = tmp_path / "broll_library"
    assert made == 1
    cached = sorted(p.name for p in gateway_dir.glob("broll_*.mp4"))
    assert cached == ["broll_00.mp4"], "cached clip must land in the gateway dir _shared/broll_library/"
    # the off-schema flat location must NOT be written to at all
    assert not flat_dir.exists() or not list(flat_dir.glob("broll_*.mp4")), \
        "_grow_bg_library wrote to the off-schema flat broll_library/ (gateway bypassed)"


def test_animated_bg_url_roundtrips_to_gateway_dir(monkeypatch, tmp_path):
    """The b-roll URL emitted into the timeline must resolve back (via _resolve_asset) to the EXACT
    gateway dir the clip was written to. If the emitted URL and the write dir drift, the timeline
    references a 404 and the whole render dies silently."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    write_path = abn_assets.shared_path("broll_library", "broll_03.mp4")
    url = abn_assets.shared_url("broll_library", "broll_03.mp4")
    assert abn_factory._resolve_asset(url) == write_path
    assert write_path.parent == tmp_path / "_shared" / "broll_library"
    assert write_path.parent != tmp_path / "broll_library"  # NOT the old off-schema flat location


def test_ensure_card_backgrounds_refuses_to_promote_non_scratch_source(monkeypatch, tmp_path):
    """If _codex_image regresses and returns a URL pointing OUTSIDE _scratch/ (e.g. a flat
    ASSETS-root path), the promoter must NOT .replace() that file into the shared bg pool — that
    would yank an arbitrary in-store file off-schema. Only GC-reapable _scratch/ keepers promote."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "_V2_VISUALS", True)

    # an existing in-store file at the flat ASSETS root (NOT under _scratch/) that must be left alone
    bystander = tmp_path / "_tmp_bg_0.png"
    bystander.write_bytes(b"\x89PNG\r\nKEEP")

    def fake_codex_image(prompt, out_name, size="1536x1024"):
        # regression: return a flat-root URL instead of the _scratch/ one the contract promises
        return f"/agenticnews-assets/{out_name}.png"

    monkeypatch.setattr(abn_factory, "_codex_image", fake_codex_image)

    abn_factory._ensure_card_backgrounds(want=1)

    gateway_dir = tmp_path / "_shared" / "card_backgrounds"
    assert not gateway_dir.exists() or not list(gateway_dir.glob("bg_*.png")), \
        "promoted an off-schema (non-_scratch) source into the shared pool"
    # the bystander file must be untouched (not moved/consumed by .replace())
    assert bystander.exists() and bystander.read_bytes() == b"\x89PNG\r\nKEEP"


def test_ensure_card_backgrounds_refuses_symlink_in_scratch_pointing_outside(monkeypatch, tmp_path):
    """SYMLINK TRAVERSAL: _codex_image returns a /_scratch/ URL, but the file sitting there is a
    SYMLINK pointing at an arbitrary in-store file OUTSIDE any reapable root. The guard checks
    src.resolve().parent — resolve() follows the link to its real (non-_scratch) parent, so the
    membership test must fail and the keeper must NOT be promoted. A naive guard using
    src.parent (the link's own dir, which IS _scratch/) would wrongly admit it and let .replace()
    rip the symlink — and on some semantics its target — into the shared pool."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "_V2_VISUALS", True)

    # a sensitive in-store file the symlink will point at (lives at the flat root, NOT _scratch/)
    secret = tmp_path / "important_render.png"
    secret.write_bytes(b"\x89PNG\r\nSECRET")

    def fake_codex_image(prompt, out_name, size="1536x1024"):
        # codex "drops" a file in _scratch/, but it's actually a symlink to the secret
        link = tmp_path / "_scratch" / f"{out_name}.png"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(secret)
        return f"/agenticnews-assets/_scratch/{out_name}.png"

    monkeypatch.setattr(abn_factory, "_codex_image", fake_codex_image)

    abn_factory._ensure_card_backgrounds(want=1)

    gateway_dir = tmp_path / "_shared" / "card_backgrounds"
    assert not gateway_dir.exists() or not list(gateway_dir.glob("bg_*.png")), \
        "promoted a _scratch/ symlink whose target lives outside any reapable root"
    # the secret target must be untouched (neither moved nor consumed via the link)
    assert secret.exists() and secret.read_bytes() == b"\x89PNG\r\nSECRET"


def test_ensure_card_backgrounds_promotes_real_scratch_file_not_symlink(monkeypatch, tmp_path):
    """Positive control for the symlink test: a REAL regular file genuinely inside _scratch/
    (src.resolve().parent == the reapable _scratch root) MUST still promote. This guards against a
    future over-tightening of the guard that rejects legitimate keepers and silently kills the
    background pool (cards would forever fall back to the flat gradient)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(abn_factory, "_V2_VISUALS", True)

    def fake_codex_image(prompt, out_name, size="1536x1024"):
        src = tmp_path / "_scratch" / f"{out_name}.png"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x89PNG\r\nREAL")
        return f"/agenticnews-assets/_scratch/{out_name}.png"

    monkeypatch.setattr(abn_factory, "_codex_image", fake_codex_image)

    abn_factory._ensure_card_backgrounds(want=1)

    promoted = list((tmp_path / "_shared" / "card_backgrounds").glob("bg_*.png"))
    assert [p.name for p in promoted] == ["bg_00.png"], "a genuine _scratch keeper must promote"
    assert promoted[0].read_bytes() == b"\x89PNG\r\nREAL"
    # .replace() is a move: the source must be gone from _scratch/ after promotion
    assert not (tmp_path / "_scratch" / "_tmp_bg_0.png").exists()


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


# OpenShot crosses a subprocess boundary inside editor_render.render() (the whole timeline is piped
# in as JSON, the child decodes it and shells libopenshot, then prints a JSON render result back).
# Two failure modes there are silent-quality killers if the factory swallows them: (a) the child
# hangs and trips subprocess.run(timeout=...) -> TimeoutExpired, and (b) a non-finite keyframe makes
# the round-tripped JSON corrupt so editor_render refuses to spawn -> RenderError. Either way the
# contract _assemble_episode_openshot promises produce_one_episode is: RAISE, so the compiler cascade
# falls through to Remotion/ffmpeg instead of shipping a hung or corrupt-audio episode. These pin the
# propagation at the in-scope factory boundary (renderer.render is the mock; the WIRING is the test).

def _minimal_openshot_timeline(ep_id):
    return {
        "fps": 30, "width": 1920, "height": 1080, "episodeId": ep_id,
        "title": "T", "totalSec": 1.0, "musicBed": None,
        "segments": [{
            "segmentId": "s0", "title": "Seg", "sourceUrl": "https://x",
            "shots": [{"id": "a", "src": "/agenticnews-assets/x.png", "startSec": 0.0,
                       "durationSec": 1.0, "type": "artifact"}],
            "wordTimestamps": [], "keywordPops": [], "lowerThirds": [],
            "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 1.0}},
            "durationSec": 1.0,
        }],
    }


@pytest.mark.asyncio
async def test_assemble_episode_openshot_propagates_subprocess_timeout(monkeypatch, tmp_path):
    """The OpenShot child runs under subprocess.run(timeout=1800) in editor_render. If it hangs and
    trips TimeoutExpired, _assemble_episode_openshot must let that bubble up (no try/except swallow,
    no phantom return) so the cascade falls through — a hung compiler can't silently win."""
    import subprocess
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    class HangingRenderer:
        backend = "openshot"
        def render(self, project, *, output_path=None, **kw):
            raise subprocess.TimeoutExpired(cmd="openshot-child", timeout=1800)

    import services.editor_render as er
    monkeypatch.setattr(er, "choose_renderer", lambda out, asset_root=None: HangingRenderer())

    with pytest.raises(subprocess.TimeoutExpired):
        await abn_factory._assemble_episode_openshot("ep_ba6601", _minimal_openshot_timeline("ep_ba6601"))
    # the renderer never wrote a file; nothing must have been left at the gateway episode path
    assert not (tmp_path / "ep_ba6601" / "renders" / "episode.mp4").exists()


@pytest.mark.asyncio
async def test_assemble_episode_openshot_propagates_corrupt_json_render_error(monkeypatch, tmp_path):
    """editor_render serializes the project with allow_nan=False and refuses to spawn when a
    non-finite keyframe would corrupt the cross-process JSON (RenderError). _assemble_episode_openshot
    must propagate that — corrupt-JSON detection is worthless if the factory eats it and ships."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    import services.editor_render as er

    class CorruptJsonRenderer:
        backend = "openshot"
        def render(self, project, *, output_path=None, **kw):
            raise er.RenderError(
                "refusing to render: project has non-finite keyframe/value "
                "that would corrupt the subprocess JSON (Out of range float values are not JSON compliant)"
            )

    monkeypatch.setattr(er, "choose_renderer", lambda out, asset_root=None: CorruptJsonRenderer())

    with pytest.raises(er.RenderError, match="non-finite keyframe"):
        await abn_factory._assemble_episode_openshot("ep_b00b01", _minimal_openshot_timeline("ep_b00b01"))
    assert not (tmp_path / "ep_b00b01" / "renders" / "episode.mp4").exists()


@pytest.mark.asyncio
async def test_compile_episode_treats_openshot_timeout_as_recoverable_and_falls_back(monkeypatch, tmp_path):
    """End-to-end: a REAL renderer timeout (raised from choose_renderer, not a stubbed
    _assemble_episode_openshot) must flow through the cascade and trigger the Remotion fallback —
    proving a hung OpenShot is a recoverable cascade trigger, not an unhandled crash that aborts
    the episode. Pins that _assemble_episode_openshot's exception is the SAME one _compile_episode
    catches; if someone wraps the timeout in a non-Exception or returns early, this flips."""
    import subprocess
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)

    class HangingRenderer:
        backend = "openshot"
        def render(self, project, *, output_path=None, **kw):
            raise subprocess.TimeoutExpired(cmd="openshot-child", timeout=1800)

    import services.editor_render as er
    monkeypatch.setattr(er, "choose_renderer", lambda out, asset_root=None: HangingRenderer())

    async def remotion_ok(ep_id, timeline):
        return "/agenticnews-assets/remotion.mp4", 600.0

    async def ffmpeg_boom(ep_id, segments):
        raise AssertionError("ffmpeg backstop must NOT run once Remotion recovers the timeout")

    monkeypatch.setattr(abn_factory, "_render_remotion", remotion_ok)
    monkeypatch.setattr(abn_factory, "_assemble_episode", ffmpeg_boom)

    seen = {e["id"] for e in abn_factory.BUS.replay()}
    url, dur = await abn_factory._compile_episode(
        "ep_700001", _minimal_openshot_timeline("ep_700001"), [])

    assert (url, dur) == ("/agenticnews-assets/remotion.mp4", 600.0)
    actions = _bus_actions_since(seen)
    assert "openshot.fallback" in actions, "an OpenShot timeout must announce the fall-through to Remotion"
    assert "remotion.done" in actions


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


# ---- callback write-path audit: wan-i2v + OCR-probe can't be tricked off-schema ----
#
# AUDIT (prod-cycle.js L80): the wan-i2v clip callback (_wan_i2v_sync) and the Flux OCR
# self-review probe (_bg_is_clean) take an upstream `name` (today a controlled `libgenN`
# tag) and compose it into a _scratch filename — `_cross_scratch_path(f"{name}_broll.mp4")`
# / `f"{name}_still.png")` — then _download() bytes to that path. If a future caller ever
# threads an untrusted/off-schema `name` through (e.g. a slug carrying `../`), the gateway
# chokepoint MUST reject the composed name BEFORE any bytes hit disk, so the GC can never be
# handed an unreapable stray outside _scratch/.
#
# Both callbacks wrap their body in a broad try/except that FAILS SAFE (wan returns None,
# the OCR probe returns True — its documented fail-open contract), so the gateway's raise is
# swallowed rather than propagated. The security-relevant invariant is therefore behavioural,
# not the exception type: an off-schema `name` results in NO _download call and NO stray file
# under the store — the bytes never reach disk. These pin exactly that.

_REAL_WAN_I2V = abn_factory._wan_i2v_sync
_REAL_BG_IS_CLEAN = abn_factory._bg_is_clean


def _no_stray_under(store):
    sc = store / "_scratch"
    return not sc.exists() or not any(sc.iterdir())


@pytest.mark.parametrize("evil", ["../escape", "sub/dir", "a\\b", ".hidden"])
def test_wan_i2v_off_schema_name_writes_nothing(scratch_store, monkeypatch, evil):
    """A malicious `name` composes into `{name}_broll.mp4`; _cross_scratch_path rejects it before
    _download is reached, so no stray clip lands outside the reapable _scratch/ root (returns None)."""
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")
    # json.load is called twice (create prediction, then poll) — first the create-response that
    # carries the poll URL, then a 'succeeded' poll so control reaches the _cross_scratch_path line.
    create_resp = {"urls": {"get": "http://x"}}
    succeeded = {"status": "succeeded", "output": ["http://clip.mp4"]}
    seq = iter([create_resp, succeeded])
    monkeypatch.setattr(abn_factory.json, "load", lambda *a, **k: next(seq))
    monkeypatch.setattr(abn_factory.urllib.request, "urlopen", lambda *a, **k: object())
    monkeypatch.setattr(abn_factory.time, "sleep", lambda *a, **k: None)
    downloaded = []
    monkeypatch.setattr(abn_factory, "_download", lambda *a, **k: downloaded.append(a) or True)

    assert _REAL_WAN_I2V("http://still.png", evil) is None
    assert not downloaded, "off-schema name must be rejected BEFORE _download writes any bytes"
    assert _no_stray_under(scratch_store), "no stray file may be created under _scratch for a rejected name"


@pytest.mark.parametrize("evil", ["../escape", "sub/dir", "a\\b", ".hidden"])
def test_bg_is_clean_off_schema_name_writes_nothing(scratch_store, monkeypatch, evil):
    """The OCR probe composes `{name}_still.png`; an off-schema `name` is rejected at the gateway
    chokepoint before the still is downloaded — no stray lands (probe fails open -> True)."""
    downloaded = []
    monkeypatch.setattr(abn_factory, "_download", lambda *a, **k: downloaded.append(a) or True)

    assert _REAL_BG_IS_CLEAN("http://still.png", evil) is True  # documented fail-open
    assert not downloaded, "off-schema name must be rejected BEFORE _download writes any bytes"
    assert _no_stray_under(scratch_store), "no stray file may be created under _scratch for a rejected name"


def test_factory_has_no_offschema_episode_asset_write_bypass():
    """Source-level audit guard (prod-cycle.js L80: 'places that bypass services/abn_assets.py for
    asset writes'). Every WRITE under the asset store must go through the gateway / its local
    chokepoint. A bare `ASSETS / f"..."` or `ASSETS / "<literal>" /` build that is then written to
    is exactly the hand-built off-schema path the gateway exists to forbid. This fails if a future
    edit reintroduces one, so the audit conclusion stays enforced, not just documented.

    We allow the known, intentional NON-scratch keeper write: the persistent broll_library/ cache
    (a MANAGED_TOP shared dir, deliberately never reaped). Everything else that constructs a store
    path with an f-string/literal must be a READ (no .write_/.copy/.move/_download to it)."""
    import re as _re
    src = Path(abn_factory.__file__).read_text().splitlines()
    # lines that build a path from ASSETS via f-string or literal subdir (candidate write targets)
    builder = _re.compile(r"ASSETS\s*/\s*(f[\"']|[\"'])")
    offenders = []
    for i, line in enumerate(src):
        if not builder.search(line):
            continue
        # the only sanctioned literal-built keeper location is the broll library cache dir.
        if "broll_library" in line:
            continue
        # _resolve_asset / _asset_url / timeline-scan all build paths for READS — they never write.
        # Flag only if this same line ALSO performs a write to the freshly-built path.
        if _re.search(r"\.write_(text|bytes)\(|\bshutil\.(move|copy)\(|_download\(", line):
            offenders.append((i + 1, line.strip()))
    assert not offenders, (
        "off-schema asset WRITE bypass(es) found in abn_factory.py — route through the "
        f"abn_assets gateway / _cross_scratch_path instead:\n" +
        "\n".join(f"  L{n}: {t}" for n, t in offenders)
    )


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


# ---------------- GC PROTECTION SCAN: complete vs incomplete (the fail-safe that guards live renders) ----------------
#
# _editor_timeline_asset_paths_checked() returns (paths, complete). A swallowed IO/parse error during
# the protection scan MUST surface as complete=False, because the destructive low-disk render trim is
# gated on `protection_complete and fresh_complete`: an incomplete scan means we may not know every
# render an active Editor Bay timeline references, so the trim must be skipped (disk pressure is
# recoverable; tombstoning a live render an editor is mid-edit on is not). These pin that contract and
# the cascade so a regression that flips an error into a silent complete=True can't ship.

def _write_timeline(assets: Path, name: str, payload) -> Path:
    d = assets / "editor_timelines"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def test_protection_scan_complete_when_no_timeline_dir(monkeypatch, tmp_path):
    """No editor_timelines dir at all → nothing to protect, and that is a COMPLETE answer (the trim
    is free to run; we are sure nothing is referenced)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert paths == set()
    assert complete is True


def test_protection_scan_complete_collects_referenced_renders(monkeypatch, tmp_path):
    """A well-formed timeline → complete=True and the referenced render path is protected (query
    string / fragment cache-busters stripped to the real on-disk path)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    _write_timeline(tmp_path, "t.json", {
        "clips": [{"url": "/agenticnews-assets/ep5/episode.mp4?rev=3"}],
    })
    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is True
    assert abn_factory._normalize_asset_path(tmp_path / "ep5" / "episode.mp4") in paths


def test_protection_scan_incomplete_on_unparseable_timeline(monkeypatch, tmp_path):
    """A timeline JSON we can't parse → complete=False. The scan does NOT abort: a sibling valid
    timeline is still collected, but the unknown one forces the fail-safe (complete=False)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    _write_timeline(tmp_path, "good.json", {"url": "/agenticnews-assets/ep1/keep.mp4"})
    _write_timeline(tmp_path, "broken.json", "{ this is not valid json :::")
    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is False, "an unparseable timeline must mark the scan incomplete"
    # the parse failure must NOT abort the scan — the good timeline's render is still protected
    assert abn_factory._normalize_asset_path(tmp_path / "ep1" / "keep.mp4") in paths


def test_protection_scan_incomplete_on_unreadable_timeline(monkeypatch, tmp_path):
    """A timeline file we can't READ (permission denied / IO error) → complete=False, scan continues."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    p = _write_timeline(tmp_path, "locked.json", {"url": "/agenticnews-assets/ep2/x.mp4"})

    orig_read_text = Path.read_text

    def boom_read_text(self, *a, **k):
        if self == p:
            raise PermissionError("permission denied")
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom_read_text)
    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is False


def test_protection_scan_incomplete_on_glob_io_error(monkeypatch, tmp_path):
    """If globbing the timeline dir itself errors (symlink loop / permission denied) we genuinely
    don't know what's referenced → complete=False and an empty set (NOT a complete empty answer)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    _write_timeline(tmp_path, "t.json", {"url": "/agenticnews-assets/ep3/y.mp4"})

    orig_glob = Path.glob

    def boom_glob(self, pattern, *a, **k):
        if self.name == "editor_timelines":
            raise OSError("simulated symlink loop / EIO")
        return orig_glob(self, pattern, *a, **k)

    monkeypatch.setattr(Path, "glob", boom_glob)
    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is False
    assert paths == set()


def test_purge_disk_skips_render_trim_when_protection_scan_incomplete(monkeypatch, tmp_path):
    """THE CASCADE: _editor_timeline_asset_paths_checked() complete=False → purge_disk must NOT
    enter the destructive render-trim branch (it must never enumerate old renders to tombstone),
    even under simulated low disk. A regression here destroys live editor renders during a disk
    crisis."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    # force the protection scan to report incomplete
    monkeypatch.setattr(abn_factory, "_editor_timeline_asset_paths_checked",
                        lambda: (set(), False))
    # nothing reapable in scratch — isolate the render-trim decision
    monkeypatch.setattr(abn_factory, "reapable_scratch", lambda: [])

    # simulate a critical low-disk condition so the trim branch WOULD run if not for the fail-safe
    class _DU:
        free = 0.0  # 0 bytes free → well under any low_disk_gb threshold

    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda *_a, **_k: _DU())

    trim_calls = []
    monkeypatch.setattr(abn_factory, "_old_episode_renders",
                        lambda: trim_calls.append("enumerated") or [])
    monkeypatch.setattr(abn_factory, "tombstone_render",
                        lambda *a, **k: pytest.fail("tombstone_render must not run on incomplete scan"))

    abn_factory.purge_disk()

    assert trim_calls == [], (
        "incomplete protection scan must short-circuit BEFORE the render-trim branch — "
        "_old_episode_renders() must never be enumerated"
    )


# ---- _render_remotion: normalize/duck passes are ATOMIC, not "non-fatal" ----
# A failed normalize/duck pass used to be logged and swallowed, shipping an episode in the
# wrong format (yuvj420p / unnormalized loudness) that then FAILS the hard yuv420p gate yet
# still reaches 'review'. These pin that both passes now RAISE so the broken render is rejected.

def _remotion_dispatch_sh(out_path, *, normalize_ok=True, duck_ok=True):
    """Build a fake _sh that simulates the remotion render + normalize + duck shell calls. The
    ffmpeg passes write their intermediate (second-to-last cmd token) so `.replace(out)` succeeds."""
    async def fake_sh(cmd, timeout=600):
        if "remotion render" in cmd:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"\x00")  # remotion produced the raw mp4
            return 0, "ok"
        if "loudnorm=I=-14" in cmd and "sidechaincompress" not in cmd:   # normalize pass
            if not normalize_ok:
                return 1, "ffmpeg: normalize boom"
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
            return 0, "ok"
        if "sidechaincompress" in cmd:            # duck pass
            if not duck_ok:
                return 1, "ffmpeg: duck boom"
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
            return 0, "ok"
        return 0, "ok"
    return fake_sh


def test_render_remotion_raises_when_duck_pass_fails(monkeypatch, tmp_path):
    """The duck pass is the last video stage and re-asserts yuv420p; if it fails the episode
    ships un-ducked and risks the yuvj420p that broke ep_d640a3eb. Must RAISE, not swallow."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    # a music bed must exist on disk for the duck pass to run at all
    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\x00")

    out = abn_assets.asset_path("ep_c333333", "episode")
    monkeypatch.setattr(abn_factory, "_sh",
                        _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=False))

    timeline = {"musicBed": bed.name, "segments": []}
    with pytest.raises(RuntimeError, match="duck pass failed"):
        asyncio.run(abn_factory._render_remotion("ep_c333333", timeline, force=True))


def test_render_remotion_raises_on_normalize_ok_but_duck_fail_seam(monkeypatch, tmp_path):
    """The mid-process inconsistent-state seam: normalize SUCCEEDS (out is now yuv420p / -14 LUFS)
    but the duck pass FAILS. The episode is left half-processed (un-ducked); shipping it would also
    risk the yuvj420p regression since the duck pass owns the final pixel-format re-encode. Pin that
    _render_remotion (a) actually ran + applied the normalize pass FIRST, (b) RAISES so the episode
    is blocked from reaching 'review', and (c) emits a FATAL duck error event (not a swallowed one)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\x00")

    out = abn_assets.asset_path("ep_f666666", "episode")
    inner = _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=False)

    # Record pass ordering so we can prove normalize ran-and-succeeded BEFORE the duck failure —
    # i.e. the failure is genuinely the normalize-ok/duck-fail seam, not an early bail.
    passes = []

    async def tracking_sh(cmd, timeout=600):
        rc, log = await inner(cmd, timeout=timeout)
        if "loudnorm=I=-14" in cmd and "sidechaincompress" not in cmd:
            passes.append(("normalize", rc))
        elif "sidechaincompress" in cmd:
            passes.append(("duck", rc))
        return rc, log
    monkeypatch.setattr(abn_factory, "_sh", tracking_sh)

    seen_before = {e["id"] for e in abn_factory.BUS.replay()}

    timeline = {"musicBed": bed.name, "segments": []}
    with pytest.raises(RuntimeError, match="duck pass failed"):
        asyncio.run(abn_factory._render_remotion("ep_f666666", timeline, force=True))

    # normalize ran and succeeded, THEN the duck pass ran and failed — the exact seam.
    assert passes == [("normalize", 0), ("duck", 1)], passes

    new_events = [e for e in abn_factory.BUS.replay() if e["id"] not in seen_before]
    # the normalize success event fired (the seam: out was already converted to yuv420p)...
    assert any(e["action"] == "render.normalize" for e in new_events), \
        "normalize pass must have applied before the duck pass failed"
    # ...and the duck failure surfaced as a FATAL error, not a swallowed 'non-fatal' one.
    assert any(e["action"] == "error" and "duck pass failed (FATAL)" in e["detail"]
               for e in new_events), "a failed duck pass must emit a FATAL error event"


def test_render_remotion_duck_pass_reencodes_video_to_yuv420p(monkeypatch, tmp_path):
    """BELT-AND-SUSPENDERS INTERLOCK (the ep_d640a3eb regression). The duck pass is the LAST
    video stage. It used to pass the video through with '-c:v copy', so a normalize-pass hiccup
    let the episode SHIP yuvj420p and FAIL the hard yuv420p gate. The duck command MUST therefore
    re-encode the video to yuv420p itself (format=yuv420p + a real libx264 encode, NEVER -c:v copy)
    so the gate holds regardless of what the normalize pass did. Pin the actual command shape."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\x00")
    out = abn_assets.asset_path("ep_b222222", "episode")
    inner = _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=True)

    duck_cmds = []

    async def capturing_sh(cmd, timeout=600):
        if "sidechaincompress" in cmd:
            duck_cmds.append(cmd)
        return await inner(cmd, timeout=timeout)
    monkeypatch.setattr(abn_factory, "_sh", capturing_sh)

    timeline = {"musicBed": bed.name, "segments": []}
    url, dur = asyncio.run(abn_factory._render_remotion("ep_b222222", timeline, force=True))
    assert url and dur == 640.0

    assert len(duck_cmds) == 1, "the duck pass must run exactly once"
    dcmd = duck_cmds[0]
    # The interlock: the duck pass forces yuv420p AND really re-encodes the video — it must NOT
    # shortcut with -c:v copy (which would pass an upstream yuvj420p through to the gate).
    assert "format=yuv420p" in dcmd, "duck pass must force yuv420p on the video stream"
    assert "-c:v libx264" in dcmd, "duck pass must re-encode the video (belt-and-suspenders)"
    assert "-c:v copy" not in dcmd, \
        "duck pass must NOT copy the video stream through (the ep_d640a3eb regression)"


def test_render_remotion_duck_reencodes_yuv420p_even_when_normalize_left_yuvj420p(
        monkeypatch, tmp_path):
    """BELT-AND-SUSPENDERS INTERLOCK, END-TO-END (the untested seam this ticket covers).

    The normalize-pass-fails gate (line ~2504) only blocks the case where normalize's
    ffmpeg EXITS non-zero. But normalize can also exit 0, run norm.replace(out) (line
    ~2497), and still leave `out` in the wrong pixel format on the normalize-failure path
    the duck-pass comment (lines 2526-2529) is explicitly hardening against. In that case
    the duck pass — the LAST video stage — is the ONLY thing that converts the file to
    yuv420p before the hard gate. The other duck tests assert the *command string* contains
    format=yuv420p; this one drives the whole chain on disk: normalize succeeds yet leaves
    yuvj420p, and we prove the FINAL bytes of `out` are the duck pass's yuv420p output
    (i.e. the interlock actually re-encoded, not passed the yuvj420p through)."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\x00")
    out = abn_assets.asset_path("ep_aabbccdd", "episode")

    passes = []

    async def fake_sh(cmd, timeout=600):
        # Use the file CONTENTS as a stand-in for the encoded pixel format so we can track
        # what each pass actually wrote to disk through the replace() chain.
        if "remotion render" in cmd:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"yuvj420p")          # raw Remotion render: full-range JPEG
            return 0, "ok"
        if "loudnorm=I=-14" in cmd and "sidechaincompress" not in cmd:   # normalize pass
            # SUCCEEDS (exit 0 -> norm.replace(out) runs) but, simulating the normalize
            # failure path the duck comment guards, leaves the file STILL yuvj420p.
            Path(shlex.split(cmd)[-2]).write_bytes(b"yuvj420p")
            passes.append("normalize")
            return 0, "ok"
        if "sidechaincompress" in cmd:                                   # duck pass
            # The belt-and-suspenders re-encode: this pass owns the final pixel format.
            assert "format=yuv420p" in cmd and "-c:v libx264" in cmd and "-c:v copy" not in cmd
            Path(shlex.split(cmd)[-2]).write_bytes(b"yuv420p")
            passes.append("duck")
            return 0, "ok"
        return 0, "ok"
    monkeypatch.setattr(abn_factory, "_sh", fake_sh)

    timeline = {"musicBed": bed.name, "segments": []}
    url, dur = asyncio.run(abn_factory._render_remotion("ep_aabbccdd", timeline, force=True))

    assert url and dur == 640.0
    # the interlock chain ran in order: normalize.replace(out) THEN duck.replace(out)
    assert passes == ["normalize", "duck"], passes
    # the decisive end-to-end assertion: even though normalize left yuvj420p on disk, the
    # duck pass re-encoded and its yuv420p output is the FINAL content of the episode — the
    # belt-and-suspenders held regardless of the normalize pass's result.
    assert out.read_bytes() == b"yuv420p", \
        "duck pass must own the final pixel format (yuv420p), overriding the normalize output"


def test_render_remotion_succeeds_when_both_passes_ok(monkeypatch, tmp_path):
    """Happy path still returns (url, duration) when normalize + duck both succeed."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\x00")
    out = abn_assets.asset_path("ep_d444444", "episode")
    monkeypatch.setattr(abn_factory, "_sh",
                        _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=True))

    timeline = {"musicBed": bed.name, "segments": []}
    url, dur = asyncio.run(abn_factory._render_remotion("ep_d444444", timeline, force=True))
    assert dur == 640.0
    assert url


def test_render_remotion_rejects_normalize_exit0_but_no_output_file(monkeypatch, tmp_path):
    """ATOMIC gate, the OTHER half. The normalize gate is `if nc == 0 and norm.exists()` —
    ffmpeg can exit 0 yet write no file (e.g. killed after open, full disk, filter emits nothing).
    The exit-0-but-no-output branch must take the FATAL `else` and RAISE, never replace `out` with a
    missing file or fall through shipping the un-normalized yuvj420p render."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    out = abn_assets.asset_path("ep_a0a0a0a0", "episode")

    async def fake_sh(cmd, timeout=600):
        if "remotion render" in cmd:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00raw")          # remotion succeeds
            return 0, "ok"
        if "loudnorm=I=-14" in cmd and "sidechaincompress" not in cmd:
            return 0, "ok"                       # normalize EXITS 0 but writes NO intermediate file
        return 0, "ok"

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    seen_before = {e["id"] for e in abn_factory.BUS.replay()}

    with pytest.raises(RuntimeError, match="normalize pass failed"):
        asyncio.run(abn_factory._render_remotion("ep_a0a0a0a0", {"musicBed": None}, force=True))

    new_events = [e for e in abn_factory.BUS.replay() if e["id"] not in seen_before]
    assert any(e["action"] == "error" and "normalize pass failed (FATAL)" in e["detail"]
               for e in new_events), "exit-0-no-output normalize must emit a FATAL error event"


def test_render_remotion_rejects_duck_exit0_but_no_output_file(monkeypatch, tmp_path):
    """ATOMIC gate, the OTHER half for the duck pass. The duck gate is `if dc == 0 and ducked.exists()`.
    If the duck ffmpeg exits 0 but produces no file, the episode is un-ducked AND (since the duck pass
    owns the final yuv420p re-encode) risks shipping the yuvj420p that broke ep_d640a3eb. The
    exit-0-but-no-output branch must hit the FATAL `else` and RAISE, never silently ship."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    _stub_remotion_dir(monkeypatch, tmp_path)

    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\x00")
    out = abn_assets.asset_path("ep_b0b0b0b0", "episode")

    async def fake_sh(cmd, timeout=600):
        if "remotion render" in cmd:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00raw")
            return 0, "ok"
        if "loudnorm=I=-14" in cmd and "sidechaincompress" not in cmd:   # normalize: succeeds normally
            Path(shlex.split(cmd)[-2]).write_bytes(b"\x00")
            return 0, "ok"
        if "sidechaincompress" in cmd:                                   # duck: exit 0 but NO file
            return 0, "ok"
        return 0, "ok"

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_sh", fake_sh)
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    seen_before = {e["id"] for e in abn_factory.BUS.replay()}

    timeline = {"musicBed": bed.name, "segments": []}
    with pytest.raises(RuntimeError, match="duck pass failed"):
        asyncio.run(abn_factory._render_remotion("ep_b0b0b0b0", timeline, force=True))

    new_events = [e for e in abn_factory.BUS.replay() if e["id"] not in seen_before]
    assert any(e["action"] == "error" and "duck pass failed (FATAL)" in e["detail"]
               for e in new_events), "exit-0-no-output duck must emit a FATAL error event"


def test_render_remotion_drops_traversal_music_bed_outside_store(monkeypatch, tmp_path):
    """A corrupted/traversal musicBed value must NOT read a file outside the asset store. The
    bed read goes through the _resolve_asset gateway + a containment check; an escaping path is
    dropped and the duck pass is skipped, so the sidechain ffmpeg call never runs against it."""
    store = tmp_path / "assets"
    store.mkdir()
    monkeypatch.setattr(abn_factory, "ASSETS", store)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", store)
    _stub_remotion_dir(monkeypatch, store)

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    # Sentinel music file living OUTSIDE the store — a traversal value would reach it.
    outside = tmp_path / "outside_bed.mp3"
    outside.write_bytes(b"\x00")

    duck_calls = []
    out = abn_assets.asset_path("ep_e555555", "episode")
    inner = _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=True)

    async def tracking_sh(cmd, timeout=600):
        if "sidechaincompress" in cmd:
            duck_calls.append(cmd)
        return await inner(cmd, timeout=timeout)
    monkeypatch.setattr(abn_factory, "_sh", tracking_sh)

    # URL-form value that escapes the store via traversal.
    timeline = {"musicBed": "/agenticnews-assets/../outside_bed.mp3", "segments": []}
    url, dur = asyncio.run(abn_factory._render_remotion("ep_e555555", timeline, force=True))
    assert dur == 640.0
    assert url
    assert duck_calls == []  # duck pass skipped: escaping bed was dropped, not read


def test_render_remotion_ducks_valid_music_bed_inside_store(monkeypatch, tmp_path):
    """The flip side of the traversal-rejection guard: a musicBed that resolves INSIDE the asset
    store must NOT be dropped by the containment check — it must survive and actually get ducked.
    Pin that the duck pass runs exactly once AND that the sidechain ffmpeg call references the
    resolved in-store bed file (not some other path), proving the valid bed reached the duck."""
    store = tmp_path / "assets"
    store.mkdir()
    monkeypatch.setattr(abn_factory, "ASSETS", store)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", store)
    _stub_remotion_dir(monkeypatch, store)

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    # A real bed living INSIDE the store under a per-episode subdir — a legit, non-escaping value.
    bed = store / "ep_e666666" / "bed.mp3"
    bed.parent.mkdir(parents=True, exist_ok=True)
    bed.write_bytes(b"\x00")

    duck_calls = []
    out = abn_assets.asset_path("ep_e666666", "episode")
    inner = _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=True)

    async def tracking_sh(cmd, timeout=600):
        if "sidechaincompress" in cmd:
            duck_calls.append(cmd)
        return await inner(cmd, timeout=timeout)
    monkeypatch.setattr(abn_factory, "_sh", tracking_sh)

    # Relative-name value that resolves under the store (the common, valid case).
    timeline = {"musicBed": "ep_e666666/bed.mp3", "segments": []}
    url, dur = asyncio.run(abn_factory._render_remotion("ep_e666666", timeline, force=True))
    assert dur == 640.0
    assert url

    # The valid bed survived containment and was ducked exactly once, against the resolved file.
    assert len(duck_calls) == 1, "valid in-store bed must be ducked exactly once, not dropped"
    assert str(bed.resolve()) in duck_calls[0] or shlex.quote(str(bed)) in duck_calls[0], \
        "duck pass must read the resolved in-store bed file"


def test_render_remotion_drops_symlink_music_bed_pointing_outside_store(monkeypatch, tmp_path):
    """The symlink edge case the '..' traversal test does NOT cover: a musicBed whose path lives
    LEXICALLY inside the asset store but is a SYMLINK whose target is OUTSIDE the store. A pure
    string/lexical containment check would pass it (the link's own path is in-store), then the
    sidechain ffmpeg call would read the attacker-controlled file the link points at. The guard
    uses bedfile.resolve() (which FOLLOWS symlinks) before .relative_to(ASSETS.resolve()), so the
    real target is what's checked — the escaping link must be dropped and the duck pass skipped.

    This is the music-bed analogue of the card-background _scratch/ symlink guard
    (test_ensure_card_backgrounds_refuses_symlink_in_scratch_pointing_outside)."""
    store = tmp_path / "assets"
    store.mkdir()
    monkeypatch.setattr(abn_factory, "ASSETS", store)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", store)
    _stub_remotion_dir(monkeypatch, store)

    async def fake_dur(path):
        return 640.0
    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    # Sensitive sentinel living OUTSIDE the store — what the in-store symlink secretly points at.
    secret = tmp_path / "outside_secret.mp3"
    secret.write_bytes(b"\x00")

    # The musicBed: a real symlink whose OWN path is inside the store (would pass a naive lexical
    # check) but whose target escapes the store.
    link = store / "ep_e777777" / "bed.mp3"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)

    duck_calls = []
    out = abn_assets.asset_path("ep_e777777", "episode")
    inner = _remotion_dispatch_sh(out, normalize_ok=True, duck_ok=True)

    async def tracking_sh(cmd, timeout=600):
        if "sidechaincompress" in cmd:
            duck_calls.append(cmd)
        return await inner(cmd, timeout=timeout)
    monkeypatch.setattr(abn_factory, "_sh", tracking_sh)

    # In-store relative name that points at the escaping symlink.
    timeline = {"musicBed": "ep_e777777/bed.mp3", "segments": []}
    url, dur = asyncio.run(abn_factory._render_remotion("ep_e777777", timeline, force=True))
    assert dur == 640.0
    assert url
    # The symlink's real target is outside the store → dropped, duck pass never runs against it.
    assert duck_calls == [], "a music-bed symlink escaping the store must be dropped, not ducked"


# ---- revisualize: pristine-timeline backup is written ATOMICALLY (this ticket) ----


def _seed_revisualize_episode(monkeypatch, tmp_path, ep_id="ep_a111111"):
    """Lay down the minimal on-disk inputs revisualize_episode needs to reach the
    backup-write branch: a valid timeline.json and the original episode mp4."""
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path)
    tlf = abn_assets.asset_path(ep_id, "timeline")
    timeline = {"segments": [{"segmentId": f"{ep_id}_s0"}], "fps": 30, "café": "naïve"}
    tlf.write_text(json.dumps(timeline), encoding="utf-8")
    orig = abn_assets.asset_path(ep_id, "episode")
    orig.write_bytes(b"fake mp4 bytes")
    bak = abn_assets.asset_path(ep_id, "assembled", "timeline.orig", ext="json")
    return ep_id, timeline, bak


def test_revisualize_backup_is_written_atomically(monkeypatch, tmp_path):
    """The pristine-timeline backup must be persisted through atomic_save (tmp+fsync+
    replace), not a raw write_text — so a crash mid-write can't truncate it. We let
    revisualize_episode reach the backup write, then make the very next shell-out (the
    audio extract) fail so the run stops right after the backup. The backup must exist,
    parse as the original timeline, and leave no tmp sidecar behind."""
    ep_id, timeline, bak = _seed_revisualize_episode(monkeypatch, tmp_path)

    async def fake_dur(path):
        return 600.0

    async def fail_sh(cmd, timeout=600):
        return 1, "boom"  # audio extract fails -> revisualize raises after the backup

    monkeypatch.setattr(abn_factory, "_dur", fake_dur)
    monkeypatch.setattr(abn_factory, "_sh", fail_sh)

    with pytest.raises(RuntimeError):
        asyncio.run(abn_factory.revisualize_episode(ep_id))

    assert bak.exists(), "pristine-timeline backup was not written"
    assert json.loads(bak.read_text(encoding="utf-8")) == timeline
    assert not list(bak.parent.glob("*.tmp")), "atomic_save leaked a tmp sidecar"


def test_revisualize_backup_crash_midwrite_leaves_no_partial(monkeypatch, tmp_path):
    """If the process dies mid-backup (here: os.fsync raises, the ENOSPC/EIO crash
    class), atomic_save must abort with NO backup file and NO tmp — never a truncated
    timeline.orig that a re-run would treat as the pristine copy. A raw write_text
    would have left a half-written, corrupt backup."""
    ep_id, _timeline, bak = _seed_revisualize_episode(
        monkeypatch, tmp_path, ep_id="ep_b222222"
    )

    async def fake_dur(path):
        return 600.0

    monkeypatch.setattr(abn_factory, "_dur", fake_dur)

    from services import json_store

    def boom(_fd):
        raise OSError("simulated fsync failure mid-backup")

    monkeypatch.setattr(json_store.os, "fsync", boom)

    with pytest.raises(OSError):
        asyncio.run(abn_factory.revisualize_episode(ep_id))

    assert not bak.exists(), "a truncated/partial backup was left after a mid-write crash"
    assert not list(bak.parent.glob("*.tmp")), "tmp sidecar leaked after mid-write crash"


# ---------- asset-path resolver parity: strip ?query / #fragment cache-busters ----------

@pytest.mark.parametrize("suffix", ["", "?rev=3", "#frag", "?rev=3&foo=bar#frag"])
def test_resolve_asset_strips_cachebuster_to_real_disk_path(suffix):
    """_resolve_asset is the inverse of _asset_url and is fed editor-persisted URLs that may carry
    a ?rev=N cache-buster (EditorBay). It MUST resolve to the bare on-disk file — in parity with
    both openshot_bridge._resolve_asset_src and the GC protection scan — or the factory reads back
    a render the GC protected as 'missing'."""
    url = f"{abn_factory.URL_PREFIX}ep_x/renders/episode.mp4{suffix}"
    expected = abn_factory.ASSETS / "ep_x" / "renders" / "episode.mp4"
    assert abn_factory._resolve_asset(url) == expected


# ---------- _download partial-file cleanup uses safe_unlink (no nested bare except) ----------

def test_download_success_streams_to_dest(monkeypatch, tmp_path):
    """A working download writes bytes to dest and returns True."""
    import io
    dest = tmp_path / "ok.bin"

    class FakeResp:
        def __init__(self): self._b = io.BytesIO(b"payload-bytes")
        def read(self, n): return self._b.read(n)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(abn_factory.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert abn_factory._download("http://x/y", dest) is True
    assert dest.read_bytes() == b"payload-bytes"


def test_download_failure_unlinks_partial_and_returns_false(monkeypatch, tmp_path):
    """When the stream blows up mid-download, the partial dest is cleaned up via safe_unlink
    and _download returns False — replacing the old nested bare-except cleanup."""
    dest = tmp_path / "partial.bin"

    class BoomResp:
        def read(self, n): raise IOError("CDN dropped the connection")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(abn_factory.urllib.request, "urlopen", lambda *a, **k: BoomResp())
    assert abn_factory._download("http://x/y", dest) is False
    assert not dest.exists(), "partial download file was not cleaned up"


def test_download_failure_with_no_dest_does_not_raise(monkeypatch, tmp_path):
    """If the download fails before any file is created, cleanup must not raise — safe_unlink
    swallows the missing-file OSError."""
    dest = tmp_path / "never_created.bin"

    def boom(*a, **k): raise ConnectionError("dns failure, nothing written")

    monkeypatch.setattr(abn_factory.urllib.request, "urlopen", boom)
    assert abn_factory._download("http://x/y", dest) is False
    assert not dest.exists()


def test_atomic_write_text_writes_full_content(tmp_path):
    """The concat-manifest writer must land the complete file."""
    p = tmp_path / "sub" / "list.txt"
    body = "file 'a.mp4'\nfile 'b.mp4'\n"
    abn_factory._atomic_write_text(p, body)
    assert p.read_text(encoding="utf-8") == body
    # parent dir was created on demand
    assert p.parent.is_dir()
    # no .tmp residue left behind
    assert not (p.parent / (p.name + ".tmp")).exists()


def test_atomic_write_text_leaves_no_partial_on_crash(tmp_path, monkeypatch):
    """If the process dies after the tmp write but before os.replace, the real
    target path must NOT exist as a truncated/partial file — the whole point of
    tmp+replace. The old code (target.write_text) would have left a partial."""
    p = tmp_path / "list.txt"

    def boom(src, dst):
        raise RuntimeError("crash before rename")

    monkeypatch.setattr(abn_factory.os, "replace", boom)
    with pytest.raises(RuntimeError):
        abn_factory._atomic_write_text(p, "file 'a.mp4'\n")
    # target was never created — a reader can't pick up a half-written manifest
    assert not p.exists()


def test_atomic_write_text_overwrites_atomically(tmp_path):
    """Rewriting an existing manifest replaces it wholesale."""
    p = tmp_path / "list.txt"
    abn_factory._atomic_write_text(p, "old\n")
    abn_factory._atomic_write_text(p, "file 'new.mp4'\n")
    assert p.read_text(encoding="utf-8") == "file 'new.mp4'\n"


# --- _extract_keywords fallback timing (Whisper alignment failed → words=[]) -------------
# services/abn_factory.py:1868-1874. When alignment is missing the heuristic still finds
# keyword candidates but they have no real timestamps; the fallback spaces the top pops
# evenly across the segment so they STILL animate (the 'overlays severely lacking' defect
# happened whenever this didn't fire). Previously only ever exercised via a stub.

def test_extract_keywords_fallback_distributes_evenly_when_words_empty():
    """No Whisper alignment + a real segment duration → pops get spaced timings, never -1."""
    script = ("ollama-uncensored runs GPT-5 locally with a 128K context for $0 "
              "from OpenAI and Anthropic at 10x speed")
    out = abn_factory._extract_keywords(
        script, words=[], tool_name="ollama-uncensored — local LLM", seg_duration=20.0)

    assert out, "fallback must still yield pops so the segment isn't bare"
    assert len(out) <= 5
    # the whole point: placeholder -1 times got replaced with real, in-window timings
    for c in out:
        assert c["s"] >= 0 and c["e"] > c["s"], f"pop never got a real time: {c}"
        # first/last ~12% margin so a pop never collides with the segment cut
        assert 20.0 * 0.12 <= c["s"] <= 20.0 * 0.88, f"pop out of safe window: {c}"
        assert "color" in c
    # emitted in chronological order
    starts = [c["s"] for c in out]
    assert starts == sorted(starts)
    # evenly spaced (distinct), not all stacked on one instant
    assert len(set(starts)) == len(starts)


def test_extract_keywords_fallback_skipped_for_too_short_segment():
    """seg_duration <= 2 → no room to space pops; timings stay at the -1 placeholder."""
    script = "GPT-5 from OpenAI hits 10x"
    out = abn_factory._extract_keywords(script, words=[], seg_duration=1.0)
    assert out, "candidates still found even when too short to space"
    assert all(c["s"] == -1.0 and c["e"] == -1.0 for c in out)


def test_extract_keywords_uses_whisper_timing_when_words_present():
    """Sanity contrast: with alignment present, pops take real word timestamps, not the
    even-distribution fallback."""
    words = [
        {"w": "GPT-5", "s": 3.0, "e": 3.4},
        {"w": "OpenAI", "s": 7.5, "e": 8.0},
    ]
    out = abn_factory._extract_keywords(
        "GPT-5 from OpenAI", words=words, seg_duration=20.0)
    by_text = {c["text"].lower(): c for c in out}
    assert by_text["gpt-5"]["s"] == 3.0
    assert by_text["openai"]["s"] == 7.5


# ---------------- v2 scene-card GUARDS (the anti-slop quality gates) ----------------
#
# services.abn_factory._v2_scene_cards turns a segment's VO into DESIGNED cards. Several inline
# guards stop malformed cards from shipping; they were untested. Two layers of test:
#   1. the pure guard helpers (_same_entity / _quote_text / _statement / _diagram_steps) directly,
#   2. _v2_scene_cards end-to-end with the scene-tagger + card renderer stubbed, asserting WHICH
#      renderer each scene routes to (so the tool-name guard, rival guard, quote floor, diagram
#      step-floor, and the episode-wide quote-cap are all exercised against the real branch logic).


def test_same_entity_flags_product_vs_its_maker():
    """A vs card pitting a product against its parent company is nonsense ('MAI-Code VS Microsoft').
    _same_entity catches same-name, substring, AND same-parent-family pairs."""
    assert abn_factory._same_entity("MAI-Code-1-Flash", "Microsoft") is True   # product vs maker
    assert abn_factory._same_entity("GPT", "OpenAI") is True                    # product vs maker
    assert abn_factory._same_entity("Claude", "Claude Code") is True           # substring
    assert abn_factory._same_entity("Cursor", "Cursor") is True                # identical


def test_same_entity_allows_real_rivals_and_handles_empty():
    """Genuine competitors are NOT the same entity (a real vs card should render), and an empty
    side is never 'same' (so the caller falls back instead of crashing)."""
    assert abn_factory._same_entity("Claude", "ChatGPT") is False
    assert abn_factory._same_entity("Cursor", "Windsurf") is False
    assert abn_factory._same_entity("", "Anthropic") is False
    assert abn_factory._same_entity("Anthropic", "") is False


def test_quote_text_strips_leadin_and_never_truncates_midsentence():
    """A quote card needs a clean COMPLETE quote — drop throat-clearing lead-ins and 'X:' preambles,
    keep whole sentences only."""
    q = abn_factory._quote_text("But here's the catch: it's mainly for enterprise teams.")
    assert q == "It's mainly for enterprise teams."
    # whole sentences, not a mid-sentence chop
    assert q.endswith(".")


def test_statement_drops_filler_opener_and_uppercases():
    """_statement yields a punchy <=8-word uppercase thought with the setup/connective stripped."""
    assert abn_factory._statement("And that is why it matters so much for everyone") \
        == "MATTERS SO MUCH FOR EVERYONE"
    assert abn_factory._statement("This matters because the model is twice as fast") \
        == "THE MODEL IS TWICE AS FAST"


def test_diagram_steps_extracts_real_mechanism_and_rejects_filler():
    """A diagram card needs >=2 real mechanism steps. CTAs/questions/naming-clauses are filtered so
    a diagram never shows a junk single box (caller then falls back to a statement)."""
    steps = abn_factory._diagram_steps("It scans the repo. Then it builds a plan. Next it writes code.")
    assert len(steps) >= 2 and steps[0] == "It scans the repo"
    assert abn_factory._diagram_steps("Want to see how it works in real time?") == []   # CTA/question
    assert abn_factory._diagram_steps("It's called the Defending Code Ref.") == []       # naming clause


# --- _v2_scene_cards integration: stub the tagger + renderer, capture the routed card type ---


class _FakeV2Cards:
    """Records which card renderer each scene hit. Each method returns a fake Path and logs the call,
    so a test can assert the GUARD routed a scene to a statement/hook instead of a malformed card."""

    BRAND_CYAN = "#7FD2FF"

    def __init__(self):
        self.calls = []

    def hook_card(self, *a, **k):
        self.calls.append(("hook", a, k)); return Path(f"/fake/{len(self.calls)}.png")

    def number_card(self, *a, **k):
        self.calls.append(("number", a, k)); return Path(f"/fake/{len(self.calls)}.png")

    def vs_card(self, *a, **k):
        self.calls.append(("vs", a, k)); return Path(f"/fake/{len(self.calls)}.png")

    def quote_card(self, *a, **k):
        self.calls.append(("quote", a, k)); return Path(f"/fake/{len(self.calls)}.png")

    def diagram_card(self, *a, **k):
        self.calls.append(("diagram", a, k)); return Path(f"/fake/{len(self.calls)}.png")


def _stub_v2_env(monkeypatch, scenes, shots, title="Cursor — the headline", comparison=""):
    """Wire _v2_scene_cards to run offline: fixed scene list + shot list, a recording fake card
    renderer, and stubbed asset-path/url so nothing touches disk. Returns the fake renderer."""
    if not abn_factory._V2_VISUALS:
        pytest.skip("factory.formats (v2 visuals) not importable in this environment")
    monkeypatch.setattr(abn_factory, "_USE_V2_VISUALS", True)
    monkeypatch.setattr(abn_factory, "tag_scenes", lambda *a, **k: list(scenes))
    monkeypatch.setattr(abn_factory, "direct_visuals", lambda *a, **k: list(shots))
    import factory.formats.scenes as _scenes_mod
    monkeypatch.setattr(_scenes_mod, "comparison_target", lambda text: comparison)
    fake = _FakeV2Cards()
    monkeypatch.setattr(abn_factory, "_v2cards", fake)
    monkeypatch.setattr(abn_factory, "asset_path",
                        lambda ep_id, kind, slug: Path(f"/tmp/{ep_id}/css/{slug}.png"))
    monkeypatch.setattr(abn_factory, "_asset_url", lambda p: f"/agenticnews-assets/{Path(p).name}")
    return fake


def _scene(idx, text):
    return type("Scene", (), {"index": idx, "text": text})()


def _shot(shot_type):
    return type("Shot", (), {"shot_type": shot_type})()


def test_v2_vs_card_rejects_fragment_tool_name_and_renders_statement(monkeypatch):
    """The vs-card tool-name guard: a title whose first segment is a verb-phrase fragment ('Use your')
    is NOT a real left entity — the card must drop to a bold STATEMENT (hook), never 'Use your VS X'."""
    scenes = [_scene(1, "Cursor crushes the competition by a wide margin today.")]
    shots = [_shot("vs_card")]
    fake = _stub_v2_env(monkeypatch, scenes, shots, title="Use your terminal — the trick", comparison="Copilot")
    out = abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Use your terminal — the trick"})
    assert out, "guard should still produce a (statement) card, not nothing"
    assert fake.calls[0][0] == "hook"   # routed to statement, NOT vs


def test_v2_vs_card_rejects_product_vs_maker_via_same_entity(monkeypatch):
    """The rival guard: even with a valid tool name, a 'rival' that is the tool's own maker
    (_same_entity True) is not a real matchup — drop to a statement instead of 'GPT VS OpenAI'."""
    scenes = [_scene(1, "GPT keeps getting faster every single release cycle.")]
    shots = [_shot("vs_card")]
    fake = _stub_v2_env(monkeypatch, scenes, shots, title="GPT — the update", comparison="OpenAI")
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "GPT — the update"})
    assert fake.calls[0][0] == "hook"   # same-entity rival rejected -> statement


def test_v2_vs_card_renders_for_a_real_matchup(monkeypatch):
    """Positive control: a clean tool name + a genuine competitor DOES render a real vs card."""
    scenes = [_scene(1, "Cursor is pulling ahead fast in the editor wars.")]
    shots = [_shot("vs_card")]
    fake = _stub_v2_env(monkeypatch, scenes, shots, title="Cursor — the headline", comparison="Windsurf")
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Cursor — the headline"})
    assert fake.calls[0][0] == "vs"
    assert fake.calls[0][1][:2] == ("Cursor", "Windsurf")   # left=tool, right=rival


def test_v2_quote_card_floor_routes_tiny_fragment_to_statement(monkeypatch):
    """The quote floor: a too-short quote ('The catch?') leaves the card mostly empty — render a
    bold STATEMENT instead. A substantive quote DOES render as a quote."""
    short = [_scene(1, "The catch?")]
    fake = _stub_v2_env(monkeypatch, short, [_shot("quote_card")], title="Cursor — x")
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Cursor — x"})
    assert fake.calls[0][0] == "hook"   # below the 24-char / 4-word floor -> statement

    big = [_scene(1, "This model writes production code with almost no hand-holding now.")]
    fake2 = _stub_v2_env(monkeypatch, big, [_shot("quote_card")], title="Cursor — x")
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Cursor — x"})
    assert fake2.calls[0][0] == "quote"


def test_v2_diagram_card_step_floor_routes_to_statement(monkeypatch):
    """The diagram step-floor: <2 real mechanism steps -> bold STATEMENT, never a junk single-box
    diagram. >=2 steps DOES render a diagram."""
    thin = [_scene(1, "Want to see how it works in real time?")]
    fake = _stub_v2_env(monkeypatch, thin, [_shot("diagram")], title="Cursor — x")
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Cursor — x"})
    assert fake.calls[0][0] == "hook"

    real = [_scene(1, "It scans the repo. Then it builds a plan. Next it writes the code.")]
    fake2 = _stub_v2_env(monkeypatch, real, [_shot("diagram")], title="Cursor — x")
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Cursor — x"})
    assert fake2.calls[0][0] == "diagram"


def test_v2_episode_quote_cap_rotates_excess_quotes_to_other_cards(monkeypatch):
    """The episode-wide quote-cap: quotes are capped at ~30% of all scenes seen; the EXCESS quote
    shots rotate through varied alternatives (statement/diagram) so the episode isn't a wall of
    quotes. With 10 quote scenes the cap is 3 -> only 3 quote cards, the rest are NOT quotes."""
    n = 10
    scenes = [_scene(i, "This model writes production code with almost no hand-holding at all.")
              for i in range(n)]
    shots = [_shot("quote_card") for _ in range(n)]
    fake = _stub_v2_env(monkeypatch, scenes, shots, title="Cursor — x")
    budget = {}
    abn_factory._v2_scene_cards("ep_t", 1, {"script": "x", "title": "Cursor — x"}, ep_budget=budget)
    quote_cards = sum(1 for c in fake.calls if c[0] == "quote")
    assert quote_cards == max(1, int(n * 0.30))   # capped at 3
    assert quote_cards < n                          # the rest were rotated away
    assert budget["quotes"] == quote_cards


def test_v2_returns_empty_when_v2_visuals_disabled(monkeypatch):
    """Master kill-switch: with v2 off, _v2_scene_cards returns [] so the legacy visual renders
    (the whole function is best-effort and must NEVER break a render)."""
    monkeypatch.setattr(abn_factory, "_USE_V2_VISUALS", False)
    assert abn_factory._v2_scene_cards("ep_t", 0, {"script": "x", "title": "t"}) == []


# ---------------- ATOMIC BINARY COPY: no truncated clips for downstream readers ----------------

def test_atomic_copy_file_copies_bytes_exactly(tmp_path):
    """_atomic_copy_file must reproduce the source bytes exactly at the destination."""
    src = tmp_path / "src.mp4"
    payload = b"\x00\x01FAKE-MP4-CLIP\xff" * 1000
    src.write_bytes(payload)
    dest = tmp_path / "out" / "dest.mp4"
    abn_factory._atomic_copy_file(src, dest)
    assert dest.read_bytes() == payload


def test_atomic_copy_file_leaves_no_tmp_and_uses_replace(tmp_path):
    """The copy goes through a tmp sibling + os.replace, so the .tmp must not survive and the
    final dest is created atomically (no partial dest is ever the live file)."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x" * 50000)
    dest = tmp_path / "dest.mp4"
    abn_factory._atomic_copy_file(src, dest)
    assert dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


def test_atomic_copy_file_crash_mid_write_leaves_dest_untouched(tmp_path, monkeypatch):
    """If a crash happens after the tmp is written but BEFORE os.replace, the destination is
    never partially overwritten — the whole point of tmp+replace. Simulate by making os.replace
    raise and asserting the pre-existing dest is intact and no live partial replaced it."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"NEW" * 10000)
    dest = tmp_path / "dest.mp4"
    dest.write_bytes(b"OLD-GOOD-CLIP")

    import os as _os
    real_replace = _os.replace
    monkeypatch.setattr(abn_factory.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        abn_factory._atomic_copy_file(src, dest)
    # the original destination must be byte-for-byte intact (never half-written)
    assert dest.read_bytes() == b"OLD-GOOD-CLIP"
