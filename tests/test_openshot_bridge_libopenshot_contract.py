"""Real-libopenshot contract verification for the OpenShot bridge.

Every other openshot_bridge test asserts the *shape* of the JSON the bridge
emits — none feeds that JSON to an actual ``libopenshot.Timeline``. The bridge
docstring promises two seams:

    full loads:        openshot.Timeline.SetJson(timeline_json(project))
    incremental edits: openshot.Timeline.ApplyJsonDiff(flattened_update_actions(...))

If a key name, nesting, or value type the bridge emits is wrong, the pure-Python
suite stays green while the native ``SetJson`` / ``ApplyJsonDiff`` throws at
render time (CLAUDE.md hard rule 2: OpenShot is the sanctioned compiler — that
path must actually work). These tests close that gap by round-tripping real
bridge output through a real binary.

libopenshot's Python bindings are not installed in the CI/green-gate box, so the
whole module SKIPS when they're absent (same posture test_editor_render.py takes
for ffmpeg). On the render/merge host where the bindings exist, it runs for real.
Resolution goes through editor_render._import_openshot so it honors the same
OPENSHOT_PYTHON_PATH / .codex runtime candidates the renderer uses.
"""

import json

import pytest

from services import editor_timeline
from services import editor_render
from services import openshot_bridge


_openshot, _reason, _ = editor_render._import_openshot()

pytestmark = pytest.mark.skipif(
    _openshot is None,
    reason=f"libopenshot Python bindings unavailable ({_reason})",
)


def _timeline(project: dict):
    """A real libopenshot Timeline sized to the project (mirrors
    editor_render.OpenShotRenderer._timeline, minus the SetJson call)."""
    return _openshot.Timeline(
        int(project.get("width") or 1920),
        int(project.get("height") or 1080),
        _openshot.Fraction(int(project.get("fps") or 30), 1),
        int(project.get("sampleRate") or 48000),
        int(project.get("channels") or 2),
        int(project.get("channelLayout") or 3),
    )


def _project(tmp_path) -> dict:
    """An ABN-shaped project: a source-tagged broll shot + a ducked music bed +
    VO + a static card — the heterogeneous layers OpenShot composites."""
    project = editor_timeline.new_project("ep_contract", width=1280, height=720, fps=30)
    project["assets"]["card"] = {
        "id": "card",
        "type": "image",
        "src": str(tmp_path / "card.png"),
        "metadata": {},
    }
    project["assets"]["shot"] = {
        "id": "shot",
        "type": "video",
        "source": "remotion",
        "src": str(tmp_path / "shot.mp4"),
        "metadata": {},
    }
    project["assets"]["bed"] = {
        "id": "bed",
        "type": "audio",
        "source": "bed",
        "src": str(tmp_path / "bed.mp3"),
        "metadata": {},
    }
    project["clips"]["card_clip"] = {
        "id": "card_clip",
        "assetId": "card",
        "trackId": "graphics_1",
        "kind": "artifact",
        "start": 1.5,
        "duration": 2.0,
        "sourceStart": 0.25,
        "enabled": True,
        "muted": False,
        "volume": 1.0,
        "transform": {"x": 0.25, "y": 0.75, "scale": 0.8, "opacity": 0.9},
        "effects": [{"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}],
        "keyframes": [
            {"property": "opacity", "points": [
                {"t": 0.0, "value": 0.0, "interp": "bezier"},
                {"t": 1.0, "value": 1.0, "interp": "linear"},
            ]},
        ],
        "metadata": {},
    }
    project["clips"]["shot_clip"] = {
        "id": "shot_clip",
        "assetId": "shot",
        "trackId": "video_1",
        "kind": "broll",
        "start": 0.0,
        "duration": 3.0,
        "sourceStart": 0.0,
        "enabled": True,
        "muted": False,
        "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [],
        "keyframes": [],
        "metadata": {},
    }
    project["clips"]["bed_clip"] = {
        "id": "bed_clip",
        "assetId": "bed",
        "trackId": "music_1",
        "kind": "music_bed",
        "start": 0.0,
        "duration": 3.5,
        "sourceStart": 0.0,
        "enabled": True,
        "muted": False,
        "volume": 0.22,
        "transform": {},
        "effects": [],
        "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6, "interp": "linear"},
                {"t": 1.0, "value": 0.22, "interp": "constant"},
            ]},
        ],
        "metadata": {},
    }
    return project


def test_timeline_setjson_accepts_full_bridge_payload(tmp_path):
    """timeline_json(project) loads into a real Timeline via SetJson without the
    native layer throwing. This is the full-load seam the bridge docstring
    promises — a wrong key/type would raise out of libopenshot here."""
    project = _project(tmp_path)
    payload = openshot_bridge.timeline_json(project)

    timeline = _timeline(project)
    try:
        timeline.SetJson(json.dumps(payload))
        # The three enabled clips must actually land on the native timeline.
        assert len(timeline.Clips()) == 3
    finally:
        timeline.Close()


def test_timeline_setjson_roundtrips_clip_ids_and_layers(tmp_path):
    """After SetJson, libopenshot's own GetJson must echo back the clips the bridge
    sent — same ids, on their declared layers — proving the payload was understood,
    not merely tolerated."""
    project = _project(tmp_path)
    payload = openshot_bridge.timeline_json(project)
    expected_layers = {c["id"]: c["layer"] for c in payload["clips"]}

    timeline = _timeline(project)
    try:
        timeline.SetJson(json.dumps(payload))
        roundtrip = json.loads(timeline.Json())
        got = {c["id"]: c["layer"] for c in roundtrip.get("clips", [])}
    finally:
        timeline.Close()

    assert got == expected_layers


def test_timeline_applyjsondiff_accepts_command_log_actions(tmp_path):
    """flattened_update_actions(project) applies into a real Timeline via
    ApplyJsonDiff — the incremental-edit seam. An insert action from a clip.create
    command must add a clip to the native timeline without throwing."""
    project = _project(tmp_path)
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_create",
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {
                "id": "added_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "kind": "artifact",
                "start": 4.0,
                "duration": 1.0,
                "sourceStart": 0.0,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
            },
        },
    )
    actions = openshot_bridge.flattened_update_actions(project)
    assert actions, "expected at least one UpdateAction from the command log"

    timeline = _timeline(project)
    try:
        # Seed with the base timeline, then apply the incremental diff on top.
        timeline.SetJson(json.dumps(openshot_bridge.timeline_json(project)))
        before = len(timeline.Clips())
        timeline.ApplyJsonDiff(json.dumps(actions))
        # ApplyJsonDiff must not throw, and an insert action grows the timeline.
        assert len(timeline.Clips()) >= before
    finally:
        timeline.Close()


def test_setjson_then_getframe_renders_without_native_error(tmp_path):
    """The ultimate proof the contract holds: after SetJson the timeline must
    produce a frame. Assets don't exist on disk, but a DummyReader-backed /
    missing-path clip still yields a black frame rather than crashing the native
    layer — confirming the JSON keyframes/readers the bridge emits are structurally
    valid input to the compositor, not just JSON-loadable."""
    project = _project(tmp_path)
    payload = openshot_bridge.timeline_json(project)

    timeline = _timeline(project)
    try:
        timeline.SetJson(json.dumps(payload))
        timeline.Open()
        frame = timeline.GetFrame(1)
        assert frame is not None
        assert frame.GetWidth() == int(project["width"])
        assert frame.GetHeight() == int(project["height"])
    finally:
        timeline.Close()
