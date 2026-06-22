import json
import subprocess
from pathlib import Path

import pytest

from services import editor_timeline
from services import openshot_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent


def _project(asset_path: Path | str) -> dict:
    project = editor_timeline.new_project("ep_real", width=1920, height=1080, fps=30)
    project["assets"]["card"] = {
        "id": "card",
        "type": "image",
        "src": str(asset_path),
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
        "effects": [],
        "keyframes": [],
        "metadata": {},
    }
    return project


def test_editor_project_exports_openshot_timeline_json(tmp_path):
    project = _project(tmp_path / "card.png")

    exported = openshot_bridge.timeline_json(project)

    assert exported["type"] == "Timeline"
    assert exported["width"] == 1920
    assert exported["height"] == 1080
    assert exported["fps"] == {"num": 30, "den": 1}
    assert exported["duration"] == 3.5
    assert len(exported["clips"]) == 1

    clip = exported["clips"][0]
    assert clip["id"] == "card_clip"
    assert clip["position"] == 1.5
    assert clip["start"] == 0.25
    assert clip["end"] == 2.25
    assert clip["layer"] == 20
    assert clip["reader"]["type"] == "QtImageReader"
    assert clip["reader"]["path"].endswith("card.png")
    assert clip["scale_x"]["Points"][0]["co"]["Y"] == 0.8
    assert clip["location_x"]["Points"][0]["co"]["Y"] == -0.5
    assert clip["location_y"]["Points"][0]["co"]["Y"] == 0.5
    assert clip["alpha"]["Points"][0]["co"]["Y"] == 0.9


def test_openshot_export_resolves_agenticnews_asset_urls(tmp_path):
    project = _project("/agenticnews-assets/card.png")

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)

    assert exported["clips"][0]["reader"]["path"] == str(tmp_path / "card.png")


def test_openshot_export_strips_cachebuster_query_from_agenticnews_url(tmp_path):
    # The editor persists render-cache URLs with a ?rev=N cache-buster (EditorBay). The static
    # mount ignores it, so the real file on disk is "card.png" — the compiler must resolve to that,
    # not "card.png?rev=3". This MUST agree with abn_factory's GC protection scan, or the GC
    # protects a render under a name the compiler can't open.
    project = _project("/agenticnews-assets/card.png?rev=3")

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)

    assert exported["clips"][0]["reader"]["path"] == str(tmp_path / "card.png")


def test_resolve_asset_src_strips_query_and_fragment_in_parity_with_gc_scan(tmp_path):
    # Direct unit on the single-source-of-truth resolver: query, fragment, and both together all
    # reduce to the same on-disk path the GC scan computes via .split("?")[0].split("#")[0].
    base = str(tmp_path / "episode.mp4")
    for url in (
        "/agenticnews-assets/episode.mp4",
        "/agenticnews-assets/episode.mp4?rev=3",
        "/agenticnews-assets/episode.mp4#frag",
        "/agenticnews-assets/episode.mp4?rev=3&foo=bar#frag",
    ):
        assert openshot_bridge._resolve_asset_src(url, asset_root=tmp_path) == base


def test_router_asset_resolvers_agree_with_bridge_on_cachebuster(tmp_path, monkeypatch):
    """The router's asset-health / render-cache resolvers feed the OpenShot compiler's
    "is this asset present?" decisions. The editor persists cache-buster URLs
    (".../episode.mp4?rev=3") on render-cache assets, and the bridge resolver strips
    them. If the router's resolvers did NOT strip, a present file would read missing and
    the compile would be falsely blocked (or the render cache read stale). Pin both
    router resolvers to the canonical bridge resolver so they can't drift apart."""
    import importlib

    agenticnews = importlib.import_module("routers.agenticnews")
    monkeypatch.setattr(agenticnews.db, "ASSETS_DIR", tmp_path)

    on_disk = tmp_path / "ep_x" / "renders" / "episode.mp4"
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(b"\x00\x01")

    bare = "/agenticnews-assets/ep_x/renders/episode.mp4"
    busted = bare + "?rev=3"
    expected = Path(openshot_bridge._resolve_asset_src(busted, asset_root=tmp_path))
    assert expected == on_disk  # bridge strips the ?rev= -> real file

    # _asset_path_from_url must resolve the cache-busted URL to the SAME real file...
    assert agenticnews._asset_path_from_url(busted) == on_disk
    assert agenticnews._asset_path_from_url(bare) == on_disk
    # ...and the render-cache existence check must see the busted URL as PRESENT.
    assert agenticnews._render_cache_path_exists(busted) is True
    assert agenticnews._render_cache_path_exists(bare) is True


def test_openshot_export_maps_editor_volume_to_openshot_percent(tmp_path):
    project = _project(tmp_path / "vo.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "audio_1"
    project["clips"]["card_clip"]["kind"] = "voiceover"
    project["clips"]["card_clip"]["volume"] = 1.0

    exported = openshot_bridge.timeline_json(project)

    clip = exported["clips"][0]
    assert clip["volume"]["Points"][0]["co"]["Y"] == 100.0
    assert clip["has_audio"]["Points"][0]["co"]["Y"] == 1
    assert clip["has_video"]["Points"][0]["co"]["Y"] == 0


def test_openshot_export_preserves_quiet_music_bed_volume(tmp_path):
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "music_1"
    project["clips"]["card_clip"]["kind"] = "music_bed"
    project["clips"]["card_clip"]["volume"] = 0.18

    exported = openshot_bridge.timeline_json(project)

    assert exported["clips"][0]["volume"]["Points"][0]["co"]["Y"] == 18.0


def test_muted_clip_with_volume_envelope_exports_silent(tmp_path):
    """A muted clip must render silent even when it carries a volume keyframe
    envelope. The envelope composes from the clip's true base volume (so an
    editor unmute animates from 1.0, not 0.0); mute must flatten the exported
    `volume` back to 0 so OpenShot's envelope math matches the timeline's muted
    state instead of resurrecting the audio."""
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "music_1"
    project["clips"]["card_clip"]["kind"] = "music_bed"
    project["clips"]["card_clip"]["volume"] = 1.0
    project["clips"]["card_clip"]["muted"] = True
    project["clips"]["card_clip"]["keyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 2.0, "value": 0.5, "interp": "linear"},
            ],
        }
    ]

    exported = openshot_bridge.timeline_json(project)

    points = exported["clips"][0]["volume"]["Points"]
    assert len(points) == 1
    assert points[0]["co"]["Y"] == 0.0
    # The keyframe envelope is preserved on the clip so an unmute restores it.
    assert project["clips"]["card_clip"]["keyframes"][0]["property"] == "volume"


def test_disabled_clip_with_opacity_envelope_exports_invisible_but_keeps_keyframes(tmp_path):
    """A disabled clip must export render-time invisible (flat alpha 0) even when
    it carries an opacity keyframe envelope — same shape as mute. The envelope
    must NOT clobber the disable (no resurrecting a hidden clip), and the clip's
    `keyframes` must survive untouched so re-enabling restores the animation.

    `timeline_json` drops disabled clips, so this checks `clip_json` directly —
    the serializer the command-log diff path emits for an individual clip."""
    project = _project(tmp_path / "card.png")
    clip = project["clips"]["card_clip"]
    clip["enabled"] = False
    clip["transform"]["opacity"] = 0.9
    clip["keyframes"] = [
        {
            "property": "opacity",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 2.0, "value": 0.5, "interp": "linear"},
            ],
        }
    ]

    payload = openshot_bridge.clip_json(project, clip)

    # Disable wins: alpha flattened to a single 0 point for the render.
    points = payload["alpha"]["Points"]
    assert len(points) == 1
    assert points[0]["co"]["Y"] == 0.0
    # The opacity envelope is preserved on the clip so re-enabling restores it.
    assert clip["keyframes"][0]["property"] == "opacity"
    assert len(clip["keyframes"][0]["points"]) == 2


def test_muted_and_disabled_clip_flattens_both_volume_and_alpha_keeping_both_envelopes(tmp_path):
    """The NEW composing contract: when a clip is BOTH muted AND disabled AND
    carries BOTH a volume and an opacity envelope, the two render-time flattens
    must coexist on the same payload — volume -> 0 and alpha -> 0 — while neither
    keyframe track is destroyed. The existing tests each exercise only ONE branch
    (mute->volume, disable->alpha); this pins the case where both fire together so
    a regression can't silently resurrect audio OR video on a hidden+silent clip.

    `timeline_json` drops disabled clips, so this drives `clip_json` directly."""
    project = _project(tmp_path / "bed.mp4")
    clip = project["clips"]["card_clip"]
    clip["assetId"] = "card"
    project["assets"]["card"]["type"] = "video"
    clip["enabled"] = False
    clip["muted"] = True
    clip["volume"] = 1.0
    clip["transform"]["opacity"] = 0.9
    clip["keyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 2.0, "value": 0.5, "interp": "linear"},
            ],
        },
        {
            "property": "opacity",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 2.0, "value": 0.5, "interp": "linear"},
            ],
        },
    ]

    payload = openshot_bridge.clip_json(project, clip)

    # Mute wins over the volume envelope: flat single 0 point.
    volume_points = payload["volume"]["Points"]
    assert len(volume_points) == 1
    assert volume_points[0]["co"]["Y"] == 0.0
    # Disable wins over the opacity envelope: flat single 0 point.
    alpha_points = payload["alpha"]["Points"]
    assert len(alpha_points) == 1
    assert alpha_points[0]["co"]["Y"] == 0.0
    # Both source envelopes survive untouched on the clip so unmute + re-enable
    # restore the original animations (the envelope re-composes from true base).
    surviving = {k["property"]: k["points"] for k in clip["keyframes"]}
    assert len(surviving["volume"]) == 2
    assert len(surviving["opacity"]) == 2


def test_openshot_export_duration_ignores_disabled_tail_clip(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["disabled_tail"] = {
        **project["clips"]["card_clip"],
        "id": "disabled_tail",
        "start": 100.0,
        "duration": 50.0,
        "enabled": False,
    }

    exported = openshot_bridge.timeline_json(project)

    assert exported["duration"] == 3.5
    assert [clip["id"] for clip in exported["clips"]] == ["card_clip"]


# ---------------------------------------------------------------------------
# ABN-import -> OpenShot seam. project_from_abn_timeline builds the project and
# the bridge translates it; each side is unit-tested, but the SEAM between them
# had zero coverage. A fidelity bug in the import path (music-bed ducking gain
# not carried, source tag lost, lower-third skipped) would survive both suites
# and only surface as a wrong-sounding/looking render. These pin the round-trip.
# ---------------------------------------------------------------------------


def _abn_timeline() -> dict:
    """A representative ABN episode: ducked music bed, VO, a source-tagged shot,
    and a text-only lower third — the heterogeneous layers OpenShot composites."""
    return {
        "episodeId": "ep_seam",
        "title": "Seam Episode",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {
                        "id": "shot_a",
                        "src": "/agenticnews-assets/ui.mp4",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "broll",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}},
                "lowerThirds": [
                    {"startSec": 0.5, "durationSec": 2.5, "headline": "Hook lower"}
                ],
            }
        ],
    }


def test_abn_imported_timeline_exports_openshot_json_with_ducked_bed_volume(tmp_path):
    """Import an ABN timeline, then run it through the OpenShot bridge. The
    music-bed ducking gain (0.22, the factory convention) must survive the seam
    as OpenShot volume 22.0; the VO stays at full 100.0. A flattened/dropped
    ducking gain here silently mixes music OVER the VO at render time."""
    project = editor_timeline.project_from_abn_timeline(
        "proj_seam", _abn_timeline(), source_episode_id="ep_seam"
    )

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)

    by_id = {c["id"]: c for c in exported["clips"]}
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    vo = next(c for c in project["clips"].values() if c["kind"] == "voiceover")
    # 0.22 editor gain -> 22.0 OpenShot percent; full VO -> 100.0
    assert by_id[bed["id"]]["volume"]["Points"][0]["co"]["Y"] == 22.0
    assert by_id[vo["id"]]["volume"]["Points"][0]["co"]["Y"] == 100.0


def test_abn_imported_timeline_keeps_source_tag_and_layer_ordering(tmp_path):
    """The broll shot's production `source` resolves to a video FFmpegReader (not
    an image reader), the bed/VO resolve to audio readers, and clips export sorted
    by track layer index — so the OpenShot timeline stacks layers correctly."""
    project = editor_timeline.project_from_abn_timeline(
        "proj_seam2", _abn_timeline(), source_episode_id="ep_seam"
    )

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    by_id = {c["id"]: c for c in exported["clips"]}

    broll = next(c for c in project["clips"].values() if c["kind"] == "broll")
    assert by_id[broll["id"]]["reader"]["type"] == "FFmpegReader"
    assert by_id[broll["id"]]["has_video"]["Points"][0]["co"]["Y"] == 1
    assert by_id[broll["id"]]["reader"]["path"].endswith("ui.mp4")

    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    assert by_id[bed["id"]]["reader"]["type"] == "FFmpegReader"
    assert by_id[bed["id"]]["has_audio"]["Points"][0]["co"]["Y"] == 1

    # A text-only lower third (no media src) falls back to a DummyReader, not a
    # phantom path that would crash libopenshot opening it.
    lower = next(c for c in project["clips"].values() if c["kind"] == "lower_third")
    assert by_id[lower["id"]]["reader"]["type"] == "DummyReader"

    # clips export sorted by track layer index (video_1=10 < graphics < titles_1=30
    # < audio_1=40 < music_1=50): the bed (music_1) is the last clip.
    layers = [c["layer"] for c in exported["clips"]]
    assert layers == sorted(layers)


def test_abn_imported_bed_keyframe_ducking_envelope_survives_the_seam(tmp_path):
    """The import lands a FLAT 0.22 bed gain today; the ponytail comment notes a
    keyframed ducking envelope can replace it later. Simulate that upgrade by
    attaching a volume envelope to the imported bed and assert the bridge carries
    it as a multi-Point OpenShot keyframe (not the flat default) — so the import
    path is future-proof for an envelope, not just a flat gain."""
    project = editor_timeline.project_from_abn_timeline(
        "proj_seam3", _abn_timeline(), source_episode_id="ep_seam"
    )
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    bed["keyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 0.6, "interp": "linear"},
                {"t": 1.0, "value": 0.22, "interp": "constant"},
                {"t": 3.0, "value": 0.6, "interp": "linear"},
            ],
        }
    ]

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    by_id = {c["id"]: c for c in exported["clips"]}
    points = by_id[bed["id"]]["volume"]["Points"]

    # three points (the envelope), not one (the flat default), scaled 0..1 -> 0..100
    assert [round(p["co"]["Y"], 2) for p in points] == [60.0, 22.0, 60.0]
    assert points[1]["interpolation"] == openshot_bridge.CONSTANT


def test_abn_musicBedKeyframes_promotes_envelope_through_the_import_seam(tmp_path):
    """The factory hands the import a `musicBedKeyframes` ducking envelope. The
    IMPORT path (project_from_abn_timeline) — not a post-import hand-attach — must
    promote it onto the bed clip so the bridge animates it. Pins the untested seam:
    ABN.musicBedKeyframes -> bed clip.keyframes -> OpenShot multi-Point volume."""
    timeline = _abn_timeline()
    timeline["musicBedKeyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 0.6, "interp": "linear"},
                {"t": 0.5, "value": 0.22, "interp": "constant"},
                {"t": 3.5, "value": 0.6, "interp": "linear"},
            ],
        }
    ]
    project = editor_timeline.project_from_abn_timeline(
        "proj_seam_kf", timeline, source_episode_id="ep_seam"
    )

    # the import promoted the envelope onto the bed clip (not the flat 0.22 gain)
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    assert bed.get("keyframes"), "import dropped musicBedKeyframes onto the floor"

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    by_id = {c["id"]: c for c in exported["clips"]}
    points = by_id[bed["id"]]["volume"]["Points"]

    assert [round(p["co"]["Y"], 2) for p in points] == [60.0, 22.0, 60.0]
    assert points[1]["interpolation"] == openshot_bridge.CONSTANT


def test_abn_dict_musicBed_keyframes_promote_through_the_import_seam(tmp_path):
    """A dict-shaped bed `{src, keyframes}` is the other factory wire-format. The
    import must read its `keyframes` envelope (and still resolve `src` as the bed
    asset) so the bridge animates the ducking — not silently flatten to 0.22."""
    timeline = _abn_timeline()
    timeline["musicBed"] = {
        "src": "/agenticnews-assets/bed.mp3",
        "keyframes": [
            {
                "property": "volume",
                "points": [
                    {"t": 0.0, "value": 0.5, "interp": "linear"},
                    {"t": 2.0, "value": 0.22, "interp": "linear"},
                ],
            }
        ],
    }
    project = editor_timeline.project_from_abn_timeline(
        "proj_seam_kf2", timeline, source_episode_id="ep_seam"
    )

    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    assert bed.get("keyframes"), "import dropped dict-bed keyframes onto the floor"

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    by_id = {c["id"]: c for c in exported["clips"]}
    clip = by_id[bed["id"]]
    # src still resolves to the bed audio reader, and the envelope animates volume
    assert clip["reader"]["path"].endswith("bed.mp3")
    assert [round(p["co"]["Y"], 2) for p in clip["volume"]["Points"]] == [50.0, 22.0]


def test_abn_shot_ducking_envelope_reaches_openshot_via_bridge(tmp_path):
    """A factory-attached shot ducking envelope (duckingKeyframes / keyframesEnvelope)
    must survive the IMPORT path and reach OpenShot through the bridge — not just the
    ffmpeg fallback. The music-bed seam is covered, but a SHOT's separate-field
    envelope had no bridge coverage: a regression in _shot_keyframes would silently
    flatten the ducking and only surface as a wrong-sounding OpenShot render."""
    for field in ("duckingKeyframes", "keyframesEnvelope"):
        timeline = _abn_timeline()
        timeline["segments"][0]["shots"][0][field] = [
            {
                "property": "volume",
                "points": [
                    {"t": 0.0, "value": 0.7, "interp": "linear"},
                    {"t": 1.0, "value": 0.22, "interp": "constant"},
                ],
            }
        ]
        project = editor_timeline.project_from_abn_timeline(
            "proj_shot_duck", timeline, source_episode_id="ep_seam"
        )
        broll = next(c for c in project["clips"].values() if c["kind"] == "broll")
        assert broll.get("keyframes"), f"import dropped shot {field} onto the floor"

        exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)
        by_id = {c["id"]: c for c in exported["clips"]}
        points = by_id[broll["id"]]["volume"]["Points"]
        # two-Point envelope (0.7 -> 0.22), scaled 0..1 -> 0..100, not the flat default
        assert [round(p["co"]["Y"], 2) for p in points] == [70.0, 22.0], field
        assert points[1]["interpolation"] == openshot_bridge.CONSTANT, field


def test_abn_shot_explicit_keyframes_beat_envelope_through_openshot_bridge(tmp_path):
    """When the SAME shot ships both an explicit `keyframes` track AND a separate
    `duckingKeyframes` envelope for the same property, the explicit track wins (the
    documented _shot_keyframes precedence) — and that precedence must hold all the way
    through to the OpenShot clip JSON, while an envelope-only property still passes."""
    timeline = _abn_timeline()
    timeline["segments"][0]["shots"][0]["keyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 0.9, "interp": "linear"},
                {"t": 1.0, "value": 0.5, "interp": "linear"},
            ],
        }
    ]
    timeline["segments"][0]["shots"][0]["duckingKeyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 0.22, "interp": "linear"},
            ],
        },
        {
            "property": "opacity",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 0.0, "interp": "linear"},
            ],
        },
    ]
    project = editor_timeline.project_from_abn_timeline(
        "proj_shot_prio", timeline, source_episode_id="ep_seam"
    )
    broll = next(c for c in project["clips"].values() if c["kind"] == "broll")

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    by_id = {c["id"]: c for c in exported["clips"]}
    clip = by_id[broll["id"]]

    # explicit volume (0.9 -> 0.5 => 90.0/50.0) wins over the envelope's 1.0 -> 0.22
    assert [round(p["co"]["Y"], 2) for p in clip["volume"]["Points"]] == [90.0, 50.0]
    # envelope-only opacity still reaches OpenShot as an animated alpha track
    assert [round(p["co"]["Y"], 2) for p in clip["alpha"]["Points"]] == [1.0, 0.0]


def test_command_log_exports_openshot_apply_json_diff(tmp_path):
    project = _project(tmp_path / "card.png")
    command = {
        "id": "cmd_transform",
        "op": "clip.transform",
        "actor": "test",
        "expectedRevision": 0,
        "payload": {
            "clipId": "card_clip",
            "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        },
    }
    project = editor_timeline.apply_command(project, command)

    actions = openshot_bridge.flattened_update_actions(project)

    assert actions == [
        {
            "type": "update",
            "key": ["clips", {"id": "card_clip"}],
            "value": actions[0]["value"],
            "old_values": actions[0]["old_values"],
            "transaction": "cmd_transform",
        }
    ]
    assert actions[0]["value"]["location_x"]["Points"][0]["co"]["Y"] == 0.0
    assert actions[0]["value"]["location_y"]["Points"][0]["co"]["Y"] == 0.0
    assert actions[0]["value"]["scale_x"]["Points"][0]["co"]["Y"] == 1.0
    assert actions[0]["old_values"]["scale_x"]["Points"][0]["co"]["Y"] == 0.8

    diff = json.loads(openshot_bridge.json_diff(project))
    assert diff[0]["type"] == "update"
    assert diff[0]["key"] == ["clips", {"id": "card_clip"}]


def test_clip_keyframes_command_exports_update_action_with_envelope(tmp_path):
    """A clip.keyframes command must emit an OpenShot update action whose value
    carries the new multi-Point envelope — the bridge previously relied on a
    bare `clip.` catch-all that would silently drop the edit if after["clip"]
    were ever absent."""
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    command = {
        "id": "cmd_keyframes",
        "op": "clip.keyframes",
        "actor": "test",
        "expectedRevision": 0,
        "payload": {
            "clipId": "card_clip",
            "keyframes": [
                {
                    "property": "volume",
                    "points": [
                        {"t": 0.0, "value": 1.0, "interp": "linear"},
                        {"t": 1.0, "value": 0.22, "interp": "constant"},
                    ],
                }
            ],
        },
    }
    project = editor_timeline.apply_command(project, command)

    actions = openshot_bridge.flattened_update_actions(project)

    assert len(actions) == 1
    action = actions[0]
    assert action["type"] == "update"
    assert action["key"] == ["clips", {"id": "card_clip"}]
    assert action["transaction"] == "cmd_keyframes"
    points = action["value"]["volume"]["Points"]
    assert [round(p["co"]["Y"], 2) for p in points] == [100.0, 22.0]


def test_clip_keyframes_action_survives_missing_after_clip(tmp_path):
    """Regression guard: if a clip.keyframes log entry carries the envelope only
    in its payload (no full after["clip"]), the bridge must still emit the update
    by overlaying the payload keyframes onto the before clip — not return None."""
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    entry = {
        "id": "cmd_kf_payload",
        "op": "clip.keyframes",
        "payload": {
            "clipId": "card_clip",
            "keyframes": [
                {
                    "property": "volume",
                    "points": [
                        {"t": 0.0, "value": 1.0, "interp": "linear"},
                        {"t": 1.0, "value": 0.5, "interp": "linear"},
                    ],
                }
            ],
        },
        "before": {"clip": project["clips"]["card_clip"]},
        "after": {},
    }

    action = openshot_bridge.update_action_from_command(project, entry)

    assert action is not None
    assert action["type"] == "update"
    assert action["key"] == ["clips", {"id": "card_clip"}]
    points = action["value"]["volume"]["Points"]
    assert [round(p["co"]["Y"], 2) for p in points] == [100.0, 50.0]


def test_clip_keyframes_orphaned_command_with_no_clip_target_returns_none(tmp_path):
    """An orphaned clip.keyframes entry — payload carries keyframes but NEITHER
    after["clip"] NOR before["clip"] resolves a target (envelope lost on the way
    to the log) — must drop to None safely, not raise. There is no clip to apply
    the envelope to, so emitting an UpdateAction would target a phantom id. Covers
    every shape of "no target": both blocks absent, both empty, and an empty
    before["clip"]."""
    project = _project(tmp_path / "bed.wav")
    payload = {
        "clipId": "card_clip",
        "keyframes": [
            {
                "property": "volume",
                "points": [{"t": 0.0, "value": 1.0, "interp": "linear"}],
            }
        ],
    }

    # after/before entirely absent
    assert (
        openshot_bridge.update_action_from_command(
            project, {"id": "kf1", "op": "clip.keyframes", "payload": payload}
        )
        is None
    )
    # after/before present but empty (no "clip" key)
    assert (
        openshot_bridge.update_action_from_command(
            project,
            {"id": "kf2", "op": "clip.keyframes", "payload": payload, "after": {}, "before": {}},
        )
        is None
    )
    # before["clip"] present but falsy (e.g. None) — still no usable target
    assert (
        openshot_bridge.update_action_from_command(
            project,
            {
                "id": "kf3",
                "op": "clip.keyframes",
                "payload": payload,
                "after": {"clip": None},
                "before": {"clip": None},
            },
        )
        is None
    )


def test_clip_keyframe_envelope_translates_to_multipoint_openshot_keyframes(tmp_path):
    """A volume-ducking envelope on the clip becomes a multi-Point OpenShot
    keyframe (frame X = t*fps+1, Y scaled to 0..100), overriding the flat default."""
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "music_1"
    project["clips"]["card_clip"]["kind"] = "music_bed"
    project["clips"]["card_clip"]["keyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 0.22, "interp": "constant"},
                {"t": 2.0, "value": 1.0, "interp": "linear"},
            ],
        }
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    points = clip["volume"]["Points"]
    # X = (sourceStart 0.25 + t) * 30fps + 1 — trimmed clips offset by sourceStart
    assert [p["co"]["X"] for p in points] == [8.5, 38.5, 68.5]
    assert [round(p["co"]["Y"], 2) for p in points] == [100.0, 22.0, 100.0]  # 0..1 -> 0..100
    assert points[0]["interpolation"] == openshot_bridge.LINEAR
    assert points[1]["interpolation"] == openshot_bridge.CONSTANT


def test_keyframe_X_offsets_by_sourceStart_for_trimmed_clips(tmp_path):
    """A keyframe at editor t maps to source frame (sourceStart + t)*fps + 1.
    A clip trimmed to sourceStart=5.0 must fire its t=1.0 keyframe at frame 181,
    not 31, so the envelope lands at the right source frame."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 5.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # (5.0 + 0.0)*30 + 1 = 151 ; (5.0 + 1.0)*30 + 1 = 181
    assert [p["co"]["X"] for p in clip["alpha"]["Points"]] == [151.0, 181.0]


def test_keyframe_X_with_no_trim_keeps_plain_t_times_fps(tmp_path):
    """sourceStart=0 (untrimmed) keeps the original t*fps + 1 mapping."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 0.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [p["co"]["X"] for p in clip["alpha"]["Points"]] == [1.0, 31.0]


def test_clip_keyframe_opacity_and_scale_map_to_alpha_and_both_scale_axes(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.0}, {"t": 0.5, "value": 1.0}]},
        {"property": "scale", "points": [{"t": 0.0, "value": 1.0}, {"t": 0.5, "value": 1.5}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [p["co"]["Y"] for p in clip["alpha"]["Points"]] == [0.0, 1.0]
    # scale animates BOTH axes from one editor track
    assert [p["co"]["Y"] for p in clip["scale_x"]["Points"]] == [1.0, 1.5]
    assert clip["scale_y"]["Points"] == clip["scale_x"]["Points"]


def test_empty_keyframes_keep_the_flat_default_keyframe(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = []

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert clip["scale_x"]["Points"] == [{"co": {"X": 1.0, "Y": 0.8}, "interpolation": openshot_bridge.CONSTANT}]


def test_clip_keyframe_position_and_rotation_envelopes_and_bezier_interp(tmp_path):
    """x/y position envelopes go through the same 0..1 -> -1..1 _location transform
    as the flat default, rotation passes through, and a `bezier` interp point maps
    to OpenShot's BEZIER code (0). These props + interp had no envelope coverage —
    a regression in the per-property transform or interp map would only bite at
    render time."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "x", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
        {"property": "y", "points": [{"t": 0.0, "value": 0.5, "interp": "bezier"}]},
        {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 90.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # x: 0.0 -> -1.0 (left edge), 1.0 -> 1.0 (right edge); frames are
    # (sourceStart 0.25 + t)*30 + 1 — trimmed clips offset keyframe X by sourceStart
    assert [p["co"]["X"] for p in clip["location_x"]["Points"]] == [8.5, 38.5]
    assert [p["co"]["Y"] for p in clip["location_x"]["Points"]] == [-1.0, 1.0]
    # y: 0.5 -> 0.0 (center), single bezier point. Assert the concrete BEZIER code
    # (0), NOT _INTERPOLATION_MAP["bezier"] — the latter is tautological and would
    # pass even if bezier silently fell through to LINEAR's default. Bezier MUST be
    # the named BEZIER constant and MUST be distinct from LINEAR, or smooth ducking
    # curves from abn_factory linearize at render.
    assert clip["location_y"]["Points"][0]["co"]["Y"] == 0.0
    assert openshot_bridge.BEZIER == 0
    assert clip["location_y"]["Points"][0]["interpolation"] == openshot_bridge.BEZIER
    assert clip["location_y"]["Points"][0]["interpolation"] != openshot_bridge.LINEAR
    # rotation passes through untransformed
    assert [p["co"]["Y"] for p in clip["rotation"]["Points"]] == [0.0, 90.0]


def test_clip_effects_translate_to_openshot_effect_objects(tmp_path):
    """A clip's editor effects become libopenshot Effect JSON objects on the clip:
    fades map to the Fade class with a direction + keyframed duration; color filters
    map to their own effect class. Empty effects stay an empty list."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}},
        {"id": "fx2", "type": "brightness", "params": {"value": -0.3}},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [e["id"] for e in clip["effects"]] == ["fx1", "fx2"]
    fade, bright = clip["effects"]
    assert fade["type"] == "Fade"
    assert fade["fade"] == "in"
    assert fade["duration"]["Points"][0]["co"]["Y"] == 0.5
    assert bright["type"] == "Brightness"
    assert bright["brightness"]["Points"][0]["co"]["Y"] == -0.3


def test_clip_with_no_effects_exports_empty_effects_list(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = []

    assert openshot_bridge.timeline_json(project)["clips"][0]["effects"] == []


def test_crossfade_transition_exports_as_inward_fade(tmp_path):
    """A crossfade transition carries into OpenShot as the Fade class with an
    inward direction and a keyframed duration — the clip-boundary compositing
    that the abn_factory music-bed ducking / cut-stitching depends on."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fxX", "type": "crossfade", "params": {"duration": 0.75}},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    (xfade,) = clip["effects"]
    assert xfade["id"] == "fxX"
    assert xfade["type"] == "Fade"
    assert xfade["fade"] == "in"
    assert xfade["duration"]["Points"][0]["co"]["Y"] == 0.75


def test_saturation_effect_exports_as_saturation_class(tmp_path):
    """Saturation is the second color filter in the closed effect vocabulary; it
    maps to its own OpenShot Saturation class with a keyframed value."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = [
        {"id": "sat1", "type": "saturation", "params": {"value": 1.4}},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    (sat,) = clip["effects"]
    assert sat["type"] == "Saturation"
    assert sat["saturation"]["Points"][0]["co"]["Y"] == 1.4


def test_keyframe_position_and_rotation_envelopes_carry_their_transforms(tmp_path):
    """An x/y position envelope animates location_x/location_y through the same
    centered ((v-0.5)*2) transform the flat default uses, and a rotation envelope
    animates rotation 1:1 — so a panning/spinning clip round-trips into OpenShot."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "x", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
        {"property": "y", "points": [{"t": 0.0, "value": 0.5}, {"t": 1.0, "value": 0.25}]},
        {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 90.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # x: 0.0 -> -1.0, 1.0 -> 1.0  (centered then doubled)
    assert [p["co"]["Y"] for p in clip["location_x"]["Points"]] == [-1.0, 1.0]
    # y: 0.5 -> 0.0, 0.25 -> -0.5
    assert [p["co"]["Y"] for p in clip["location_y"]["Points"]] == [0.0, -0.5]
    # rotation passes through unchanged
    assert [p["co"]["Y"] for p in clip["rotation"]["Points"]] == [0.0, 90.0]
    # frame X = (sourceStart 0.25 + t)*fps + 1 for every envelope (trimmed-clip offset)
    assert [p["co"]["X"] for p in clip["location_x"]["Points"]] == [8.5, 38.5]


def test_all_six_keyframe_properties_compose_on_one_clip_without_clobbering(tmp_path):
    """Every editor keyframe property animated at once must land on its OWN
    OpenShot key with its own transform — volume->volume, opacity->alpha,
    scale->scale_x+scale_y, x->location_x, y->location_y, rotation->rotation —
    and an un-keyframed channel (shear_x) must keep its flat default. This is the
    "do they compose?" gap: a future regression that lets one envelope overwrite
    another's key, or that drops an un-keyframed default, only shows at render."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 0.0  # keep frame math = t*fps+1
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "volume", "points": [{"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.5}]},
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.2}, {"t": 1.0, "value": 0.8}]},
        {"property": "scale", "points": [{"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 2.0}]},
        {"property": "x", "points": [{"t": 0.0, "value": 0.5}, {"t": 1.0, "value": 1.0}]},
        {"property": "y", "points": [{"t": 0.0, "value": 0.5}, {"t": 1.0, "value": 0.0}]},
        {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 45.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # Each envelope is independent: distinct Y transforms, two points each.
    assert [p["co"]["Y"] for p in clip["volume"]["Points"]] == [100.0, 50.0]  # 0..1 -> 0..100
    assert [p["co"]["Y"] for p in clip["alpha"]["Points"]] == [0.2, 0.8]
    assert [p["co"]["Y"] for p in clip["scale_x"]["Points"]] == [1.0, 2.0]
    assert clip["scale_y"]["Points"] == clip["scale_x"]["Points"]  # scale drives both axes
    assert [p["co"]["Y"] for p in clip["location_x"]["Points"]] == [0.0, 1.0]  # 0.5->0, 1.0->1
    assert [p["co"]["Y"] for p in clip["location_y"]["Points"]] == [0.0, -1.0]  # 0.5->0, 0.0->-1
    assert [p["co"]["Y"] for p in clip["rotation"]["Points"]] == [0.0, 45.0]
    # All envelopes share the same frame grid; none clobbered another's X.
    for key in ("volume", "alpha", "scale_x", "scale_y", "location_x", "location_y", "rotation"):
        assert [p["co"]["X"] for p in clip[key]["Points"]] == [1.0, 31.0], key
    # An un-keyframed channel keeps its flat single-point default.
    assert clip["shear_x"]["Points"] == [
        {"co": {"X": 1.0, "Y": 0.0}, "interpolation": openshot_bridge.CONSTANT}
    ]


def test_single_point_envelope_replaces_flat_default_with_one_dynamic_point(tmp_path):
    """A one-point keyframe track still overrides the flat static default: the
    output is a single Point carrying the transformed value and declared interp,
    not the CONSTANT flat default. Pins the degenerate single-keyframe case."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 0.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.5, "value": 0.4, "interp": "linear"}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert clip["alpha"]["Points"] == [
        {"co": {"X": 0.5 * 30 + 1.0, "Y": 0.4}, "interpolation": openshot_bridge.LINEAR}
    ]


def test_zero_duration_clip_with_keyframes_still_translates_envelope(tmp_path):
    """A zero-duration clip must not blow up keyframe translation. duration floors
    to 0.001 for the reader, but the envelope frame math (source_start + t)*fps + 1
    is independent of duration, so a t=0 keyframe still lands at frame 1."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 0.0
    project["clips"]["card_clip"]["duration"] = 0.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 1.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert clip["alpha"]["Points"] == [
        {"co": {"X": 1.0, "Y": 1.0}, "interpolation": openshot_bridge.LINEAR}
    ]
    # duration floored so the clip is still renderable, not zero-length.
    assert clip["duration"] >= 0.001


def test_bezier_interp_mixes_with_linear_constant_in_one_multipoint_envelope(tmp_path):
    """bezier was only ever tested as a lone single point. A real ease curve mixes
    interps across a multi-point track; each point must carry its OWN interp code
    (bezier->0, linear->1, constant->2) — a regression in the interp map per-point
    lookup would silently flatten the easing."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 0.0
    project["clips"]["card_clip"]["keyframes"] = [
        {
            "property": "scale",
            "points": [
                {"t": 0.0, "value": 0.0, "interp": "bezier"},
                {"t": 1.0, "value": 1.0, "interp": "linear"},
                {"t": 2.0, "value": 1.0, "interp": "constant"},
            ],
        }
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [p["interpolation"] for p in clip["scale_x"]["Points"]] == [
        openshot_bridge._INTERPOLATION_MAP["bezier"],
        openshot_bridge.LINEAR,
        openshot_bridge.CONSTANT,
    ]
    assert openshot_bridge._INTERPOLATION_MAP["bezier"] == 0  # BEZIER sentinel, not linear
    # scale_y mirrors scale_x interp-for-interp, not just value-for-value.
    assert clip["scale_y"]["Points"] == clip["scale_x"]["Points"]


def test_negative_source_start_offsets_keyframe_frames_below_one(tmp_path):
    """sourceStart math at the envelope frame line is unguarded: a negative
    sourceStart (e.g. lead-in handle) shifts keyframe X below 1.0 by the same
    (source_start + t)*fps + 1 rule. Pin it so the framerate edge case is explicit
    rather than an accidental crash or clamp later."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = -1.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 2.0, "value": 10.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # (-1.0 + 0.0)*30 + 1 = -29 ; (-1.0 + 2.0)*30 + 1 = 31
    assert [p["co"]["X"] for p in clip["rotation"]["Points"]] == [-29.0, 31.0]


def test_effect_add_command_exports_update_action_with_effects(tmp_path):
    """clip.effect.add flows through the generic clip.* update path: the resulting
    OpenShot UpdateAction carries the full clip JSON with the new effects array."""
    project = _project(tmp_path / "card.png")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_fx",
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip", "effect": {"id": "fx1", "type": "fadeOut", "params": {"duration": 1.0}}},
        },
    )

    actions = openshot_bridge.flattened_update_actions(project)
    fx_action = next(a for a in actions if a["transaction"] == "cmd_fx")
    assert fx_action["type"] == "update"
    assert fx_action["key"] == ["clips", {"id": "card_clip"}]
    effects = fx_action["value"]["effects"]
    assert effects[0]["type"] == "Fade"
    assert effects[0]["fade"] == "out"


def test_unsplit_exports_update_and_delete_actions(tmp_path):
    project = _project(tmp_path / "card.png")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {
                "clipId": "card_clip",
                "at": 2.5,
                "newClipId": "card_clip_b",
            },
        },
    )
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_unsplit",
            "op": "clip.unsplit",
            "actor": "human",
            "expectedRevision": 1,
            "revertsCommandId": "cmd_split",
            "payload": {
                "clipId": "card_clip",
                "createdClipId": "card_clip_b",
                "clip": project["commandLog"][0]["before"]["clip"],
            },
        },
    )

    actions = openshot_bridge.flattened_update_actions(project)
    unsplit_actions = [
        action for action in actions if action["transaction"] == "cmd_unsplit"
    ]

    assert [action["type"] for action in unsplit_actions] == ["update", "delete"]
    assert unsplit_actions[0]["key"] == ["clips", {"id": "card_clip"}]
    assert unsplit_actions[1]["key"] == ["clips", {"id": "card_clip_b"}]
    assert unsplit_actions[1]["old_values"]["id"] == "card_clip_b"


# ---------------------------------------------------------------------------
# Isolated regression coverage for update_action_from_command and
# update_actions_from_command_log — the command->UpdateAction mapping that the
# editor-render pipeline relies on. Previously only exercised transitively via
# flattened_update_actions.
# ---------------------------------------------------------------------------


def test_command_create_maps_to_insert_action(tmp_path):
    project = _project(tmp_path / "card.png")
    new_clip = {**project["clips"]["card_clip"], "id": "card_clip_new"}
    entry = {"id": "cmd_create", "op": "clip.create", "after": {"clip": new_clip}}

    action = openshot_bridge.update_action_from_command(project, entry)

    assert action["type"] == "insert"
    assert action["key"] == ["clips"]
    assert action["old_values"] is None
    assert action["transaction"] == "cmd_create"
    assert action["value"]["id"] == "card_clip_new"


def test_generic_clip_command_maps_to_update_action(tmp_path):
    project = _project(tmp_path / "card.png")
    after = {**project["clips"]["card_clip"], "duration": 4.0}
    entry = {
        "id": "cmd_resize",
        "op": "clip.resize",
        "before": {"clip": project["clips"]["card_clip"]},
        "after": {"clip": after},
    }

    action = openshot_bridge.update_action_from_command(project, entry)

    assert action["type"] == "update"
    assert action["key"] == ["clips", {"id": "card_clip"}]
    assert action["value"]["duration"] == 4.0
    assert action["old_values"]["duration"] == 2.0
    assert action["transaction"] == "cmd_resize"


def test_split_command_returns_unflattened_batch(tmp_path):
    project = _project(tmp_path / "card.png")
    left = {**project["clips"]["card_clip"], "duration": 1.0}
    right = {**project["clips"]["card_clip"], "id": "card_clip_b", "duration": 1.0}
    entry = {
        "id": "cmd_split",
        "op": "clip.split",
        "before": {"clip": project["clips"]["card_clip"]},
        "after": {"clip": left, "createdClip": right},
    }

    action = openshot_bridge.update_action_from_command(project, entry)

    # Split is a batch (not flattened here) — update existing + insert new.
    assert set(action) == {"batch"}
    assert [a["type"] for a in action["batch"]] == ["update", "insert"]
    assert action["batch"][0]["key"] == ["clips", {"id": "card_clip"}]
    assert action["batch"][1]["value"]["id"] == "card_clip_b"


def test_non_clip_commands_return_none(tmp_path):
    project = _project(tmp_path / "card.png")

    # Markers/notes and other non-timeline ops are intentionally ignored.
    assert openshot_bridge.update_action_from_command(
        project, {"id": "n1", "op": "note.add", "after": {"text": "hi"}}
    ) is None
    assert openshot_bridge.update_action_from_command(
        project, {"id": "m1", "op": "marker.add", "after": {}}
    ) is None
    # A clip op missing its clip payload yields nothing.
    assert openshot_bridge.update_action_from_command(
        project, {"id": "x1", "op": "clip.transform", "after": {}}
    ) is None
    # Empty entry doesn't raise.
    assert openshot_bridge.update_action_from_command(project, {}) is None


def test_command_log_keeps_split_batches_unflattened(tmp_path):
    project = _project(tmp_path / "card.png")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip", "at": 2.5, "newClipId": "card_clip_b"},
        },
    )

    raw = openshot_bridge.update_actions_from_command_log(project)
    flat = openshot_bridge.flattened_update_actions(project)

    # The split surfaces as a single batch entry pre-flattening...
    assert len(raw) == 1
    assert "batch" in raw[0]
    # ...and as two discrete actions once flattened.
    assert len(flat) == 2
    assert [a["type"] for a in flat] == ["update", "insert"]


# ---------------------------------------------------------------------------
# Isolated unit coverage for the four core translation functions —
# source_media_type, clip_json, reader_json, effect_json — called DIRECTLY with
# hand-built dicts rather than through timeline_json. A regression in clip
# transform coercion or effect class mapping silently corrupts the OpenShot
# payload (the clip/effect skips render with no error), so these paths need
# point-blank tests, not just transitive integration coverage.
# ---------------------------------------------------------------------------


def test_source_media_type_maps_the_closed_source_vocabulary():
    """Every production source in the closed SOURCE_TYPES vocab resolves to its
    libopenshot media kind. video-producing sources (remotion/webscroll/css/broll)
    collapse to "video"; still->image; vo/bed->audio. This is the single source of
    truth for "what kind of footage exists" — a wrong mapping mislabels has_audio/
    has_video and picks the wrong reader."""
    assert openshot_bridge.source_media_type("remotion") == "video"
    assert openshot_bridge.source_media_type("webscroll") == "video"
    assert openshot_bridge.source_media_type("css") == "video"
    assert openshot_bridge.source_media_type("broll") == "video"
    assert openshot_bridge.source_media_type("still") == "image"
    assert openshot_bridge.source_media_type("vo") == "audio"
    assert openshot_bridge.source_media_type("bed") == "audio"
    # every key in the closed vocab maps to exactly its declared media kind
    for source, entry in openshot_bridge.SOURCE_TYPES.items():
        assert openshot_bridge.source_media_type(source) == entry["media"]


def test_source_media_type_is_case_insensitive_and_passes_unknowns_through():
    """Source lookup lowercases input; an unknown/empty source falls through
    unchanged so a raw media `type` still works downstream."""
    assert openshot_bridge.source_media_type("REMOTION") == "video"
    assert openshot_bridge.source_media_type("WebScroll") == "video"
    # unknown source returns itself (not an error, not a default media kind)
    assert openshot_bridge.source_media_type("mystery") == "mystery"
    assert openshot_bridge.source_media_type("") == ""
    assert openshot_bridge.source_media_type(None) == ""


def test_clip_json_coerces_negative_scale_to_zero_and_zeroes_disabled_opacity():
    """Direct clip_json: a negative scale is clamped to 0 (L237) and a disabled
    clip is forced to alpha 0 regardless of its transform.opacity (L238). A
    regression here corrupts the transform and the clip renders wrong/invisible."""
    project = {"fps": 30, "assets": {}, "tracks": {}}
    clip = {
        "id": "c1",
        "assetId": "missing",
        "trackId": "",
        "start": 0.0,
        "duration": 2.0,
        "enabled": False,
        "transform": {"scale": -5.0, "opacity": 0.9},
    }

    payload = openshot_bridge.clip_json(project, clip)

    # negative scale clamped to 0 on both axes
    assert payload["scale_x"]["Points"][0]["co"]["Y"] == 0.0
    assert payload["scale_y"]["Points"][0]["co"]["Y"] == 0.0
    # disabled clip -> alpha 0 even though transform.opacity is 0.9
    assert payload["alpha"]["Points"][0]["co"]["Y"] == 0.0


def test_clip_json_mutes_volume_and_defaults_geometry():
    """A muted clip zeroes volume regardless of its volume field; an enabled clip
    with no transform keeps the centered/full-scale defaults."""
    project = {"fps": 30, "assets": {}, "tracks": {}}
    muted = openshot_bridge.clip_json(
        project,
        {"id": "m", "assetId": "a", "trackId": "", "start": 0, "duration": 1, "muted": True, "volume": 1.0},
    )
    assert muted["volume"]["Points"][0]["co"]["Y"] == 0.0

    plain = openshot_bridge.clip_json(
        project,
        {"id": "p", "assetId": "a", "trackId": "", "start": 0, "duration": 1},
    )
    # default transform: full scale, centered (0.5 -> 0.0), full alpha
    assert plain["scale_x"]["Points"][0]["co"]["Y"] == 1.0
    assert plain["location_x"]["Points"][0]["co"]["Y"] == 0.0
    assert plain["alpha"]["Points"][0]["co"]["Y"] == 1.0
    assert plain["volume"]["Points"][0]["co"]["Y"] == 100.0  # editor 1.0 -> openshot 100


def test_reader_json_selects_reader_class_by_media_kind():
    """reader_json picks DummyReader when there's no src, FFmpegReader for
    video/audio assets, and QtImageReader for images. A wrong reader class makes
    libopenshot fail to open the asset."""
    project = {"width": 1920, "height": 1080, "fps": 30}

    no_src = openshot_bridge.reader_json(project, {"id": "x", "src": ""}, duration=2.0)
    assert no_src["type"] == "DummyReader"

    video = openshot_bridge.reader_json(project, {"id": "v", "src": "/clip.mp4", "type": "video"}, duration=2.0)
    assert video["type"] == "FFmpegReader"
    assert video["has_video"] is True and video["has_audio"] is False

    audio = openshot_bridge.reader_json(project, {"id": "a", "src": "/vo.wav", "type": "audio"}, duration=2.0)
    assert audio["type"] == "FFmpegReader"
    assert audio["has_audio"] is True and audio["has_video"] is False

    image = openshot_bridge.reader_json(project, {"id": "i", "src": "/card.png", "type": "image"}, duration=2.0)
    assert image["type"] == "QtImageReader"


def test_reader_json_resolves_source_tagged_assets_and_video_length():
    """An asset's production `source` (remotion->video) wins over its raw type for
    reader selection, and video_length is duration*fps (floored at 1 frame)."""
    project = {"width": 1280, "height": 720, "fps": 24}
    asset = {"id": "shot", "src": "/term.mp4", "source": "remotion"}

    reader = openshot_bridge.reader_json(project, asset, duration=3.0)

    assert reader["type"] == "FFmpegReader"  # remotion -> video -> FFmpegReader
    assert reader["has_video"] is True
    assert reader["fps"] == {"num": 24, "den": 1}
    assert reader["video_length"] == int(3.0 * 24)
    # zero-ish duration still yields at least one frame
    assert openshot_bridge.reader_json(project, asset, duration=0.0)["video_length"] == 1


def test_effect_json_maps_classes_and_falls_through_for_unknown_types():
    """effect_json maps the closed editor effect vocab to libopenshot classes and
    falls through with the raw type for anything unknown (so nothing is silently
    dropped). A bad class name makes OpenShot skip the effect at render with no
    error — exactly the silent-corruption case this pins down."""
    fade_in = openshot_bridge.effect_json({"id": "f1", "type": "fadeIn", "params": {"duration": 0.5}}, fps=30)
    assert fade_in["type"] == "Fade"
    assert fade_in["fade"] == "in"
    assert fade_in["duration"]["Points"][0]["co"]["Y"] == 0.5

    fade_out = openshot_bridge.effect_json({"id": "f2", "type": "fadeOut", "params": {}}, fps=30)
    assert fade_out["fade"] == "out"
    assert fade_out["duration"]["Points"][0]["co"]["Y"] == 0.0  # missing param -> 0

    bright = openshot_bridge.effect_json({"id": "b", "type": "brightness", "params": {"value": -0.3}}, fps=30)
    assert bright["type"] == "Brightness"
    assert bright["brightness"]["Points"][0]["co"]["Y"] == -0.3
    assert bright["contrast"]["Points"][0]["co"]["Y"] == 0.0

    sat = openshot_bridge.effect_json({"id": "s", "type": "saturation", "params": {"value": 1.4}}, fps=30)
    assert sat["type"] == "Saturation"
    assert sat["saturation"]["Points"][0]["co"]["Y"] == 1.4

    # unknown type falls through with the raw type and no class-specific props
    unknown = openshot_bridge.effect_json({"id": "u", "type": "kaleidoscope", "params": {}}, fps=30)
    assert unknown["type"] == "kaleidoscope"
    assert unknown == {"id": "u", "type": "kaleidoscope"}


def test_effect_json_tolerates_malformed_param_values():
    """Poisoned/malformed effect params (corrupted project files, malicious JSON)
    must not raise ValueError/TypeError out of effect_json and abort the whole
    render. Non-numeric param values fall back to the neutral 0.0 default — the
    same value a missing param already produces — so the bad effect degrades
    quietly instead of taking the episode down with it."""
    # string that float() can't parse
    bright = openshot_bridge.effect_json(
        {"id": "b", "type": "brightness", "params": {"value": "not-a-number"}}, fps=30
    )
    assert bright["type"] == "Brightness"
    assert bright["brightness"]["Points"][0]["co"]["Y"] == 0.0

    # wrong type entirely (list) on a fade duration
    fade = openshot_bridge.effect_json(
        {"id": "f", "type": "fadeIn", "params": {"duration": ["bogus"]}}, fps=30
    )
    assert fade["fade"] == "in"
    assert fade["duration"]["Points"][0]["co"]["Y"] == 0.0

    # dict value on saturation
    sat = openshot_bridge.effect_json(
        {"id": "s", "type": "saturation", "params": {"value": {"x": 1}}}, fps=30
    )
    assert sat["type"] == "Saturation"
    assert sat["saturation"]["Points"][0]["co"]["Y"] == 0.0

    # a numeric string still parses correctly (regression: don't over-clamp)
    ok = openshot_bridge.effect_json(
        {"id": "ok", "type": "brightness", "params": {"value": "0.5"}}, fps=30
    )
    assert ok["brightness"]["Points"][0]["co"]["Y"] == 0.5


def test_every_editor_effect_type_translates_to_a_real_openshot_class():
    """Drift guard: every effect type the editor accepts (editor_timeline.EFFECT_TYPES)
    MUST have a libopenshot class mapping in openshot_bridge — otherwise effect_json
    falls through with the raw editor type, OpenShot can't construct the effect, and it
    is silently skipped at render with no error. This is the exact untested translation
    gap (a new transition / color filter added upstream but never wired into the bridge)
    that the production-gaps audit flagged. It fails loudly the moment the vocabularies
    diverge instead of losing the effect in a rendered episode."""
    editor_types = set(editor_timeline.EFFECT_TYPES)
    mapped = set(openshot_bridge._EFFECT_CLASS_MAP)
    missing = editor_types - mapped
    assert not missing, (
        "editor effect types with no openshot_bridge class mapping (they fall through "
        f"as raw types and OpenShot silently drops them at render): {sorted(missing)}"
    )

    # And the mapping must resolve to a real class name (a non-empty value that is NOT
    # just the editor type echoed back), and effect_json must emit it for every type
    # exercised end-to-end. Build a minimal valid effect for each editor type from its
    # own param spec so this stays in lockstep with the upstream vocabulary.
    for effect_type, spec in editor_timeline.EFFECT_TYPES.items():
        cls = openshot_bridge._EFFECT_CLASS_MAP[effect_type]
        assert cls and cls != effect_type, (
            f"{effect_type} maps to a bogus class name {cls!r}"
        )
        params = {name: low for name, (low, high) in spec.items()}
        out = openshot_bridge.effect_json(
            {"id": f"fx_{effect_type}", "type": effect_type, "params": params}, fps=30
        )
        assert out["type"] == cls, (
            f"effect_json did not emit the mapped class for {effect_type}: got {out['type']!r}"
        )
        # Fades carry a direction; color filters carry their keyframed property.
        if effect_type in openshot_bridge._FADE_DIRECTION_MAP:
            assert out["fade"] == openshot_bridge._FADE_DIRECTION_MAP[effect_type]
            assert "duration" in out and "Points" in out["duration"]
        else:
            assert any(
                isinstance(v, dict) and "Points" in v
                for k, v in out.items()
                if k not in {"id", "type"}
            ), f"{effect_type} emitted no keyframed property"


def test_every_fade_type_has_a_direction():
    """Parallel drift guard for the fade family: every effect that maps to the Fade
    class must also have an entry in _FADE_DIRECTION_MAP, or effect_json emits a Fade
    with no `fade` field and OpenShot defaults the direction (wrong-way crossfades /
    missing dissolves at clip boundaries). Pin the two fade tables in lockstep."""
    fade_types = {
        t for t, cls in openshot_bridge._EFFECT_CLASS_MAP.items() if cls == "Fade"
    }
    undirected = fade_types - set(openshot_bridge._FADE_DIRECTION_MAP)
    assert not undirected, (
        "effect types mapped to the Fade class with no direction in _FADE_DIRECTION_MAP "
        f"(they render with a default/ambiguous fade direction): {sorted(undirected)}"
    )


# ── repo-hygiene guard ────────────────────────────────────────────────────────
# openshot_bridge.py is imported at app load (routers/agenticnews.py). It was once
# untracked and nearly lost; a clean checkout that's missing it raises
# ModuleNotFoundError at startup. prod-cycle.js (lines 107/151) explicitly guards
# for this on every merge. These tests pin the invariant so it can't silently regress.

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_openshot_bridge_is_git_tracked():
    """openshot_bridge.py must be committed — the app imports it at load time."""
    tracked = _git("ls-files", "--error-unmatch", "services/openshot_bridge.py")
    assert tracked.returncode == 0, (
        "services/openshot_bridge.py is NOT git-tracked — a clean checkout will "
        f"ModuleNotFoundError on import.\n{tracked.stderr}"
    )


def test_openshot_bridge_is_not_gitignored():
    """It must NOT be ignored — an ignored module silently drops from clean clones."""
    ignored = _git("check-ignore", "-v", "services/openshot_bridge.py")
    # git check-ignore exits 0 (and prints the matching rule) when a path IS ignored.
    assert ignored.returncode != 0, (
        "services/openshot_bridge.py matches a .gitignore rule:\n"
        f"{ignored.stdout}"
    )


def test_app_imported_python_modules_are_all_tracked():
    """Generalize the openshot_bridge lesson: every services/ + routers/ module the
    app can import at runtime must be tracked, so a clean checkout never raises
    ModuleNotFoundError. Catches the whole class of bug, not just one file."""
    candidates = sorted(
        p for d in ("services", "routers")
        for p in (REPO_ROOT / d).rglob("*.py")
        if "__pycache__" not in p.parts
    )
    rel = [str(p.relative_to(REPO_ROOT)) for p in candidates]
    assert rel, "no python modules found under services/ or routers/ — wrong repo root?"

    listed = _git("ls-files", "--", *rel)
    tracked = set(listed.stdout.splitlines())
    untracked = [p for p in rel if p not in tracked]
    assert not untracked, (
        "app-imported python modules are NOT git-tracked (clean checkout would "
        f"ModuleNotFoundError): {untracked}"
    )


def test_asset_src_resolution_is_consolidated(tmp_path):
    """All three former copies of /agenticnews-assets/ resolution now route through
    the one canonical resolver (openshot_bridge._resolve_asset_src). Pin that the
    bridge resolver, editor_render's Path wrapper, and the ffmpeg fallback's method
    agree on every branch so they can never silently diverge again."""
    from services import editor_render

    asset_url = "/agenticnews-assets/card.png"
    passthrough = str(tmp_path / "vo.wav")
    expected = str(tmp_path / "card.png")

    # Canonical resolver (str -> str), accepts Path or str asset_root.
    assert openshot_bridge._resolve_asset_src(asset_url, asset_root=tmp_path) == expected
    assert openshot_bridge._resolve_asset_src(asset_url, asset_root=str(tmp_path)) == expected
    # No asset_root and non-asset srcs pass straight through.
    assert openshot_bridge._resolve_asset_src(asset_url, asset_root=None) == asset_url
    assert openshot_bridge._resolve_asset_src(passthrough, asset_root=tmp_path) == passthrough

    # editor_render's Path-typed wrapper must agree, returning a Path for the same inputs.
    assert editor_render._resolve_asset_src(asset_url, tmp_path) == Path(expected)
    assert editor_render._resolve_asset_src(asset_url, None) == Path(asset_url)
    assert editor_render._resolve_asset_src(passthrough, tmp_path) == Path(passthrough)

    # The ffmpeg fallback's method delegates to the same wrapper via self.asset_root.
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders", asset_root=tmp_path)
    assert renderer._resolve_src(asset_url) == Path(expected)
    assert renderer._resolve_src(passthrough) == Path(passthrough)
    rootless = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    assert rootless._resolve_src(asset_url) == Path(asset_url)


def test_split_with_both_anchored_fades_exports_render_refit_durations(tmp_path):
    """clip.split on a clip carrying BOTH a start-anchored fade (fadeIn) and an
    end-anchored fade (fadeOut) must leave the timeline state and the render path
    in agreement on the refitted fade durations:

      - HEAD owns the original start: it keeps fadeIn at full duration and drops
        the end-anchored fadeOut.
      - TAIL owns the original end: its source front is trimmed by split_offset,
        which eats into the start-anchored fadeIn. The tail's effects, once exported
        through openshot_bridge, must carry the EXACT same fade durations that the
        render path (editor_render._refit_effects, front_trim=split_offset,
        windowed_duration=tail_duration) would apply — otherwise the editor preview
        and the compiled OpenShot video disagree on the fade.
    """
    from services import editor_render

    project = _project(tmp_path / "card.png")
    clip = project["clips"]["card_clip"]
    clip["start"] = 1.5
    clip["duration"] = 3.0
    clip["effects"] = [
        {"id": "fxIn", "type": "fadeIn", "params": {"duration": 1.0}},
        {"id": "fxOut", "type": "fadeOut", "params": {"duration": 1.0}},
    ]

    split_at = 2.1  # split_offset = 0.6 into the source
    split_offset = split_at - clip["start"]
    tail_duration = clip["duration"] - split_offset  # 2.4

    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip", "at": split_at, "newClipId": "tail_clip"},
        },
    )

    # HEAD: fadeIn kept whole, fadeOut dropped.
    head = openshot_bridge.timeline_json(project)["clips"]
    head_clip = next(c for c in head if c["id"] == "card_clip")
    head_fades = {
        f["fade"]: f["duration"]["Points"][0]["co"]["Y"]
        for f in head_clip["effects"]
        if f["type"] == "Fade"
    }
    assert head_fades == {"in": 1.0}  # fadeIn whole, fadeOut gone

    # TAIL: export the openshot fade durations the timeline produced...
    tail_clip = next(c for c in head if c["id"] == "tail_clip")
    exported_tail_fades = {
        f["id"]: f["duration"]["Points"][0]["co"]["Y"]
        for f in tail_clip["effects"]
        if f["type"] == "Fade"
    }

    # ...and what the render path would compute from the SAME original effects.
    render_refit = editor_render._refit_effects(
        [
            {"id": "fxIn", "type": "fadeIn", "params": {"duration": 1.0}},
            {"id": "fxOut", "type": "fadeOut", "params": {"duration": 1.0}},
        ],
        front_trim=split_offset,
        windowed_duration=tail_duration,
    )
    render_fades = {fx["id"]: fx["params"]["duration"] for fx in render_refit}

    # The contract: timeline-exported durations == render-path durations, exactly.
    assert exported_tail_fades == render_fades
    # And concretely: fadeIn 1.0 - 0.6 front trim = 0.4; fadeOut stays 1.0 (end-anchored,
    # under the 2.4 window). A divergence in either path would break this equality.
    assert exported_tail_fades == {"fxIn": pytest.approx(0.4), "fxOut": 1.0}
