"""F-SCRAMBLER-PILOT-409-1031: preflight origins coverage + 409 logging.

The 15 h 409 loop (``scrambler_unavailable: origin_mapping_unknown:<sha12>``)
was invisible: the startup preflight logged ``ready`` while the enqueue refused
every job. Regressions, red on main and green on head:

1. a listed page whose current master is missing from origins.json (or whose
   upstream window is lost) is logged by name and NOT reported ready — proven
   through the REAL resolution seam, not a stub (MED-1);
2. a listed page that cannot be resolved to masters at all fails CLOSED with
   ``master_resolution_failed:<short>`` instead of reading ready (MED-1);
3. a corrupt/unparseable origins map fails CLOSED instead of reading ready
   (LOW-1); a ShipStream vault/network failure fails soft, NOT READY (LOW-2);
4. every POST /api/control-plane/v1/jobs 409 logs page id + detail.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from services import scrambler_preflight as pf
from tests.test_control_plane_source_execution import (  # noqa: F401
    _isolated_source_dna,  # autouse: main #279 isolated source-DNA registry
    MASTER_SHA,
    PAGE_ID,
    headers,
    job_body,
    lab,
)
from tests.test_scrambler_executor_behavior import (  # noqa: F401
    _originality_feed,
    _scrambler_on,
    isolated_scrambler_state,
)


def _preflight_env(tmp_path: Path, *, pages: str = "acct:trailheadtravis") -> dict:
    feed = tmp_path / "originality"
    feed.mkdir(parents=True)
    (feed / "usage.jsonl").write_text("")
    (feed / "origins.json").write_text("{}")
    return {
        "SCRAMBLER_ENABLED": "1",
        "SCRAMBLER_PAGES": pages,
        "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT": "1",
        "CONTENT_LAB_ORIGINALITY_USAGE_PATH": str(feed / "usage.jsonl"),
        "SCRAMBLER_FEED_R2_BUCKET": "rt-content-buckets",
        "SCRAMBLER_FEED_R2_PREFIX": "originality/feed/",
        "RAILWAY_VOLUME_MOUNT_PATH": str(tmp_path),
        "SCRAMBLER_HISTORY_PATH": str(tmp_path / "scrambler-history.json"),
    }


def test_preflight_names_page_whose_master_is_missing_from_origins(tmp_path, monkeypatch, caplog):
    env = _preflight_env(tmp_path)
    # The planner's master resolution, stubbed: this page plans on a master
    # whose sha is absent from the (empty) origins.json map.
    monkeypatch.setattr(pf, "_resolve_page_master_shas", lambda page: (["e" * 64], None),
                        raising=False)
    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    problems = pf.log_startup_preflight(env=env)
    assert "page=acct:trailheadtravis reason=origin_mapping_unknown:eeeeeeeeeeee" in problems, problems
    assert ("scrambler preflight: NOT READY page=acct:trailheadtravis "
            "reason=origin_mapping_unknown:eeeeeeeeeeee") in caplog.text
    assert "scrambler preflight: ready" not in caplog.text


def test_preflight_ready_when_master_has_usable_origin_window(tmp_path, monkeypatch, caplog):
    env = _preflight_env(tmp_path)
    feed = Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).parent
    (feed / "origins.json").write_text(json.dumps({
        "e" * 64: {"origin_kind": "yt", "origin_id": "TestVideo-1",
                   "origin_start_s": 120.0, "origin_end_s": 301.0},
    }))
    monkeypatch.setattr(pf, "_resolve_page_master_shas", lambda page: (["e" * 64], None),
                        raising=False)
    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    assert pf.log_startup_preflight(env=env) == []
    assert "scrambler preflight: ready" in caplog.text


def test_real_resolution_names_page_whose_master_is_missing_from_origins(lab, caplog):
    """MED-1: drive the REAL master-resolution seam (no stub).

    ``lab`` registers a real sourced_video recipe whose library holds MASTER_SHA
    and binds the roster intent for PAGE_ID. With an empty origins.json the real
    ``_resolve_page_master_shas`` must still resolve the master, and preflight
    must name the page NOT READY (origin_mapping_unknown) — not silently [].
    """
    client, tmp_path, _ = lab
    env = _preflight_env(tmp_path, pages=PAGE_ID)

    # The real seam resolves to MASTER_SHA even though origins.json is empty.
    shas, failure = pf._resolve_page_master_shas(PAGE_ID)
    assert failure is None, failure
    assert shas == [MASTER_SHA], shas

    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    problems = pf.log_startup_preflight(env=env)
    expected = f"page={PAGE_ID} reason=origin_mapping_unknown:{MASTER_SHA[:12]}"
    assert expected in problems, problems
    assert f"scrambler preflight: NOT READY {expected}" in caplog.text
    assert "scrambler preflight: ready" not in caplog.text


def test_preflight_reports_unparseable_origins_map_not_ready(tmp_path, caplog):
    """LOW-1: a corrupt (unparseable) origins map must not read ready.

    preflight_problems(full=False) only notices a MISSING map; an unparseable
    one is caught here. Without a usable map no master's origin can be
    verified, so every listed page is named NOT READY (fail closed).
    """
    env = _preflight_env(tmp_path)
    feed = Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).parent
    (feed / "origins.json").write_text("{not valid json")
    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    problems = pf.log_startup_preflight(env=env)
    expected = ("page=acct:trailheadtravis "
                "reason=origins_map_unavailable:origins_map_invalid")
    assert expected in problems, problems
    assert f"scrambler preflight: NOT READY {expected}" in caplog.text
    assert "scrambler preflight: ready" not in caplog.text


def test_vault_network_failure_reports_not_ready(lab, monkeypatch, caplog):
    """LOW-2: a vault/manifest fetch failure at boot must fail soft and CLOSED.

    A page whose recipe cannot be resolved because the ShipStream vault is
    unreachable must be logged NOT READY (master_resolution_failed), never
    crash, never log ready.
    """
    import services.control_plane_sources as sources
    import services.shipstream_source_manifest as source_manifest

    client, tmp_path, _ = lab
    env = _preflight_env(tmp_path, pages=PAGE_ID)

    def vault_offline(_publication):
        raise source_manifest.ShipStreamSourceUnavailable(
            "ShipStream vault is offline (network)")

    monkeypatch.setattr(sources, "resolve_source_recipe", vault_offline)

    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    problems = pf.log_startup_preflight(env=env)
    expected = ("page=" + PAGE_ID + " reason=master_resolution_failed:"
                "resolve_exception:ShipStreamSourceUnavailable")
    assert expected in problems, problems
    assert f"scrambler preflight: NOT READY {expected}" in caplog.text
    assert "scrambler preflight: ready" not in caplog.text


@pytest.mark.asyncio
async def test_job_409_logs_page_and_detail(lab, monkeypatch, tmp_path, caplog):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch, pages=PAGE_ID)
    feed = _originality_feed(monkeypatch, tmp_path)
    # The recipe plans on MASTER_SHA; empty the origins map so the planner
    # refuses every job with origin_mapping_unknown.
    (feed / "origins.json").write_text("{}")
    caplog.set_level(logging.INFO, logger="control_plane")
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("preflight-origin-409"))
    assert response.status_code == 409, response.json()
    assert response.json()["detail"] == (
        f"scrambler_unavailable: origin_mapping_unknown:{MASTER_SHA[:12]}")
    assert (f"POST /api/control-plane/v1/jobs 409 page={PAGE_ID} "
            f"detail=scrambler_unavailable: origin_mapping_unknown:{MASTER_SHA[:12]}"
            ) in caplog.text
