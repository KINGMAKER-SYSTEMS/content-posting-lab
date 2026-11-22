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
import json

import pytest

import routers.control_plane as cp
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

LIBRARY_SHA = "f" * 64
HISTORY_SHA = "d" * 64
OTHER_PAGE_SHA = "a" * 64
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


def _record_history_master(store_path, sha256: str, duration_ms: int, *, library_id="shipstream-old") -> None:
    """A completed job this page cut earlier from a master no longer bound."""
    store = json.loads(store_path.read_text())
    row = _manifest_row(sha256, duration_ms, PAGE_ID)
    store["jobs"]["history-job"] = {
        "jobId": "history-job",
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
    row = _manifest_row(LIBRARY_SHA, 60_000, PAGE_ID)
    rebuilt = masters_from_job({JOB_LIBRARY_MASTERS_KEY: [row]})
    assert [master.sha256 for master in rebuilt] == [LIBRARY_SHA]
    assert rebuilt[0].role == "backup"
    assert master_row(rebuilt[0]) == row
    # Anything short of a whole, valid record rebuilds nothing (fail closed).
    assert masters_from_job({}) == ()
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
    assert job["clips"][0]["source"]["sourceLibraryId"] == LIBRARY_ID


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
