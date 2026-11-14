"""Closed scoped lyric-video selection: no caller-authored cue arrays or text.

Page identity (page id, account, sound, notion page) is derived from the page's
bound lyric timing set, never from compiled-in constants. A page is registered
iff its timing set exists and matches the request's page/account/notion identity.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from services.lyric_timing import (
    lyric_timing_catalog,
    resolve_lyric_timing,
    _timing_set_for_page,
)

EXECUTOR_PATH = Path(__file__).resolve().parents[1] / "recipes/executors/syzygy-slideshow-lyric-video.v1.json"

# The Dossier editor and the strict treatment decoder both bound filters.brightness
# to [0, 3] (services/control_plane_generation.py FILTER_RANGES and
# services/source_treatment.py normalized_visual_treatment). Only that one scalar
# may vary from the approved once-graded treatment; every other treatment key must
# stay exactly neutral as before.
LYRIC_BRIGHTNESS_RANGE = (0.0, 3.0)


def lyric_treatment_matches(treatment: dict) -> bool:
    """True when only filters.brightness varies inside the allowed Dossier range.

    A dialed brightness is required: absent, non-numeric, non-finite or
    out-of-range values refuse. Contrast, saturation, warmth, fade, grain,
    vignette, clipSpeed and clipCrop must still equal the neutral base.
    """
    from services.source_treatment import normalized_visual_treatment
    if not isinstance(treatment, dict) or not isinstance(treatment.get("filters"), dict):
        return False
    raw = treatment["filters"].get("brightness")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    try:
        actual = normalized_visual_treatment(treatment)
    except (TypeError, ValueError):
        return False
    if not LYRIC_BRIGHTNESS_RANGE[0] <= actual["filters"]["brightness"] <= LYRIC_BRIGHTNESS_RANGE[1]:
        return False
    expected = normalized_visual_treatment({"filters": {"brightness": actual["filters"]["brightness"]}})
    return actual == expected


def lyric_ingredient(*, page_id: str, master_pages: dict | None) -> dict:
    account = master_pages.get("handle") if isinstance(master_pages, dict) else None
    options = lyric_timing_catalog(page_id=page_id, account=account) if isinstance(master_pages, dict) else []
    if (not isinstance(master_pages, dict)
            or not options
            or master_pages.get("notionPageId") != options[0]["notionPageId"]
            or master_pages.get("automationMode") != "Automation"
            or master_pages.get("archived") is not False):
        options = []
    return {"ingredientId": "lyric-timing", "kind": "timed_lyrics", "required": True,
            "status": "bound" if options else "missing", "binding": options[0] if options else None,
            "options": options, "selectionControl": "lyricTimingSetId"}


def validate_lyric_recipe(spec: dict) -> dict:
    """Resolve immutable timing and source-program revisions before any render POST.

    The sound restriction here is the recipe's timing sound, not authority to
    attach unofficial audio. The coordinator must additionally bind its exact
    official sound to this value when submitting the timed final request.
    """
    master = spec.get("masterPages", {})
    if isinstance(master, dict) and master.get("pageId") == "acct:burnt-mill-rd":
        raise ValueError("burnt lyric video disabled: static captions only")
    production = spec.get("production", {})
    controls = production.get("controls", {}) if isinstance(production, dict) else None
    if (not isinstance(master, dict)
            or master.get("automationMode") != "Automation" or master.get("archived") is not False
            or not isinstance(controls, dict) or set(controls) != {"lyricTimingSetId"}):
        raise ValueError("lyric video requires exactly its advertised timing selector")
    if not lyric_treatment_matches(spec.get("renderTreatment")):
        raise ValueError("lyric video requires its approved once-graded 20-second source treatment")
    options = lyric_timing_catalog(page_id=master.get("pageId"), account=master.get("handle"))
    if (len(options) != 1
            or controls["lyricTimingSetId"] != options[0]["id"]
            or master.get("notionPageId") != options[0]["notionPageId"]):
        raise ValueError("lyric timing selection is not available for this page")
    timing = resolve_lyric_timing(controls["lyricTimingSetId"], page_id=master["pageId"],
                                 account=master["handle"], sound_id=options[0]["soundId"], expected_sha256=options[0]["sha256"])
    if hashlib.sha256(EXECUTOR_PATH.read_bytes()).hexdigest() != timing["sourceProgramSha256"]:
        raise ValueError("lyric source program revision differs from timing authority")
    return timing


def validate_lyric_wire_plan(plan: dict, page_id: str) -> None:
    """Refuse a foreign or altered video program even at the render-wire boundary."""
    import json
    from services.control_plane_slideshows import (
        _parse_curated_video_plan, curated_plan_signature, curated_variant_number, curated_variant_plan,
    )
    if page_id == "acct:burnt-mill-rd":
        raise ValueError("burnt lyric video disabled: static captions only")
    timing_set = _timing_set_for_page(page_id)
    if timing_set is None:
        raise ValueError("lyric video page is not registered")
    timing = resolve_lyric_timing(timing_set["id"], page_id=page_id, account=timing_set["account"],
                                 sound_id=timing_set["soundId"], expected_sha256=timing_set["sha256"])
    raw = EXECUTOR_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != timing["sourceProgramSha256"]:
        raise ValueError("lyric source program is stale")
    curated = _parse_curated_video_plan(json.loads(raw)["pageTreatments"][page_id]["curatedVideoPlan"])
    # The approved program (variant 0) or one of its re-chop variants: every
    # variant keeps each window's output timing, so the lyric timing still fits.
    variant = curated_variant_number(plan)
    expected = curated_variant_plan(curated, variant) if variant is not None else None
    if (expected is None or not isinstance(plan, dict)
            or any(plan.get(k) != v for k, v in expected.items())
            or plan.get("signature") != curated_plan_signature(expected)):
        raise ValueError("lyric video plan differs from the registered source program")


def validate_managed_lyric_job(job: dict, publication: dict, recipe_spec: dict) -> dict:
    """Check the durable job against its exact registered immutable publication."""
    import json
    import re
    raw=publication.get('recipeSpecCanonical')
    expected=publication.get('recipeSpecHash')
    if (not isinstance(raw,str) or not isinstance(expected,str)
            or hashlib.sha256(raw.encode()).hexdigest()!=expected.removeprefix('sha256:')
            or json.loads(raw)!=recipe_spec
            or job.get('recipeSpecHash')!=expected
            or job.get('recipeId')!='lyric-video:master' or publication.get('recipeId')!=job['recipeId']
            or job.get('engine')!='lyrics_slideshows' or publication.get('engine')!=job['engine']
            or job.get('recipeVersion')!=publication.get('recipeVersion')
            or job.get('sourceKind')!='syzygy_slideshow'
            or any(not isinstance(job.get(k),str) or not job[k].strip() or len(job[k])>512
                   or any(ord(c)<32 for c in job[k]) for k in ('jobId','recipeVersion'))):
        raise ValueError('lyric managed job does not match its pinned publication')
    timing = validate_lyric_recipe(recipe_spec)
    if (job.get('pageId')!=timing['pageId'] or publication.get('pageId')!=timing['pageId']):
        raise ValueError('lyric managed job does not match its pinned publication')
    return timing


def managed_lyric_binding(job: dict, publication: dict, recipe_spec: dict, source_sha256: str) -> dict:
    """Call after actual managed background execution and exact media hashing."""
    import re
    timing=validate_managed_lyric_job(job,publication,recipe_spec)
    if not isinstance(source_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',source_sha256):
        raise ValueError('lyric source hash is missing')
    return {'schema':'content-lab.lyric-timing-binding.v1',
            'pageId':timing['pageId'],'account':timing['account'],'notionPageId':timing['notionPageId'],
            'soundId':timing['soundId'],'timingSetId':timing['id'],'timingSha256':timing['sha256'],
            'manifestJson':timing['manifestJson'],
            'sourceProgramId':timing['sourceProgramId'],'sourceProgramSha256':timing['sourceProgramSha256'],
            'durationMs':timing['durationMs'],'sourceSha256':source_sha256,
            'generationJobId':job['jobId'],'recipeId':job['recipeId'],'recipeVersion':job['recipeVersion'],
            'recipeSpecHash':job['recipeSpecHash']}
