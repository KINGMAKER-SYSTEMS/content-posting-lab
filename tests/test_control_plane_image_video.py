"""Closed contracts for page-owned photo-to-MP4 preparation.

Covers: exact page-bound plan identity/signature, deterministic no-permutation
order, honest single-plan exhaustion, strict config parsing, library
identity/bytes/hash validation, and the blank-text/no-audio photo spec with an
idempotency-scoped render receipt that never claims an applied brightness gain.
"""

import hashlib
import json
import math
from copy import deepcopy

import pytest

import services.control_plane_image_video as iv
from services.control_plane_image_video import (
    EXECUTOR_PATH,
    build_image_video_spec,
    image_video_executor_contract,
    image_video_plan,
    image_video_plan_signature,
    parse_plan_config,
    plan_image_video,
    submit_image_video_render,
    validate_image_video_library,
)


PENPAL = "acct:pen-pal3"
TWIN = "acct:twin47996"
UNKNOWN = "acct:someone-else"


def _penpal_observations(*, extra=False):
    obs = [
        {
            "key": "animals/kitten-flowers/photos/pinterest_784681935144260004.jpg",
            "bytes": 132530,
            "sha256": "09a1b391a05c29944705bd5b3c662cad05218b476e1a8d2e813c01dda119247d",
        },
        {
            "key": "animals/kitten-flowers/photos/pinterest_784681935144260020.jpg",
            "bytes": 227679,
            "sha256": "39a5346f6cd04217b766bf00fc590993d8a6f5546f191785ae2885748ab0e92b",
        },
        {
            "key": "animals/kitten-flowers/photos/pinterest_784681935144259989.jpg",
            "bytes": 224944,
            "sha256": "5ad6eb5aa5f43607e1d27366feccd7b8c8a0fd4ae482ddbaaa241f8d0066d288",
        },
    ]
    if extra:
        obs.append({
            "key": "animals/kitten-flowers/photos/extra-000.jpg",
            "bytes": 999,
            "sha256": "e" * 64,
        })
    return obs


def _twin_observations():
    return [
        {
            "key": "interiors/lamplit-apartment/photos/pinterest_784681935144259920.jpg",
            "bytes": 130640,
            "sha256": "cf5cafb364516ea0e2e333f9a7379bbb6b6be76e08e8fd6976ae73c8fd44d85f",
        }
    ]


def _good_plan_config(page_id=PENPAL):
    executor, _ = image_video_executor_contract()
    cfg = deepcopy(executor["plans"][page_id])
    cfg.pop("pageId", None)
    return cfg


def test_executor_contract_is_closed_and_hashes():
    executor, digest = image_video_executor_contract()
    assert executor["executorId"] == "syzygy-slideshow"
    assert executor["source"] == "syzygy_r2_library"
    assert executor["selection"] == "deterministic_single_plan_without_replacement"
    assert set(executor["plans"]) == {PENPAL, TWIN}
    assert digest == hashlib.sha256(EXECUTOR_PATH.read_bytes()).hexdigest()


def test_penpal_plan_three_ordered_4s_holds():
    plan = image_video_plan(PENPAL)
    assert plan is not None
    assert plan["planSlug"] == "page-photo-hold"
    assert plan["libraryId"] == "syzygy:animals:kitten-flowers"
    assert plan["subject"] == "pen-pal3"
    assert plan["librarySubject"] == "kitten-flowers"
    assert plan["totalDurationSeconds"] == 12
    assert plan["brightnessGainFromOriginals"] == 0.85
    keys = [item["key"] for item in plan["media"]]
    assert keys == [
        "animals/kitten-flowers/photos/pinterest_784681935144260004.jpg",
        "animals/kitten-flowers/photos/pinterest_784681935144260020.jpg",
        "animals/kitten-flowers/photos/pinterest_784681935144259989.jpg",
    ]
    assert [item["duration"] for item in plan["media"]] == [4, 4, 4]
    assert [(item["focalX"], item["focalY"]) for item in plan["media"]] == [
        (0.05, 0.5), (0.5, 0.5), (0.7, 0.5),
    ]
    assert all(item["cropMode"] == "cover" for item in plan["media"])
    assert all(item["kenBurns"] == {"dir": "in", "zoom": 0.025} for item in plan["media"])
    assert plan["signature"] == image_video_plan_signature(plan)


def test_twin_plan_one_12s_hold_zero_ken_burns():
    plan = image_video_plan(TWIN)
    assert plan is not None
    assert plan["libraryId"] == "syzygy:interiors:lamplit-apartment"
    assert plan["subject"] == "twin47996"
    assert plan["librarySubject"] == "lamplit-apartment"
    assert plan["totalDurationSeconds"] == 12
    # Latest managed output applies .85 once; static composition remains unchanged.
    assert plan["brightnessGainFromOriginals"] == 0.85
    assert len(plan["media"]) == 1
    media = plan["media"][0]
    assert media["key"] == "interiors/lamplit-apartment/photos/pinterest_784681935144259920.jpg"
    assert media["duration"] == 12
    assert (media["focalX"], media["focalY"]) == (0.5, 0.5)
    assert media["cropMode"] == "cover"
    assert media["kenBurns"] == {"dir": "none", "zoom": 0}


def test_unknown_or_foreign_page_has_no_plan():
    assert image_video_plan(UNKNOWN) is None
    assert image_video_plan("") is None
    assert image_video_plan("acct:pen-pal3-extra") is None
    assert plan_image_video(UNKNOWN, set()) == []


def test_plan_is_deterministic_single_no_permutation():
    first = image_video_plan(PENPAL)
    second = image_video_plan(PENPAL)
    assert first == second
    assert first["signature"] == second["signature"]


def test_exhaustion_once_reserved():
    plan = image_video_plan(PENPAL)
    assert plan is not None
    assert plan_image_video(PENPAL, set()) == [plan]
    # Once reserved, the single ordered plan is truthfully exhausted.
    assert plan_image_video(PENPAL, {plan["signature"]}) == []


def test_library_validation_accepts_exact_observations():
    snapshot = validate_image_video_library(PENPAL, _penpal_observations())
    assert snapshot == hashlib.sha256(
        json.dumps(
            [
                ["animals/kitten-flowers/photos/pinterest_784681935144260004.jpg", 132530,
                 "09a1b391a05c29944705bd5b3c662cad05218b476e1a8d2e813c01dda119247d"],
                ["animals/kitten-flowers/photos/pinterest_784681935144260020.jpg", 227679,
                 "39a5346f6cd04217b766bf00fc590993d8a6f5546f191785ae2885748ab0e92b"],
                ["animals/kitten-flowers/photos/pinterest_784681935144259989.jpg", 224944,
                 "5ad6eb5aa5f43607e1d27366feccd7b8c8a0fd4ae482ddbaaa241f8d0066d288"],
            ],
            separators=(",", ":"), ensure_ascii=False,
        ).encode()
    ).hexdigest()


def test_library_validation_rejects_wrong_bytes_sha_missing_duplicate():
    # Wrong bytes.
    bad = _penpal_observations()
    bad[0]["bytes"] = 1
    with pytest.raises(iv.ImageVideoError):
        validate_image_video_library(PENPAL, bad)
    # Wrong sha256.
    bad = _penpal_observations()
    bad[1]["sha256"] = "a" * 64
    with pytest.raises(iv.ImageVideoError):
        validate_image_video_library(PENPAL, bad)
    # Missing configured object.
    with pytest.raises(iv.ImageVideoError):
        validate_image_video_library(PENPAL, _penpal_observations()[:2])
    # Duplicate observation.
    bad = _penpal_observations()
    bad.append(dict(bad[0]))
    with pytest.raises(ValueError):
        validate_image_video_library(PENPAL, bad)
    # Unknown page.
    with pytest.raises(iv.ImageVideoError):
        validate_image_video_library(UNKNOWN, _penpal_observations())


def test_library_extras_never_selected():
    plan = image_video_plan(PENPAL)
    snapshot = validate_image_video_library(PENPAL, _penpal_observations(extra=True))
    # The extra observed object is ignored and never enters the plan/snapshot.
    assert all("extra-000" not in item["key"] for item in plan["media"])
    assert "extra-000" not in snapshot


def test_spec_has_explicit_media_and_blank_text_no_audio():
    plan = image_video_plan(PENPAL)
    spec = build_image_video_spec(plan)
    assert spec["format"] == "photo"
    assert spec["aspect"] == "9:16"
    assert spec["transition"] == 0
    assert len(spec["media"]) == 3
    for item in spec["media"]:
        assert item["type"] == "photo"
        assert "library" in item and "/photos/" in item["library"]
        assert item["duration"] == 4
        assert item["focal_x"] == [.05, .5, .7][spec["media"].index(item)]
        assert item["focal_y"] == .5
        assert item["crop_mode"] == "cover"
        assert item["ken_burns"] == {"dir": "in", "zoom": 0.025}
    # No template/cuts/flow-stage defaults.
    assert "template_slug" not in spec
    assert "cuts" not in spec
    assert "templateSlug" not in spec
    # Blank words, no source audio.
    assert spec["hook"] == {"text": "", "position": "none"}
    assert spec["lyrics"] == []
    assert spec["captions"] == {"position": "none"}
    assert spec["textCards"] == []
    assert spec["text_cards"] == []
    assert spec["lyrics_lrc"] == ""
    assert spec["lyrics_required"] is False
    assert spec["uses_original_audio"] is False
    assert spec["effects"] == []
    assert spec["overlays"] == []
    # Total duration 12 via timing section; no source audio URL.
    assert spec["audio"] == {"section_start": 0, "section_end": 12}
    assert "src" not in spec["audio"]
    assert "url" not in spec["audio"]


def test_spec_never_carries_brightness_gain():
    # Brightness is applied later at the source-treatment boundary, so the
    # render wire must not advertise or embed it.
    for page in (PENPAL, TWIN):
        spec = build_image_video_spec(image_video_plan(page))
        assert "brightness" not in spec
        assert "brightnessGainFromOriginals" not in spec
        assert "gain" not in spec


async def test_submit_receipt_idempotency_scoped_and_no_brightness(monkeypatch):
    captured = {}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None,
                                timeout=90.0):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["body"] = body
        captured["idempotency_key"] = idempotency_key
        captured["timeout"] = timeout
        return {"job_id": "job-image-video", "quality": "draft"}

    plan = image_video_plan(PENPAL)
    assert plan is not None
    job_id, quality = await submit_image_video_render(
        plan, page_id=PENPAL, recipe_version="v1", request_json=fake_request_json,
    )
    assert (job_id, quality) == ("job-image-video", "draft")
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/v1/render"
    # Idempotency scoped to page + recipe + plan signature.
    expected_key = iv.image_video_idempotency_key(
        PENPAL, "v1", plan["signature"], "render-v1",
    )
    assert captured["idempotency_key"] == expected_key
    # A different page/recipe/plan must never reuse the same idempotency key.
    other = image_video_plan(TWIN)
    assert iv.image_video_idempotency_key(
        TWIN, "v1", other["signature"], "render-v1",
    ) != expected_key
    # The captured spec is the supported photo spec; no brightness claim.
    spec = captured["body"]["spec"]
    assert spec["format"] == "photo"
    assert "brightness" not in spec and "gain" not in spec


def test_parser_rejects_malformed_nonfinite_bool_negative():
    good = _good_plan_config()

    def bad(**mutations):
        cfg = deepcopy(good)
        for key, value in mutations.items():
            cfg[key] = value
        return cfg

    cases = [
        bad(totalDurationSeconds=0),
        bad(totalDurationSeconds=-1),
        bad(totalDurationSeconds=float("nan")),
        bad(totalDurationSeconds=float("inf")),
        bad(totalDurationSeconds=True),
        bad(brightnessGainFromOriginals=-0.1),
        bad(brightnessGainFromOriginals=float("nan")),
        bad(brightnessGainFromOriginals=False),
        bad(lane="BAD LANE"),
        bad(subject="UPPER-Case"),
        bad(objects=[]),
    ]
    for cfg in cases:
        with pytest.raises(ValueError):
            parse_plan_config(PENPAL, cfg)
    # Unknown top-level field.
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, {**good, "extra": 1})
    # Unknown object field.
    cfg = deepcopy(good)
    cfg["objects"][0]["extra"] = 1
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, cfg)


def test_parser_rejects_bad_objects():
    good = _good_plan_config()

    def mutate_object(index, **changes):
        cfg = deepcopy(good)
        for key, value in changes.items():
            cfg["objects"][index][key] = value
        return cfg

    cases = [
        mutate_object(0, bytes=True),
        mutate_object(0, bytes=-1),
        mutate_object(0, bytes=0),
        mutate_object(0, duration=True),
        mutate_object(0, duration=0),
        mutate_object(0, duration=float("inf")),
        mutate_object(0, focalX=-0.1),
        mutate_object(0, focalX=1.1),
        mutate_object(0, focalX=True),
        mutate_object(0, focalY=float("nan")),
        mutate_object(0, cropMode="fill"),
        mutate_object(0, sha256="short"),
        mutate_object(0, kenBurns={"dir": "in", "zoom": -0.1}),
        mutate_object(0, kenBurns={"dir": "sideways", "zoom": 0.025}),
        mutate_object(0, kenBurns={"dir": "none", "zoom": 0.025}),
        mutate_object(0, kenBurns={"dir": "in", "zoom": 0}),
        mutate_object(0, kenBurns={"dir": "in"}),
        # Runtime key filename differs from source (not the same filename).
        mutate_object(0, key="animals/kitten-flowers/photos/other_000.jpg"),
        # Runtime key in the wrong library lane/subject.
        mutate_object(0, key="interiors/lamplit-apartment/photos/pinterest_784681935144260004.jpg"),
    ]
    for cfg in cases:
        with pytest.raises(ValueError):
            parse_plan_config(PENPAL, cfg)
    # Duplicate runtime key.
    cfg = deepcopy(good)
    cfg["objects"][1]["key"] = cfg["objects"][0]["key"]
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, cfg)
    # Duplicate source key.
    cfg = deepcopy(good)
    cfg["objects"][1]["sourceKey"] = cfg["objects"][0]["sourceKey"]
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, cfg)
    # Durations do not sum to the total.
    cfg = deepcopy(good)
    cfg["objects"][0]["duration"] = 5
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, cfg)


def test_runtime_keys_are_the_existing_source_pool_keys():
    # The pinned photos now live directly under the existing Syzygy library
    # folders, so the runtime key IS the source key (page subject is separate).
    plan = image_video_plan(PENPAL)
    for item in plan["media"]:
        assert item["key"] == item["sourceKey"]
        assert item["key"].startswith("animals/kitten-flowers/photos/")
    twin = image_video_plan(TWIN)
    assert twin["media"][0]["key"] == twin["media"][0]["sourceKey"]
    assert twin["media"][0]["key"].startswith("interiors/lamplit-apartment/photos/")


def test_plan_image_video_rejects_invalid_reservation_set():
    plan = image_video_plan(PENPAL)
    assert plan_image_video(PENPAL, {"not-a-sha"}) == []
    assert plan_image_video(PENPAL, {True}) == []
    assert plan_image_video(PENPAL, "not-a-set") == []
    assert plan_image_video("", set()) == []


@pytest.mark.asyncio
async def test_submit_refuses_foreign_or_forged_plan_before_transport():
    calls = []
    async def request(*args, **kwargs):
        calls.append(args)
        return {"job_id": "never", "quality": "ok"}
    canonical = image_video_plan(PENPAL)
    for page, plan in [(TWIN, canonical), (PENPAL, {**canonical, "signature": "0" * 64})]:
        with pytest.raises(iv.ImageVideoError):
            await submit_image_video_render(plan, page_id=page, recipe_version="sha256:" + "a" * 64, request_json=request)
    forged = deepcopy(canonical)
    forged["media"][0]["focalX"] = .9
    forged["signature"] = image_video_plan_signature(forged)
    with pytest.raises(iv.ImageVideoError):
        await submit_image_video_render(forged, page_id=PENPAL, recipe_version="v1", request_json=request)
    assert calls == []


def test_slideshow_library_address_uses_storage_folder_and_refuses_foreign_handle():
    from services.content_engine_registry import load_engine_registry
    from services.control_plane_slideshows import slideshow_library_address

    profiles, _ = load_engine_registry()
    profile = profiles["image-video"]
    pages = {
        "pageId": "acct:pen-pal3",
        "handle": "pen.pal3",
        "notionPageId": "3ed1465b-b829-809f-afb4-da66093e7f9d",
        "contentNiche": "slideshow",
        "contentEngine": "sourced_slideshow",
    }
    address = slideshow_library_address(profile, pages)
    assert address == ("syzygy:animals:kitten-flowers", "animals", "kitten-flowers")
    # A foreign page handle must not resolve the owned photo plan/library.
    assert slideshow_library_address(profile, {**pages, "handle": "foreign.page"}) is None
    assert slideshow_library_address(profile, {**pages, "pageId": "acct:someone-else"}) is None


def test_parser_refuses_key_outside_library_subject_folder():
    good = _good_plan_config()
    # librarySubject is present, so the key must live under
    # animals/kitten-flowers/photos/, not the page subject folder.
    cfg = deepcopy(good)
    cfg["objects"][0]["key"] = "animals/pen-pal3/photos/pinterest_784681935144260004.jpg"
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, cfg)


def test_parser_refuses_bad_library_subject_characters():
    good = _good_plan_config()
    for bad in ("UPPER-Case", "bad subject", "", "a" * 101):
        cfg = deepcopy(good)
        cfg["librarySubject"] = bad
        with pytest.raises(ValueError):
            parse_plan_config(PENPAL, cfg)


def test_plan_without_library_subject_behaves_as_before():
    cfg = deepcopy(_good_plan_config())
    cfg.pop("librarySubject")
    for obj in cfg["objects"]:
        obj["key"] = "animals/pen-pal3/photos/" + obj["sourceKey"].rsplit("/", 1)[1]
    parsed = parse_plan_config(PENPAL, cfg)
    assert parsed["subject"] == "pen-pal3"
    assert parsed["librarySubject"] is None
    assert [obj["key"] for obj in parsed["objects"]] == [
        "animals/pen-pal3/photos/pinterest_784681935144260004.jpg",
        "animals/pen-pal3/photos/pinterest_784681935144260020.jpg",
        "animals/pen-pal3/photos/pinterest_784681935144259989.jpg",
    ]
    # A key outside lane/subject/photos/ is still refused when librarySubject is absent.
    cfg["objects"][0]["key"] = "animals/kitten-flowers/photos/pinterest_784681935144260004.jpg"
    with pytest.raises(ValueError):
        parse_plan_config(PENPAL, cfg)
