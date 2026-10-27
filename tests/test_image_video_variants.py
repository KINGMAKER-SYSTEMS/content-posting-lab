"""Image-video photo-hold variants drawn from the page's own library.

Variant 0 is the approved plan (same signature as before variants existed).
Variants 1..N re-draw the target media count from the page's own
``<lane>/<subject>/photos/`` library in a deterministic order, so a page keeps
posting after its approved plan is reserved. Fixtures are the real R2 object
keys/bytes captured for pen-pal3 and twin47996.
"""
import hashlib
import json
from pathlib import Path

import pytest

import services.control_plane_image_video as iv
import services.control_plane_slideshows as ss
from tests.test_control_plane_image_video import (
    PENPAL as PENPAL_PAGE,
    TWIN as TWIN_PAGE,
)

FIXTURE = Path(__file__).resolve().parents[0] / "fixtures" / "pentwin-keys.tsv"
PENPAL_CANONICAL = "9b2b68c3a69e99cbbf1e5bb8f1f263e43d8590e93052c9fc68586f326e0e7aa5"
TWIN_CANONICAL = "d65c9f465a6af985195ebf25462405a2bae8665b0fa5f65470ce5d1cbde322ac"

PAGES = [
    ("pen-pal3", PENPAL_PAGE, "animals", "pen-pal3", "syzygy:animals:pen-pal3",
     "image-video", PENPAL_CANONICAL, 3),
    ("twin47996", TWIN_PAGE, "interiors", "twin47996", "syzygy:interiors:twin47996",
     "single-image-video", TWIN_CANONICAL, 1),
]


def _real_library(lane, subject, library_id):
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        key, _, bytestr = line.rpartition("\t")
        if key.startswith(f"{lane}/{subject}/photos/"):
            rows.append((key, int(bytestr)))
    objects = tuple(
        ss.SyzygyObject(key, nbytes, f"https://r2.example/{key}")
        for key, nbytes in sorted(rows)
    )
    canonical = json.dumps(
        [[obj.key, obj.bytes] for obj in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    return ss.SyzygyLibrary(
        library_id, lane, subject, objects,
        hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _recipe(page, format_slug, library):
    plan = iv.image_video_plan(page)
    return ss.SlideshowRecipe(
        recipe_id=f"{format_slug}:master",
        format_slug=format_slug,
        engine="sourced_image" if format_slug == "single-image-video" else "sourced_slideshow",
        engine_registry_hash="e" * 64,
        format_contract_version="sha256:" + "f" * 64,
        material_source="image_library",
        asset_type="video/mp4",
        executor_version="sha256:" + "d" * 64,
        library_id=plan["libraryId"],
        lane=plan["lane"],
        subject=plan["subject"],
        minimum_library_items=1,
        templates=(ss.SyzygyTemplate("page-photo-hold", 1),),
        max_quantity=1,
        output_width=1080,
        output_height=1920,
        encode_preset="tiktok_delivery_v1",
        recipe_spec={
            "masterPages": {
                "pageId": page,
                "handle": "pen.pal3" if page == PENPAL_PAGE else "twin47996",
                "notionPageId": ("3ed1465b-b829-809f-afb4-da66093e7f9d"
                                 if page == PENPAL_PAGE
                                 else "3ed1465b-b829-8056-aa9b-f8579c3210b8"),
                "contentNiche": "slideshow" if page == PENPAL_PAGE else "POV",
                "contentEngine": "sourced_slideshow" if page == PENPAL_PAGE else "sourced_image",
            }
        },
        engine_profile_hash="a" * 64,
    )


def _media_content(plan):
    """The exact posted asset identity: ordered media + per-slot render params."""
    return tuple(
        (
            item["key"],
            item["duration"],
            item["focalX"],
            item["focalY"],
            json.dumps(item["kenBurns"], sort_keys=True),
        )
        for item in plan["media"]
    )


def _library_with(library, lane, subject, extra=(), drop=()):
    """Rebuild a library with photos added/removed, snapshot hash recomputed."""
    dropped = set(drop)
    rows = [(obj.key, obj.bytes) for obj in library.objects if obj.key not in dropped]
    rows += list(extra)
    objects = tuple(
        ss.SyzygyObject(key, nbytes, f"https://r2.example/{key}")
        for key, nbytes in sorted(rows)
    )
    canonical = json.dumps(
        [[obj.key, obj.bytes] for obj in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    return ss.SyzygyLibrary(
        library.library_id, lane, subject, objects,
        hashlib.sha256(canonical.encode()).hexdigest(),
    )


@pytest.mark.parametrize("name,page,lane,subject,library_id,format_slug,canonical,count", PAGES,
                         ids=["pen-pal3", "twin47996"])
def test_first_plan_unchanged_and_variants_offer_after_reservation(
    name, page, lane, subject, library_id, format_slug, canonical, count,
):
    library = _real_library(lane, subject, library_id)
    variants = iv.image_video_variant_plans(page, library)
    assert variants, "page must offer its approved plan"
    assert variants[0]["signature"] == canonical
    assert variants[0]["signature"] == iv.image_video_plan(page)["signature"]
    assert len(variants[0]["media"]) == count
    # Reserving the approved plan never exhausts the page now.
    after = ss.plan_slideshows(
        _recipe(page, format_slug, library), library, 1, {canonical}, "run",
    )
    assert len(after) == 1, "a reserved first plan must not zero maxQuantity"
    assert after[0]["signature"] != canonical
    assert after[0]["signature"] in {plan["signature"] for plan in variants}


@pytest.mark.parametrize("name,page,lane,subject,library_id,format_slug,canonical,count", PAGES,
                         ids=["pen-pal3", "twin47996"])
def test_variants_are_distinct_deterministic_and_page_scoped(
    name, page, lane, subject, library_id, format_slug, canonical, count,
):
    library = _real_library(lane, subject, library_id)
    variants = iv.image_video_variant_plans(page, library)
    signatures = [plan["signature"] for plan in variants]
    assert len(signatures) == len(set(signatures)), "no two plans may share a signature"
    assert len(variants) == len({json.dumps([m["key"] for m in plan["media"]], sort_keys=True)
                                for plan in variants})
    # Deterministic: a second call is identical, run_id does not matter.
    assert iv.image_video_variant_plans(page, library) == variants
    assert ss.plan_slideshows(_recipe(page, format_slug, library), library, 1, set(), "a") \
        == ss.plan_slideshows(_recipe(page, format_slug, library), library, 1, set(), "b")
    # Every variant draws only from the page's own lane/subject photos.
    prefix = f"{lane}/{subject}/photos/"
    for plan in variants:
        assert all(media["key"].startswith(prefix) for media in plan["media"])
        assert len(plan["media"]) == count
        # Every selected object carries a full SHA-256 (pinned or key-embedded).
        for media in plan["media"]:
            assert iv.SHA256.fullmatch(media["sha256"])


@pytest.mark.parametrize("name,page,lane,subject,library_id,format_slug,canonical,count", PAGES,
                         ids=["pen-pal3", "twin47996"])
def test_reserving_every_variant_exhausts_the_page(
    name, page, lane, subject, library_id, format_slug, canonical, count,
):
    library = _real_library(lane, subject, library_id)
    variants = iv.image_video_variant_plans(page, library)
    everything = {plan["signature"] for plan in variants}
    assert ss.plan_slideshows(
        _recipe(page, format_slug, library), library, 1, everything, "run",
    ) == []


def test_foreign_subject_objects_are_never_drawn():
    library = _real_library("animals", "pen-pal3", "syzygy:animals:pen-pal3")
    # Inject a foreign-lane object with a valid embedded SHA. It must never be
    # selected even though it is grammatically well-formed.
    foreign_key = (
        "animals/twin47996/photos/pexels_foreign_"
        + "f" * 64 + ".jpg"
    )
    foreign = ss.SyzygyObject(foreign_key, 999_999, "https://r2.example/" + foreign_key)
    library = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject,
        tuple(sorted((*library.objects, foreign), key=lambda o: o.key)),
        library.snapshot_hash,
    )
    variants = iv.image_video_variant_plans(PENPAL_PAGE, library)
    for plan in variants:
        assert all(media["key"].startswith("animals/pen-pal3/photos/")
                   for media in plan["media"])
        assert all("twin47996" not in media["key"] for media in plan["media"])


@pytest.mark.parametrize("name,page,lane,subject,library_id,format_slug,canonical,count", PAGES,
                         ids=["pen-pal3", "twin47996"])
def test_signature_is_content_addressed_not_variant_bound(
    name, page, lane, subject, library_id, format_slug, canonical, count,
):
    library = _real_library(lane, subject, library_id)
    variants = iv.image_video_variant_plans(page, library)
    assert len(variants) >= 2
    plan = variants[1]
    assert plan["variant"] == 1
    assert iv.image_video_plan_signature(plan) == plan["signature"]
    # The variant number is carried for the runner's re-derivation but is not
    # part of the identity: re-numbering (or dropping) it leaves the signature
    # for the same content unchanged.
    assert iv.image_video_plan_signature({**plan, "variant": 2}) == plan["signature"]
    unnumbered = {key: value for key, value in plan.items() if key != "variant"}
    assert iv.image_video_plan_signature(unnumbered) == plan["signature"]


@pytest.mark.parametrize("name,page,lane,subject,library_id,format_slug,canonical,count", PAGES,
                         ids=["pen-pal3", "twin47996"])
def test_library_drift_never_reoffers_posted_content(
    name, page, lane, subject, library_id, format_slug, canonical, count,
):
    library = _real_library(lane, subject, library_id)
    variants = iv.image_video_variant_plans(page, library)
    assert len(variants) >= 6, "need v0..v5 to reserve"
    posted = variants[:6]
    reserved = {plan["signature"] for plan in posted}
    posted_content = {_media_content(plan) for plan in posted}
    # Variant 0 stays byte-identical to the pre-variant approved plan.
    assert posted[0]["signature"] == canonical
    recipe = _recipe(page, format_slug, library)

    # (a) Add one page photo that sorts first: no posted content re-offered.
    new_key = (
        f"{lane}/{subject}/photos/pexels_00000001_"
        + hashlib.sha256(b"new").hexdigest()
        + ".jpg"
    )
    add_lib = _library_with(library, lane, subject, extra=[(new_key, 12_345)])
    offered = ss.plan_slideshows(recipe, add_lib, 10_000, reserved, "run")
    assert offered, "the page must still offer fresh variants after an add"
    assert all(
        _media_content(plan["photoPlan"]) not in posted_content for plan in offered
    )

    # (b) Remove the first unpinned photo: no posted content re-offered, and the
    # next job is not an exact repeat.
    unpinned = sorted(
        item["key"]
        for plan in variants
        for item in plan["media"]
        if item["key"].rsplit("/", 1)[-1].startswith("pexels_")
    )
    drop_lib = _library_with(library, lane, subject, drop={unpinned[0]})
    offered = ss.plan_slideshows(recipe, drop_lib, 10_000, reserved, "run")
    assert offered, "the page must still offer fresh variants after a drop"
    assert all(
        _media_content(plan["photoPlan"]) not in posted_content for plan in offered
    )
    assert _media_content(offered[0]["photoPlan"]) not in posted_content, (
        "the next job must not be an exact posted photo hold"
    )
