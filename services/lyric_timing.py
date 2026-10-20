"""Immutable page/sound timing authority for the scoped lyric-video format."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any

_TIMING_PATH = Path(__file__).resolve().parents[1] / "recipes" / "lyric-timing" / "burnt-heavy-foot-chorus.v1.json"
_TIMING_ID = "burnt-heavy-foot-chorus-v1"
_TIMING_SHA256 = "134be148cc3c522fa8ff48e7bcdd8aebc5ff0c6f1a8073ee5b4010558c3f3e9f"
_FIELDS = {"schema", "id", "pageId", "account", "soundId", "durationMs", "phrases", "canonicalCaption", "referenceAudioSha256", "timingProvenance", "intervalSemantics", "sourceProgramId", "sourceProgramSha256", "notionPageId"}

class LyricTimingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

def _load() -> dict[str, Any]:
    try:
        raw = _TIMING_PATH.read_bytes()
    except OSError as exc:
        raise LyricTimingError("lyric_timing_unavailable", "timing manifest is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != _TIMING_SHA256:
        raise LyricTimingError("lyric_timing_hash_mismatch", "timing manifest bytes differ from the registered revision")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LyricTimingError("lyric_timing_invalid", "timing manifest is invalid") from exc
    valid = (isinstance(value, dict) and set(value) == _FIELDS
             and value.get("schema") == "content-lab.lyric-timing-set.v1"
             and value.get("id") == _TIMING_ID
             and value.get("notionPageId") == "3451465b-b829-80a8-b9d1-fb7e34b98ff5"
             and value.get("sourceProgramId") == "burnt-heavy-foot-tirana-v3"
             and value.get("sourceProgramSha256") == "78da457008391bd873dbc23c57fc7621f9707b8e139781e8d98cea4cdb9f65c9"
             and value.get("pageId") == "acct:burnt-mill-rd"
             and value.get("account") == "burnt.mill.rd"
             and value.get("soundId") == "7611482162773035025"
             and type(value.get("durationMs")) is int and value["durationMs"] == 20_000
             and isinstance(value.get("phrases"), list) and len(value["phrases"]) == 7)
    if not valid:
        raise LyricTimingError("lyric_timing_invalid", "timing identity or shape is invalid")
    previous_end = None
    for phrase in value["phrases"]:
        if (not isinstance(phrase, dict) or set(phrase) != {"startMs", "endMs", "text"}
                or type(phrase.get("startMs")) is not int or type(phrase.get("endMs")) is not int
                or not 0 <= phrase["startMs"] < phrase["endMs"] <= value["durationMs"]
                or (previous_end is not None and phrase["startMs"] != previous_end)
                or not isinstance(phrase.get("text"), str) or not phrase["text"].strip()
                or phrase["text"] != phrase["text"].strip() or len(phrase["text"].encode()) > 1_000
                or any(ord(c) < 32 for c in phrase["text"])):
            raise LyricTimingError("lyric_timing_invalid", "timing phrases must be bounded, contiguous and nonoverlapping")
        previous_end = phrase["endMs"]
    canonical = "\n".join(p["text"] for p in value["phrases"])
    if previous_end != value["durationMs"] or value["canonicalCaption"] != canonical or len(canonical) > 4_000:
        raise LyricTimingError("lyric_timing_invalid", "timing caption or final boundary is invalid")
    # Retain the exact registered UTF-8 bytes; reserialization changes authority.
    return {**value, "sha256": _TIMING_SHA256, "manifestJson": raw.decode("utf-8")}

def resolve_lyric_timing(set_id: str, *, page_id: str, account: str, sound_id: str, expected_sha256: str) -> dict[str, Any]:
    if set_id != _TIMING_ID:
        raise LyricTimingError("lyric_timing_unknown", "timing selection is not registered")
    if expected_sha256 != _TIMING_SHA256:
        raise LyricTimingError("lyric_timing_hash_mismatch", "timing selection revision is stale or invalid")
    value = _load()
    if (page_id, account, sound_id) != (value["pageId"], value["account"], value["soundId"]):
        raise LyricTimingError("lyric_timing_scope_mismatch", "timing selection belongs to a different page, account or sound")
    return value

def lyric_timing_catalog(*, page_id: str, account: str) -> list[dict[str, Any]]:
    if (page_id, account) != ("acct:burnt-mill-rd", "burnt.mill.rd"):
        return []
    value = _load()
    return [{"id": value["id"], "sha256": value["sha256"], "soundId": value["soundId"], "durationMs": value["durationMs"], "canonicalCaption": value["canonicalCaption"], "sourceProgramId": value["sourceProgramId"], "sourceProgramSha256": value["sourceProgramSha256"], "notionPageId": value["notionPageId"]}]
