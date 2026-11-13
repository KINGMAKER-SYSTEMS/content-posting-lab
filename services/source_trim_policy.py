"""Bind a source-duration-repair command to the immutable slot it is for.

``rt.source-duration-repair.v1`` (services/source_duration_repair.py) is a
self-describing command: it names its own slot, page, source and evidence. A
schema-valid command is therefore not authority by itself; any caller holding
the bearer could send one for a slot that never asked for a cut.

The slot is the authority. A slot that is to be cut carries a hash-free trim
policy, ``source_trim``, in its immutable payload: the sound, the trim, the
clean interval, the reference evidence and the recipe the clip was made under,
and nothing that names the slot or its hash (the command carries the slot hash, so the slot cannot carry the
command). The Worker derives the command from the slot; this module checks
that derivation:

* every identity in the command equals the outer request, which is already
  bound to the slot (slot id and payload hash, page, program, device, account,
  processed source, renderer);
* the command's sound is the slot's sound and its format is the slot's format;
* the command's trim, evidence and recipe fields are the slot's
  ``source_trim``, value for value;
* its font is the slot's caption font and that font file's own digest;
* its expected frames are exactly its duration on the 30 fps delivery clock.

A slot with ``source_trim`` and no command, or a command with no
``source_trim``, is refused: there is never a silent untrimmed render.

Not bound here, because neither the slot payload nor the request carries it:
``owner_id``. It stays inside the command digest the receipt records.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SOURCE_TRIM_POLICY_SCHEMA = "rt.source-trim-policy.v1"
DELIVERY_FPS = 30
FONT_DIRECTORY = Path(__file__).resolve().parents[1] / "fonts"

# Command fields that repeat the outer request, which the submission has
# already bound to the hashed slot payload.
REQUEST_IDENTITY_KEYS = (
    "slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial",
    "account", "source_sha256", "renderer_id", "renderer_version",
)
# Command fields that describe the cut, its evidence and the clip's recipe: the
# slot's policy.
TRIM_POLICY_KEYS = (
    "approved_music_id", "compressed_reference_sha256", "cue_evidence_end_ms",
    "cue_evidence_id", "cue_evidence_sha256", "decoded_reference_sha256",
    "expected_output_duration_ms", "expected_output_frames",
    "measured_quantization_ms", "native_official_start_ms", "official_end_ms",
    "official_start_ms", "opening_evidence_end_ms", "opening_evidence_id",
    "opening_evidence_sha256", "recipe_id", "recipe_version",
    "retimed_lyric_cue_sha256", "sample_clock_hz", "source_trim_end_ms",
    "source_trim_start_ms", "tail_evidence_end_ms", "tail_evidence_id",
    "tail_evidence_sha256",
)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def trim_policy_of(command: dict) -> dict:
    """The hash-free policy a slot must carry for this command."""
    return {"schema": SOURCE_TRIM_POLICY_SCHEMA, **{key: command[key] for key in TRIM_POLICY_KEYS}}


def font_file_sha256(font_id: object) -> str | None:
    """SHA-256 of the Lab's own font file of that name, or None if it has none."""
    if not isinstance(font_id, str) or not font_id or "/" in font_id or "\\" in font_id or font_id.startswith("."):
        return None
    path = FONT_DIRECTORY / font_id
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bind_repair_command(command: dict | None, slot: dict, request) -> None:
    """Raise ValueError unless the command is exactly what this slot asks for."""
    has_policy = "source_trim" in slot
    if command is None:
        if has_policy:
            raise ValueError("slot carries a source trim but the request has no repair command")
        return
    if not has_policy or not isinstance(slot["source_trim"], dict):
        raise ValueError("repair command is not backed by a source trim in the immutable slot")
    for key in REQUEST_IDENTITY_KEYS:
        if command[key] != getattr(request, key):
            raise ValueError(f"repair command {key} does not match the render request")
    sound = slot.get("sound")
    if not isinstance(sound, dict) or command["approved_music_id"] != sound.get("music_id"):
        raise ValueError("repair command sound does not match the immutable slot sound")
    if command["format_id"] != slot.get("format"):
        raise ValueError("repair command format does not match the immutable slot")
    if _canonical(slot["source_trim"]) != _canonical(trim_policy_of(command)):
        raise ValueError("repair command trim, evidence or recipe does not match the immutable slot source trim")
    treatment = slot.get("render_treatment")
    style = treatment.get("captionStyle") if isinstance(treatment, dict) else None
    font = style.get("font") if isinstance(style, dict) else None
    if command["font_id"] != font or command["font_sha256"] != font_file_sha256(font):
        raise ValueError("repair command font is not the slot caption font and its file digest")
    if command["expected_output_frames"] * 1000 != command["expected_output_duration_ms"] * DELIVERY_FPS:
        raise ValueError("repair command frames are not its duration on the 30 fps delivery clock")


def repair_output_met(command: dict, decoded_video_frames, duration_ms) -> bool:
    """Exactly the commanded frames and duration; an upper bound is not enough."""
    return (type(decoded_video_frames) is int and decoded_video_frames == command["expected_output_frames"]
            and type(duration_ms) is int and duration_ms == command["expected_output_duration_ms"])
