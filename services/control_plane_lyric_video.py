"""Closed Burnt lyric-video selection: no caller-authored cue arrays or text."""
from __future__ import annotations
import hashlib
from pathlib import Path
from services.lyric_timing import lyric_timing_catalog, resolve_lyric_timing

PAGE_ID = "acct:burnt-mill-rd"
ACCOUNT = "burnt.mill.rd"
SOUND_ID = "7611482162773035025"
FORMAT_ID = "lyric-video"
NOTION_ID = "3451465b-b829-80a8-b9d1-fb7e34b98ff5"
EXECUTOR_PATH = Path(__file__).resolve().parents[1] / "recipes/executors/syzygy-slideshow-lyric-video.v1.json"


def lyric_ingredient(*, page_id: str, master_pages: dict | None) -> dict:
    account = master_pages.get("handle") if isinstance(master_pages, dict) else None
    options = (lyric_timing_catalog(page_id=page_id, account=account)
               if isinstance(master_pages, dict) and master_pages.get("notionPageId") == NOTION_ID
               and master_pages.get("automationMode") == "Automation" and master_pages.get("archived") is False else [])
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
    production = spec.get("production", {})
    controls = production.get("controls", {}) if isinstance(production, dict) else None
    if (not isinstance(master, dict) or master.get("notionPageId") != NOTION_ID
            or master.get("automationMode") != "Automation" or master.get("archived") is not False
            or not isinstance(controls, dict) or set(controls) != {"lyricTimingSetId"}):
        raise ValueError("lyric video requires exactly its advertised timing selector")
    from services.source_treatment import normalized_visual_treatment
    expected_visual = normalized_visual_treatment({"filters": {"brightness": 0.85}})
    if normalized_visual_treatment(spec.get("renderTreatment")) != expected_visual:
        raise ValueError("lyric video requires its approved once-graded 20-second source treatment")
    options = lyric_timing_catalog(page_id=master.get("pageId"), account=master.get("handle"))
    if len(options) != 1 or controls["lyricTimingSetId"] != options[0]["id"]:
        raise ValueError("lyric timing selection is not available for this page")
    timing = resolve_lyric_timing(controls["lyricTimingSetId"], page_id=master["pageId"],
                                 account=master["handle"], sound_id=SOUND_ID, expected_sha256=options[0]["sha256"])
    if hashlib.sha256(EXECUTOR_PATH.read_bytes()).hexdigest() != timing["sourceProgramSha256"]:
        raise ValueError("lyric source program revision differs from timing authority")
    return timing


def validate_lyric_wire_plan(plan: dict, page_id: str) -> None:
    """Refuse a foreign or altered video program even at the render-wire boundary."""
    import json
    from services.control_plane_slideshows import _parse_curated_video_plan, curated_plan_signature
    options = lyric_timing_catalog(page_id=page_id, account=ACCOUNT)
    if len(options) != 1:
        raise ValueError("lyric video page is not registered")
    timing = resolve_lyric_timing(options[0]["id"], page_id=page_id, account=ACCOUNT,
                                 sound_id=SOUND_ID, expected_sha256=options[0]["sha256"])
    raw = EXECUTOR_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != timing["sourceProgramSha256"]:
        raise ValueError("lyric source program is stale")
    curated = _parse_curated_video_plan(json.loads(raw)["pageTreatments"][page_id]["curatedVideoPlan"])
    sources = {source["file"]: source for source in curated["sources"]}
    windows = []
    keys = []
    for window in curated["windows"]:
        source = sources[window["source"]]
        key = f'{curated["lane"]}/{curated["subject"]}/clips/{window["source"]}'
        if key not in keys:
            keys.append(key)
        windows.append({"key": key, **{k: window[k] for k in ("sourceStart", "sourceEnd", "outputStart", "outputEnd", "focalX")},
                        "sourceSha256": source["sha256"], "sourceBytes": source["bytes"], "sourceDuration": source["duration"]})
    expected = {"templateSlug": "curated-video-montage", "mediaKeys": keys, "windows": windows,
                **{k: curated[k] for k in ("muted", "speed", "cropMode", "audioSectionEnd")}}
    if (not isinstance(plan, dict) or any(plan.get(k) != v for k, v in expected.items())
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
            or job.get('pageId')!=PAGE_ID or publication.get('pageId')!=PAGE_ID
            or job.get('recipeId')!='lyric-video:master' or publication.get('recipeId')!=job['recipeId']
            or job.get('engine')!='lyrics_slideshows' or publication.get('engine')!=job['engine']
            or job.get('recipeVersion')!=publication.get('recipeVersion')
            or job.get('sourceKind')!='syzygy_slideshow'
            or any(not isinstance(job.get(k),str) or not job[k].strip() or len(job[k])>512
                   or any(ord(c)<32 for c in job[k]) for k in ('jobId','recipeVersion'))):
        raise ValueError('lyric managed job does not match its pinned publication')
    return validate_lyric_recipe(recipe_spec)


def managed_lyric_binding(job: dict, publication: dict, recipe_spec: dict, source_sha256: str) -> dict:
    """Call after actual managed background execution and exact media hashing."""
    import re
    timing=validate_managed_lyric_job(job,publication,recipe_spec)
    if not isinstance(source_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',source_sha256):
        raise ValueError('lyric source hash is missing')
    return {'schema':'content-lab.lyric-timing-binding.v1',
            'pageId':PAGE_ID,'account':ACCOUNT,'notionPageId':NOTION_ID,
            'soundId':timing['soundId'],'timingSetId':timing['id'],'timingSha256':timing['sha256'],
            'manifestJson':timing['manifestJson'],
            'sourceProgramId':timing['sourceProgramId'],'sourceProgramSha256':timing['sourceProgramSha256'],
            'durationMs':timing['durationMs'],'sourceSha256':source_sha256,
            'generationJobId':job['jobId'],'recipeId':job['recipeId'],'recipeVersion':job['recipeVersion'],
            'recipeSpecHash':job['recipeSpecHash']}
