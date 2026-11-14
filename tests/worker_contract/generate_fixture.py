"""Generate the real Lab image-video variant sources for the Worker contract test.

Every source here is the exact ``syzygy-slideshow-source.v1`` envelope the Lab
runner emits at render completion (see ``routers/control_plane.py``
``_source_provenance``), built from the real R2 keys/bytes in
``tests/fixtures/pentwin-keys.tsv``. Variant 0 is the approved plan (no index
key); variants 1..N carry ``variantIndex`` and are never spelled with the
legacy ``variant`` key.
"""
import json
from pathlib import Path

import services.control_plane_image_video as iv
from tests.test_image_video_variants import (
    PENPAL_PAGE,
    TWIN_PAGE,
    _real_library,
    _recipe,
)

HERE = Path(__file__).resolve().parent
OUT = HERE / "pentwin-variant-sources.json"

PAGES = [
    ("pen-pal3", PENPAL_PAGE, "animals", "pen-pal3", "syzygy:animals:pen-pal3",
     "image-video", "sourced_slideshow", "slideshow",
     "3ed1465b-b829-809f-afb4-da66093e7f9d", "pen.pal3"),
    ("twin47996", TWIN_PAGE, "interiors", "twin47996", "syzygy:interiors:twin47996",
     "single-image-video", "sourced_image", "POV",
     "3ed1465b-b829-8056-aa9b-f8579c3210b8", "twin47996"),
]


def build_sources() -> list[dict]:
    sources = []
    for (name, page, lane, subject, library_id, format_slug, engine, niche,
         notion, handle) in PAGES:
        library = _real_library(lane, subject, library_id)
        recipe = _recipe(page, format_slug, library)
        envelopes = iv.recipe_image_video_plans(recipe, library)
        assert envelopes, f"{name} must offer its approved plan"
        for index, envelope in enumerate(envelopes):
            plan = envelope["photoPlan"]
            media = [
                {
                    "objectKey": item["key"],
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                    "etag": '"pinned"',
                }
                for item in plan["media"]
            ]
            sources.append({
                "schema": "content-lab.syzygy-slideshow-source.v1",
                "kind": "syzygy_slideshow",
                "recipeId": f"{format_slug}:master",
                "recipeVersion": "sha256:" + "a" * 64,
                "sourceLibraryId": library_id,
                "librarySnapshotHash": envelope["librarySnapshotHash"],
                "templateSlug": envelope["templateSlug"],
                "planSignature": envelope["signature"],
                "rendererRevision": "b" * 64,
                "renderJobId": f"render-{name}-{index:04d}",
                "media": media,
                "photoPlan": plan,
                "pageId": page,
                "masterPagesHash": "sha256:" + "c" * 64,
                "contentNiche": niche,
                "contentEngine": engine,
                "vaultUrl": f"https://shipstream.risingtidesviral.com/vault/{handle}",
            })
    return sources


def main() -> None:
    sources = build_sources()
    OUT.write_text(
        json.dumps(sources, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(sources)} sources to {OUT}")


if __name__ == "__main__":
    main()
