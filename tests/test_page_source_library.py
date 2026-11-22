"""A page whose bound masters are used up cuts from its own source library.

Owner 2026-11-21 (pad #407, part 7): when every window of a page's bound
source library is cut, the Lab looks at every source ever registered or cut
for that page (its library), not only the bound batch, and cuts the spans not
yet used. The no-repeat rule stays whole: a new body never shares footage with
a body already cut for that page, forward or mirrored, whatever library it
came from. Unknown span history counts as used. Only when every span of every
library source is used does the page stay exhausted, and the answer then says
plainly that the page needs new footage and points at the fetcher (#227).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

import routers.control_plane as cp
from services import job_store_compaction as compaction
from services.source_dna_registry import parse_source_dna_manifest
from services.control_plane_sources import (
    SOURCE_WINDOWS_EXHAUSTED,
    plan_source_cuts,
    source_cut_overlaps_used_frames,
)
from services.page_source_library import (
    JOB_LIBRARY_MASTERS_KEY,
    NEEDS_NEW_FOOTAGE,
    ZERO_REASON_HELP_HEADER,
    library_recipe,
    master_row,
    masters_from_job,
)
from tests.test_control_plane_source_execution import (  # noqa: F401 (fixtures)
    LIBRARY_ID,
    MASTER_SHA,
    PAGE_ID,
    TOKEN,
    headers,
    job_body,
    lab,
    offline_shipstream_manifest,
    publication,
)
from tests.source_dna_fixture import isolate_source_dna_registry
from services.control_plane_sources import SourceRecipe
from services.page_source_library import original_source_keys
from services.shipstream_source_manifest import SHIPSTREAM_VAULT_ORIGIN_ENV
from services.source_dna_registry import MasterSource

LIBRARY_SHA = "f" * 64
HISTORY_SHA = "d" * 64
OTHER_PAGE_SHA = "a" * 64
REENCODED_SHA = "b" * 64
CAPABILITY_HEADERS = {"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    """An isolated registry copy, so a test may register a sibling library."""
    return isolate_source_dna_registry(monkeypatch, tmp_path)


def _manifest_row(sha256: str, duration_ms: int, page_tag: str) -> dict:
    return {
        "sourceId": f"youtube-{page_tag}-{sha256[:6]}",
        "sha256": sha256,
        "bytes": 1_000_000,
        "filename": f"{sha256[:8]}.mp4",
        "mimeType": "video/mp4",
        "storageKey": f"source-dna/pov-dirt-bike/{page_tag}/{sha256}.mp4",
        "durationMs": duration_ms,
        # Past the one-minute lead-in skip a raw library keeps, so every
        # library-local second from 0 is cuttable (as the bound fixture is).
        "sourceOffsetMs": 120_000,
        "provenance": {
            "sourceUrl": f"https://www.youtube.com/watch?v={sha256[:11]}",
            "acquiredAt": "2026-11-21T00:00:00Z",
            "authority": "test fixture: operator-reviewed original footage",
        },
    }


def _register_sibling_library(registry, library_id: str, page_id: str, sha256: str, duration_ms: int):
    (registry / f"{library_id}.json").write_text(json.dumps({
        "schema": "content-lab.source-dna-library.v2",
        "libraryId": library_id,
        "format": "pov-dirt-bike",
        "pageId": page_id,
        "masters": [_manifest_row(sha256, duration_ms, page_id)],
    }, sort_keys=True, separators=(",", ":")))


def _shrink_bound_master(monkeypatch, duration_ms: int = 6_000):
    """The page's bound master holds exactly one window: one cut exhausts it."""
    resolve = cp._dossier_source_recipe

    def tiny(payload, refusal_reasons=None):
        recipe = resolve(payload, refusal_reasons)
        if recipe is None:
            return None
        return replace(recipe, masters=tuple(
            replace(master, duration_ms=duration_ms) for master in recipe.masters
        ))

    monkeypatch.setattr(cp, "_dossier_source_recipe", tiny)


def _exhaust_bound_master(client) -> str:
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("bound-last-window"),
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["jobId"]
    assert cp._load_jobs()["jobs"][job_id]["sourceCuts"][0]["masterSha256"] == MASTER_SHA
    cp._update_job(job_id, status="completed", completedAt="2026-11-21T12:00:00+00:00")
    return job_id


def _max_quantity(client) -> int:
    capability = client.get(
        "/api/control-plane/v1/capabilities", headers=CAPABILITY_HEADERS,
    ).json()["capabilities"]
    return capability[0]["maxQuantity"]


def _record_history_master(
    store_path, sha256: str, duration_ms: int, *, library_id="shipstream-old",
    job_id="history-job", source_url=None, origin=None,
) -> None:
    """A completed job this page cut earlier from a master no longer bound."""
    store = json.loads(store_path.read_text())
    row = _manifest_row(sha256, duration_ms, PAGE_ID)
    if source_url is not None:
        row["provenance"]["sourceUrl"] = source_url
    if origin is not None:
        row["origin"] = origin
    store["jobs"][job_id] = {
        "jobId": job_id,
        "pageId": PAGE_ID,
        "sourceKind": "dossier_source_dna",
        "status": "completed",
        "completedAt": "2026-11-01T12:00:00+00:00",
        "sourceLibraryId": library_id,
        "sourceLibraryHash": "0" * 64,
        "recipeVersion": "dossier-older",
        "sourceCuts": [{
            "slotId": f"{sha256}:10000:7000",
            "masterSha256": sha256,
            "libraryStartMs": 10_000,
            "startMs": 10_000,
            "durationMs": 7_000,
        }],
        "clips": [{
            "sha256": "1" * 64,
            "source": {
                "recipeId": "pov-dirt-bike:master",
                "sourceLibraryId": library_id,
                "sourceLibraryVersion": "sha256:" + "0" * 64,
                "master": row,
                "cutWindow": {"libraryStartMs": 10_000, "libraryEndMs": 17_000, "durationMs": 7_000},
                "pageId": PAGE_ID,
            },
            # The body was delivered mirrored: the span record is the same.
            "transforms": {"mirror": True},
        }],
    }
    store_path.write_text(json.dumps(store))


def test_exhausted_page_recycles_a_registered_sibling_library(lab, monkeypatch, registry):
    client, tmp_path, _ = lab
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _shrink_bound_master(monkeypatch)
    assert _max_quantity(client) == 1  # the bound master still has its one window
    _exhaust_bound_master(client)

    assert _max_quantity(client) >= 1, "the library's unused spans count as runway"
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("library-cut"),
    )
    assert created.status_code == 200, created.text
    job = cp._load_jobs()["jobs"][created.json()["jobId"]]
    assert job["sourceCuts"][0]["masterSha256"] == LIBRARY_SHA
    assert job["sourceLibraryId"] == LIBRARY_ID, "the bound library does not move"
    recorded = masters_from_job(job)
    assert [master.sha256 for master in recorded] == [LIBRARY_SHA]
    assert recorded[0].role == "backup", "a library master is never promoted over the bound footage"


def test_exhausted_page_recycles_a_master_it_cut_before(lab, monkeypatch):
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000)

    assert _max_quantity(client) >= 1
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("history-cut"),
    )
    assert created.status_code == 200, created.text
    cut = cp._load_jobs()["jobs"][created.json()["jobId"]]["sourceCuts"][0]
    assert cut["masterSha256"] == HISTORY_SHA
    # Disjoint from the span the earlier (mirrored) body used, 10.0-17.0 s.
    assert cut["startMs"] + cut["durationMs"] <= 10_000 or cut["startMs"] >= 17_000


def test_a_recycled_body_never_shares_one_second_with_an_earlier_body(lab, monkeypatch):
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 30_000)
    store = cp._load_jobs()
    bound = cp._dossier_source_recipe(publication())
    pooled = library_recipe(bound, store)
    assert pooled is not None
    master = next(master for master in pooled.masters if master.sha256 == HISTORY_SHA)
    served, history = cp._source_dna_cut_ledger(store, pooled, publication()["recipeVersion"])
    # 16.0-23.0 s shares exactly 1.0 s (16.0-17.0) with the earlier 10.0-17.0 s body.
    assert source_cut_overlaps_used_frames(pooled, served, master, 16_000, 7_000)
    assert source_cut_overlaps_used_frames(pooled, served, master, 4_000, 7_000)   # 10.0-11.0 s
    assert not source_cut_overlaps_used_frames(pooled, served, master, 17_000, 7_000)
    assert not source_cut_overlaps_used_frames(pooled, served, master, 3_000, 7_000)
    plan = plan_source_cuts(pooled, 10, served, history=history)
    assert plan, "unused spans remain on both sides of the earlier body"
    for cut in plan:
        assert cut.master.sha256 == HISTORY_SHA
        assert cut.start_ms + cut.duration_ms <= 10_000 or cut.start_ms >= 17_000


def test_unknown_span_history_counts_as_used(lab, monkeypatch):
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000)
    store_path = tmp_path / "jobs.json"
    store = json.loads(store_path.read_text())
    store["jobs"]["history-job"]["sourceCuts"][0]["startMs"] = None  # bounds lost
    store_path.write_text(json.dumps(store))

    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("unknown-history"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def test_a_page_with_no_library_stays_capped_and_names_new_footage(lab, monkeypatch, registry):
    client, tmp_path, _ = lab
    # Another page's footage and a retired master are not this page's library.
    _register_sibling_library(registry, "pov-dirt-bike-other-v1", "tt-other-page", OTHER_PAGE_SHA, 60_000)
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)

    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("no-library"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}
    assert stopped.headers.get("X-RT-Zero-Reason") == SOURCE_WINDOWS_EXHAUSTED
    help_text = stopped.headers.get(ZERO_REASON_HELP_HEADER)
    assert help_text == NEEDS_NEW_FOOTAGE
    assert "needs new footage" in help_text and "#227" in help_text


def test_execution_rebuilds_recycled_masters_only_from_a_whole_record():
    manifest = _manifest_row(LIBRARY_SHA, 60_000, PAGE_ID)
    row = {**manifest, "libraryId": "pov-dirt-bike-fixture-v2", "libraryVersion": "sha256:" + "2" * 64}
    rebuilt = masters_from_job({JOB_LIBRARY_MASTERS_KEY: [row]})
    assert [master.sha256 for master in rebuilt] == [LIBRARY_SHA]
    assert rebuilt[0].role == "backup"
    assert master_row(rebuilt[0]) == manifest
    # Anything short of a whole, valid record rebuilds nothing (fail closed).
    assert masters_from_job({}) == ()
    assert masters_from_job({JOB_LIBRARY_MASTERS_KEY: [manifest]}) == ()  # no manifest named
    assert masters_from_job({JOB_LIBRARY_MASTERS_KEY: [{**row, "libraryVersion": ""}]}) == ()
    assert masters_from_job({JOB_LIBRARY_MASTERS_KEY: "not-a-list"}) == ()
    assert masters_from_job({JOB_LIBRARY_MASTERS_KEY: [{**row, "durationMs": "60000"}]}) == ()
    assert masters_from_job({JOB_LIBRARY_MASTERS_KEY: [{k: v for k, v in row.items() if k != "storageKey"}]}) == ()
    assert masters_from_job({JOB_LIBRARY_MASTERS_KEY: [row, {**row, "sha256": "zz"}]}) == ()


@pytest.mark.asyncio
async def test_runner_cuts_a_library_master_from_the_job_record(lab, monkeypatch, registry):
    from tests.test_control_plane_source_execution import _pin_cuts_to_first_frame, _write_av_test_clip

    client, tmp_path, _ = lab
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("library-run"),
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["jobId"]
    assert cp._load_jobs()["jobs"][job_id]["sourceCuts"][0]["masterSha256"] == LIBRARY_SHA
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "library-master.mp4"
    _write_av_test_clip(source, duration=12.0)
    served: list[str] = []

    async def cached_source(_page_id, master, _job_id):
        served.append(master.sha256)
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert served == [LIBRARY_SHA]
    assert job["clips"][0]["source"]["master"]["sha256"] == LIBRARY_SHA
    # The receipt names the manifest the master really came from, not the bound one.
    sibling = parse_source_dna_manifest((registry / "pov-dirt-bike-fixture-v2.json").read_bytes())
    assert job["clips"][0]["source"]["sourceLibraryId"] == "pov-dirt-bike-fixture-v2"
    assert job["clips"][0]["source"]["sourceLibraryVersion"] == f"sha256:{sibling.sha256}"
    assert job["sourceLibraryId"] == LIBRARY_ID  # the binding itself did not move


@pytest.mark.asyncio
async def test_runner_refuses_a_library_cut_whose_record_is_broken(lab, monkeypatch, registry):
    client, tmp_path, _ = lab
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("library-broken"),
    )
    job_id = created.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id][JOB_LIBRARY_MASTERS_KEY][0]["durationMs"] = 0  # record no longer whole
    cp.atomic_save(cp._jobs_path(), store)

    async def never(*_):
        raise AssertionError("no master bytes are fetched for an unrebuildable cut")

    monkeypatch.setattr(cp, "_cached_source_master", never)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "source_recipe_cut_invalid"


def test_a_second_encoding_of_the_bound_source_is_not_recycled(lab, monkeypatch, registry):
    """Same original footage, new bytes: the ledger cannot compare spans, so it counts as used."""
    client, _, _ = lab
    bound = json.loads((registry / f"{LIBRARY_ID}.json").read_text())
    bound_url = next(m["provenance"]["sourceUrl"] for m in bound["masters"] if m["sha256"] == MASTER_SHA)
    row = _manifest_row(REENCODED_SHA, 60_000, PAGE_ID)
    row["provenance"]["sourceUrl"] = bound_url
    (registry / "pov-dirt-bike-fixture-v2.json").write_text(json.dumps({
        "schema": "content-lab.source-dna-library.v2",
        "libraryId": "pov-dirt-bike-fixture-v2",
        "format": "pov-dirt-bike",
        "pageId": PAGE_ID,
        "masters": [row],
    }, sort_keys=True, separators=(",", ":")))
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)

    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("same-source"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def test_a_master_whose_job_lost_its_cut_rows_counts_as_used(lab, monkeypatch):
    """Clips prove footage was posted from the master; without its cut rows no span is known."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000)
    store_path = tmp_path / "jobs.json"
    store = json.loads(store_path.read_text())
    store["jobs"]["history-job"]["sourceCuts"] = {}
    store_path.write_text(json.dumps(store))

    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("lost-rows"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def test_a_master_with_no_known_original_is_not_recycled(lab, monkeypatch):
    """A vault asset address names bytes, not footage: unknown original counts as used."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    vault_url = "https://shipstream.test/assets/vault%2Fpage%2Fmasters%2F" + HISTORY_SHA + ".mp4"
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000, source_url=vault_url)
    assert _max_quantity(client) == 0

    # The same master with its upstream origin recorded is comparable, so it recycles.
    origin = {"originSourceId": "yt-original-1", "originStartMs": 120_000, "originEndMs": 180_000}
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000, source_url=vault_url, origin=origin)
    assert _max_quantity(client) >= 1
    # A second extraction of that same origin (new bytes) is not fresh footage,
    # and the two cannot be compared with each other either: both are out.
    _record_history_master(
        tmp_path / "jobs.json", REENCODED_SHA, 60_000, job_id="history-job-2",
        source_url=vault_url.replace(HISTORY_SHA, REENCODED_SHA), origin=origin,
    )
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None
    assert _max_quantity(client) == 0


def test_footage_posted_under_another_format_blocks_its_recycled_copy(lab, monkeypatch, registry):
    """A clip cut under another recipe still proves the page posted that footage."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    sibling = json.loads((registry / "pov-dirt-bike-fixture-v2.json").read_text())
    same_footage_url = sibling["masters"][0]["provenance"]["sourceUrl"]
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000, source_url=same_footage_url)
    store = json.loads((tmp_path / "jobs.json").read_text())
    store["jobs"]["history-job"]["clips"][0]["source"]["recipeId"] = "pov-other:master"
    (tmp_path / "jobs.json").write_text(json.dumps(store))

    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None
    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("other-format"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def test_an_unreadable_clip_record_counts_its_master_as_used(lab, monkeypatch, registry):
    """A legacy clip record that no longer parses still names the master it cut."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000)
    store = json.loads((tmp_path / "jobs.json").read_text())
    del store["jobs"]["history-job"]["sourceCuts"]
    del store["jobs"]["history-job"]["clips"][0]["source"]["master"]["bytes"]
    (tmp_path / "jobs.json").write_text(json.dumps(store))

    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("unreadable-record"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def test_a_compacted_job_without_cut_rows_keeps_its_page_from_recycling(lab, monkeypatch, registry):
    """Once compacted, a rowless job leaves only its clips in the archive: no library."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000)
    store = json.loads((tmp_path / "jobs.json").read_text())
    del store["jobs"]["history-job"]["sourceCuts"]
    # Old enough to archive the history job (2026-11-01) but not today's bound cut.
    compacted, archived = compaction.compact_job_store(
        store, datetime(2026, 12, 10, tzinfo=timezone.utc),
    )
    assert [job["jobId"] for job in archived] == ["history-job"]
    (tmp_path / "jobs.json").write_text(json.dumps(compacted))

    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None
    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("compacted-rowless"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def _compact_history(tmp_path, store) -> list[str]:
    """Archive every 2026-11 job in ``store`` (not today's bound cut) and persist it."""
    compacted, archived = compaction.compact_job_store(
        store, datetime(2026, 12, 10, tzinfo=timezone.utc),
    )
    (tmp_path / "jobs.json").write_text(json.dumps(compacted))
    return sorted(job["jobId"] for job in archived)


def test_an_archived_clip_from_another_job_kind_does_not_block_the_library(lab, monkeypatch, registry):
    """Only source-job clips need cut rows; a compacted render's clip is not missing footage."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    assert _max_quantity(client) >= 1  # the sibling library is on offer
    store = json.loads((tmp_path / "jobs.json").read_text())
    store["jobs"]["render-old"] = {
        "jobId": "render-old",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "completed",
        "createdAt": "2026-11-01T11:00:00+00:00",
        "completedAt": "2026-11-01T12:00:00+00:00",
        "clips": [{"sha256": "2" * 64}],
    }
    assert _compact_history(tmp_path, store) == ["render-old"]
    archive = compaction.read_archive_index(cp._load_jobs())
    assert "2" * 64 in archive["usedClipSha256"][PAGE_ID]

    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is not None
    assert _max_quantity(client) >= 1


def test_one_archived_job_cannot_lend_its_cut_rows_to_another(lab, monkeypatch, registry):
    """Job A lost its rows (its clip is 10-17 s on the sibling), job B kept two: still a gap."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000, job_id="history-a")
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000, job_id="history-b")
    store = json.loads((tmp_path / "jobs.json").read_text())
    del store["jobs"]["history-a"]["sourceCuts"]
    job_b = store["jobs"]["history-b"]
    job_b["clips"][0]["sha256"] = "3" * 64
    job_b["sourceCuts"] = [
        {"slotId": f"{LIBRARY_SHA}:{start}:7000", "masterSha256": LIBRARY_SHA,
         "libraryStartMs": start, "startMs": start, "durationMs": 7_000}
        for start in (20_000, 30_000)
    ]
    assert _compact_history(tmp_path, store) == ["history-a", "history-b"]

    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None
    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("lent-rows"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def test_an_archive_written_before_per_clip_evidence_counts_as_unknown(lab, monkeypatch, registry):
    """An index with the page's digests but no per-clip record predates this check: no library."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    store = json.loads((tmp_path / "jobs.json").read_text())
    legacy = compaction.empty_index()
    legacy.pop("sourceDnaClips", None)
    legacy["usedClipSha256"] = {PAGE_ID: ["4" * 64]}
    store[compaction.ARCHIVE_INDEX_KEY] = legacy
    (tmp_path / "jobs.json").write_text(json.dumps(store))
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None

    # Compaction carries that history forward as unknown rather than erasing it.
    compacted, _ = compaction.compact_job_store(store, datetime(2026, 12, 10, tzinfo=timezone.utc))
    (tmp_path / "jobs.json").write_text(json.dumps(compacted))
    assert compacted[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"] == {PAGE_ID: None}
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None
    assert _max_quantity(client) == 0


def test_a_render_clip_naming_an_original_without_a_sha_only_blocks_that_original(lab, monkeypatch, registry):
    """A non-source clip with no master sha cannot taint by bytes, but its original is spoken for."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    sibling = json.loads((registry / "pov-dirt-bike-fixture-v2.json").read_text())
    sibling_url = sibling["masters"][0]["provenance"]["sourceUrl"]
    store = json.loads((tmp_path / "jobs.json").read_text())
    store["jobs"]["render-live"] = {
        "jobId": "render-live",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "completed",
        "createdAt": "2026-11-20T11:00:00+00:00",
        "completedAt": "2026-11-20T12:00:00+00:00",
        "clips": [{"sha256": "5" * 64, "source": {"master": {
            "provenance": {"sourceUrl": "https://example.test/other-footage.mp4"},
        }}}],
    }
    (tmp_path / "jobs.json").write_text(json.dumps(store))
    assert _max_quantity(client) >= 1  # another original: the sibling stays on offer

    store["jobs"]["render-live"]["clips"][0]["source"]["master"]["provenance"]["sourceUrl"] = sibling_url
    (tmp_path / "jobs.json").write_text(json.dumps(store))
    assert _max_quantity(client) == 0  # the sibling's own original was posted: out


def test_a_clip_whose_master_has_no_row_counts_as_lost_when_another_master_has_rows(lab, monkeypatch, registry):
    """One job, two masters: a row for the first never stands in for a clip on the second."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", HISTORY_SHA, 60_000)  # one row, on HISTORY_SHA
    store = json.loads((tmp_path / "jobs.json").read_text())
    first = store["jobs"]["history-job"]["clips"][0]
    second = json.loads(json.dumps(first))
    second["sha256"] = "7" * 64
    second["source"]["master"] = _manifest_row(LIBRARY_SHA, 60_000, PAGE_ID)
    store["jobs"]["history-job"]["clips"].append(second)  # cut on the sibling, no row for it
    assert _compact_history(tmp_path, store) == ["history-job"]
    archive = compaction.read_archive_index(cp._load_jobs())
    assert [(c["masterSha256"], c["cutRows"]) for c in archive["sourceDnaClips"][PAGE_ID]] == [
        (HISTORY_SHA, 1), (LIBRARY_SHA, 0),
    ]

    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None
    assert _max_quantity(client) == 0


def test_migration_rebuilds_per_clip_evidence_from_the_archive_records(lab, monkeypatch, registry):
    """An index written before the per-clip record is rebuilt from the whole archived jobs."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000)  # 10-17 s, with its row
    store = json.loads((tmp_path / "jobs.json").read_text())
    when = datetime(2026, 12, 10, tzinfo=timezone.utc)
    compacted, archived = compaction.compact_job_store(store, when)
    assert [job["jobId"] for job in archived] == ["history-job"]
    compaction.append_archive_records(tmp_path / "jobs.json", archived, when)
    del compacted[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"]  # the legacy shape
    (tmp_path / "jobs.json").write_text(json.dumps(compacted))
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None

    scan = compaction.read_archived_jobs(tmp_path / "jobs.json")
    assert [job["jobId"] for job in scan] == ["history-job"] and scan.bad_lines == 0
    migrated, _ = compaction.compact_job_store(cp._load_jobs(), when, archive_scan=scan)
    assert migrated[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"] == {PAGE_ID: [
        {"sha256": "1" * 64, "jobId": "history-job", "masterSha256": LIBRARY_SHA, "cutRows": 1},
    ]}
    (tmp_path / "jobs.json").write_text(json.dumps(migrated))
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is not None
    assert _max_quantity(client) >= 1  # the sibling's unused spans are back on offer


def test_a_page_whose_archive_record_is_missing_stays_unknown_until_rebuilt(lab, monkeypatch, registry):
    """No record for one of the page's archived clips: unknown, never empty; a rebuild lifts it."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000)
    store = json.loads((tmp_path / "jobs.json").read_text())
    when = datetime(2026, 12, 10, tzinfo=timezone.utc)
    compacted, archived = compaction.compact_job_store(store, when)
    del compacted[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"]

    # Archive files lost: migration keeps the page unknown, and remembers the attempt.
    migrated, _ = compaction.compact_job_store(compacted, when, archive_scan=compaction.ArchiveScan())
    assert migrated[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"] == {PAGE_ID: None}
    assert migrated[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClipsTried"] == {PAGE_ID: {}}
    # A record for a different job never covers this page's clip.
    other = json.loads(json.dumps(archived[0]))
    other["jobId"], other["clips"][0]["sha256"] = "other-job", "8" * 64
    assert compaction.rebuild_source_dna_clips(migrated, PAGE_ID, [other]) is None
    assert migrated[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"] == {PAGE_ID: None}
    (tmp_path / "jobs.json").write_text(json.dumps(migrated))
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None

    # The whole record found again (restored archive, or by hand): the page is lifted.
    assert compaction.rebuild_source_dna_clips(migrated, PAGE_ID, archived) == [
        {"sha256": "1" * 64, "jobId": "history-job", "masterSha256": LIBRARY_SHA, "cutRows": 1},
    ]
    assert migrated[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClipsTried"] == {}
    (tmp_path / "jobs.json").write_text(json.dumps(migrated))
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is not None


def test_another_pages_unknown_bounds_row_is_seen_by_the_capability_count(lab, monkeypatch, registry):
    """The capability count reads every page's cut rows on a library master, as job creation does."""
    client, tmp_path, _ = lab
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    assert _max_quantity(client) >= 1
    store = json.loads((tmp_path / "jobs.json").read_text())
    store["jobs"]["other-page-job"] = {
        "jobId": "other-page-job",
        "pageId": "tt-other-page",
        "sourceKind": "dossier_source_dna",
        "status": "completed",
        "createdAt": "2026-11-20T11:00:00+00:00",
        "completedAt": "2026-11-20T12:00:00+00:00",
        "sourceLibraryId": "shipstream-other",
        "sourceCuts": [{  # runs past the master's end: unknown bounds
            "slotId": f"{LIBRARY_SHA}:59000:7000", "masterSha256": LIBRARY_SHA,
            "libraryStartMs": 59_000, "startMs": 59_000, "durationMs": 7_000,
        }],
        "clips": [{"sha256": "9" * 64}],
    }
    (tmp_path / "jobs.json").write_text(json.dumps(store))

    assert _max_quantity(client) == 0
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("other-page-bounds"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": SOURCE_WINDOWS_EXHAUSTED}


def _legacy_archived_store(client, tmp_path, monkeypatch, registry, *, keep_record: bool = True):
    """A small store whose index predates the per-clip record, with the page's
    history job in a month archive file (kept or lost). Returns the archive path."""
    _shrink_bound_master(monkeypatch)
    _exhaust_bound_master(client)
    _register_sibling_library(registry, "pov-dirt-bike-fixture-v2", PAGE_ID, LIBRARY_SHA, 60_000)
    _record_history_master(tmp_path / "jobs.json", LIBRARY_SHA, 60_000)
    store = json.loads((tmp_path / "jobs.json").read_text())
    when = datetime(2026, 12, 10, tzinfo=timezone.utc)
    compacted, archived = compaction.compact_job_store(store, when)
    assert [job["jobId"] for job in archived] == ["history-job"]
    if keep_record:
        compaction.append_archive_records(tmp_path / "jobs.json", archived, when)
    else:
        compaction.append_archive_records(tmp_path / "jobs.json", [{"jobId": "unrelated"}], when)
    del compacted[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"]  # the legacy shape
    (tmp_path / "jobs.json").write_text(json.dumps(compacted))
    return compaction.archive_path_for(tmp_path / "jobs.json", when)


def test_a_cut_off_character_in_an_old_archive_skips_one_line_not_the_pass(lab):
    """Archives are written with non-ASCII intact; a truncated multi-byte character
    in an old month file is one counted bad line, and the other records still load."""
    _, tmp_path, _ = lab
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(cp._empty_jobs()))
    archive = compaction.archive_path_for(path, datetime(2026, 10, 1, tzinfo=timezone.utc))
    archive.write_bytes(
        json.dumps({"jobId": "whole", "note": "café"}, ensure_ascii=False).encode("utf-8")
        + b"\n" + b'{"jobId":"cut","note":"\xe2\x82'  # the file ended mid-character
    )
    scan = compaction.read_archived_jobs(path)
    assert [job["jobId"] for job in scan] == ["whole"]
    assert scan.bad_lines == 1
    assert list(scan.files) == [archive.name]


def test_a_small_store_rebuilds_its_legacy_record_without_waiting_to_grow(lab, monkeypatch, registry):
    """Below the size threshold and with nothing to archive, the pass still rebuilds
    the per-clip record from the archive files and writes the index."""
    client, tmp_path, _ = lab
    _legacy_archived_store(client, tmp_path, monkeypatch, registry)
    assert (tmp_path / "jobs.json").stat().st_size < compaction.DEFAULT_SIZE_THRESHOLD_BYTES
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is None

    result = cp.maybe_compact_jobs(datetime(2026, 12, 10, tzinfo=timezone.utc))
    assert result["archived"] == 0
    index = compaction.read_archive_index(cp._load_jobs())
    assert index["sourceDnaClips"] == {PAGE_ID: [
        {"sha256": "1" * 64, "jobId": "history-job", "masterSha256": LIBRARY_SHA, "cutRows": 1},
    ]}
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is not None
    assert _max_quantity(client) >= 1


def test_a_page_that_cannot_be_lifted_is_retried_only_when_an_archive_file_changes(lab, monkeypatch, registry):
    """A lost record is tried once per archive state: no re-read on the next pass,
    one re-read (and the lift) once the record appears in a changed file."""
    client, tmp_path, _ = lab
    archive = _legacy_archived_store(client, tmp_path, monkeypatch, registry, keep_record=False)
    reads: list[int] = []
    real_read = compaction.read_archived_jobs

    def counted_read(store_path):
        reads.append(1)
        return real_read(store_path)

    monkeypatch.setattr(compaction, "read_archived_jobs", counted_read)
    when = datetime(2026, 12, 10, tzinfo=timezone.utc)

    cp.run_compaction_once(tmp_path / "jobs.json", when)
    index = compaction.read_archive_index(cp._load_jobs())
    assert index["sourceDnaClips"] == {PAGE_ID: None}
    assert index["sourceDnaClipsTried"] == {PAGE_ID: compaction.archive_fingerprint(tmp_path / "jobs.json")}
    assert len(reads) == 1

    cp.run_compaction_once(tmp_path / "jobs.json", when)  # same files: no archive read
    assert len(reads) == 1
    assert compaction.read_archive_index(cp._load_jobs())["sourceDnaClips"] == {PAGE_ID: None}

    store = json.loads((tmp_path / "jobs.json").read_text())
    # The record turns up (restored by hand): the file changed, so the pass reads again and lifts.
    (tmp_path / "history-only.json").write_text(json.dumps(cp._empty_jobs()))
    _record_history_master(tmp_path / "history-only.json", LIBRARY_SHA, 60_000)
    history = json.loads((tmp_path / "history-only.json").read_text())["jobs"]
    compaction.append_archive_records(tmp_path / "jobs.json", list(history.values()), when)
    assert archive.stat().st_size > 0
    cp.run_compaction_once(tmp_path / "jobs.json", when)
    assert len(reads) == 2
    index = compaction.read_archive_index(cp._load_jobs())
    assert index["sourceDnaClips"] == {PAGE_ID: [
        {"sha256": "1" * 64, "jobId": "history-job", "masterSha256": LIBRARY_SHA, "cutRows": 1},
    ]}
    assert index["sourceDnaClipsTried"] == {}
    assert store["jobs"].keys() == cp._load_jobs()["jobs"].keys()  # the pass archived nothing


def test_a_quiet_small_store_is_not_loaded_again_to_ask_about_a_rebuild(lab, monkeypatch, registry):
    """Once a small store answers "no rebuild wanted", later passes with the same
    jobs file and archive files read nothing; a changed jobs file is asked again."""
    client, tmp_path, _ = lab
    _legacy_archived_store(client, tmp_path, monkeypatch, registry)
    when = datetime(2026, 12, 10, tzinfo=timezone.utc)
    cp.maybe_compact_jobs(when)  # rebuilds the record and saves the index
    loads: list[int] = []
    real_load = cp._load_jobs

    def counted_load():
        loads.append(1)
        return real_load()

    monkeypatch.setattr(cp, "_load_jobs", counted_load)
    cp.maybe_compact_jobs(when)
    assert len(loads) == 1  # asked once: no rebuild wanted
    cp.maybe_compact_jobs(when)
    cp.maybe_compact_jobs(when)
    assert len(loads) == 1  # quiet passes: nothing loaded
    path = tmp_path / "jobs.json"
    path.write_text(path.read_text() + "\n")  # the jobs file changed (one more byte)
    cp.maybe_compact_jobs(when)
    assert len(loads) == 2
    assert library_recipe(cp._dossier_source_recipe(publication()), cp._load_jobs()) is not None


# ── one video under two key kinds (lab-train review, head e7b20de5) ──────────
VIDEO_ID = "oKw0bu2qFVU"
OTHER_VIDEO_ID = "Zz9Zz9Zz9Zz"


def _master(sha256: str, url: str, *, origin: dict | None = None) -> MasterSource:
    return MasterSource(
        source_id="s-" + sha256[:6], sha256=sha256, bytes=1, filename=sha256[:8] + ".mp4",
        mime_type="video/mp4", storage_key=f"vault/x/masters/{sha256}.mp4",
        duration_ms=600_000, source_offset_ms=600_000,
        provenance={"sourceUrl": url, "acquiredAt": "2026-01-01T00:00:00Z", "authority": "t"},
        origin=origin,
    )


def _vault_url(sha256: str) -> str:
    return f"https://shipstream.test/assets/vault%2Fx%2Fmasters%2F{sha256}.mp4"


def _origin(video_id: str) -> dict:
    return {"originSourceId": video_id, "originStartMs": 600_000, "originEndMs": 1_200_000}


def _recipe(masters: list[MasterSource]) -> SourceRecipe:
    return SourceRecipe(
        recipe_id="pov-night-core:master", format_slug="pov-night-core", engine="sourced_video",
        max_quantity=5, engine_registry_hash="", format_contract_version="", executor_id="",
        executor_version="", source_library_id="lib", source_library_hash="h",
        masters=tuple(masters), cut_duration_ms=7_000, minimum_source_start_ms=0,
        output_width=1, output_height=1, encode_preset="", material_source="", asset_type="",
        recipe_spec={"masterPages": {"pageId": "pg"}},
    )


def _page_job(master: MasterSource, start_ms: int) -> dict:
    return {
        "pageId": "pg", "sourceKind": "dossier_source_dna", "status": "completed",
        "sourceCuts": [{
            "slotId": f"{master.sha256}:{start_ms}:7000", "masterSha256": master.sha256,
            "startMs": start_ms, "durationMs": 7_000,
        }],
        "clips": [{"sha256": "2" * 64, "source": {"recipeId": "pov-night-core:master", "master": master_row(master)}}],
    }


@pytest.mark.parametrize("bound_kind", ["youtube_url", "origin_id"])
def test_two_masters_of_one_video_match_whatever_key_kind_each_carries(monkeypatch, registry, bound_kind):
    """A YouTube-keyed master and an origin-id-keyed master of the same video are
    one video: the history master is never pooled (its spans cannot be compared
    with the bound master's), in either direction. A different video still is."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    by_url = _master("a" * 64, f"https://www.youtube.com/watch?v={VIDEO_ID}")
    by_origin = _master("b" * 64, _vault_url("b" * 64), origin=_origin(VIDEO_ID))
    other_video = _master("c" * 64, _vault_url("c" * 64), origin=_origin(OTHER_VIDEO_ID))
    assert original_source_keys(by_url) & original_source_keys(by_origin) == {("origin", VIDEO_ID)}
    bound, history = (by_url, by_origin) if bound_kind == "youtube_url" else (by_origin, by_url)
    store = {"jobs": {
        "bound-job": _page_job(bound, 20_000),      # posted video 620-627 s
        "history-job": _page_job(history, 100_000),  # the same video, other bytes
        "other-job": _page_job(other_video, 0),
    }}
    pooled = library_recipe(_recipe([bound]), store)
    assert pooled is not None
    assert [master.sha256 for master in pooled.masters] == [bound.sha256, other_video.sha256]


def test_two_masters_at_one_storage_address_are_one_video(monkeypatch, registry):
    """Different bytes recorded at the same storage key share an identity: fail closed."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    bound = _master("a" * 64, f"https://www.youtube.com/watch?v={VIDEO_ID}")
    same_address = replace(
        _master("b" * 64, f"https://www.youtube.com/watch?v={OTHER_VIDEO_ID}"), storage_key=bound.storage_key,
    )
    store = {"jobs": {"bound-job": _page_job(bound, 20_000), "history-job": _page_job(same_address, 0)}}
    assert library_recipe(_recipe([bound]), store) is None


THIRD_VIDEO_ID = "Qq8Qq8Qq8Qq"


@pytest.mark.parametrize("url", [
    f"https://music.youtube.com/watch?v={VIDEO_ID}",
    f"https://www.music.youtube.com/watch?v={VIDEO_ID}",
    f"https://m.music.youtube.com/watch?v={VIDEO_ID}&list=RD{VIDEO_ID}",
    f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
    f"https://youtube-nocookie.com/embed/{VIDEO_ID}?rel=0",
    f"https://m.youtube-nocookie.com/embed/{VIDEO_ID}",
    f"https://yewtu.be/watch?v={VIDEO_ID}",                 # mirror host, by shape
    f"https://invidious.example.net/embed/{VIDEO_ID}",      # mirror host, by shape
    f"http://www.youtube.com/watch?v={VIDEO_ID}",           # plain http
    f"https://youtu.be/{VIDEO_ID}/",                        # trailing slash
    f"https://www.youtube.com/live/{VIDEO_ID}",             # live path
])
def test_every_youtube_host_shape_meets_the_bare_video_id(monkeypatch, registry, url):
    """Music and no-cookie YouTube hosts key to the video id too, so the same
    video recorded by origin id is never pooled next to them."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    by_url = _master("a" * 64, url)
    by_origin = _master("b" * 64, _vault_url("b" * 64), origin=_origin(VIDEO_ID))
    assert ("origin", VIDEO_ID) in original_source_keys(by_url)
    store = {"jobs": {"bound-job": _page_job(by_url, 20_000), "history-job": _page_job(by_origin, 100_000)}}
    assert library_recipe(_recipe([by_url]), store) is None


UNRESOLVABLE_YOUTUBE_URLS = [
    "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",  # known host, no video id
    "https://www.youtube.com/@somechannel",                      # handle page
    "https://youtube.example-mirror.net/playlist?list=PLabcdefgh",  # unknown host, no video id
    "https://www.youtube.com/MrBeast6000",                       # legacy channel name, 11 chars, not a video
]


@pytest.mark.parametrize("url", UNRESOLVABLE_YOUTUBE_URLS)
def test_a_master_whose_only_address_is_unresolvable_fails_closed(monkeypatch, registry, url):
    """A YouTube-looking address with no video id and no origin id names nothing:
    bound, it means no library; posted, it refuses the library; as a candidate it
    is left out while a different video still pools."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    odd = _master("a" * 64, url)
    assert original_source_keys(odd) == frozenset()
    other_video = _master("c" * 64, _vault_url("c" * 64), origin=_origin(OTHER_VIDEO_ID))
    bound = _master("d" * 64, f"https://www.youtube.com/watch?v={THIRD_VIDEO_ID}")
    # Bound: no library at all.
    store = {"jobs": {"bound-job": _page_job(odd, 20_000), "other-job": _page_job(other_video, 0)}}
    assert library_recipe(_recipe([odd]), store) is None
    # Posted by this page: the library is refused, a different video notwithstanding.
    store = {"jobs": {
        "bound-job": _page_job(bound, 20_000), "odd-job": _page_job(odd, 0), "other-job": _page_job(other_video, 0),
    }}
    assert library_recipe(_recipe([bound]), store) is None
    # Candidate only (registered, never posted): left out, the other video pools.
    _register_sibling_library(registry, "odd-lib", "pg", "a" * 64, 600_000)
    odd_row = json.loads((registry / "odd-lib.json").read_text())
    odd_row["format"] = "pov-night-core"
    odd_row["masters"][0]["provenance"]["sourceUrl"] = url
    (registry / "odd-lib.json").write_text(json.dumps(odd_row, sort_keys=True, separators=(",", ":")))
    store = {"jobs": {"bound-job": _page_job(bound, 20_000), "other-job": _page_job(other_video, 0)}}
    pooled = library_recipe(_recipe([bound]), store)
    assert pooled is not None
    assert [master.sha256 for master in pooled.masters] == [bound.sha256, other_video.sha256]


@pytest.mark.parametrize("url", [
    f"https://youtu.be/{VIDEO_ID}",
    f"https://www.youtu.be/{VIDEO_ID}/",
])
def test_a_lone_path_id_is_a_video_only_on_the_short_link_host(monkeypatch, registry, url):
    """``youtu.be/<id>`` is a video; ``youtube.com/<11 chars>`` is a channel name
    and must not be mistaken for one, or the real video would never meet it."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    assert ("origin", VIDEO_ID) in original_source_keys(_master("a" * 64, url))
    for host in ("www.youtube.com", "m.youtube.com", "youtube.com"):
        assert original_source_keys(_master("a" * 64, f"https://{host}/{VIDEO_ID}")) == frozenset()
    # A mirror host keeps its plain address key, but a lone id is not a video there either.
    assert ("origin", VIDEO_ID) not in original_source_keys(_master("a" * 64, f"https://yewtu.be/{VIDEO_ID}"))


def test_a_vault_only_posted_master_without_an_origin_id_refuses_the_library(monkeypatch, registry):
    """ShipStream writes only the vault address when the upstream URL is unknown.
    Such a posted master names no footage, so a same-video candidate with other
    bytes could never be told apart from it: the page gets no library. Without
    that master the same candidates pool (positive control)."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    posted = _master("b" * 64, _vault_url("b" * 64))
    assert original_source_keys(posted) == frozenset()
    same_video = _master("c" * 64, _vault_url("c" * 64), origin=_origin(VIDEO_ID))
    other_video = _master("e" * 64, _vault_url("e" * 64), origin=_origin(OTHER_VIDEO_ID))
    bound = _master("d" * 64, f"https://www.youtube.com/watch?v={THIRD_VIDEO_ID}")
    jobs = {
        "bound-job": _page_job(bound, 20_000), "same-job": _page_job(same_video, 0),
        "other-job": _page_job(other_video, 0),
    }
    pooled = library_recipe(_recipe([bound]), {"jobs": dict(jobs)})
    assert pooled is not None
    assert [master.sha256 for master in pooled.masters] == [bound.sha256, same_video.sha256, other_video.sha256]
    jobs["posted-job"] = _page_job(posted, 0)
    assert library_recipe(_recipe([bound]), {"jobs": jobs}) is None


@pytest.mark.parametrize("url", [
    f"http://www.youtube.com/watch?v={VIDEO_ID}",
    f"https://youtu.be/{VIDEO_ID}/",
    f"https://www.youtube.com/live/{VIDEO_ID}",
    "https://www.youtube.com/@somechannel",
])
def test_posted_evidence_keeps_the_origin_id_whatever_its_address_looks_like(monkeypatch, registry, url):
    """A posted master with an odd address AND an origin id still names its video
    by the origin id, so a same-video candidate with other bytes is never pooled;
    a different video still is (positive control)."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    posted = _master("b" * 64, url, origin=_origin(VIDEO_ID))
    assert ("origin", VIDEO_ID) in original_source_keys(posted)
    same_video = _master("c" * 64, _vault_url("c" * 64), origin=_origin(VIDEO_ID))
    other_video = _master("e" * 64, _vault_url("e" * 64), origin=_origin(OTHER_VIDEO_ID))
    bound = _master("d" * 64, f"https://www.youtube.com/watch?v={THIRD_VIDEO_ID}")
    store = {"jobs": {
        "bound-job": _page_job(bound, 20_000), "posted-job": _page_job(posted, 0),
        "same-job": _page_job(same_video, 0), "other-job": _page_job(other_video, 0),
    }}
    pooled = library_recipe(_recipe([bound]), store)
    assert pooled is not None
    assert [master.sha256 for master in pooled.masters] == [bound.sha256, other_video.sha256]


@pytest.mark.parametrize("url", [
    "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing",
    "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUv",
    "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOpQrStUv",
    "https://drive.google.com/file/u/0/d/1AbCdEfGhIjKlMnOpQrStUv/view",
    "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOpQrStUv&export=download",
])
def test_a_drive_file_address_meets_its_bare_file_id(monkeypatch, registry, url):
    """A Drive file recorded by address on one master and by bare file id on
    another is one file: the second copy is never pooled."""
    monkeypatch.setenv(SHIPSTREAM_VAULT_ORIGIN_ENV, "https://shipstream.test")
    by_url = _master("a" * 64, url)
    by_id = _master("b" * 64, _vault_url("b" * 64), origin=_origin("1AbCdEfGhIjKlMnOpQrStUv"))
    assert ("origin", "1AbCdEfGhIjKlMnOpQrStUv") in original_source_keys(by_url)
    store = {"jobs": {"bound-job": _page_job(by_url, 20_000), "history-job": _page_job(by_id, 100_000)}}
    assert library_recipe(_recipe([by_url]), store) is None
