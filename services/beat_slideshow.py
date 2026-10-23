"""Opt-in cat photo experiment. All times are relative to the unchanged sound start."""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math

NOTES = ("1/16", "1/8", "1/4", "1/2")
BEATS = (.25, .5, 1., 2.)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"experimentId", "soundId", "soundUrl", "bpm", "firstBeatOffset", "beatSource", "captionPoolId", "captions", "canaryPercent"}:
        raise ValueError("beat slideshow config fields are invalid")
    for key in ("experimentId", "soundId", "soundUrl", "captionPoolId"):
        if not isinstance(config[key], str) or not 1 <= len(config[key]) <= 4096:
            raise ValueError("beat slideshow identity is invalid")
    from urllib.parse import urlsplit
    url = urlsplit(config["soundUrl"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("beat slideshow sound URL is invalid")
    for key, lo, hi in (("bpm", 40, 240), ("firstBeatOffset", 0, 60)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError("beat slideshow tempo is invalid")
    if config["beatSource"] not in {"metadata", "detected"}:
        raise ValueError("beat slideshow beat source is invalid")
    captions = config["captions"]
    if not isinstance(captions, list) or not 1 <= len(captions) <= 32 or any(not isinstance(x, str) or not x.strip() or len(x) > 240 for x in captions) or len(set(captions)) != len(captions):
        raise ValueError("beat slideshow page caption pool is invalid")
    if type(config["canaryPercent"]) is not int or not 0 <= config["canaryPercent"] <= 100:
        raise ValueError("beat slideshow canary percentage is invalid")
    return copy.deepcopy(config)


def cohort(page_id, config, variant=0):
    # Asset-level assignment allows a canary and holdout on the same cat page.
    bucket = int(digest([config["experimentId"], page_id, variant // 4])[:8], 16) % 100
    return "canary" if bucket < config["canaryPercent"] else "holdout"


def grid(total, bpm, first_beat, note):
    step = 60 / bpm * BEATS[NOTES.index(note)]
    # Include every subdivision of the beat grid after t=0, even before the first beat.
    start = math.floor(-first_beat / step) + 1
    times = [0.]
    for n in range(start, start + 1000):
        time = first_beat + n * step
        if time >= total - 1e-9:
            break
        if time > 1e-9:
            times.append(round(time, 9))
    times.append(total)
    return times


def variant_count(base, config):
    return math.factorial(len(base["media"])) * 4 * len(config["captions"])


def make_plan(base, config, variant):
    config = validate_config(config)
    if base["lane"] != "animals" or not 2 <= len(base["media"]) <= 6:
        raise ValueError("beat slideshow requires a cat photo arrangement")
    if type(variant) is not int or not 0 <= variant < variant_count(base, config):
        raise ValueError("beat slideshow variant is invalid")
    result = copy.deepcopy(base)
    result.pop("signature", None)
    group = cohort(base["pageId"], config, variant)
    # Full factorial: timing varies fastest; caption and arrangement contrasts stay identifiable.
    timing = variant % 4
    caption_index = variant // 4 % len(config["captions"])
    arrangement = variant // (4 * len(config["captions"]))
    order = list(itertools.islice(itertools.permutations(range(len(base["media"]))), arrangement, arrangement + 1))[0]
    receipt = {"pageId": base["pageId"], "config": config, "variant": variant, "cohort": group,
               "note": NOTES[timing], "arrangement": list(order), "captionIndex": caption_index,
               "caption": config["captions"][caption_index], "audioStart": 0}
    if group == "holdout":
        # Hold timing fixed while preserving independent arrangement/caption contrasts.
        receipt.update(variant=variant // 4 * 4, note="fixed")
        result["media"] = [copy.deepcopy(base["media"][index]) for index in order]
        for item in result["media"]:
            item["kenBurns"] = None
    else:
        times = grid(base["totalDurationSeconds"], config["bpm"], config["firstBeatOffset"], NOTES[timing])
        result["media"] = []
        for i, (start, end) in enumerate(zip(times, times[1:])):
            item = copy.deepcopy(base["media"][order[i % len(order)]])
            item["duration"] = end - start
            item["kenBurns"] = None
            result["media"].append(item)
    result["beatSlideshow"] = receipt
    from services.control_plane_image_video import image_video_plan_signature
    result["signature"] = image_video_plan_signature(result)
    return result


def valid_beats(metadata):
    if not isinstance(metadata, dict):
        return False
    return all(type(metadata.get(key)) in (int, float) and math.isfinite(metadata[key]) and lo <= metadata[key] <= hi
               for key, lo, hi in (("bpm", 40, 240), ("firstBeatOffset", 0, 60)))


async def resolve_sound_beats(sound_url, metadata=None):
    """Metadata first; bounded original-link analysis otherwise. Never trim the sound."""
    import asyncio
    import logging
    import tempfile
    import time
    from pathlib import Path
    from services.control_plane_source_imports import validate_source_url
    from scraper.frame_extractor import download_video
    start = time.monotonic()
    from urllib.parse import urlsplit
    parsed = urlsplit(sound_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or len(sound_url) > 4096:
        raise ValueError("beat sound requires an HTTPS link")
    if valid_beats(metadata):
        return {"bpm": metadata["bpm"], "firstBeatOffset": metadata["firstBeatOffset"], "beatSource": "metadata", "analysisSeconds": 0, "audioStart": 0}
    url = await asyncio.to_thread(validate_source_url, sound_url)
    async def run(*args, timeout):
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise ValueError("beat analysis could not decode or detect this sound")
            return stdout
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    async with _analysis_gate():
        with tempfile.TemporaryDirectory(prefix="lab-beats-") as directory:
            root = Path(directory)
            task = asyncio.create_task(download_video(url, root / "sound.mp4", max_filesize=20_000_000, source_import_mode=True))
            try:
                deadline = asyncio.get_running_loop().time() + 90
                while not task.done():
                    if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > 24_000_000 or asyncio.get_running_loop().time() > deadline:
                        raise ValueError("beat sound download exceeded its time or byte budget")
                    await asyncio.wait({task}, timeout=.25)
                media = Path(await task)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            wav = root / "sound.wav"
            await run("ffmpeg", "-v", "error", "-i", str(media), "-t", "60", "-ac", "1", "-ar", "22050", str(wav), timeout=20)
            # Process isolation enforces a CPU deadline and keeps numerical imports off the API loop.
            import sys

            result = json.loads(await run(sys.executable, "-c", DETECT_AUDIO_CODE, str(wav), timeout=45))
            if not valid_beats(result):
                raise ValueError("detected sound tempo is unsupported")
            result.update(beatSource="detected", audioStart=0, analysisSeconds=round(time.monotonic()-start, 3), audioSha256=hashlib.sha256(wav.read_bytes()).hexdigest())
            logging.getLogger(__name__).info("beat_sound_analysis_complete seconds=%.3f", time.monotonic()-start)
            return result


_ANALYSIS_GATE = None


def _analysis_gate():
    global _ANALYSIS_GATE
    import asyncio
    if _ANALYSIS_GATE is None:
        _ANALYSIS_GATE = asyncio.Semaphore(1)
    return _ANALYSIS_GATE


DETECT_AUDIO_CODE = '''import json,librosa,sys,numpy as np
x,sr=librosa.load(sys.argv[1],sr=22050,duration=60)
if len(x)<sr*2 or np.max(np.abs(x))<.001: raise ValueError("insufficient sound")
tempo,beats=librosa.beat.beat_track(y=x,sr=sr,hop_length=128,trim=True)
times=librosa.frames_to_time(beats,sr=sr,hop_length=128)
if len(times)<4 or np.std(np.diff(times))/np.mean(np.diff(times))>.15: raise ValueError("insufficient regular beats")
period,origin=np.polyfit(np.arange(len(times)),times,1)
bpm=60/float(period)
# Anchor to the earliest observed onset on this beat grid. Preserve whole-beat
# count: modulo one beat would flip half-note parity after leading silence.
onsets=librosa.onset.onset_detect(y=x,sr=sr,hop_length=128,units='time')
aligned=[(float(t),float(origin+round((t-origin)/period)*period)) for t in onsets]
aligned=[(t,b) for t,b in aligned if b>=0 and abs(t-b)<.05]
if not aligned: raise ValueError("no measured first beat")
first=min(aligned)[1]
print(json.dumps({"bpm":bpm,"firstBeatOffset":first}))'''


def summarize_results(rows):
    """Compare exported 24-hour outcomes by experiment, sound and independent factor.

    Input rows carry the exact source photoPlan.beatSlideshow receipt, not a
    reconstructed label. Unposted/unmatured observations are not measurements.
    """
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("beat comparison requires at most 10000 outcome rows")
    seen = set()
    groups = {}
    for row in rows:
        post = row.get("postId")
        views = row.get("views24h")
        receipt = row.get("receipt", {})
        if not isinstance(post, str) or not post or post in seen or type(views) is not int or views < 0:
            raise ValueError("beat comparison needs unique posted IDs and measured 24-hour views")
        seen.add(post)
        cfg = validate_config(receipt.get("config"))
        from services.control_plane_image_video import image_video_plan
        original = image_video_plan(receipt.get("pageId"))
        if original is None or make_plan(original, cfg, receipt.get("variant"))["beatSlideshow"] != receipt:
            raise ValueError("beat comparison receipt is invalid")
        factors = {"timing": receipt["note"], "arrangement": str(receipt["arrangement"]),
                   "caption": receipt["caption"], "cohort": receipt["cohort"]}
        for factor, arm in factors.items():
            key = (cfg["experimentId"], cfg["soundId"], digest(cfg), receipt["pageId"], factor, arm)
            values = groups.setdefault(key, [])
            values.append(views)
    return [dict(experimentId=k[0], soundId=k[1], configSha256=k[2], pageId=k[3], factor=k[4], arm=k[5], posts=len(v),
                 totalViews24h=sum(v), meanViews24h=sum(v)/len(v)) for k,v in sorted(groups.items())]
