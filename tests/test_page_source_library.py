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

    records = compaction.read_archived_jobs(tmp_path / "jobs.json")
    assert [job["jobId"] for job in records] == ["history-job"]
    migrated, _ = compaction.compact_job_store(cp._load_jobs(), when, archived_jobs=records)
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

    # Archive files lost: migration keeps the page unknown.
    migrated, _ = compaction.compact_job_store(compacted, when, archived_jobs=[])
    assert migrated[compaction.ARCHIVE_INDEX_KEY]["sourceDnaClips"] == {PAGE_ID: None}
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
