import copy
import shutil
import subprocess

import pytest

from services import editor_timeline as timeline


def test_imports_abn_timeline_as_atomic_assets_clips_and_tracks(tmp_path):
    abn_timeline = {
        "episodeId": "ep_fixture",
        "title": "Fixture Episode",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "totalSec": 8.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [
            {
                "segmentId": "s0",
                "title": "Hook",
                "durationSec": 5.0,
                "shots": [
                    {
                        "id": "shot_a",
                        "src": "/agenticnews-assets/card_a.png",
                        "startSec": 1.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo_s0.wav", "duration": 5.0}},
                "lowerThirds": [
                    {"startSec": 0.5, "durationSec": 2.5, "headline": "Hook lower"}
                ],
            },
            {
                "segmentId": "s1",
                "title": "Proof",
                "durationSec": 3.0,
                "shots": [
                    {
                        "src": "/agenticnews-assets/demo.mp4",
                        "startSec": 0.25,
                        "durationSec": 1.5,
                        "type": "demo",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo_s1.wav", "duration": 3.0}},
            },
        ],
    }

    project = timeline.project_from_abn_timeline(
        "proj_ep_fixture", abn_timeline, source_episode_id="ep_fixture"
    )

    assert project["projectId"] == "proj_ep_fixture"
    assert project["revision"] == 0
    assert set(project["tracks"]) >= {"video_1", "graphics_1", "audio_1", "titles_1"}
    assert any(a["src"] == "/agenticnews-assets/card_a.png" for a in project["assets"].values())
    assert any(c["start"] == 1.0 and c["duration"] == 2.0 for c in project["clips"].values())
    assert any(c["start"] == 5.25 and c["duration"] == 1.5 for c in project["clips"].values())
    assert any(c["kind"] == "lower_third" for c in project["clips"].values())
    assert any(c["kind"] == "music_bed" and c["trackId"] == "music_1" for c in project["clips"].values())

    store = timeline.TimelineStore(tmp_path)
    saved = store.save(project)
    assert saved == project
    loaded = store.load("proj_ep_fixture")
    assert loaded["projectId"] == "proj_ep_fixture"


def test_save_fsyncs_before_replace_so_a_crash_cannot_lose_edits(tmp_path, monkeypatch):
    """save() must flush+fsync the tmp file before the atomic rename.

    Without fsync, the rename can hit disk while the data is still in the OS
    page cache, so a CPU crash leaves a renamed-but-empty/truncated project
    file. atomic_save (the proven telegram/json_store path) fsyncs first; this
    guards that editor_timeline routes through it and never regresses to a raw
    write_text()+replace.
    """
    import os

    fsynced: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("services.json_store.os.fsync", spy_fsync)

    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_durable", title="Durable")
    store.save(project)

    assert fsynced, "save() did not fsync the tmp file before the atomic rename"
    # round-trips cleanly and leaves no stray tmp file behind
    assert store.load("proj_durable")["title"] == "Durable"
    assert not list(tmp_path.glob("*.tmp"))


def test_load_corrupt_project_raises_filenotfound_not_jsondecode(tmp_path):
    """load() must not crash on a truncated/corrupt project file.

    A crash mid-write (before save()'s atomic rename was complete) can leave a
    half-written project JSON. The old load() called json.loads() directly and
    raised JSONDecodeError -> unhandled 500. Robustified like
    json_store.atomic_load, load() now degrades a corrupt-but-present file to the
    same FileNotFoundError the routers already handle as a clean 404.
    """
    store = timeline.TimelineStore(tmp_path)
    path = store.path_for("proj_corrupt")
    path.write_text('{"projectId": "proj_corrupt", "clips": {', encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        store.load("proj_corrupt")

    # a structurally-valid JSON that isn't an object (e.g. a stray array/string)
    # is also not a usable project -> same graceful FileNotFoundError
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        store.load("proj_corrupt")


def test_import_preserves_production_source_for_openshot():
    """A shot whose `type` is a known production source (broll/remotion/webscroll/css)
    carries that onto the asset as `source`, so the OpenShot bridge keeps layers
    distinguishable. Unknown types (artifact) get no source and fall back to media kind."""
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_src",
        "segments": [{
            "segmentId": "s0",
            "durationSec": 4.0,
            "shots": [
                {"id": "b", "src": "/agenticnews-assets/ui.mp4", "startSec": 0.0, "durationSec": 2.0, "type": "broll"},
                {"id": "a", "src": "/agenticnews-assets/card.png", "startSec": 2.0, "durationSec": 2.0, "type": "artifact"},
            ],
        }],
    }
    project = timeline.project_from_abn_timeline("proj_ep_src", abn_timeline, source_episode_id="ep_src")

    broll = next(a for a in project["assets"].values() if a["src"] == "/agenticnews-assets/ui.mp4")
    artifact = next(a for a in project["assets"].values() if a["src"] == "/agenticnews-assets/card.png")
    assert broll["source"] == "broll"
    assert "source" not in artifact
    # the bridge resolves the source tag to the right libopenshot media kind
    assert openshot_bridge._asset_type(broll) == "video"


def test_import_synthesizes_crossfade_from_shot_transition_for_openshot():
    """A shot's inter-clip `transitionSec` (the factory's choreographed boundary crossfade)
    becomes a validated `crossfade` effect on the clip, so OpenShot dissolves the boundary
    instead of hard-cutting. An explicit `effects` crossfade is NOT duplicated."""
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_xf",
        "segments": [{
            "segmentId": "s0",
            "durationSec": 6.0,
            "shots": [
                {"id": "a", "src": "/agenticnews-assets/a.png", "startSec": 0.0, "durationSec": 2.0, "type": "artifact"},
                {"id": "b", "src": "/agenticnews-assets/b.png", "startSec": 2.0, "durationSec": 2.0,
                 "type": "artifact", "transitionSec": 0.4},
                {"id": "c", "src": "/agenticnews-assets/c.png", "startSec": 4.0, "durationSec": 2.0,
                 "type": "artifact", "transitionSec": 0.5,
                 "effects": [{"id": "fx_xf", "type": "crossfade", "params": {"duration": 1.0}}]},
            ],
        }],
    }
    project = timeline.project_from_abn_timeline("proj_xf", abn_timeline, source_episode_id="ep_xf")

    def clip_for(src):
        asset_id = next(a["id"] for a in project["assets"].values() if a["src"] == src)
        return next(c for c in project["clips"].values() if c["assetId"] == asset_id)

    a, b, c = (clip_for(f"/agenticnews-assets/{n}.png") for n in ("a", "b", "c"))
    # shot a (first / no transition) hard-cuts in
    assert not any(e["type"] == "crossfade" for e in a["effects"])
    # shot b gets a synthesized crossfade of its transitionSec
    xf_b = [e for e in b["effects"] if e["type"] == "crossfade"]
    assert len(xf_b) == 1 and xf_b[0]["params"]["duration"] == 0.4
    # shot c already had an explicit crossfade — not duplicated, the explicit one wins
    xf_c = [e for e in c["effects"] if e["type"] == "crossfade"]
    assert len(xf_c) == 1 and xf_c[0]["params"]["duration"] == 1.0
    # the synthesized crossfade survives translation to an OpenShot Fade(in) dissolve,
    # carrying both the `in` direction and the original transition duration — a dropped
    # direction or zeroed duration here would hard-cut the boundary at render.
    fx = openshot_bridge.effect_json(xf_b[0], fps=30)
    assert fx["type"] == "Fade"
    assert fx["fade"] == "in"
    assert fx["duration"]["Points"][0]["co"]["Y"] == 0.4


def test_music_bed_imports_pre_ducked_under_vo():
    """The music bed must import ducked (0.22, the factory convention) so a render has
    music UNDER the VO, not competing with it at full volume. VO stays at 1.0."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0, "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [],
                      "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}}}],
    })
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    vo = next(c for c in project["clips"].values() if c["kind"] == "voiceover")
    assert bed["volume"] == 0.22
    assert vo["volume"] == 1.0


def test_import_promotes_shot_volume_envelope_alongside_ken_burns():
    """A shot that ships an explicit `keyframes` volume ducking envelope must survive
    import. Previously line 137 unconditionally overwrote clip.keyframes with only the
    kenBurns-derived scale/x/y tracks, silently dropping the volume envelope before it
    could reach _volume_filter / openshot_bridge."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [{
            "id": "shot_a", "src": "/agenticnews-assets/card.png", "type": "artifact",
            "startSec": 0.0, "durationSec": 4.0,
            "kenBurns": {"startScale": 1.0, "endScale": 1.1},
            "keyframes": [{"property": "volume", "points": [
                {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.22},
            ]}],
        }]}],
    })
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")
    props = {t["property"]: t for t in clip["keyframes"]}
    # kenBurns scale track AND the explicit volume envelope both survive.
    assert "scale" in props
    assert "volume" in props
    assert [p["t"] for p in props["volume"]["points"]] == [0.0, 1.0]
    assert props["volume"]["points"][1]["value"] == 0.22


def test_import_promotes_shot_ducking_envelope_from_separate_field():
    """A factory-attached ducking envelope passed as a SEPARATE shot field
    (keyframesEnvelope / duckingKeyframes) must survive import, mirroring how the
    music bed reads its sibling `musicBedKeyframes`. Previously _shot_keyframes only
    read `shot.keyframes`, so a dynamically-attached envelope was silently dropped and
    the render lost the ducking the factory intended."""
    for field in ("keyframesEnvelope", "duckingKeyframes"):
        project = timeline.project_from_abn_timeline("p", {
            "episodeId": "e", "totalSec": 4.0,
            "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [{
                "id": "shot_a", "src": "/agenticnews-assets/card.png", "type": "artifact",
                "startSec": 0.0, "durationSec": 4.0,
                "kenBurns": {"startScale": 1.0, "endScale": 1.1},
                field: [{"property": "volume", "points": [
                    {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.2},
                ]}],
            }]}],
        })
        clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")
        props = {t["property"]: t for t in clip["keyframes"]}
        # kenBurns scale track AND the separate-field volume envelope both survive.
        assert "scale" in props, field
        assert "volume" in props, field
        assert props["volume"]["points"][1]["value"] == 0.2, field


def test_import_explicit_shot_keyframes_win_over_separate_envelope():
    """When both `keyframes` and a separate envelope field set the same property, the
    hand-authored explicit `keyframes` track wins (same precedence as kenBurns vs
    explicit), while non-overlapping envelope properties still pass through."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [{
            "id": "shot_a", "src": "/agenticnews-assets/card.png", "type": "artifact",
            "startSec": 0.0, "durationSec": 4.0,
            "keyframes": [{"property": "volume", "points": [
                {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.5},
            ]}],
            "keyframesEnvelope": [
                {"property": "volume", "points": [
                    {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.99},
                ]},
                {"property": "opacity", "points": [
                    {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.0},
                ]},
            ],
        }]}],
    })
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")
    props = {t["property"]: t for t in clip["keyframes"]}
    # explicit volume (0.5) wins over the envelope's 0.99
    assert props["volume"]["points"][1]["value"] == 0.5
    # envelope-only property still passes through
    assert "opacity" in props


def test_shot_ducking_envelope_reaches_render_path_unflattened():
    """End-to-end guard for an AUDIO/VIDEO shot clip (the case the music-bed tests
    never cover): a factory-attached ducking envelope must survive import AND compile
    through editor_render._volume_filter as its absolute gains, not collapse to the
    clip's flat `volume`.

    editor_render._volume_filter SCALES a volume envelope by the clip's flat volume
    (line ~178). A shot clip imports with the default volume=1.0, so the envelope must
    pass through 1:1 — at the intro the clip stays 0.7, not 0.7*<something>. Without
    the _shot_keyframes promotion the clip would have no envelope and _volume_filter
    would emit a flat `volume=` instead, silently losing the ducking entirely."""
    from services import editor_render

    for field in ("keyframesEnvelope", "duckingKeyframes"):
        project = timeline.project_from_abn_timeline("p", {
            "episodeId": "e", "totalSec": 4.0,
            "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [{
                "id": "shot_a", "src": "/agenticnews-assets/clip.mp4", "type": "webscroll",
                "startSec": 0.0, "durationSec": 4.0,
                field: [{"property": "volume", "points": [
                    {"t": 0.0, "value": 0.7}, {"t": 1.0, "value": 0.22},
                ]}],
            }]}],
        })
        clip = next(c for c in project["clips"].values() if c["kind"] == "webscroll")
        # The shot clip keeps the default flat volume (no music-bed-style 0.22 duck),
        # so the envelope's absolute gains pass through unscaled.
        assert clip["volume"] == 1.0, field
        assert any(t["property"] == "volume" for t in clip["keyframes"]), field
        flt = editor_render._volume_filter(clip, float(clip["volume"]))
        # The envelope's points are ABSOLUTE gains (matching openshot_bridge), so
        # _volume_filter emits a time-varying piecewise expression evaluated per frame,
        # NOT a flat `volume=0.7000` and NOT a base-scaled `(0.2200)*(...)` form. A flat
        # `volume=` (no `eval=frame`) would mean the ducking envelope was dropped/flattened.
        assert flt.startswith("volume='") and flt.endswith(":eval=frame"), field
        assert "if(" in flt, field  # piecewise, time-varying — not a single flat gain
        # the intro gain (0.7) and the ducked gain (0.22) both appear in the expression
        assert "0.7000" in flt and "0.2200" in flt, field


def test_clip_keyframes_command_neutralizes_flat_volume_end_to_end():
    """A `clip.keyframes` command that adds a volume envelope to a pre-ducked music
    bed (flat volume=0.22) must neutralize the flat gain to 1.0, and that
    neutralization must survive the round-trip to BOTH render backends so neither
    double-attenuates the bed (the documented 0.6 -> 0.6*0.22 = 0.132 bug).

    Covers the gap the import-time tests miss: the envelope arrives via the live
    command path (editor_timeline.apply_command, op clip.keyframes) rather than at
    import. Asserts (1) the timeline state neutralizes, (2) openshot_bridge.clip_json
    emits the envelope's ABSOLUTE gains (not base-scaled, not flat 0.22), and (3)
    editor_render._volume_filter emits the same absolute, time-varying expression."""
    from services import editor_render
    from services import openshot_bridge

    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0, "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [],
                      "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}}}],
    })
    bed_id = next(cid for cid, c in project["clips"].items() if c["kind"] == "music_bed")
    # Bed imported pre-ducked with a FLAT gain (no envelope yet) — the dangerous
    # starting state: if the command doesn't neutralize, this 0.22 stays alongside
    # the absolute envelope and both backends double-attenuate.
    assert project["clips"][bed_id]["volume"] == 0.22
    assert not project["clips"][bed_id].get("keyframes")

    # User adds an absolute ducking envelope via the live command path.
    project = timeline.apply_command(project, {
        "id": "cmd_kf",
        "op": "clip.keyframes",
        "actor": "editor",
        "expectedRevision": project["revision"],
        "payload": {"clipId": bed_id, "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6}, {"t": 1.0, "value": 0.22},
            ]},
        ]},
    })

    bed = project["clips"][bed_id]
    # (1) Timeline state: the flat 0.22 duck is neutralized to 1.0 so the absolute
    # envelope drives ducking; the envelope itself survives.
    assert bed["volume"] == 1.0
    assert any(t["property"] == "volume" for t in bed["keyframes"])

    # (2) OpenShot JSON round-trip: the volume keyframe track carries the envelope's
    # ABSOLUTE gains (scaled to OpenShot's 0..100), NOT the flat base and NOT a
    # base-scaled value. 0.6 -> 60.0, 0.22 -> 22.0. A double-attenuated render would
    # show 0.6*0.22*100 = 13.2 here.
    cj = openshot_bridge.clip_json(project, bed)
    ys = [round(pt["co"]["Y"], 4) for pt in cj["volume"]["Points"]]
    assert ys == [60.0, 22.0]

    # (3) ffmpeg-mux round-trip: _volume_filter emits the same absolute, time-varying
    # expression — never a flat `volume=0.22` and never a 0.132 double-attenuation.
    flt = editor_render._volume_filter(bed, float(bed["volume"]))
    assert flt.startswith("volume='") and flt.endswith(":eval=frame")
    assert "0.6000" in flt and "0.2200" in flt
    assert "0.1320" not in flt  # the double-attenuation bug must not appear


def test_import_drops_malformed_shot_envelope_without_aborting():
    """A bad shot envelope must be skipped, not fatal — the raw shot is preserved in
    metadata.shot for re-import, and the rest of the import must still succeed."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [{
            "id": "shot_a", "src": "/agenticnews-assets/card.png", "type": "artifact",
            "startSec": 0.0, "durationSec": 4.0,
            "keyframes": [{"property": "warp", "points": [{"t": 0, "value": 1}]}],
        }]}],
    })
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")
    assert clip["keyframes"] == []  # malformed envelope dropped
    assert clip["metadata"]["shot"]["keyframes"]  # raw shot still preserved


def test_import_promotes_dict_music_bed_ducking_envelope():
    """A {src, keyframes} music bed (a factory that bakes its own ducking envelope)
    must promote that envelope onto the bed clip instead of dropping it for the flat
    0.22 gain. The asset src is still the bare path string."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "musicBed": {"src": "/agenticnews-assets/bed.mp3", "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6}, {"t": 1.0, "value": 0.18},
                {"t": 3.0, "value": 0.6},
            ]},
        ]},
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    bed_asset = project["assets"][bed["assetId"]]
    assert bed_asset["src"] == "/agenticnews-assets/bed.mp3"
    env = next(t for t in bed["keyframes"] if t["property"] == "volume")
    assert [p["t"] for p in env["points"]] == [0.0, 1.0, 3.0]
    assert min(p["value"] for p in env["points"]) == 0.18
    # The envelope holds ABSOLUTE gains; the flat 0.22 must be neutralized to 1.0 so
    # the render path doesn't scale the envelope down (double-attenuation).
    assert bed["volume"] == 1.0


def test_import_keeps_flat_duck_when_promoted_envelope_has_no_volume_track():
    """The dual-attenuation guard (editor_timeline ~line 228) only neutralizes the
    flat 0.22 duck when the promoted envelope actually carries a `volume` track. If a
    factory attaches an envelope that animates a NON-volume property (e.g. opacity),
    the flat 0.22 MUST survive — otherwise the bed snaps to full volume and mixes
    OVER the VO. Pins the asymmetry of the `any(t.property == 'volume')` condition."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "musicBedKeyframes": [{"property": "opacity", "points": [
            {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.0},
        ]}],
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    # opacity envelope promoted, but no volume track -> flat duck stays in force.
    assert any(t["property"] == "opacity" for t in bed["keyframes"])
    assert not any(t["property"] == "volume" for t in bed["keyframes"])
    assert bed["volume"] == 0.22


def test_import_promotes_top_level_music_bed_keyframes_field():
    """`musicBedKeyframes` at the timeline top level (bed stays a path string) is also
    promoted onto the bed clip."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "musicBedKeyframes": [{"property": "volume", "points": [
            {"t": 0.0, "value": 0.5}, {"t": 2.0, "value": 0.2},
        ]}],
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    env = next(t for t in bed["keyframes"] if t["property"] == "volume")
    assert [p["t"] for p in env["points"]] == [0.0, 2.0]
    assert bed["volume"] == 1.0


def test_keyframed_bed_envelope_is_not_double_attenuated_by_flat_gain():
    """The whole point of the ducking-envelope upgrade: a render must apply the
    envelope's absolute gains, NOT scale them by the legacy flat 0.22.

    editor_render._volume_filter multiplies a volume envelope by the clip's flat
    `volume`. With the flat gain neutralized to 1.0 (because the envelope now drives
    ducking), the compiled ffmpeg expression must reproduce the envelope value 1:1 —
    at the intro the bed should be 0.6, not 0.6*0.22."""
    from services import editor_render

    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "musicBedKeyframes": [{"property": "volume", "points": [
            {"t": 0.0, "value": 0.6}, {"t": 1.0, "value": 0.22},
        ]}],
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    flt = editor_render._volume_filter(bed, float(bed["volume"]))
    # The envelope's absolute gains pass through 1:1 — the intro is 0.6, NOT
    # 0.6*0.22. _volume_filter no longer scales the envelope by the flat gain (it
    # mirrors openshot_bridge, where the envelope overwrites the flat volume), so
    # the flat 0.22 cannot double-attenuate regardless of whether it was neutralized.
    assert flt.startswith("volume='if(lt(t,")
    assert "0.6000" in flt and "(0.2200)*(" not in flt


def test_clip_keyframes_command_neutralizes_flat_bed_gain_post_import(tmp_path):
    """A bed imported with the flat volume=0.22 duck (NO envelope at import) and then
    given a volume envelope LATER via a `clip.keyframes` command must not keep both:
    the absolute envelope drives ducking, so the flat 0.22 is neutralized to 1.0.

    This is the post-import gap the importer's neutralization (~line 230) never covered:
    `clip.keyframes` previously just overwrote `clip.keyframes` and left the flat 0.22
    sitting alongside the new absolute envelope (a double-attenuation state). The render
    path is defensive, but the timeline state itself must stay consistent."""
    from services import editor_render

    store = timeline.TimelineStore(tmp_path)
    project = timeline.project_from_abn_timeline("proj_kf", {
        "episodeId": "e", "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",  # flat-ducked, no envelope at import
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })
    store.save(project)
    bed_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "music_bed")
    # Pre-condition: imported with the flat 0.22 duck and no volume envelope.
    assert project["clips"][bed_id]["volume"] == 0.22
    assert not any(t["property"] == "volume" for t in project["clips"][bed_id]["keyframes"])

    project = store.apply_command("proj_kf", {
        "op": "clip.keyframes",
        "actor": "human",
        "expectedRevision": project["revision"],
        "payload": {"clipId": bed_id, "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6}, {"t": 1.0, "value": 0.22},
            ]},
        ]},
    })
    bed = project["clips"][bed_id]
    # The envelope is installed AND the flat gain is neutralized to 1.0 — no stale 0.22.
    assert any(t["property"] == "volume" for t in bed["keyframes"])
    assert bed["volume"] == 1.0
    # End-to-end: the render path emits the envelope's absolute gains, not 0.6*0.22.
    flt = editor_render._volume_filter(bed, float(bed["volume"]))
    assert "0.6000" in flt and "(0.2200)*(" not in flt


def test_clip_keyframes_command_keeps_flat_gain_for_non_volume_envelope(tmp_path):
    """Asymmetry guard mirroring the importer: a `clip.keyframes` command that installs
    a NON-volume envelope (e.g. opacity) must NOT touch the flat volume duck — otherwise
    the bed would snap to full volume and mix over the VO."""
    store = timeline.TimelineStore(tmp_path)
    project = timeline.project_from_abn_timeline("proj_kf2", {
        "episodeId": "e", "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })
    store.save(project)
    bed_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "music_bed")

    project = store.apply_command("proj_kf2", {
        "op": "clip.keyframes",
        "actor": "human",
        "expectedRevision": project["revision"],
        "payload": {"clipId": bed_id, "keyframes": [
            {"property": "opacity", "points": [
                {"t": 0.0, "value": 1.0}, {"t": 1.0, "value": 0.0},
            ]},
        ]},
    })
    bed = project["clips"][bed_id]
    assert any(t["property"] == "opacity" for t in bed["keyframes"])
    assert not any(t["property"] == "volume" for t in bed["keyframes"])
    assert bed["volume"] == 0.22  # flat duck survives — no volume track to override it


def test_import_infers_missing_shot_durations_from_next_boundary():
    project = timeline.project_from_abn_timeline(
        "proj_boundaries",
        {
            "episodeId": "ep_boundaries",
            "segments": [
                {
                    "segmentId": "s0",
                    "durationSec": 6.0,
                    "shots": [
                        {
                            "id": "a",
                            "src": "/agenticnews-assets/a.png",
                            "startSec": 1.0,
                            "type": "artifact",
                        },
                        {
                            "id": "b",
                            "src": "/agenticnews-assets/b.png",
                            "startSec": 3.5,
                            "type": "artifact",
                        },
                    ],
                }
            ],
        },
    )

    clips = {clip["metadata"]["shot"]["id"]: clip for clip in project["clips"].values()}

    assert clips["a"]["duration"] == 2.5
    assert clips["b"]["duration"] == 2.5
    assert project["metadata"]["abnImportVersion"] == timeline.ABN_IMPORT_VERSION


def test_commands_mutate_only_through_revision_checked_command_log(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_cmd")
    store.save(project)

    project = store.apply_command(
        "proj_cmd",
        {
            "op": "asset.import",
            "actor": "test-agent",
            "expectedRevision": 0,
            "payload": {
                "assetId": "asset_card",
                "type": "image",
                "src": "/agenticnews-assets/card.png",
            },
        },
    )
    project = store.apply_command(
        "proj_cmd",
        {
            "op": "clip.create",
            "actor": "test-agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "clip_card",
                "assetId": "asset_card",
                "trackId": "graphics_1",
                "start": 12.0,
                "duration": 3.0,
            },
        },
    )
    project = store.apply_command(
        "proj_cmd",
        {
            "op": "clip.move",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "clip_card", "start": 12.5},
        },
    )

    assert project["revision"] == 3
    assert project["clips"]["clip_card"]["start"] == 12.5
    assert [c["op"] for c in project["commandLog"]] == [
        "asset.import",
        "clip.create",
        "clip.move",
    ]
    assert project["commandLog"][2]["actor"] == "human"
    assert project["commandLog"][2]["revision"] == 3

    with pytest.raises(timeline.RevisionConflict):
        store.apply_command(
            "proj_cmd",
            {
                "op": "clip.move",
                "actor": "late-agent",
                "expectedRevision": 2,
                "payload": {"clipId": "clip_card", "start": 13.0},
            },
        )


def test_replay_rebuilds_state_and_revert_appends_inverse_command(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_replay")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 1.0,
                "duration": 2.0,
            },
        },
        {
            "op": "clip.trim",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.25, "duration": 1.5},
        },
    ]:
        project = store.apply_command("proj_replay", command)

    rebuilt = timeline.replay_project(project)
    assert rebuilt["clips"] == project["clips"]
    assert rebuilt["assets"] == project["assets"]
    assert rebuilt["revision"] == project["revision"]

    reverted = store.revert_last_command("proj_replay", actor="human")
    assert reverted["revision"] == 4
    assert reverted["clips"]["c1"]["start"] == 1.0
    assert reverted["clips"]["c1"]["duration"] == 2.0
    assert reverted["commandLog"][-1]["op"] == "clip.update"


def test_revert_last_command_restores_transform_exactly_and_replays(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_undo_transform")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
        {
            "op": "clip.transform",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "transform": {"rotation": 12, "x": 0.25}},
        },
    ]:
        project = store.apply_command("proj_undo_transform", command)

    assert project["clips"]["c1"]["transform"]["rotation"] == 12

    reverted = store.revert_last_command("proj_undo_transform", actor="human", expected_revision=3)

    assert reverted["revision"] == 4
    assert reverted["commandLog"][-1]["op"] == "clip.update"
    assert "rotation" not in reverted["clips"]["c1"]["transform"]
    assert reverted["clips"]["c1"]["transform"] == {
        "x": 0.5,
        "y": 0.5,
        "scale": 1.0,
        "opacity": 1.0,
    }
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]
    assert rebuilt["revision"] == reverted["revision"]


def test_revert_last_split_restores_original_clip_and_removes_split_clip(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_undo_split")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 10,
                "duration": 6,
                "sourceStart": 2,
            },
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 12.5, "newClipId": "c1_b"},
        },
    ]:
        project = store.apply_command("proj_undo_split", command)

    assert set(project["clips"]) == {"c1", "c1_b"}

    reverted = store.revert_last_command("proj_undo_split", actor="human", expected_revision=3)

    assert reverted["revision"] == 4
    assert reverted["commandLog"][-1]["op"] == "clip.unsplit"
    assert set(reverted["clips"]) == {"c1"}
    assert reverted["clips"]["c1"]["start"] == 10
    assert reverted["clips"]["c1"]["duration"] == 6
    assert reverted["clips"]["c1"]["sourceStart"] == 2
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]


def test_direct_unsplit_requires_matching_recorded_split_inverse(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_unsplit_guard")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 10,
                "duration": 6,
                "sourceStart": 2,
            },
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 12.5, "newClipId": "c1_b"},
        },
    ]:
        project = store.apply_command("proj_unsplit_guard", command)

    split_command = project["commandLog"][-1]
    original = split_command["before"]["clip"]
    unsplit_payload = {
        "clipId": "c1",
        "createdClipId": "c1_b",
        "clip": original,
    }
    with pytest.raises(timeline.CommandValidationError, match="requires revertsCommandId"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 3,
                "payload": unsplit_payload,
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="does not reference a command"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": "cmd_missing",
                "payload": unsplit_payload,
            },
        )

    tampered_original = {**original, "duration": 9}
    with pytest.raises(timeline.CommandValidationError, match="payload does not match inverse"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": split_command["id"],
                "payload": {**unsplit_payload, "clip": tampered_original},
            },
        )

    project = store.apply_command(
        "proj_unsplit_guard",
        {
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1_b", "start": 13.0},
        },
    )
    with pytest.raises(timeline.CommandValidationError, match="current created clip no longer matches"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": project["revision"],
                "revertsCommandId": split_command["id"],
                "payload": unsplit_payload,
            },
        )


def test_reverts_command_id_must_match_server_inverse(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_revert_guard")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/a.png"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 2,
            },
        },
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1},
        },
    ]:
        project = store.apply_command("proj_revert_guard", command)

    with pytest.raises(timeline.CommandValidationError, match="payload does not match inverse"):
        store.apply_command(
            "proj_revert_guard",
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": "cmd_move",
                "payload": {
                    "clipId": "c1",
                    "patch": {"start": 99},
                },
            },
        )

    reverted = store.revert_last_command("proj_revert_guard", actor="human", expected_revision=3)

    assert reverted["commandLog"][-1]["revertsCommandId"] == "cmd_move"
    assert reverted["clips"]["c1"]["start"] == 0

    with pytest.raises(timeline.CommandValidationError, match="already reverted"):
        store.apply_command(
            "proj_revert_guard",
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 4,
                "revertsCommandId": "cmd_move",
                "payload": {
                    "clipId": "c1",
                    "patch": {"start": 0},
                },
            },
        )


def test_apply_command_deep_copies_payload_boundaries():
    command = {
        "op": "asset.import",
        "actor": "agent",
        "expectedRevision": 0,
        "payload": {
            "assetId": "a1",
            "type": "image",
            "src": "/a.png",
            "metadata": {"label": "original"},
        },
    }
    project = timeline.apply_command(timeline.new_project("proj_clone_boundary"), command)

    command["payload"]["metadata"]["label"] = "mutated"

    assert project["assets"]["a1"]["metadata"]["label"] == "original"
    assert project["commandLog"][0]["payload"]["metadata"]["label"] == "original"


def test_command_ids_must_be_unique(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_unique_command_ids")
    store.save(project)
    project = store.apply_command(
        "proj_unique_command_ids",
        {
            "id": "cmd_duplicate",
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/a.png"},
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="command id already exists"):
        store.apply_command(
            "proj_unique_command_ids",
            {
                "id": "cmd_duplicate",
                "op": "asset.import",
                "actor": "agent",
                "expectedRevision": 1,
                "payload": {"assetId": "a2", "type": "image", "src": "/b.png"},
            },
        )

    loaded = store.load("proj_unique_command_ids")
    assert loaded["revision"] == project["revision"]
    assert list(loaded["assets"]) == ["a1"]


def test_revert_last_marker_and_note_add_delete_only_added_items(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_undo_notes")
    store.save(project)
    project = store.apply_command(
        "proj_undo_notes",
        {
            "op": "marker.add",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"markerId": "m1", "time": 1.2, "label": "Review"},
        },
    )
    project = store.apply_command(
        "proj_undo_notes",
        {
            "op": "note.add",
            "actor": "human",
            "expectedRevision": 1,
            "payload": {"noteId": "n1", "text": "Fix beat", "target": {"time": 1.2}},
        },
    )

    reverted_note = store.revert_last_command("proj_undo_notes", actor="human", expected_revision=2)
    assert reverted_note["commandLog"][-1]["op"] == "note.delete"
    assert "n1" not in reverted_note["notes"]
    assert "m1" in reverted_note["markers"]

    reverted_marker = store.revert_last_command("proj_undo_notes", actor="human", expected_revision=3)
    assert reverted_marker["commandLog"][-1]["op"] == "marker.delete"
    assert "m1" not in reverted_marker["markers"]
    rebuilt = timeline.replay_project(reverted_marker)
    assert rebuilt["markers"] == reverted_marker["markers"]
    assert rebuilt["notes"] == reverted_marker["notes"]


def test_split_marker_note_and_property_commands(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_atomic")
    store.save(project)
    commands = [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 10.0,
                "duration": 6.0,
                "sourceStart": 2.0,
            },
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 12.5, "newClipId": "c1_b"},
        },
        {
            "op": "clip.opacity",
            "actor": "human",
            "expectedRevision": 3,
            "payload": {"clipId": "c1_b", "opacity": 0.4},
        },
        {
            "op": "marker.add",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"markerId": "m1", "time": 12.5, "label": "split review"},
        },
        {
            "op": "note.add",
            "actor": "agent",
            "expectedRevision": 5,
            "payload": {
                "noteId": "n1",
                "target": {"clipId": "c1_b", "time": 12.5},
                "text": "Check the post-split beat.",
                "suggestedCommand": {
                    "op": "clip.move",
                    "payload": {"clipId": "c1_b", "start": 12.75},
                },
            },
        },
        {
            "op": "clip.update",
            "actor": "human",
            "expectedRevision": 6,
            "payload": {
                "clipId": "c1_b",
                "patch": {
                    "volume": 0.25,
                    "transform": {"x": 0.4, "y": 0.6, "scale": 0.5, "opacity": 0.8},
                },
            },
        },
    ]
    for command in commands:
        project = store.apply_command("proj_atomic", command)

    assert project["clips"]["c1"]["start"] == 10.0
    assert project["clips"]["c1"]["duration"] == 2.5
    assert project["clips"]["c1_b"]["start"] == 12.5
    assert project["clips"]["c1_b"]["duration"] == 3.5
    assert project["clips"]["c1_b"]["sourceStart"] == 4.5
    assert project["clips"]["c1_b"]["transform"] == {"x": 0.4, "y": 0.6, "scale": 0.5, "opacity": 0.8}
    assert project["clips"]["c1_b"]["volume"] == 0.25
    assert project["markers"]["m1"]["label"] == "split review"
    assert project["notes"]["n1"]["suggestedCommand"]["op"] == "clip.move"


def test_split_preserves_keyframes_and_effects_on_both_halves(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_split_kf")
    store.save(project)
    commands = [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 10.0,
                "duration": 6.0,
            },
        },
        {
            # opacity ramp across the whole clip: 0 at t=0, 1 at t=2 (the split
            # point), 0.5 at t=5 (clip-local seconds).
            "op": "clip.keyframes",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "keyframes": [
                    {
                        "property": "opacity",
                        "points": [
                            {"t": 0.0, "value": 0.0, "interp": "linear"},
                            {"t": 2.0, "value": 1.0, "interp": "linear"},
                            {"t": 5.0, "value": 0.5, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
        {
            "op": "clip.effect.add",
            "actor": "human",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}}},
        },
        {
            "op": "clip.effect.add",
            "actor": "human",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "effect": {"id": "fx_out", "type": "fadeOut", "params": {"duration": 0.5}}},
        },
        {
            "op": "clip.effect.add",
            "actor": "human",
            "expectedRevision": 5,
            "payload": {"clipId": "c1", "effect": {"id": "fx_bri", "type": "brightness", "params": {"value": 0.2}}},
        },
        {
            # split 2s into the clip (timeline t=12.0)
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 6,
            "payload": {"clipId": "c1", "at": 12.0, "newClipId": "c1_b"},
        },
    ]
    for command in commands:
        project = store.apply_command("proj_split_kf", command)

    head = project["clips"]["c1"]
    tail = project["clips"]["c1_b"]

    # Head keeps only the points up to the split (t <= 2), unshifted.
    head_pts = head["keyframes"][0]["points"]
    assert [(p["t"], p["value"]) for p in head_pts] == [(0.0, 0.0), (2.0, 1.0)]

    # Tail rebases to its own t=0 at the split: t=2 -> 0, t=5 -> 3.
    tail_pts = tail["keyframes"][0]["points"]
    assert [(p["t"], p["value"]) for p in tail_pts] == [(0.0, 1.0), (3.0, 0.5)]

    # fadeIn is start-anchored: full on the head; on the tail the 2s front trim fully
    # eats the 0.5s ramp (0.5 - 2.0 <= 0), so it is DROPPED rather than kept as a 0s
    # no-op fade. fadeOut is end-anchored: dropped from the head, kept on the tail.
    # brightness is whole-clip and stays on both.
    assert {e["id"] for e in head["effects"]} == {"fx_in", "fx_bri"}
    assert {e["id"] for e in tail["effects"]} == {"fx_out", "fx_bri"}
    tail_fx = {e["id"]: e for e in tail["effects"]}
    assert tail_fx["fx_out"]["params"]["duration"] == 0.5

    # Replay reproduces the same partitioned envelopes deterministically.
    rebuilt = timeline.replay_project(project)
    assert rebuilt["clips"]["c1"]["keyframes"] == head["keyframes"]
    assert rebuilt["clips"]["c1_b"]["keyframes"] == tail["keyframes"]
    assert rebuilt["clips"]["c1"]["effects"] == head["effects"]
    assert rebuilt["clips"]["c1_b"]["effects"] == tail["effects"]


def test_clip_hide_show_mute_unmute_toggle_enabled_and_muted_flags(tmp_path):
    """The four UI visibility/audio toggles flip exactly the `enabled`/`muted`
    booleans and leave every other clip field untouched."""
    store = timeline.TimelineStore(tmp_path)
    project = _seed_project_with_clip(store, "proj_toggles")

    # Fresh clips start visible and audible.
    assert project["clips"]["c1"]["enabled"] is True
    assert project["clips"]["c1"]["muted"] is False

    for revision, (op, expected_enabled, expected_muted) in enumerate(
        [
            ("clip.hide", False, False),
            ("clip.mute", False, True),
            ("clip.show", True, True),
            ("clip.unmute", True, False),
        ],
        start=2,
    ):
        project = store.apply_command(
            "proj_toggles",
            {"op": op, "actor": "human", "expectedRevision": revision, "payload": {"clipId": "c1"}},
        )
        assert project["clips"]["c1"]["enabled"] is expected_enabled, op
        assert project["clips"]["c1"]["muted"] is expected_muted, op

    # No collateral damage to the rest of the clip.
    assert project["clips"]["c1"]["start"] == 0
    assert project["clips"]["c1"]["duration"] == 4
    assert project["clips"]["c1"]["volume"] == 1.0


def test_revert_last_clip_mute_restores_prior_muted_flag_and_replays(tmp_path):
    """clip.mute is revertible: undo emits a clip.update inverse that restores the
    pre-mute `muted` flag, and replaying the log reproduces the reverted state."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_undo_mute")

    project = store.apply_command(
        "proj_undo_mute",
        {"op": "clip.mute", "actor": "human", "expectedRevision": 2, "payload": {"clipId": "c1"}},
    )
    assert project["clips"]["c1"]["muted"] is True

    reverted = store.revert_last_command("proj_undo_mute", actor="human", expected_revision=3)

    assert reverted["revision"] == 4
    assert reverted["commandLog"][-1]["op"] == "clip.update"
    assert reverted["clips"]["c1"]["muted"] is False
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]
    assert rebuilt["revision"] == reverted["revision"]


def test_clip_update_accepts_direct_patch_fields_for_agent_control(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_direct_update")
    store.save(project)
    project = store.apply_command(
        "proj_direct_update",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_direct_update",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )

    project = store.apply_command(
        "proj_direct_update",
        {
            "op": "clip.update",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "volume": 0.25,
                "transform": {"x": 0.4, "y": 0.6, "scale": 0.5, "opacity": 0.8},
            },
        },
    )

    assert project["clips"]["c1"]["volume"] == 0.25
    assert project["clips"]["c1"]["transform"] == {
        "x": 0.4,
        "y": 0.6,
        "scale": 0.5,
        "opacity": 0.8,
    }


def test_clip_update_rejects_invalid_transform_values(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_invalid_transform")
    store.save(project)
    project = store.apply_command(
        "proj_invalid_transform",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_invalid_transform",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="transform.x must be numeric"):
        store.apply_command(
            "proj_invalid_transform",
            {
                "op": "clip.update",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "c1",
                    "patch": {
                        "transform": {
                            "x": "bad",
                            "y": 0.5,
                            "scale": 1,
                            "opacity": 1,
                        },
                    },
                },
            },
        )

    loaded = store.load("proj_invalid_transform")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["transform"] == project["clips"]["c1"]["transform"]


def test_clip_update_rejects_missing_track_id(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_invalid_track_update")
    store.save(project)
    project = store.apply_command(
        "proj_invalid_track_update",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_invalid_track_update",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="track does not exist"):
        store.apply_command(
            "proj_invalid_track_update",
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "c1",
                    "patch": {"trackId": "missing_track"},
                },
            },
        )

    loaded = store.load("proj_invalid_track_update")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["trackId"] == project["clips"]["c1"]["trackId"]


def test_clip_update_moves_clip_to_a_valid_track_through_command_log_round_trip(tmp_path):
    """clip.update with a valid trackId reassigns the clip's track and the move
    survives both a save/load and a full command-log replay."""
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_track_switch"))
    store.apply_command(
        "proj_track_switch",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_track_switch",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )
    assert project["clips"]["c1"]["trackId"] == "video_1"

    project = store.apply_command(
        "proj_track_switch",
        {
            "op": "clip.update",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "patch": {"trackId": "graphics_1"},
            },
        },
    )
    assert project["clips"]["c1"]["trackId"] == "graphics_1"

    loaded = store.load("proj_track_switch")
    assert loaded["clips"]["c1"]["trackId"] == "graphics_1"

    replayed = timeline.replay_project(loaded)
    assert replayed["clips"]["c1"]["trackId"] == "graphics_1"


def test_invalid_commands_are_rejected_without_mutating_state(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_invalid"))

    with pytest.raises(timeline.CommandValidationError):
        store.apply_command(
            "proj_invalid",
            {
                "op": "clip.create",
                "actor": "agent",
                "expectedRevision": 0,
                "payload": {
                    "clipId": "c_missing_asset",
                    "assetId": "missing",
                    "trackId": "video_1",
                    "start": 0,
                    "duration": 1,
                },
            },
        )

    project = store.load("proj_invalid")
    assert project["revision"] == 0
    assert project["clips"] == {}
    assert project["commandLog"] == []


def _seed_project_with_clip(store, project_id):
    """Create a project with one imported asset and clip `c1` on video_1 (revision 2)."""
    store.save(timeline.new_project(project_id))
    store.apply_command(
        project_id,
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    return store.apply_command(
        project_id,
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 4,
            },
        },
    )


@pytest.mark.parametrize(
    "op,payload",
    [
        ("clip.move", {"clipId": "ghost", "start": 1.0}),
        ("clip.split", {"clipId": "ghost", "at": 1.0}),
        ("clip.trim", {"clipId": "ghost", "duration": 1.0}),
        ("clip.mute", {"clipId": "ghost"}),
        ("clip.transform", {"clipId": "ghost", "transform": {"x": 0.4}}),
    ],
)
def test_clip_operations_on_missing_clip_are_rejected_without_mutating_state(tmp_path, op, payload):
    """Every clip-targeted op funnels through _require_clip; a missing clipId must
    raise CommandValidationError and leave the project untouched."""
    store = timeline.TimelineStore(tmp_path)
    project = _seed_project_with_clip(store, "proj_missing_clip")

    with pytest.raises(timeline.CommandValidationError, match="clip does not exist: ghost"):
        store.apply_command(
            "proj_missing_clip",
            {"op": op, "actor": "agent", "expectedRevision": 2, "payload": payload},
        )

    loaded = store.load("proj_missing_clip")
    assert loaded["revision"] == project["revision"]
    assert set(loaded["clips"]) == {"c1"}


def test_clip_op_without_clip_id_is_rejected(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_no_clip_id")

    with pytest.raises(timeline.CommandValidationError, match="clipId is required"):
        store.apply_command(
            "proj_no_clip_id",
            {"op": "clip.move", "actor": "agent", "expectedRevision": 2, "payload": {"start": 1.0}},
        )

    assert store.load("proj_no_clip_id")["revision"] == 2


def test_apply_command_rejects_stale_revision_without_mutating(tmp_path):
    """The RevisionConflict guard in apply_command fires when expectedRevision is behind
    the live project, and the rejected command never touches state."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_stale")  # advances to revision 2

    with pytest.raises(timeline.RevisionConflict, match="expected revision 0, current revision 2"):
        store.apply_command(
            "proj_stale",
            {
                "op": "clip.move",
                "actor": "stale-agent",
                "expectedRevision": 0,
                "payload": {"clipId": "c1", "start": 2.0},
            },
        )

    loaded = store.load("proj_stale")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["start"] == 0


def test_apply_command_requires_expected_revision(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_no_rev"))

    with pytest.raises(timeline.CommandValidationError, match="expectedRevision is required"):
        store.apply_command(
            "proj_no_rev",
            {"op": "asset.import", "actor": "agent", "payload": {"assetId": "a1", "type": "image", "src": "/a.png"}},
        )


def test_revert_last_command_on_empty_log_is_rejected(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_empty_log"))

    with pytest.raises(timeline.CommandValidationError, match="no command to revert"):
        store.revert_last_command("proj_empty_log", actor="human")


def test_revert_last_command_rejects_stale_expected_revision(tmp_path):
    """revert_last_command has its own RevisionConflict guard separate from apply_command."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_revert_stale")  # revision 2

    with pytest.raises(timeline.RevisionConflict, match="expected revision 99, current revision 2"):
        store.revert_last_command("proj_revert_stale", actor="human", expected_revision=99)

    # nothing reverted: clip.create is still the last log entry
    assert store.load("proj_revert_stale")["commandLog"][-1]["op"] == "clip.create"


def test_revert_of_unrevertable_op_is_rejected(tmp_path):
    """asset.import has no inverse; reverting it must raise rather than corrupt state."""
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_unrevertable"))
    store.apply_command(
        "proj_unrevertable",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/a.png"},
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="cannot revert command asset.import"):
        store.revert_last_command("proj_unrevertable", actor="human")

    assert store.load("proj_unrevertable")["revision"] == 1


def test_unsplit_requires_original_clip_and_created_clip_id(tmp_path):
    """The early structural guards in the clip.unsplit branch reject malformed payloads
    before any _clip_values_match() comparison runs."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_unsplit_payload")  # revision 2

    with pytest.raises(timeline.CommandValidationError, match="requires original clip"):
        store.apply_command(
            "proj_unsplit_payload",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"createdClipId": "c1_b"},
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="requires createdClipId"):
        store.apply_command(
            "proj_unsplit_payload",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clip": {"id": "c1"}},
            },
        )

    assert store.load("proj_unsplit_payload")["revision"] == 2


def test_unsplit_reverts_command_id_must_point_at_a_split(tmp_path):
    """A clip.unsplit whose revertsCommandId targets a non-split entry is rejected before
    mutating clips. The generic _validate_reverts_command_id guard fires first because a
    clip.move's server inverse is a clip.update, not a clip.unsplit."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_unsplit_nonsplit")
    project = store.apply_command(
        "proj_unsplit_nonsplit",
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.0},
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="op does not match inverse command"):
        store.apply_command(
            "proj_unsplit_nonsplit",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": project["revision"],
                "revertsCommandId": "cmd_move",
                "payload": {
                    "clipId": "c1",
                    "createdClipId": "c1_b",
                    "clip": {"id": "c1"},
                },
            },
        )

    assert store.load("proj_unsplit_nonsplit")["revision"] == project["revision"]


@pytest.mark.parametrize(
    "transform,message",
    [
        ({"opacity": 1.5}, "transform.opacity must be between 0.0 and 1.0"),
        ({"opacity": -0.1}, "transform.opacity must be non-negative"),
        ({"scale": 0}, "transform.scale must be positive"),
        ({"scale": -2}, "transform.scale must be non-negative"),
    ],
)
def test_clip_transform_rejects_out_of_bounds_values(tmp_path, transform, message):
    """_validated_transform enforces opacity in [0,1] and scale > 0. Out-of-bounds
    payloads must be rejected, not clamped, and must not advance the revision."""
    store = timeline.TimelineStore(tmp_path)
    project = _seed_project_with_clip(store, "proj_transform_bounds")
    baseline = project["clips"]["c1"]["transform"]

    with pytest.raises(timeline.CommandValidationError, match=message):
        store.apply_command(
            "proj_transform_bounds",
            {
                "op": "clip.transform",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "transform": transform},
            },
        )

    loaded = store.load("proj_transform_bounds")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["transform"] == baseline


def test_clip_opacity_op_rejects_out_of_range_value(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_opacity_bounds")

    with pytest.raises(timeline.CommandValidationError, match="opacity must be between 0.0 and 1.0"):
        store.apply_command(
            "proj_opacity_bounds",
            {
                "op": "clip.opacity",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "opacity": 2.0},
            },
        )

    assert store.load("proj_opacity_bounds")["revision"] == 2


def test_clip_volume_op_rejects_negative_value(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_volume_neg")

    with pytest.raises(timeline.CommandValidationError, match="volume must be non-negative"):
        store.apply_command(
            "proj_volume_neg",
            {
                "op": "clip.volume",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "volume": -0.5},
            },
        )

    assert store.load("proj_volume_neg")["revision"] == 2


def test_split_outside_clip_bounds_is_rejected(tmp_path):
    """clip.split must reject a split point at or beyond the clip edges."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_split_bounds")  # c1 start=0 duration=4

    with pytest.raises(timeline.CommandValidationError, match="split point must be inside the clip"):
        store.apply_command(
            "proj_split_bounds",
            {
                "op": "clip.split",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "at": 4.0},
            },
        )

    loaded = store.load("proj_split_bounds")
    assert loaded["revision"] == 2
    assert set(loaded["clips"]) == {"c1"}


def test_split_partitions_and_rebases_keyframes_and_effects(tmp_path):
    """clip.split must not blindly clone animation onto the tail half.

    A clip [0, 10) with a fadeIn (head-anchored), a fadeOut (tail-anchored) and a
    volume envelope is split at t=8.0. The head keeps fadeIn and only the points up
    to the split (t <= 8); the tail keeps fadeOut and rebases its points to its new
    t=0 (t >= 8 → t -= 8). Without this, the tail clip fires the parent's keyframes
    at the wrong absolute times and inherits a fadeIn it should not have.
    """
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_split_anim"))
    store.apply_command(
        "proj_split_anim",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0.0,
                "duration": 10.0,
            },
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "effect": {"id": "fi", "type": "fadeIn", "params": {"duration": 1.0}}},
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fo", "type": "fadeOut", "params": {"duration": 1.0}}},
        },
    )
    # crossfade is start-anchored too (maps to OpenShot's `in` Fade): it must follow
    # fadeIn onto the head and NOT be cloned onto the tail with the parent's timing.
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "effect": {"id": "xf", "type": "crossfade", "params": {"duration": 1.0}}},
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 5,
            "payload": {
                "clipId": "c1",
                "keyframes": [
                    {
                        "property": "opacity",
                        "points": [
                            {"t": 0.0, "value": 1.0, "interp": "linear"},
                            {"t": 7.5, "value": 0.5, "interp": "linear"},
                            {"t": 8.0, "value": 0.4, "interp": "linear"},
                            {"t": 9.5, "value": 0.0, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
    )

    project = store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 6,
            "payload": {"clipId": "c1", "at": 8.0, "newClipId": "c1_tail"},
        },
    )

    head = project["clips"]["c1"]
    tail = project["clips"]["c1_tail"]

    # Geometry split as before.
    assert head["duration"] == 8.0
    assert (tail["start"], tail["duration"]) == (8.0, 2.0)

    # Head owns the original start: it keeps the start-anchored fadeIn + crossfade at
    # full duration and drops the end-anchored fadeOut.
    assert [e["id"] for e in head["effects"]] == ["fi", "xf"]
    head_fades = {e["id"]: e["params"]["duration"] for e in head["effects"]}
    assert head_fades == {"fi": 1.0, "xf": 1.0}

    # Tail owns the original end: its 8s-trimmed front fully eats the start-anchored
    # fadeIn + crossfade (1.0 - 8.0 <= 0), so both are DROPPED (re-fit like the render
    # path's editor_render._refit_effects) rather than kept as 0s no-op fades OpenShot
    # ignores. The end-anchored fadeOut survives.
    assert [e["id"] for e in tail["effects"]] == ["fo"]
    tail_fades = {e["id"]: e["params"]["duration"] for e in tail["effects"]}
    assert tail_fades == {"fo": 1.0}

    # Head envelope keeps only points up to the split (t <= 8); the t=9.5 point is gone.
    assert [p["t"] for p in head["keyframes"][0]["points"]] == [0.0, 7.5, 8.0]
    # Tail envelope keeps points at/after the split, rebased to its new t=0.
    assert [p["t"] for p in tail["keyframes"][0]["points"]] == [0.0, 1.5]
    assert tail["keyframes"][0]["points"][0]["value"] == 0.4  # the boundary point anchors the tail
    assert tail["keyframes"][0]["points"][1]["value"] == 0.0  # was t=9.5 on the parent


def test_split_refits_crossfade_duration_on_tail(tmp_path):
    """clip.split must re-fit a start-anchored crossfade onto the tail half.

    crossfade is start-anchored (it maps to OpenShot's `in` Fade, same as fadeIn —
    see editor_render._refit_effects / openshot_bridge._FADE_DIRECTION_MAP). It is a
    whole-clip effect, so the split keeps it on BOTH halves — but the tail's source
    front is trimmed by `split_offset`, which eats that much of the crossfade ramp.
    Blindly cloning the parent's full crossfade duration onto the tail (ticket #1 bug)
    over-fades a windowed clip: a 3s crossfade on a clip split so the tail is 2s long
    would crossfade the entire tail.

    The render path already gets this right in editor_render._refit_effects with
    front_trim=split_offset and windowed_duration=tail_duration; clip.split must
    produce the SAME crossfade duration so the timeline and the compiled video agree.
    """
    from services import editor_render

    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_split_xfade"))
    store.apply_command(
        "proj_split_xfade",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    store.apply_command(
        "proj_split_xfade",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0.0,
                "duration": 10.0,
            },
        },
    )
    store.apply_command(
        "proj_split_xfade",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "effect": {"id": "xf", "type": "crossfade", "params": {"duration": 3.0}}},
        },
    )

    # Split so the tail is only 2s long; the start-anchored 3s crossfade can't survive
    # intact on a 2s clip whose front was trimmed by 8s.
    project = store.apply_command(
        "proj_split_xfade",
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "at": 8.0, "newClipId": "c1_tail"},
        },
    )

    head = project["clips"]["c1"]
    tail = project["clips"]["c1_tail"]

    split_offset = 8.0
    tail_duration = 2.0

    # Head owns the original start, so its crossfade is unchanged.
    head_xf = next(e for e in head["effects"] if e["id"] == "xf")
    assert head_xf["params"]["duration"] == 3.0

    # Tail's crossfade must match exactly what the render path would compute, so the
    # timeline state and the compiled OpenShot fade agree. front_trim is the split
    # offset (the tail's source front moves forward by that much); windowed_duration
    # is the tail's new duration.
    expected = editor_render._refit_effects(
        [{"id": "xf", "type": "crossfade", "params": {"duration": 3.0}}],
        front_trim=split_offset,
        windowed_duration=tail_duration,
    )
    # A 3s start-anchored fade trimmed by 8s re-fits to <= 0 and is DROPPED entirely —
    # not kept as a `duration: 0.0` no-op fade OpenShot silently ignores.
    assert expected == []

    tail_xf = [e for e in tail["effects"] if e["id"] == "xf"]
    assert tail_xf == [], (
        "a crossfade fully consumed by the split front trim must be dropped on the "
        "tail, matching editor_render._refit_effects — not carried as a 0s no-op fade"
    )


def test_abn_import_does_not_duplicate_crossfade_when_shot_has_explicit_and_transition():
    """An ABN shot can carry BOTH an explicit `effects` crossfade and an inter-clip
    `transitionSec` (the factory's choreographed boundary dissolve). Both routes
    create a `crossfade` editor effect, so a naive import would stack TWO crossfades
    on one clip — conflicting Fade-`in` effects OpenShot would composite twice.

    `_shot_effects` dedupes: an explicit crossfade WINS and the synthesized
    transitionSec one is suppressed. This pins the no-duplicate contract the ticket
    flags (explicit-vs-synthesized crossfade interaction)."""
    shot = {
        "id": "shot_x",
        "src": "/agenticnews-assets/remotion_a.mp4",
        "type": "remotion",
        "startSec": 0.0,
        "durationSec": 4.0,
        # Author-set explicit crossfade.
        "effects": [{"id": "fx_xf", "type": "crossfade", "params": {"duration": 0.5}}],
        # Factory-choreographed boundary transition into this shot.
        "transitionSec": 1.0,
    }
    effects = timeline._shot_effects(shot)

    crossfades = [e for e in effects if e["type"] == "crossfade"]
    assert len(crossfades) == 1, "explicit + transitionSec must NOT stack two crossfades"
    # The explicit one wins; the synthesized `xf_shot` (transitionSec=1.0) is dropped.
    assert crossfades[0]["id"] == "fx_xf"
    assert crossfades[0]["params"]["duration"] == 0.5


def test_abn_import_synthesizes_single_crossfade_from_transition_only():
    """A shot carrying only `transitionSec` (no explicit effects crossfade) gets
    exactly one synthesized start-anchored crossfade at that duration — the factory's
    boundary dissolve reaching OpenShot without a manual edit."""
    shot = {"id": "s2", "src": "/agenticnews-assets/y.mp4", "type": "remotion", "transitionSec": 0.75}
    effects = timeline._shot_effects(shot)

    crossfades = [e for e in effects if e["type"] == "crossfade"]
    assert len(crossfades) == 1
    assert crossfades[0]["id"] == "xf_shot"
    assert crossfades[0]["params"]["duration"] == 0.75


def test_imported_clip_crossfade_survives_split_and_exports_to_openshot():
    """End-to-end: an imported ABN clip with a synthesized transitionSec crossfade is
    split, then exported to OpenShot. The split keeps the start-anchored crossfade on
    the HEAD (which owns the original clip start), drops it from the tail, and the head
    exports to a libopenshot Fade-`in` effect carrying that crossfade's duration. This
    proves the crossfade survives the import -> split -> OpenShot-export pipeline as a
    single, correctly-directed fade (no duplicate, no conflicting tail fade)."""
    from services import openshot_bridge

    abn = {
        "episodeId": "ep_xf",
        "fps": 10,
        "width": 64,
        "height": 48,
        "totalSec": 8.0,
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 8.0,
                "shots": [
                    {
                        "id": "shot_a",
                        "src": "/agenticnews-assets/remotion_a.mp4",
                        "type": "remotion",
                        "startSec": 0.0,
                        "durationSec": 8.0,
                        "transitionSec": 2.0,
                    }
                ],
            }
        ],
    }
    project = timeline.project_from_abn_timeline("proj_xf_e2e", abn)
    clip_id = next(
        cid for cid, clip in project["clips"].items() if clip["kind"] == "remotion"
    )
    # The import synthesized exactly one start-anchored crossfade.
    assert [e["type"] for e in project["clips"][clip_id]["effects"]] == ["crossfade"]

    project["revision"] = 0  # apply_command targets a fresh-revision project
    split = timeline.apply_command(
        project,
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"clipId": clip_id, "at": 6.0, "newClipId": "xf_tail"},
        },
    )
    head = split["clips"][clip_id]
    tail = split["clips"]["xf_tail"]

    # Head owns the original start -> keeps the crossfade at full duration. The tail's
    # front is trimmed by the 6s split, fully eating the 2s start-anchored crossfade
    # (2.0 - 6.0 <= 0), so it is DROPPED exactly like editor_render._refit_effects —
    # not kept as a duration-0 no-op fade OpenShot silently ignores. Dropping it keeps
    # the timeline state and the compiled OpenShot fade in agreement.
    assert [e["type"] for e in head["effects"]] == ["crossfade"]
    assert tail["effects"] == []

    # The surviving head crossfade exports to a libopenshot Fade-`in` with its duration.
    head_json = openshot_bridge.clip_json(split, head)
    fades = [e for e in head_json["effects"] if e["type"] == "Fade"]
    assert len(fades) == 1, "exactly one Fade effect (no duplicate from synth+export)"
    assert fades[0]["fade"] == "in"  # crossfade is start-anchored
    assert fades[0]["duration"]["Points"][0]["co"]["Y"] == 2.0  # carried duration


def test_unsupported_op_is_rejected(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_bad_op"))

    with pytest.raises(timeline.CommandValidationError, match="unsupported command op: clip.teleport"):
        store.apply_command(
            "proj_bad_op",
            {"op": "clip.teleport", "actor": "agent", "expectedRevision": 0, "payload": {}},
        )

    assert store.load("proj_bad_op")["revision"] == 0


def _project_with_bed_clip(store: timeline.TimelineStore, project_id: str) -> dict:
    store.save(timeline.new_project(project_id))
    store.apply_command(
        project_id,
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "bed", "type": "audio", "src": "/agenticnews-assets/bed.mp3"},
        },
    )
    return store.apply_command(
        project_id,
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "bed_clip",
                "assetId": "bed",
                "trackId": "music_1",
                "start": 0.0,
                "duration": 6.0,
            },
        },
    )


def test_clip_keyframes_command_sets_sorted_envelope_and_reverts(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _project_with_bed_clip(store, "proj_keyframes")

    # Unsorted-on-purpose ducking envelope: full -> ducked under VO -> back up.
    project = store.apply_command(
        "proj_keyframes",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "bed_clip",
                "keyframes": [
                    {
                        "property": "volume",
                        "points": [
                            {"t": 5.0, "value": 1.0, "interp": "linear"},
                            {"t": 0.0, "value": 1.0},
                            {"t": 1.0, "value": 0.22, "interp": "constant"},
                        ],
                    }
                ],
            },
        },
    )

    envelope = project["clips"]["bed_clip"]["keyframes"]
    assert [p["t"] for p in envelope[0]["points"]] == [0.0, 1.0, 5.0]  # sorted by time
    assert envelope[0]["points"][1]["interp"] == "constant"
    assert envelope[0]["points"][0]["interp"] == "linear"  # default applied

    reverted = store.revert_last_command("proj_keyframes", actor="human", expected_revision=3)
    assert reverted["clips"]["bed_clip"]["keyframes"] == []  # back to flat


def test_clip_effect_add_update_delete_and_revert(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_effects")  # clip c1 at revision 2

    # add a fadeIn effect
    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}},
        },
    )
    assert project["clips"]["c1"]["effects"] == [
        {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}
    ]

    # update it in place
    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.update",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 1.5}}},
        },
    )
    assert project["clips"]["c1"]["effects"][0]["params"]["duration"] == 1.5

    # add a second effect, then delete the first by id
    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "id": "fx2", "type": "brightness", "params": {"value": -0.3}},
        },
    )
    assert [e["id"] for e in project["clips"]["c1"]["effects"]] == ["fx1", "fx2"]

    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.delete",
            "actor": "agent",
            "expectedRevision": 5,
            "payload": {"clipId": "c1", "effectId": "fx1"},
        },
    )
    assert [e["id"] for e in project["clips"]["c1"]["effects"]] == ["fx2"]

    # revert the delete: fx1 comes back (effects list restored exactly)
    reverted = store.revert_last_command("proj_effects", actor="human", expected_revision=6)
    assert reverted["commandLog"][-1]["op"] == "clip.update"
    assert [e["id"] for e in reverted["clips"]["c1"]["effects"]] == ["fx1", "fx2"]

    # replay rebuilds identical state from the command log
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]
    assert rebuilt["revision"] == reverted["revision"]


def test_clip_effect_delete_last_effect_keeps_keyframes_into_openshot_bridge(tmp_path):
    """Cross-module contract: clip.effect.delete only rewrites clip['effects']; the
    sibling clip['keyframes'] envelope must stay intact and still reach
    openshot_bridge.clip_json — even when the LAST effect is deleted (effects -> []).

    effects and keyframes are sibling fields on one in-place-mutated clip dict, and
    clip_json reads both off that same live clip, so they cannot desync. This test
    pins that behavior so a future refactor of the delete path can't silently drop
    the keyframe envelope at export time.
    """
    from services import openshot_bridge

    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_fx_kf")  # clip c1 at revision 2

    # Author a volume keyframe envelope on the clip.
    store.apply_command(
        "proj_fx_kf",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "keyframes": [
                    {
                        "property": "volume",
                        "points": [
                            {"t": 0.0, "value": 0.2},
                            {"t": 4.0, "value": 1.0},
                        ],
                    }
                ],
            },
        },
    )

    # Add a single effect, then delete it — leaving effects empty.
    store.apply_command(
        "proj_fx_kf",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}},
        },
    )
    project = store.apply_command(
        "proj_fx_kf",
        {
            "op": "clip.effect.delete",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "effectId": "fx1"},
        },
    )

    clip = project["clips"]["c1"]
    # Deleting the last effect empties effects but must not touch keyframes.
    assert clip["effects"] == []
    assert [t["property"] for t in clip["keyframes"]] == ["volume"]

    # The cross-module read: clip_json sees no effects but a real volume envelope.
    oj = openshot_bridge.clip_json(project, clip)
    assert oj["effects"] == []
    vol_points = oj["volume"]["Points"]
    assert len(vol_points) == 2  # multi-point envelope, not a flattened single point
    # editor volume is scaled by 100 for libopenshot; the duck-then-rise survives.
    assert vol_points[0]["co"]["Y"] < vol_points[-1]["co"]["Y"]


def test_clip_effect_commands_reject_bad_type_params_and_duplicates(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_effects_bad")  # revision 2

    # unknown effect type
    with pytest.raises(timeline.CommandValidationError, match="unsupported effect type"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "fx", "type": "warp", "params": {}},
            },
        )

    # out-of-bounds param value
    with pytest.raises(timeline.CommandValidationError, match="effect.value must be between"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "fx", "type": "brightness", "params": {"value": 5}},
            },
        )

    # unknown param key
    with pytest.raises(timeline.CommandValidationError, match="unsupported effect params"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "fx", "type": "fadeIn", "params": {"duration": 1, "bogus": 2}},
            },
        )

    # updating / deleting an effect that doesn't exist
    with pytest.raises(timeline.CommandValidationError, match="effect does not exist"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.update",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "ghost", "type": "fadeIn", "params": {"duration": 1}},
            },
        )
    with pytest.raises(timeline.CommandValidationError, match="effect does not exist"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.delete",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "effectId": "ghost"},
            },
        )

    # duplicate effect id on add
    project = store.apply_command(
        "proj_effects_bad",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "id": "fx1", "type": "fadeOut", "params": {"duration": 0.5}},
        },
    )
    with pytest.raises(timeline.CommandValidationError, match="effect already exists"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": project["revision"],
                "payload": {"clipId": "c1", "id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}},
            },
        )

    # the failed commands never advanced revision past the one good add
    assert store.load("proj_effects_bad")["revision"] == 3


def test_clip_keyframes_command_rejects_bad_property_and_interp(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _project_with_bed_clip(store, "proj_keyframes_bad")

    with pytest.raises(timeline.CommandValidationError, match="unsupported keyframe property"):
        store.apply_command(
            "proj_keyframes_bad",
            {
                "op": "clip.keyframes",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "bed_clip",
                    "keyframes": [{"property": "warp", "points": [{"t": 0, "value": 1}]}],
                },
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="unsupported keyframe interp"):
        store.apply_command(
            "proj_keyframes_bad",
            {
                "op": "clip.keyframes",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "bed_clip",
                    "keyframes": [
                        {"property": "volume", "points": [{"t": 0, "value": 1, "interp": "warp"}]}
                    ],
                },
            },
        )

    assert store.load("proj_keyframes_bad")["revision"] == 2  # nothing committed


def test_clip_keyframes_multi_track_sorts_each_and_keeps_boundary_values(tmp_path):
    """Two overlapping tracks (volume + opacity) in one envelope, each with
    boundary-value t/value pairs and an out-of-order point. Exercises the
    per-track sort (line 986) independently and confirms boundary numbers
    (t=0.0, value=0.0, value=1.0) survive validation rather than being
    rejected or clamped away."""
    store = timeline.TimelineStore(tmp_path)
    _project_with_bed_clip(store, "proj_keyframes_multi")

    project = store.apply_command(
        "proj_keyframes_multi",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "bed_clip",
                "keyframes": [
                    # volume track: unsorted, includes the 0.0 floor boundary
                    {
                        "property": "volume",
                        "points": [
                            {"t": 2.0, "value": 1.0, "interp": "linear"},
                            {"t": 0.0, "value": 0.0, "interp": "constant"},
                        ],
                    },
                    # opacity track: overlaps the same time window as volume,
                    # includes the 1.0 ceiling boundary and a duplicate-t pair
                    {
                        "property": "opacity",
                        "points": [
                            {"t": 2.0, "value": 1.0},
                            {"t": 2.0, "value": 0.5},
                            {"t": 0.0, "value": 1.0},
                        ],
                    },
                ],
            },
        },
    )

    envelope = project["clips"]["bed_clip"]["keyframes"]
    # Both tracks persist, in submitted order; each sorted by t independently.
    assert [t["property"] for t in envelope] == ["volume", "opacity"]
    vol, opac = envelope
    assert [p["t"] for p in vol["points"]] == [0.0, 2.0]
    assert [p["value"] for p in vol["points"]] == [0.0, 1.0]  # 0.0 floor kept
    assert vol["points"][0]["interp"] == "constant"
    assert [p["t"] for p in opac["points"]] == [0.0, 2.0, 2.0]  # stable on dup-t
    assert opac["points"][0]["value"] == 1.0  # 1.0 ceiling kept


def test_clip_keyframes_rejects_empty_points_and_non_dict_members(tmp_path):
    """Envelope structural edges: an empty point list, a non-dict track, and a
    non-dict point inside an otherwise-valid track must each raise and commit
    nothing (the envelope is rejected wholesale, not partially applied)."""
    store = timeline.TimelineStore(tmp_path)
    _project_with_bed_clip(store, "proj_keyframes_empty")

    def _apply(keyframes):
        store.apply_command(
            "proj_keyframes_empty",
            {
                "op": "clip.keyframes",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "bed_clip", "keyframes": keyframes},
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="requires points"):
        _apply([{"property": "volume", "points": []}])

    with pytest.raises(timeline.CommandValidationError, match="track must be an object"):
        _apply(["volume"])

    with pytest.raises(timeline.CommandValidationError, match="point must be an object"):
        _apply([{"property": "volume", "points": [{"t": 0, "value": 1}, 0.5]}])

    with pytest.raises(timeline.CommandValidationError, match="keyframes must be a list"):
        _apply({"property": "volume", "points": [{"t": 0, "value": 1}]})

    # Negative t is rejected by _non_negative even when value is in-range.
    with pytest.raises(timeline.CommandValidationError):
        _apply([{"property": "volume", "points": [{"t": -0.1, "value": 1}]}])

    assert store.load("proj_keyframes_empty")["revision"] == 2  # nothing committed


def test_crossfade_and_saturation_effects_validate_and_persist(tmp_path):
    """crossfade (transition) and saturation (color filter) are in the closed
    effect vocabulary but were untested. Confirm both pass validation, clamp
    params, and persist exactly — the editor side of the round-trip whose
    OpenShot translation lives in test_openshot_bridge."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_xfade_sat")  # clip c1 at revision 2

    project = store.apply_command(
        "proj_xfade_sat",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "id": "xf", "type": "crossfade", "params": {"duration": 0.75}},
        },
    )
    project = store.apply_command(
        "proj_xfade_sat",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "id": "sat", "type": "saturation", "params": {"value": 1.4}},
        },
    )

    assert project["clips"]["c1"]["effects"] == [
        {"id": "xf", "type": "crossfade", "params": {"duration": 0.75}},
        {"id": "sat", "type": "saturation", "params": {"value": 1.4}},
    ]

    # saturation is bounded 0..4; out-of-range is rejected without advancing revision
    with pytest.raises(timeline.CommandValidationError, match="effect.value must be between"):
        store.apply_command(
            "proj_xfade_sat",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 4,
                "payload": {"clipId": "c1", "id": "sat2", "type": "saturation", "params": {"value": 9}},
            },
        )
    assert store.load("proj_xfade_sat")["revision"] == 4


def test_save_uses_atomic_store_and_fsyncs(tmp_path, monkeypatch):
    """TimelineStore.save must route through the shared atomic_save util (tmp+fsync+
    replace) — not a bare write — so a crash mid-write can't truncate a timeline config."""
    calls = {}

    def _spy(path, data, **kwargs):
        calls["path"] = path
        calls["data"] = data
        from services.json_store import atomic_save as real
        real(path, data, **kwargs)

    monkeypatch.setattr(timeline, "atomic_save", _spy)

    store = timeline.TimelineStore(tmp_path)
    project = {"projectId": "proj_atomic", "revision": 0, "clips": {}}
    store.save(project)

    # routed through the shared util...
    assert calls["path"] == store.path_for("proj_atomic")
    assert calls["data"]["projectId"] == "proj_atomic"
    # ...and the real atomic_save left no stray tmp file and round-trips.
    assert not list(tmp_path.glob("*.tmp"))
    assert store.load("proj_atomic") == project


def test_abn_import_ducking_envelope_and_shot_effect_round_trip_to_openshot(tmp_path):
    """The full ABN -> Editor Bay -> OpenShot fidelity path for a KEYFRAMED ducking
    envelope and a clip effect.

    test_music_bed_imports_pre_ducked_under_vo() only proves the bed lands at the flat
    0.22 gain on import. It does NOT prove that a *keyframed* volume envelope (the real
    sidechain duck: full -> under VO -> back up) survives all the way to the compiler,
    nor that a crossfade on a shot does. abn_factory's duck pass and clip-boundary
    crossfades depend on both reaching OpenShot, so this nails the round-trip end to end:
    import the timeline, attach the envelope + effect through the command core, and
    assert openshot_bridge emits a multi-point volume keyframe (0..100 scale, correct
    interpolations) and a Fade effect — not the flat single-point default that a silent
    discard would leave behind.
    """
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_round_trip",
        "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {
                        "id": "card",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}},
            }
        ],
    }

    store = timeline.TimelineStore(tmp_path)
    project = timeline.project_from_abn_timeline("proj_round_trip", abn_timeline)
    store.save(project)

    bed_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "music_bed")
    shot_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "artifact")
    base_rev = project["revision"]

    # Replace the flat 0.22 gain with a real ducking envelope: full -> ducked under
    # the VO (constant-held floor) -> back up at the VO tail.
    project = store.apply_command(
        "proj_round_trip",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": base_rev,
            "payload": {
                "clipId": bed_id,
                "keyframes": [
                    {
                        "property": "volume",
                        "points": [
                            {"t": 0.0, "value": 1.0, "interp": "linear"},
                            {"t": 1.0, "value": 0.22, "interp": "constant"},
                            {"t": 3.0, "value": 1.0, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
    )
    # And a clip-boundary crossfade on the shot.
    project = store.apply_command(
        "proj_round_trip",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": project["revision"],
            "payload": {
                "clipId": shot_id,
                "id": "xf",
                "type": "crossfade",
                "params": {"duration": 0.5},
            },
        },
    )

    bridge = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    bed_oj = next(c for c in bridge["clips"] if c["id"] == bed_id)
    shot_oj = next(c for c in bridge["clips"] if c["id"] == shot_id)

    # The ducking envelope is a real multi-point keyframe, NOT the flat single-point
    # default — values on OpenShot's 0..100 scale, interpolations preserved per point.
    volume_points = bed_oj["volume"]["Points"]
    assert [round(p["co"]["Y"], 2) for p in volume_points] == [100.0, 22.0, 100.0]
    assert [p["interpolation"] for p in volume_points] == [
        openshot_bridge.LINEAR,
        openshot_bridge.CONSTANT,
        openshot_bridge.LINEAR,
    ]

    # The crossfade survives as an OpenShot Fade effect (not silently dropped).
    assert [e["type"] for e in shot_oj["effects"]] == ["Fade"]
    assert shot_oj["effects"][0]["id"] == "xf"
    assert shot_oj["effects"][0]["fade"] == "in"


def test_abn_import_promotes_shot_level_effects_and_keyframes():
    """Shot-level effects/kenBurns are wired onto the active clip fields on import.

    abn_factory shots carry per-shot envelopes (e.g. `kenBurns`) and can carry an
    `effects` list. project_from_abn_timeline now promotes those onto the active
    clip.effects / clip.keyframes fields that openshot_bridge reads, so factory
    crossfades and Ken-Burns reach the compiler on import alone — no extra editor
    command required. The raw shot is still preserved losslessly under
    clip.metadata.shot, so re-import remains lossless.
    """
    abn_timeline = {
        "episodeId": "ep_shot_fx",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {
                        "id": "card",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                        "effects": [
                            {"id": "xf", "type": "crossfade", "params": {"duration": 0.5}}
                        ],
                        "kenBurns": {
                            "startScale": 1.0,
                            "endScale": 1.1,
                            "startX": 0.5,
                            "endX": 0.5,  # no pan on X -> no track
                            "startY": 0.4,
                            "endY": 0.6,  # pans on Y -> a track
                        },
                    }
                ],
            }
        ],
    }

    project = timeline.project_from_abn_timeline("proj_shot_fx", abn_timeline)
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")

    # Effects are promoted (and validated) onto the active field.
    assert [e["type"] for e in clip["effects"]] == ["crossfade"]
    assert clip["effects"][0]["id"] == "xf"
    assert clip["effects"][0]["params"]["duration"] == 0.5

    # kenBurns becomes 2-point linear scale + y keyframe tracks spanning the clip;
    # the static X axis (start == end) is omitted.
    tracks = {t["property"]: t for t in clip["keyframes"]}
    assert set(tracks) == {"scale", "y"}
    scale_pts = tracks["scale"]["points"]
    assert [p["t"] for p in scale_pts] == [0.0, 2.0]
    assert [p["value"] for p in scale_pts] == [1.0, 1.1]
    assert all(p["interp"] == "linear" for p in scale_pts)
    assert [p["value"] for p in tracks["y"]["points"]] == [0.4, 0.6]

    # ...and the raw shot (and everything on it) is still retained, so nothing is lost.
    assert clip["metadata"]["shot"]["effects"][0]["type"] == "crossfade"
    assert clip["metadata"]["shot"]["kenBurns"]["endScale"] == 1.1


def test_validated_effect_accepts_every_effect_type_at_its_param_bounds():
    """Each EFFECT_TYPES entry must round-trip cleanly at both inclusive bounds.

    Guards the closed effect vocabulary (fadeIn/fadeOut/crossfade/brightness/
    saturation) and the numeric param ranges that translate 1:1 to OpenShot
    effect JSON. Bounds are inclusive, so low and high must both pass.
    """

    for effect_type, spec in timeline.EFFECT_TYPES.items():
        assert len(spec) == 1, "test assumes one param per effect type"
        (param_name, (low, high)), = spec.items()
        for bound in (low, high):
            out = timeline._validated_effect(
                {"id": f"fx_{effect_type}", "type": effect_type,
                 "params": {param_name: bound}}
            )
            assert out == {
                "id": f"fx_{effect_type}",
                "type": effect_type,
                "params": {param_name: bound},
            }


@pytest.mark.parametrize(
    "effect_type,param_name,value",
    [
        ("fadeIn", "duration", 600.0001),   # above duration high
        ("fadeOut", "duration", -0.0001),   # below duration low
        ("crossfade", "duration", 601.0),
        ("brightness", "value", 1.0001),    # above brightness high
        ("brightness", "value", -1.0001),   # below brightness low
        ("saturation", "value", 4.0001),    # above saturation high
        ("saturation", "value", -0.0001),   # below saturation low (0..4)
    ],
)
def test_validated_effect_rejects_out_of_bounds_param(effect_type, param_name, value):
    with pytest.raises(timeline.CommandValidationError,
                       match=f"effect.{param_name} must be between"):
        timeline._validated_effect(
            {"id": "fx1", "type": effect_type, "params": {param_name: value}}
        )


def test_validated_effect_rejects_unknown_type():
    with pytest.raises(timeline.CommandValidationError,
                       match="unsupported effect type: zoomBlur"):
        timeline._validated_effect(
            {"id": "fx1", "type": "zoomBlur", "params": {}}
        )


def test_validated_effect_rejects_unknown_param():
    with pytest.raises(timeline.CommandValidationError,
                       match="unsupported effect params"):
        timeline._validated_effect(
            {"id": "fx1", "type": "fadeIn",
             "params": {"duration": 0.5, "easing": "linear"}}
        )


def test_validated_effect_rejects_missing_declared_param():
    with pytest.raises(timeline.CommandValidationError,
                       match="effect param required: duration"):
        timeline._validated_effect(
            {"id": "fx1", "type": "fadeIn", "params": {}}
        )


def test_validated_effect_rejects_non_numeric_param():
    with pytest.raises(timeline.CommandValidationError,
                       match="effect.value must be numeric"):
        timeline._validated_effect(
            {"id": "fx1", "type": "brightness", "params": {"value": "bright"}}
        )


def test_validated_effect_requires_an_id_and_object_shape():
    with pytest.raises(timeline.CommandValidationError, match="effect must be an object"):
        timeline._validated_effect("fadeIn")
    with pytest.raises(timeline.CommandValidationError, match="effect id is required"):
        timeline._validated_effect({"type": "fadeIn", "params": {"duration": 0.5}})
    # effectId is an accepted alias for id, and stringifies on the way out.
    out = timeline._validated_effect(
        {"effectId": 7, "type": "fadeIn", "params": {"duration": 0.5}}
    )
    assert out["id"] == "7"


# --- clip.split keyframe / effect partitioning edge cases ---
# These exercise _keyframes_before / _keyframes_after / _effects_for_split_half,
# the helpers behind clip.split's animation rebasing (commits 537af946, dfa6c6c0).


def _kf(prop, *points):
    return {"property": prop, "points": [{"t": t, "value": v, "interp": "linear"} for t, v in points]}


def test_keyframes_before_keeps_boundary_point_on_the_head():
    # A point sitting exactly on the split offset belongs to the head (t <= offset).
    tracks = [_kf("volume", (0.0, 1.0), (2.0, 0.5), (4.0, 0.0))]
    head = timeline._keyframes_before(tracks, 2.0)
    assert [p["t"] for p in head[0]["points"]] == [0.0, 2.0]
    assert head[0]["points"][-1]["value"] == 0.5


def test_keyframes_after_keeps_boundary_point_rebased_to_zero():
    # The same boundary point also seeds the tail at t=0, so the envelope is continuous.
    tracks = [_kf("volume", (0.0, 1.0), (2.0, 0.5), (4.0, 0.0))]
    tail = timeline._keyframes_after(tracks, 2.0)
    assert [p["t"] for p in tail[0]["points"]] == [0.0, 2.0]
    assert tail[0]["points"][0]["value"] == 0.5  # boundary value preserved across the cut


def test_keyframes_before_and_after_drop_emptied_tracks():
    # A track whose points all land on one side must not survive as an empty husk on the other.
    tracks = [_kf("opacity", (3.0, 0.2), (5.0, 0.9))]
    assert timeline._keyframes_before(tracks, 1.0) == []      # nothing at t <= 1.0
    assert timeline._keyframes_after(tracks, 6.0) == []       # nothing at t >= 6.0


def test_keyframes_helpers_handle_no_keyframes():
    # Clips with no envelope (None or []) must not blow up.
    assert timeline._keyframes_before(None, 2.0) == []
    assert timeline._keyframes_after([], 2.0) == []


def test_effects_for_split_half_anchors_fades_and_copies_whole_clip_effects():
    effects = [
        {"id": "fi", "type": "fadeIn", "params": {"duration": 0.5}},
        {"id": "fo", "type": "fadeOut", "params": {"duration": 0.5}},
        {"id": "br", "type": "brightness", "params": {"value": 0.2}},
    ]
    head = timeline._effects_for_split_half(effects, "head")
    # Head keeps the start-anchored fadeIn (full) + whole-clip brightness, drops fadeOut.
    assert {e["id"] for e in head} == {"fi", "br"}
    assert next(e for e in head if e["id"] == "fi")["params"]["duration"] == 0.5

    # Tail keeps everything; its trimmed front re-fits the start-anchored fadeIn (here
    # the 0.3s front trim shortens the 0.5s fadeIn to 0.2s), fadeOut + brightness pass.
    tail = timeline._effects_for_split_half(
        effects, "tail", split_offset=0.3, half_duration=2.0
    )
    assert {e["id"] for e in tail} == {"fi", "fo", "br"}
    tail_by_id = {e["id"]: e for e in tail}
    assert tail_by_id["fi"]["params"]["duration"] == pytest.approx(0.2)  # 0.5 - 0.3 trim
    assert tail_by_id["fo"]["params"]["duration"] == 0.5  # end-anchored, untouched


def test_effects_for_split_half_deep_copies_so_halves_do_not_alias():
    effects = [{"id": "br", "type": "brightness", "params": {"value": 0.2}}]
    head = timeline._effects_for_split_half(effects, "head")
    head[0]["params"]["value"] = 0.9
    # Mutating the head's copy must not bleed into the source or the other half.
    assert effects[0]["params"]["value"] == 0.2
    assert timeline._effects_for_split_half(effects, "tail")[0]["params"]["value"] == 0.2


def test_clip_split_partitions_multitrack_keyframes_and_straddling_effects(tmp_path):
    """End-to-end: splitting a clip carrying volume+opacity envelopes and both fades
    routes each track and each anchored effect to the correct half."""
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_split_edge"))
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 6,
            },
        },
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "keyframes": [
                    {"property": "volume", "points": [
                        {"t": 0.0, "value": 1.0}, {"t": 3.0, "value": 0.5}, {"t": 6.0, "value": 0.0}]},
                    {"property": "opacity", "points": [
                        {"t": 1.0, "value": 0.2}, {"t": 5.0, "value": 0.9}]},
                ],
            },
        },
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {
                "id": "fi", "type": "fadeIn", "params": {"duration": 0.5}}},
        },
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "effect": {
                "id": "fo", "type": "fadeOut", "params": {"duration": 0.5}}},
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 5,
            "payload": {"clipId": "c1", "at": 3.0, "newClipId": "c1_b"},
        },
    ]:
        project = store.apply_command("proj_split_edge", command)

    head = project["clips"]["c1"]
    tail = project["clips"]["c1_b"]

    head_kf = {t["property"]: t for t in head["keyframes"]}
    tail_kf = {t["property"]: t for t in tail["keyframes"]}

    # volume crosses the 3.0 boundary: head keeps t<=3 (boundary point included),
    # tail keeps t>=3 rebased so the cut point lands at t=0 with the same value.
    assert [p["t"] for p in head_kf["volume"]["points"]] == [0.0, 3.0]
    assert [p["t"] for p in tail_kf["volume"]["points"]] == [0.0, 3.0]
    assert tail_kf["volume"]["points"][0]["value"] == 0.5

    # opacity has no point on the boundary: head gets only t=1, tail gets t=5 -> t=2.
    assert [p["t"] for p in head_kf["opacity"]["points"]] == [1.0]
    assert "opacity" not in tail_kf or [p["t"] for p in tail_kf["opacity"]["points"]] == [2.0]
    assert [p["t"] for p in tail_kf["opacity"]["points"]] == [2.0]

    # fadeIn is start-anchored: full on the head; on the tail the 3s front trim fully
    # eats the 0.5s ramp (0.5 - 3.0 <= 0), so it is DROPPED rather than kept as a 0s
    # no-op fade. fadeOut is end-anchored: dropped from the head, kept on the tail.
    # This matches editor_render._refit_effects so the timeline and compiled video agree.
    assert {e["id"] for e in head["effects"]} == {"fi"}
    assert next(e for e in head["effects"] if e["id"] == "fi")["params"]["duration"] == 0.5
    assert {e["id"] for e in tail["effects"]} == {"fo"}
    tail_fx = {e["id"]: e for e in tail["effects"]}
    assert tail_fx["fo"]["params"]["duration"] == 0.5


def test_abn_import_skips_unsupported_shot_effects_without_aborting():
    """An effect outside EFFECT_TYPES is skipped, not fatal; import still succeeds."""
    abn_timeline = {
        "episodeId": "ep_bad_fx",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 2.0,
                "shots": [
                    {
                        "id": "card",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                        "effects": [
                            {"id": "good", "type": "fadeIn", "params": {"duration": 0.3}},
                            {"id": "bad", "type": "glitch", "params": {}},
                        ],
                    }
                ],
            }
        ],
    }

    project = timeline.project_from_abn_timeline("proj_bad_fx", abn_timeline)
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")

    # Only the supported effect survives; the unknown one is dropped, not raised.
    assert [e["type"] for e in clip["effects"]] == ["fadeIn"]
    assert clip["keyframes"] == []


def test_abn_import_promoted_keyframes_reach_openshot_bridge():
    """The promoted scale keyframe shows up as a real multi-Point envelope in the
    compiler JSON — proving the wiring actually reaches OpenShot, not just the model."""
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_kb_bridge",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 2.0,
                "shots": [
                    {
                        "id": "card",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                        "kenBurns": {"startScale": 1.0, "endScale": 1.2},
                    }
                ],
            }
        ],
    }

    project = timeline.project_from_abn_timeline("proj_kb_bridge", abn_timeline)
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")

    oj = openshot_bridge.clip_json(project, clip)
    scale_points = oj["scale_x"]["Points"]
    assert [round(p["co"]["Y"], 3) for p in scale_points] == [1.0, 1.2]
    assert len(scale_points) == 2


def _abn_timeline_single_artifact(*, episode_id, shot_id, src, seg_duration, title=""):
    """A minimal one-segment ABN timeline with a single artifact shot + VO.

    Shot ids are stable, so re-importing with the same shot id reproduces the same
    clip id (see _stable_id) — that's what lets a human edit's command replay onto
    the freshly re-rendered structure.
    """
    return {
        "episodeId": episode_id,
        "title": title,
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "totalSec": seg_duration,
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": seg_duration,
                "shots": [
                    {"id": shot_id, "src": src, "startSec": 0.0, "durationSec": 2.0, "type": "artifact"}
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo_s0.wav", "duration": seg_duration}},
            }
        ],
    }


def test_reimport_replays_edits_onto_freshly_rendered_abn_structure():
    """The critical update path: a re-rendered ABN episode is re-imported and the human's
    command-log edits (a clip move + a marker) replay on top, so a factory re-run doesn't
    silently lose work done since the last import.
    """
    abn_v1 = _abn_timeline_single_artifact(
        episode_id="ep_reimport", shot_id="shot_a",
        src="/agenticnews-assets/card_a.png", seg_duration=4.0,
    )
    project = timeline.project_from_abn_timeline(
        "proj_reimport", abn_v1, source_episode_id="ep_reimport"
    )
    clip_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "artifact")

    # Human moves the artifact clip and drops a review marker — these live in the command log.
    project = timeline.apply_command(
        project,
        {"op": "clip.move", "actor": "human", "expectedRevision": project["revision"],
         "payload": {"clipId": clip_id, "start": 3.5}},
    )
    project = timeline.apply_command(
        project,
        {"op": "marker.add", "actor": "human", "expectedRevision": project["revision"],
         "payload": {"markerId": "m1", "time": 1.0, "label": "beat"}},
    )
    assert project["revision"] == 2

    # Factory re-renders the same episode (same shot id -> same stable clip id) with a longer segment.
    abn_v2 = _abn_timeline_single_artifact(
        episode_id="ep_reimport", shot_id="shot_a",
        src="/agenticnews-assets/card_a.png", seg_duration=5.0, title="v2",
    )
    migrated = timeline.reimport_abn_timeline_preserving_commands(
        project, abn_v2, source_episode_id="ep_reimport"
    )

    # The human's move replayed cleanly onto the re-imported clip.
    assert migrated["clips"][clip_id]["start"] == 3.5
    assert migrated["revision"] == 2
    # The marker was carried over (markers/notes merge in addition to the command replay).
    assert "m1" in migrated["markers"]
    # Fresh structure from v2 came through, and nothing was lost to a warning.
    assert migrated["title"] == "v2"
    assert migrated["metadata"]["migrationWarnings"] == []
    assert migrated["metadata"]["migratedFromRevision"] == 2
    # Re-stamped so the router's _needs_abn_import_migration gate won't loop.
    assert migrated["metadata"]["abnImportVersion"] == timeline.ABN_IMPORT_VERSION


def test_reimport_records_a_warning_instead_of_crashing_when_a_render_drops_an_edited_clip():
    """If the factory re-render removes a shot a human had edited, the replayed command
    can't find its clip. The re-import must record that as a migrationWarning and keep
    going — never raise — so a single stale edit can't abort the whole update and lose
    every other edit.
    """
    abn_v1 = _abn_timeline_single_artifact(
        episode_id="ep_drop", shot_id="shot_x",
        src="/agenticnews-assets/x.png", seg_duration=6.0,
    )
    project = timeline.project_from_abn_timeline("proj_drop", abn_v1, source_episode_id="ep_drop")
    clip_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "artifact")
    project = timeline.apply_command(
        project,
        {"id": "cmd_mv", "op": "clip.move", "actor": "human",
         "expectedRevision": project["revision"], "payload": {"clipId": clip_id, "start": 2.0}},
    )

    # Re-render replaces shot_x with shot_y, so the edited clip id no longer exists.
    abn_v2 = _abn_timeline_single_artifact(
        episode_id="ep_drop", shot_id="shot_y",
        src="/agenticnews-assets/y.png", seg_duration=6.0,
    )
    migrated = timeline.reimport_abn_timeline_preserving_commands(project, abn_v2)

    # No crash; the orphaned edit is surfaced as a warning, not silently swallowed.
    warnings = migrated["metadata"]["migrationWarnings"]
    assert len(warnings) == 1
    assert warnings[0]["commandId"] == "cmd_mv"
    assert warnings[0]["op"] == "clip.move"
    assert "clip does not exist" in warnings[0]["reason"]
    # The failed replay left the revision at the fresh-import baseline.
    assert clip_id not in migrated["clips"]
    assert migrated["revision"] == 0


def test_reimport_replay_logs_bad_asset_import_and_keeps_replaying_later_edits():
    """Graceful degradation in the replay loop (services/editor_timeline.py:248-265).

    A command log can contain an entry that fails validation at replay time — here an
    `asset.import` whose payload is missing its src so `_asset_from_payload` raises
    CommandValidationError. The re-import must (a) catch it, (b) record it to
    metadata.migrationWarnings, and crucially (c) NOT abort: every other valid edit
    in the log (a marker added AFTER the bad command) must still replay. Otherwise one
    corrupt/stale command during a factory re-render silently nukes all subsequent edits.
    """
    abn = _abn_timeline_single_artifact(
        episode_id="ep_grace", shot_id="shot_a",
        src="/agenticnews-assets/a.png", seg_duration=4.0,
    )
    project = timeline.project_from_abn_timeline("proj_grace", abn, source_episode_id="ep_grace")

    # Splice a malformed asset.import directly into the command log (simulating a
    # command that no longer validates), bracketed by a good marker BEFORE and a good
    # marker AFTER so we can prove the bad one neither aborts nor skips the rest.
    project["commandLog"] = [
        {"id": "cmd_before", "op": "marker.add", "actor": "human",
         "payload": {"markerId": "m_before", "time": 0.5, "label": "before"}},
        {"id": "cmd_bad", "op": "asset.import", "actor": "agent",
         "payload": {"assetId": "asset_bad", "type": "video"}},  # missing src -> invalid
        {"id": "cmd_after", "op": "marker.add", "actor": "human",
         "payload": {"markerId": "m_after", "time": 1.5, "label": "after"}},
    ]

    migrated = timeline.reimport_abn_timeline_preserving_commands(project, abn)

    # The bad command surfaced as exactly one warning, naming the offending command.
    warnings = migrated["metadata"]["migrationWarnings"]
    assert len(warnings) == 1
    assert warnings[0]["commandId"] == "cmd_bad"
    assert warnings[0]["op"] == "asset.import"
    assert warnings[0]["reason"]  # non-empty CommandValidationError message
    # The bad asset never landed in state.
    assert "asset_bad" not in migrated["assets"]
    # Graceful degradation: BOTH valid markers replayed despite the bad command between them.
    assert "m_before" in migrated["markers"]
    assert "m_after" in migrated["markers"]
    # Two good commands applied -> revision advanced by exactly 2 (the bad one didn't count).
    assert migrated["revision"] == 2


def test_reimport_is_idempotent_for_the_router_migration_gate():
    """routers/agenticnews._needs_abn_import_migration re-imports whenever abnImportVersion
    is stale. After one re-import the version is current, so a second pass over the SAME
    timeline reproduces the same state and doesn't re-loop or re-apply the command log.
    """
    abn = _abn_timeline_single_artifact(
        episode_id="ep_idem", shot_id="shot_a",
        src="/agenticnews-assets/a.png", seg_duration=4.0,
    )
    project = timeline.project_from_abn_timeline("proj_idem", abn, source_episode_id="ep_idem")
    clip_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "artifact")
    project = timeline.apply_command(
        project,
        {"op": "clip.move", "actor": "human", "expectedRevision": project["revision"],
         "payload": {"clipId": clip_id, "start": 1.25}},
    )

    once = timeline.reimport_abn_timeline_preserving_commands(project, abn)
    twice = timeline.reimport_abn_timeline_preserving_commands(once, abn)

    # The gate is satisfied after the first pass...
    assert once["metadata"]["abnImportVersion"] == timeline.ABN_IMPORT_VERSION
    # ...and a second re-import over the same timeline is stable (edit still applied once).
    assert twice["clips"][clip_id]["start"] == 1.25
    assert twice["revision"] == once["revision"]
    assert twice["metadata"]["migrationWarnings"] == []


# ---------------------------------------------------------------------------
# apply_command() edge-case / malformed-payload coverage
#
# The happy paths and the revert-guard error paths are covered above. These
# tests exercise the per-op validation branches (missing/malformed payloads,
# unknown clip/asset/track IDs, command-specific validation) that the
# editor-bay mutation path depends on rejecting cleanly with a
# CommandValidationError rather than corrupting the timeline or raising a
# bare KeyError/TypeError.
# ---------------------------------------------------------------------------

def _seed_clip_project(tmp_path, project_id="proj_edge"):
    """A saved project with one asset, the default video_1 track, and one clip."""
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project(project_id)
    store.save(project)
    store.apply_command(project_id, {
        "op": "asset.import", "actor": "agent", "expectedRevision": 0,
        "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
    })
    project = store.apply_command(project_id, {
        "op": "clip.create", "actor": "agent", "expectedRevision": 1,
        "payload": {"clipId": "c1", "assetId": "a1", "trackId": "video_1",
                    "start": 0.0, "duration": 6.0},
    })
    return store, project


def test_apply_command_requires_expected_revision_and_known_op(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_op_guard")
    store.save(project)

    # expectedRevision is mandatory (omitting it is a malformed command, not rev 0).
    with pytest.raises(timeline.CommandValidationError, match="expectedRevision is required"):
        store.apply_command("proj_op_guard", {"op": "clip.show", "actor": "x",
                                              "payload": {"clipId": "c1"}})

    # An unknown op must be rejected, not silently no-op'd.
    with pytest.raises(timeline.CommandValidationError, match="unsupported command op"):
        store.apply_command("proj_op_guard", {"op": "clip.frobnicate", "actor": "x",
                                              "expectedRevision": 0, "payload": {}})

    # A None op also falls through to the unsupported-op guard.
    with pytest.raises(timeline.CommandValidationError, match="unsupported command op"):
        store.apply_command("proj_op_guard", {"actor": "x", "expectedRevision": 0,
                                              "payload": {}})

    # The store and revision are untouched by the rejected commands.
    assert store.load("proj_op_guard")["revision"] == 0


def test_asset_import_rejects_missing_required_fields(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_asset_guard")
    store.save(project)

    base = {"op": "asset.import", "actor": "agent", "expectedRevision": 0}
    with pytest.raises(timeline.CommandValidationError, match="assetId is required"):
        store.apply_command("proj_asset_guard", {**base, "payload": {"type": "video", "src": "/a.mp4"}})
    with pytest.raises(timeline.CommandValidationError, match="asset type is required"):
        store.apply_command("proj_asset_guard", {**base, "payload": {"assetId": "a1", "src": "/a.mp4"}})
    with pytest.raises(timeline.CommandValidationError, match="asset src is required"):
        store.apply_command("proj_asset_guard", {**base, "payload": {"assetId": "a1", "type": "video"}})

    # Empty payload is rejected on the first missing field, not crashed.
    with pytest.raises(timeline.CommandValidationError):
        store.apply_command("proj_asset_guard", {**base, "payload": {}})
    assert store.load("proj_asset_guard")["revision"] == 0


def test_clip_create_rejects_unknown_asset_track_and_bad_geometry(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_create_guard")
    rev = store.load("proj_create_guard")["revision"]
    base = {"op": "clip.create", "actor": "agent", "expectedRevision": rev}

    with pytest.raises(timeline.CommandValidationError, match="clipId is required"):
        store.apply_command("proj_create_guard", {**base, "payload": {"assetId": "a1", "trackId": "video_1", "duration": 3}})
    with pytest.raises(timeline.CommandValidationError, match="asset does not exist: nope"):
        store.apply_command("proj_create_guard", {**base, "payload": {"clipId": "c2", "assetId": "nope", "trackId": "video_1", "duration": 3}})
    with pytest.raises(timeline.CommandValidationError, match="track does not exist: nope"):
        store.apply_command("proj_create_guard", {**base, "payload": {"clipId": "c2", "assetId": "a1", "trackId": "nope", "duration": 3}})
    # duration must be strictly positive; 0 and negatives are rejected.
    with pytest.raises(timeline.CommandValidationError, match="duration must be positive"):
        store.apply_command("proj_create_guard", {**base, "payload": {"clipId": "c2", "assetId": "a1", "trackId": "video_1", "duration": 0}})
    # non-numeric geometry is a clean validation error, not a TypeError.
    with pytest.raises(timeline.CommandValidationError, match="duration must be numeric"):
        store.apply_command("proj_create_guard", {**base, "payload": {"clipId": "c2", "assetId": "a1", "trackId": "video_1", "duration": "abc"}})
    with pytest.raises(timeline.CommandValidationError, match="start must be non-negative"):
        store.apply_command("proj_create_guard", {**base, "payload": {"clipId": "c2", "assetId": "a1", "trackId": "video_1", "duration": 3, "start": -1}})


def test_clip_mutations_reject_missing_and_unknown_clip_ids(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_mutate_guard")
    rev = store.load("proj_mutate_guard")["revision"]

    # Every clip-targeting op routes through _require_clip first.
    for op in ("clip.move", "clip.trim", "clip.hide", "clip.show", "clip.mute",
               "clip.unmute", "clip.transform", "clip.opacity", "clip.volume",
               "clip.keyframes", "clip.effect.add", "clip.effect.update",
               "clip.effect.delete", "clip.split"):
        with pytest.raises(timeline.CommandValidationError, match="clipId is required"):
            store.apply_command("proj_mutate_guard", {"op": op, "actor": "x", "expectedRevision": rev, "payload": {}})
        with pytest.raises(timeline.CommandValidationError, match="clip does not exist: ghost"):
            store.apply_command("proj_mutate_guard", {"op": op, "actor": "x", "expectedRevision": rev, "payload": {"clipId": "ghost"}})


def test_clip_property_ops_reject_out_of_range_and_non_numeric_values(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_range_guard")
    rev = store.load("proj_range_guard")["revision"]

    # opacity is bounded [0, 1].
    with pytest.raises(timeline.CommandValidationError, match="opacity must be between"):
        store.apply_command("proj_range_guard", {"op": "clip.opacity", "actor": "x", "expectedRevision": rev, "payload": {"clipId": "c1", "opacity": 1.5}})
    # volume is non-negative.
    with pytest.raises(timeline.CommandValidationError, match="volume must be non-negative"):
        store.apply_command("proj_range_guard", {"op": "clip.volume", "actor": "x", "expectedRevision": rev, "payload": {"clipId": "c1", "volume": -0.1}})
    # move start must be a finite number.
    with pytest.raises(timeline.CommandValidationError, match="start must be numeric"):
        store.apply_command("proj_range_guard", {"op": "clip.move", "actor": "x", "expectedRevision": rev, "payload": {"clipId": "c1", "start": float("inf")}})
    # transform payload must be an object.
    with pytest.raises(timeline.CommandValidationError, match="transform must be an object"):
        store.apply_command("proj_range_guard", {"op": "clip.transform", "actor": "x", "expectedRevision": rev, "payload": {"clipId": "c1", "transform": [1, 2]}})
    # transform.scale must be positive.
    with pytest.raises(timeline.CommandValidationError, match="transform.scale must be positive"):
        store.apply_command("proj_range_guard", {"op": "clip.transform", "actor": "x", "expectedRevision": rev, "payload": {"clipId": "c1", "transform": {"scale": 0}}})


def test_clip_update_patch_validation(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_patch_guard")
    rev = store.load("proj_patch_guard")["revision"]
    base = {"op": "clip.update", "actor": "x", "expectedRevision": rev}

    with pytest.raises(timeline.CommandValidationError, match="patch must be an object"):
        store.apply_command("proj_patch_guard", {**base, "payload": {"clipId": "c1", "patch": "nope"}})
    with pytest.raises(timeline.CommandValidationError, match="track does not exist: ghost"):
        store.apply_command("proj_patch_guard", {**base, "payload": {"clipId": "c1", "patch": {"trackId": "ghost"}}})
    with pytest.raises(timeline.CommandValidationError, match="effects must be a list"):
        store.apply_command("proj_patch_guard", {**base, "payload": {"clipId": "c1", "patch": {"effects": {"id": "x"}}}})
    with pytest.raises(timeline.CommandValidationError, match="duration must be positive"):
        store.apply_command("proj_patch_guard", {**base, "payload": {"clipId": "c1", "patch": {"duration": 0}}})
    with pytest.raises(timeline.CommandValidationError, match="duration must be non-negative"):
        store.apply_command("proj_patch_guard", {**base, "payload": {"clipId": "c1", "patch": {"duration": -2}}})
    with pytest.raises(timeline.CommandValidationError, match="volume must be non-negative"):
        store.apply_command("proj_patch_guard", {**base, "payload": {"clipId": "c1", "patch": {"volume": -1}}})


def test_clip_effect_ops_validate_type_params_and_existence(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_fx_guard")
    rev = store.load("proj_fx_guard")["revision"]

    # add: unsupported type / missing id / missing required param / unknown param.
    with pytest.raises(timeline.CommandValidationError, match="effect id is required"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"type": "fadeIn", "params": {"duration": 0.5}}}})
    with pytest.raises(timeline.CommandValidationError, match="unsupported effect type"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "wobble", "params": {}}}})
    with pytest.raises(timeline.CommandValidationError, match="effect param required: duration"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {}}}})
    with pytest.raises(timeline.CommandValidationError, match="unsupported effect params"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5, "bogus": 1}}}})

    # A valid add succeeds; a duplicate id is then rejected.
    project = store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                                    "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}}})
    rev = project["revision"]
    with pytest.raises(timeline.CommandValidationError, match="effect already exists: fx1"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}}})

    # update / delete against a non-existent effect id.
    with pytest.raises(timeline.CommandValidationError, match="effect does not exist: ghost"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.update", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"id": "ghost", "type": "fadeIn", "params": {"duration": 0.5}}}})
    with pytest.raises(timeline.CommandValidationError, match="effect does not exist: ghost"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.delete", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effectId": "ghost"}})

    # brightness param is signed and bounded [-1, 1].
    with pytest.raises(timeline.CommandValidationError, match="effect.value must be between"):
        store.apply_command("proj_fx_guard", {"op": "clip.effect.add", "actor": "x", "expectedRevision": rev,
                                              "payload": {"clipId": "c1", "effect": {"id": "fx2", "type": "brightness", "params": {"value": 5}}}})


def test_clip_keyframes_validation_rejects_malformed_envelopes(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_kf_guard")
    rev = store.load("proj_kf_guard")["revision"]
    base = {"op": "clip.keyframes", "actor": "x", "expectedRevision": rev}

    with pytest.raises(timeline.CommandValidationError, match="keyframes must be a list"):
        store.apply_command("proj_kf_guard", {**base, "payload": {"clipId": "c1", "keyframes": {"prop": "opacity"}}})
    with pytest.raises(timeline.CommandValidationError, match="unsupported keyframe property"):
        store.apply_command("proj_kf_guard", {**base, "payload": {"clipId": "c1", "keyframes": [{"property": "wobble", "points": [{"t": 0, "value": 1}]}]}})
    with pytest.raises(timeline.CommandValidationError, match="requires points"):
        store.apply_command("proj_kf_guard", {**base, "payload": {"clipId": "c1", "keyframes": [{"property": "opacity", "points": []}]}})
    with pytest.raises(timeline.CommandValidationError, match="keyframe point must be an object"):
        store.apply_command("proj_kf_guard", {**base, "payload": {"clipId": "c1", "keyframes": [{"property": "opacity", "points": ["nope"]}]}})
    with pytest.raises(timeline.CommandValidationError, match="unsupported keyframe interp"):
        store.apply_command("proj_kf_guard", {**base, "payload": {"clipId": "c1", "keyframes": [{"property": "opacity", "points": [{"t": 0, "value": 1, "interp": "warp"}]}]}})


def test_clip_split_rejects_split_point_outside_clip(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_split_guard")  # clip c1: start 0, duration 6
    rev = store.load("proj_split_guard")["revision"]
    base = {"op": "clip.split", "actor": "x", "expectedRevision": rev}

    # at the start boundary -> offset 0 -> not inside.
    with pytest.raises(timeline.CommandValidationError, match="split point must be inside the clip"):
        store.apply_command("proj_split_guard", {**base, "payload": {"clipId": "c1", "at": 0.0}})
    # past the end -> offset >= duration -> not inside.
    with pytest.raises(timeline.CommandValidationError, match="split point must be inside the clip"):
        store.apply_command("proj_split_guard", {**base, "payload": {"clipId": "c1", "at": 6.0}})
    # negative split point is non-numeric/negative -> non-negative guard.
    with pytest.raises(timeline.CommandValidationError, match="at must be non-negative"):
        store.apply_command("proj_split_guard", {**base, "payload": {"clipId": "c1", "at": -1.0}})



def test_clip_split_rejects_duplicate_new_clip_id(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_split_dup")
    rev = store.load("proj_split_dup")["revision"]
    with pytest.raises(timeline.CommandValidationError, match="clip already exists: c1"):
        store.apply_command("proj_split_dup", {"op": "clip.split", "actor": "x", "expectedRevision": rev,
                                               "payload": {"clipId": "c1", "at": 3.0, "newClipId": "c1"}})


def test_marker_and_note_delete_reject_unknown_ids(tmp_path):
    store, _ = _seed_clip_project(tmp_path, "proj_mn_guard")
    rev = store.load("proj_mn_guard")["revision"]

    with pytest.raises(timeline.CommandValidationError, match="marker does not exist"):
        store.apply_command("proj_mn_guard", {"op": "marker.delete", "actor": "x", "expectedRevision": rev, "payload": {"markerId": "ghost"}})
    with pytest.raises(timeline.CommandValidationError, match="note does not exist"):
        store.apply_command("proj_mn_guard", {"op": "note.delete", "actor": "x", "expectedRevision": rev, "payload": {"noteId": "ghost"}})
    # note.add with empty text is rejected.
    with pytest.raises(timeline.CommandValidationError, match="note text is required"):
        store.apply_command("proj_mn_guard", {"op": "note.add", "actor": "x", "expectedRevision": rev, "payload": {"text": ""}})
    # marker.add with a non-numeric time is a clean validation error.
    with pytest.raises(timeline.CommandValidationError, match="time must be numeric"):
        store.apply_command("proj_mn_guard", {"op": "marker.add", "actor": "x", "expectedRevision": rev, "payload": {"time": "soon"}})


def test_rejected_command_leaves_revision_and_command_log_untouched(tmp_path):
    """A malformed command must not bump revision or append to the log — the
    editor-bay relies on failed mutations being fully no-op so retries stay safe."""
    store, project = _seed_clip_project(tmp_path, "proj_noop")
    rev_before = project["revision"]
    log_len_before = len(project["commandLog"])

    with pytest.raises(timeline.CommandValidationError):
        store.apply_command("proj_noop", {"op": "clip.opacity", "actor": "x", "expectedRevision": rev_before,
                                          "payload": {"clipId": "c1", "opacity": 99}})

    reloaded = store.load("proj_noop")
    assert reloaded["revision"] == rev_before
    assert len(reloaded["commandLog"]) == log_len_before


def test_concurrent_apply_command_does_not_lose_updates(tmp_path):
    """Concurrent mutations on one project must not lost-update each other.

    apply_command is a read-modify-write (load -> apply -> atomic_save). Without a
    per-project lock, two threads can both read revision N between load() and
    save(); both pass the optimistic revision check and the second save() clobbers
    the first (the first command vanishes from the log even though it "succeeded").

    Fire many threads, all distinct commands but all targeting the SAME starting
    revision. With correct serialization exactly ONE wins per revision (the rest
    raise RevisionConflict because they observe the prior write), and the final
    on-disk state is internally consistent: every command the store reported as
    succeeded is present in the log and the revision bumped once per success.
    """
    import threading

    store, project = _seed_clip_project(tmp_path, "proj_race")
    start_rev = project["revision"]

    n = 24
    barrier = threading.Barrier(n)
    succeeded: list[str] = []
    conflicted = 0
    lock = threading.Lock()

    def worker(i: int) -> None:
        nonlocal conflicted
        cmd_id = f"cmd_race_{i}"
        barrier.wait()  # maximize the read-modify-write overlap window
        try:
            store.apply_command("proj_race", {
                "op": "clip.move", "actor": f"agent{i}", "id": cmd_id,
                "expectedRevision": start_rev,
                "payload": {"clipId": "c1", "start": float(i)},
            })
            with lock:
                succeeded.append(cmd_id)
        except timeline.RevisionConflict:
            with lock:
                conflicted += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads aimed at the same revision, so exactly one can win.
    assert len(succeeded) == 1
    assert conflicted == n - 1

    reloaded = store.load("proj_race")
    # Revision bumped once per reported success — no lost update, no double-apply.
    assert reloaded["revision"] == start_rev + len(succeeded)
    # The single winning command is actually persisted in the log.
    log_ids = {c.get("id") for c in reloaded["commandLog"]}
    for cmd_id in succeeded:
        assert cmd_id in log_ids


# ---------------------------------------------------------------------------
# E2E: factory-attached keyframed ducking envelope -> import -> editor_render
# -> an ACTUAL rendered audio mix. The unit/seam suites stop at JSON/filter
# strings; nothing previously proved the line 212-230 envelope-promotion path
# survives all the way to a real render and actually ducks the bed. This drives
# the deterministic ffmpeg-layered renderer (CI-available; the OpenShot path is
# native and may be absent) end to end and measures the output.
# ---------------------------------------------------------------------------

_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _tone_wav(path, *, duration=4.0):
    """A real constant-amplitude sine WAV so a downstream gain change is audible
    in the measured mean volume."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=220:duration={duration}",
         "-ac", "1", "-ar", "48000", str(path)],
        check=True, capture_output=True, text=True,
    )
    return path


def _mean_volume_db(path, *, start, duration):
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start), "-t", str(duration),
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr
    line = next(l for l in out.splitlines() if "mean_volume:" in l)
    return float(line.split("mean_volume:")[1].strip().split(" ")[0])


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe required for E2E render")
def test_keyframed_ducking_envelope_drives_actual_render(tmp_path):
    """End-to-end proof of the lines 212-230 path: a factory hands a dict-shaped
    music bed with a ducking volume envelope (loud 0.6 intro, quiet 0.15 under VO),
    the import promotes it onto the bed clip and neutralizes the flat 0.22 gain, and
    the REAL ffmpeg-layered render must reproduce that envelope — the intro window
    must be measurably louder than the ducked window. If the flat 0.22 weren't
    neutralized the intro would be double-attenuated (0.6*0.22) and the envelope's
    shape would collapse; if the envelope were dropped at import the bed would be a
    flat 0.22 and the two windows would measure equal."""
    bed = _tone_wav(tmp_path / "bed.wav", duration=4.0)

    project = timeline.project_from_abn_timeline("p_e2e", {
        "episodeId": "e", "totalSec": 4.0, "fps": 30, "width": 320, "height": 240,
        "musicBed": {"src": str(bed), "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6, "interp": "linear"},
                {"t": 1.0, "value": 0.15, "interp": "linear"},
                {"t": 3.0, "value": 0.15, "interp": "linear"},
            ]},
        ]},
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": []}],
    })

    bed_clip = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    # the envelope (not the flat 0.22) drives ducking, so the flat gain is neutralized
    assert bed_clip["volume"] == 1.0
    assert any(t["property"] == "volume" for t in bed_clip["keyframes"])

    from services import editor_render

    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "e2e.mp4")

    out = tmp_path / "renders" / "e2e.mp4"
    assert out.exists() and out.stat().st_size > 0
    assert result["video"] == str(out)

    intro_db = _mean_volume_db(out, start=0.1, duration=0.6)   # ~0.6 gain region
    ducked_db = _mean_volume_db(out, start=2.0, duration=0.9)  # ~0.15 gain region
    # The envelope actually ducked the bed: the intro is clearly louder than the
    # VO-window. 0.6 vs 0.15 is ~12 dB; require a solid margin to rule out a flat
    # render (which would measure ~0 dB difference).
    assert intro_db - ducked_db > 6.0


def test_abn_to_openshot_roundtrip_preserves_all_motion_and_fades():
    """End-to-end gateway assertion: ABN -> Editor -> OpenShot Timeline JSON keeps
    every motion/fade an ABN factory shot carries, and silently drops only the
    effect types outside the closed vocabulary (never aborting the import).

    This is the regression guard the ticket asks for: the prior bug was the import
    promotion path (clip.effects / clip.keyframes) silently losing shot effects,
    the synthesized transitionSec crossfade, kenBurns, or a shot-level keyframe
    envelope before they reached the compiler. Earlier tests check each piece via
    clip_json in isolation; this one drives the WHOLE timeline_json so the layers
    can't desync and a future edit to either module is caught.
    """
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_gateway",
        "totalSec": 4.0,
        "fps": 30,
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    # Shot A: kenBurns scale push only.
                    {
                        "id": "a",
                        "src": "/agenticnews-assets/a.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                        "kenBurns": {"startScale": 1.0, "endScale": 1.2},
                    },
                    # Shot B: an explicit fadeIn effect, an UNKNOWN effect type that
                    # must be dropped (not fatal), a choreographed boundary crossfade
                    # via transitionSec, AND a shot-level volume ducking envelope.
                    {
                        "id": "b",
                        "src": "/agenticnews-assets/b.png",
                        "startSec": 2.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                        "transitionSec": 0.5,
                        "effects": [
                            {"id": "fx_fi", "type": "fadeIn", "params": {"duration": 0.3}},
                            {"id": "fx_bad", "type": "glitch", "params": {}},
                        ],
                        "keyframes": [
                            {"property": "volume", "points": [
                                {"t": 0.0, "value": 0.2},
                                {"t": 2.0, "value": 0.2},
                            ]},
                        ],
                    },
                ],
            }
        ],
    }

    project = timeline.project_from_abn_timeline("p_gateway", abn_timeline)
    tj = openshot_bridge.timeline_json(project, asset_root="/tmp")
    clips = {c["id"]: c for c in tj["clips"]}

    a = clips["clip_s0_a_a_png"]
    b = clips["clip_s0_b_b_png"]

    # Shot A's kenBurns reaches OpenShot as a real 2-point scale envelope.
    assert [round(p["co"]["Y"], 3) for p in a["scale_x"]["Points"]] == [1.0, 1.2]
    assert a["effects"] == []

    # Shot B: the explicit fadeIn AND the synthesized transitionSec crossfade both
    # land as Fade effects; the unknown "glitch" effect is dropped, not fatal.
    assert [e["type"] for e in b["effects"]] == ["Fade", "Fade"]
    assert all(e["fade"] == "in" for e in b["effects"])
    assert not any(str(e.get("id")) == "fx_bad" for e in b["effects"])

    # Shot B's volume ducking envelope reaches OpenShot as a multi-Point volume
    # keyframe on the 0..100 scale (0.2 editor -> 20.0 openshot).
    vol_points = b["volume"]["Points"]
    assert len(vol_points) == 2
    assert [round(p["co"]["Y"], 1) for p in vol_points] == [20.0, 20.0]


def test_merge_keyframes_keeps_kenburns_motion_and_explicit_volume_envelope():
    """_merge_keyframes (called during ABN import to combine kenBurns motion with a
    shot-level envelope) must keep BOTH a kenBurns-derived scale/x/y track AND a
    distinct volume envelope — neither silently dropped.

    Precedence is first-track-seen per property: kenBurns groups are passed first so
    their scale/x/y win over any same-property explicit track, while a volume track
    (which kenBurns never synthesizes) passes through untouched. A regression that
    dropped the volume track to the kenBurns scale would lose factory ducking.
    """

    ken_burns = [
        {"property": "scale", "keyframes": [{"time": 0, "value": 1.0}, {"time": 1, "value": 1.1}]},
        {"property": "x", "keyframes": [{"time": 0, "value": 0}, {"time": 1, "value": 5}]},
        {"property": "y", "keyframes": [{"time": 0, "value": 0}, {"time": 1, "value": 5}]},
    ]
    explicit = [
        # collides with kenBurns scale -> kenBurns wins (dropped here)
        {"property": "scale", "keyframes": [{"time": 0, "value": 9.9}]},
        # unique property -> survives
        {"property": "volume", "keyframes": [{"time": 0, "value": 1.0}, {"time": 1, "value": 0.2}]},
    ]

    merged = timeline._merge_keyframes(ken_burns, explicit)

    by_prop = {t["property"]: t for t in merged}
    # No property dropped: motion (scale/x/y) AND the volume envelope all present.
    assert set(by_prop) == {"scale", "x", "y", "volume"}
    # kenBurns scale won the collision (its 1.0->1.1 motion, not the explicit 9.9).
    assert by_prop["scale"] is ken_burns[0]
    assert [kf["value"] for kf in by_prop["scale"]["keyframes"]] == [1.0, 1.1]
    # The volume ducking envelope survived untouched — this is the regression guard.
    assert by_prop["volume"] is explicit[1]
    assert [kf["value"] for kf in by_prop["volume"]["keyframes"]] == [1.0, 0.2]


def test_merge_keyframes_tolerates_none_and_empty_groups():
    """_shot_keyframes/_ken_burns_keyframes can yield None/[] (no envelope, no
    kenBurns block); _merge_keyframes must skip those without error."""

    vol = [{"property": "volume", "keyframes": [{"time": 0, "value": 0.5}]}]
    assert timeline._merge_keyframes(None, [], vol) == vol
    assert timeline._merge_keyframes() == []


def _split_fixture_project():
    """A project with one 10s clip carrying a volume envelope that spans the split
    point and four effects (start-anchored fadeIn/crossfade, end-anchored fadeOut,
    whole-clip brightness) — the exact surface the clip.split mutation paths touch."""
    project = timeline.new_project("p_split", title="Split")
    project = timeline.apply_command(project, {
        "id": "cmd_asset", "op": "asset.import", "actor": "editor",
        "expectedRevision": project["revision"],
        "payload": {"assetId": "a1", "type": "video", "src": "/x/clip.mp4"},
    })
    project = timeline.apply_command(project, {
        "id": "cmd_clip", "op": "clip.create", "actor": "editor",
        "expectedRevision": project["revision"],
        "payload": {"clipId": "c1", "assetId": "a1", "trackId": "video_1",
                    "start": 5.0, "duration": 10.0, "sourceStart": 1.0},
    })
    # Volume envelope: points straddling the split at clip-local t=4.
    project = timeline.apply_command(project, {
        "id": "cmd_kf", "op": "clip.keyframes", "actor": "editor",
        "expectedRevision": project["revision"],
        "payload": {"clipId": "c1", "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 1.0}, {"t": 2.0, "value": 0.5},
                {"t": 4.0, "value": 0.8}, {"t": 8.0, "value": 0.2},
            ]},
        ]},
    })
    for fx in (
        {"id": "fx_in", "type": "fadeIn", "params": {"duration": 2.0}},
        {"id": "fx_xf", "type": "crossfade", "params": {"duration": 6.0}},
        {"id": "fx_out", "type": "fadeOut", "params": {"duration": 3.0}},
        {"id": "fx_bri", "type": "brightness", "params": {"value": 0.3}},
    ):
        project = timeline.apply_command(project, {
            "id": f"cmd_{fx['id']}", "op": "clip.effect.add", "actor": "editor",
            "expectedRevision": project["revision"],
            "payload": {"clipId": "c1", "effect": fx},
        })
    return project


def test_clip_split_rebases_keyframes_and_routes_effects_across_halves():
    """clip.split must (1) trim the head keyframe envelope to t<=offset, (2) rebase the
    tail envelope to t>=offset with t-=offset, (3) keep start-anchored fades whole on
    the head and drop the end-anchored fadeOut there, and (4) on the tail keep fadeOut
    but re-fit start-anchored fades for the trimmed front (front_trim=offset), dropping
    any whose ramp the trim fully ate. Exercises _keyframes_before/_keyframes_after and
    _effects_for_split_half — the mutation paths test_editor_timeline_api never hits."""
    project = _split_fixture_project()
    # Clip starts at 5.0, duration 10.0 -> split at absolute 9.0 == clip-local t=4.0.
    project = timeline.apply_command(project, {
        "id": "cmd_split", "op": "clip.split", "actor": "editor",
        "expectedRevision": project["revision"],
        "payload": {"clipId": "c1", "at": 9.0, "newClipId": "c2"},
    })
    head = project["clips"]["c1"]
    tail = project["clips"]["c2"]

    # Geometry: head shrinks to the split, tail picks up the remainder + source offset.
    assert head["duration"] == 4.0
    assert tail["start"] == 9.0
    assert tail["duration"] == 6.0
    assert tail["sourceStart"] == 1.0 + 4.0  # original sourceStart + split offset
    assert tail["metadata"]["splitFrom"] == "c1"

    # (1) Head keyframes trimmed to t <= 4.0 (boundary point kept).
    head_vol = next(t for t in head["keyframes"] if t["property"] == "volume")
    assert [p["t"] for p in head_vol["points"]] == [0.0, 2.0, 4.0]
    assert [p["value"] for p in head_vol["points"]] == [1.0, 0.5, 0.8]
    # (2) Tail keyframes rebased: t >= 4.0 with t -= 4.0 (continuous boundary at t=0).
    tail_vol = next(t for t in tail["keyframes"] if t["property"] == "volume")
    assert [p["t"] for p in tail_vol["points"]] == [0.0, 4.0]
    assert [p["value"] for p in tail_vol["points"]] == [0.8, 0.2]

    # (3) Head effects: keeps start-anchored fadeIn/crossfade whole, drops end-anchored
    # fadeOut, keeps whole-clip brightness.
    head_fx = {e["type"]: e for e in head["effects"]}
    assert set(head_fx) == {"fadeIn", "crossfade", "brightness"}
    assert head_fx["fadeIn"]["params"]["duration"] == 2.0
    assert head_fx["crossfade"]["params"]["duration"] == 6.0
    assert head_fx["brightness"]["params"]["value"] == 0.3

    # (4) Tail effects: fadeOut kept; crossfade re-fit by front_trim=4 -> 6-4=2; the
    # fadeIn (duration 2) is fully eaten by the trim (2-4<=0) and dropped; brightness
    # copies unchanged.
    tail_fx = {e["type"]: e for e in tail["effects"]}
    assert set(tail_fx) == {"crossfade", "fadeOut", "brightness"}
    assert tail_fx["crossfade"]["params"]["duration"] == 2.0
    assert tail_fx["fadeOut"]["params"]["duration"] == 3.0
    assert "fadeIn" not in tail_fx


def test_clip_unsplit_restores_original_keyframes_and_effects():
    """clip.unsplit must revert a recorded clip.split exactly: the head clip is restored
    to its pre-split keyframes/effects/duration and the created tail clip is removed.
    Uses the split command's own recorded `before.clip` baseline (the contract enforced
    by _validate_unsplit_reverts_recorded_split)."""
    project = _split_fixture_project()
    pre_split = copy.deepcopy(project["clips"]["c1"])
    project = timeline.apply_command(project, {
        "id": "cmd_split", "op": "clip.split", "actor": "editor",
        "expectedRevision": project["revision"],
        "payload": {"clipId": "c1", "at": 9.0, "newClipId": "c2"},
    })
    assert "c2" in project["clips"]

    # The split entry records the original clip under before.clip — the unsplit baseline.
    split_entry = next(e for e in project["commandLog"] if e["id"] == "cmd_split")
    original = split_entry["before"]["clip"]

    project = timeline.apply_command(project, {
        "id": "cmd_unsplit", "op": "clip.unsplit", "actor": "editor",
        "expectedRevision": project["revision"],
        "revertsCommandId": "cmd_split",
        "payload": {"clipId": "c1", "createdClipId": "c2", "clip": original},
    })

    # Tail clip removed; head clip restored bit-for-bit to its pre-split state.
    assert "c2" not in project["clips"]
    restored = project["clips"]["c1"]
    assert restored["duration"] == 10.0
    restored_vol = next(t for t in restored["keyframes"] if t["property"] == "volume")
    assert [p["t"] for p in restored_vol["points"]] == [0.0, 2.0, 4.0, 8.0]
    assert {e["type"] for e in restored["effects"]} == {
        "fadeIn", "crossfade", "fadeOut", "brightness"
    }
    assert restored == pre_split
