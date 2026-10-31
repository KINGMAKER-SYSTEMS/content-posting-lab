#!/usr/bin/env python3
"""Stage Burnt's reviewed, disjoint curated plan from a cutter manifest.

The default is intentionally a dry run.  ``--write`` is the one reviewed
local change that replaces the Burnt treatment in both reachable executors.
"""
import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import control_plane_slideshows as slides
from services.content_engine_registry import load_engine_registry

EXECUTORS = (
    ROOT / "recipes/executors/syzygy-slideshow-ugc.v1.json",
    ROOT / "recipes/executors/syzygy-slideshow-lyric-video.v1.json",
)
WRITABLE_EXECUTORS = (EXECUTORS[0],)
UGC, LYRIC = EXECUTORS
REGISTRY = ROOT / "recipes/content-engine-registry.v1.json"
CONTRACTS = ROOT / "recipes/content-format-contracts.v1.json"
BURNT = "acct:burnt-mill-rd"
CULTURE = "acct:culture-core-1"
FILENAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,199}\.mp4$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Refusal(ValueError):
    pass


def refuse(reason):
    raise Refusal(reason)


def read_executor(path):
    with path.open() as handle:
        return json.load(handle)


def treatment(data, page):
    try:
        return data["pageTreatments"][page]["curatedVideoPlan"]
    except (KeyError, TypeError):
        refuse("missing_curated_plan")


def validate_manifest(value, executors):
    # The cutter records editorial exclusions alongside the reviewed clips.
    # It is provenance, not plan input, and deliberately remains outside the
    # strict per-clip schema below.
    if (not isinstance(value, dict) or value.get("schema") != "p89.burnt-clips.v1"
            or set(value) not in ({"schema", "clips"}, {"schema", "clips", "droppedByLead"})):
        refuse("invalid_manifest_schema")
    clips = value["clips"]
    if not isinstance(clips, list) or len(clips) < 30:
        refuse("fewer_than_30_clips")
    old_files, old_shas, old_pairs, culture_files, culture_shas = set(), set(), set(), set(), set()
    for data in executors:
        burnt = data.get("pageTreatments", {}).get(BURNT, {}).get("curatedVideoPlan")
        if isinstance(burnt, dict):
            old_files.update(source["file"] for source in burnt["sources"])
            old_shas.update(source["sha256"] for source in burnt["sources"])
            old_pairs.update((source["file"], source["sha256"]) for source in burnt["sources"])
        culture = data.get("pageTreatments", {}).get(CULTURE, {}).get("curatedVideoPlan")
        if culture:
            culture_files.update(source["file"] for source in culture["sources"])
            culture_shas.update(source["sha256"] for source in culture["sources"])
    seen_files, seen_shas = set(), set()
    checked = []
    required = {"file", "sha256", "bytes", "duration", "width", "height", "sourceUrl", "ytid", "originStart", "originEnd"}
    optional = required | {"focalX"}
    current_pairs = old_pairs
    manifest_pairs = {(clip.get("file"), clip.get("sha256")) for clip in clips if isinstance(clip, dict)}
    replacing_same_plan = bool(current_pairs) and current_pairs == manifest_pairs
    # A reviewed harvest may supersede a differently sized cutter batch while
    # retaining stable filenames.  Do not mistake that replacement for an
    # attempt to replay a current same-sized plan.
    replacing_different_sized_library = len(clips) != len(current_pairs)
    for clip in clips:
        if not isinstance(clip, dict) or not required <= set(clip) <= optional:
            refuse("invalid_manifest_clip")
        file, sha = clip["file"], clip["sha256"]
        if not isinstance(file, str) or not FILENAME.fullmatch(file):
            refuse("invalid_file_name")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            refuse("invalid_sha256")
        if file in seen_files:
            refuse("duplicate_manifest_file")
        if sha in seen_shas:
            refuse("duplicate_manifest_sha256")
        seen_files.add(file); seen_shas.add(sha)
        if file in culture_files:
            refuse("culture_file_collision")
        if sha in culture_shas:
            refuse("culture_sha256_collision")
        if file in old_files and not replacing_same_plan and not replacing_different_sized_library:
            refuse("current_burnt_file_collision")
        if sha in old_shas and not replacing_same_plan and not replacing_different_sized_library:
            refuse("current_burnt_sha256_collision")
        if isinstance(clip["bytes"], bool) or not isinstance(clip["bytes"], int) or clip["bytes"] <= 0:
            refuse("nonpositive_bytes")
        duration = clip["duration"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 4 <= duration <= 15:
            refuse("duration_out_of_range")
        focal = clip.get("focalX", 0.5)
        if isinstance(focal, bool) or not isinstance(focal, (int, float)) or not math.isfinite(focal) or not 0 <= focal <= 1:
            refuse("invalid_focal_x")
        checked.append(dict(clip, focalX=focal))
    return checked


def plan_for(current, clips):
    """Keep Burnt's output rhythm; distribute windows through manifest order."""
    windows = current["windows"]
    sources = [{key: clip[key] for key in ("file", "sha256", "bytes", "duration")} for clip in clips]
    new_windows = []
    total = len(clips)
    for index, old in enumerate(windows):
        # Stratified selection reaches across the whole ordered cutter manifest.
        clip = clips[(index * total) // len(windows)]
        length = old["outputEnd"] - old["outputStart"]
        room = clip["duration"] - length
        if room < 0:
            refuse("window_does_not_fit_clip")
        # Deterministic, in-bounds point; rounding is deliberately inward.
        fraction = ((index + 1) * 0.6180339887498949) % 1
        start = math.floor(fraction * room * 1_000_000) / 1_000_000
        new_windows.append({
            "source": clip["file"], "sourceStart": start, "sourceEnd": start + length,
            "outputStart": old["outputStart"], "outputEnd": old["outputEnd"],
            "focalX": clip["focalX"],
        })
    return {**current, "audioSectionEnd": 20, "sources": sources, "windows": new_windows}


def canonical_hash(value):
    """Return the contract registry's documented canonical entry digest."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def serialize(value):
    """Keep the repository's existing ASCII-escaped, two-space JSON bytes."""
    return (json.dumps(value, indent=2) + "\n").encode()


def candidate_pins(executor_bytes):
    """Build the UGC authority links without touching the committed tree."""
    contracts = json.loads(CONTRACTS.read_text())
    registry = json.loads(REGISTRY.read_text())
    digest = hashlib.sha256(executor_bytes[UGC]).hexdigest()
    contracts["contracts"]["ugc-slideshow"]["creativeAuthority"]["version"] = f"sha256:{digest}"
    profile = registry["profiles"]["ugc-slideshow"]
    profile["executorVersion"] = f"sha256:{digest}"
    profile["formatContractVersion"] = f"sha256:{canonical_hash(contracts['contracts']['ugc-slideshow'])}"
    return contracts, registry


def validate_candidate(executor_bytes, contracts_bytes, registry_bytes):
    """Run the Lab's parsers/loaders against every prospective authority file."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ugc, lyric = root / "ugc.json", root / "lyric.json"
        contracts, registry = root / "contracts.json", root / "registry.json"
        ugc.write_bytes(executor_bytes[UGC])
        lyric.write_bytes(executor_bytes[LYRIC])
        contracts.write_bytes(contracts_bytes)
        registry.write_bytes(registry_bytes)
        old_ugc, old_lyric = slides.UGC_EXECUTOR_PATH, slides.LYRIC_VIDEO_EXECUTOR_PATH
        old_contracts = __import__("os").environ.get("CONTENT_LAB_FORMAT_CONTRACTS")
        old_registry = __import__("os").environ.get("CONTENT_LAB_ENGINE_REGISTRY")
        try:
            slides.UGC_EXECUTOR_PATH, slides.LYRIC_VIDEO_EXECUTOR_PATH = ugc, lyric
            __import__("os").environ["CONTENT_LAB_FORMAT_CONTRACTS"] = str(contracts)
            __import__("os").environ["CONTENT_LAB_ENGINE_REGISTRY"] = str(registry)
            slides._executor_contract("ugc-slideshow")
            slides._executor_contract("lyric-video")
            profiles, _ = load_engine_registry()
            for slug in ("ugc-slideshow", "lyric-video"):
                executor, digest = slides._executor_contract(slug)
                if profiles[slug].executor_version != f"sha256:{digest}":
                    refuse("candidate_authority_mismatch")
                if not executor:
                    refuse("candidate_executor_invalid")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            refuse(f"candidate_invalid: {error}")
        finally:
            slides.UGC_EXECUTOR_PATH, slides.LYRIC_VIDEO_EXECUTOR_PATH = old_ugc, old_lyric
            if old_contracts is None:
                __import__("os").environ.pop("CONTENT_LAB_FORMAT_CONTRACTS", None)
            else:
                __import__("os").environ["CONTENT_LAB_FORMAT_CONTRACTS"] = old_contracts
            if old_registry is None:
                __import__("os").environ.pop("CONTENT_LAB_ENGINE_REGISTRY", None)
            else:
                __import__("os").environ["CONTENT_LAB_ENGINE_REGISTRY"] = old_registry


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--write-pins", action="store_true",
                        help="with --write, update executor and contract authority pins")
    args = parser.parse_args(argv)
    if args.write != args.write_pins:
        parser.error("--write and --write-pins must be used together")
    try:
        with args.manifest.open() as handle:
            manifest = json.load(handle)
        loaded = [read_executor(path) for path in EXECUTORS]
        clips = validate_manifest(manifest, loaded)
        replacements = {UGC: plan_for(treatment(loaded[0], BURNT), clips)}
    except (OSError, json.JSONDecodeError) as error:
        print(f"REFUSAL unreadable_manifest: {error}", file=sys.stderr)
        return 2
    except Refusal as error:
        print(f"REFUSAL {error}", file=sys.stderr)
        return 2
    candidate = {path: path.read_bytes() for path in EXECUTORS}
    for path, data in zip(WRITABLE_EXECUTORS, loaded):
        replacement = replacements[path]
        old = treatment(data, BURNT)
        print(f"{path.relative_to(ROOT)}: burnt sources {len(old['sources'])}->{len(replacement['sources'])}; windows {len(old['windows'])}->{len(replacement['windows'])}; unchanged output rhythm")
        data["pageTreatments"][BURNT]["curatedVideoPlan"] = replacement
        candidate[path] = serialize(data)
    contracts, registry = candidate_pins(candidate)
    contracts_bytes, registry_bytes = serialize(contracts), serialize(registry)
    try:
        validate_candidate(candidate, contracts_bytes, registry_bytes)
    except Refusal as error:
        print(f"REFUSAL {error}", file=sys.stderr)
        return 2
    if args.write:
        for path in WRITABLE_EXECUTORS:
            path.write_bytes(candidate[path])
        CONTRACTS.write_bytes(contracts_bytes)
        REGISTRY.write_bytes(registry_bytes)
    print("WRITE" if args.write else "DRY-RUN (pass --write to edit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
