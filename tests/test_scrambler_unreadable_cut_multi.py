"""Tides check of ported hunk 4 on the shipping head: an UNREADABLE duration on
one cut of a multi-cut Scrambler job refuses only that cut (the gap the port
report names), and never touches the Layer B warn-and-deliver counter."""
from __future__ import annotations

from pathlib import Path

import pytest

import routers.control_plane as cp
from services.scrambler import RELEASED, RENDERED
from tests.test_control_plane_source_execution import _isolated_source_dna, lab  # noqa: F401
from tests.test_scrambler_executor_behavior import (  # noqa: F401
    _job_scrambler_records, _wire_executor, isolated_scrambler_state,
)
from tests.test_scrambler_pilot_fixes import _enqueue, _fresh_index_cache, _states  # noqa: F401


@pytest.mark.asyncio
async def test_unreadable_duration_refuses_only_that_scrambler_cut(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, monkeypatch, tmp_path, 3, "tides-hunk4-unreadable")
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    before = cp._layer_b_probe_unreadable_count

    def probe(path):
        stem = Path(path).stem
        if stem.endswith("0002"):
            return None
        index = int(stem[-4:])
        return _job_scrambler_records(job_id, index)["lengthMs"] / 1000.0

    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", probe)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert [clip["scrambler"]["cutIndex"] for clip in job["clips"]] == [0, 1]
    assert _states(job_id) == [RENDERED, RENDERED, RELEASED]
    assert not Path(lane["renders"][2]["dst"]).exists()
    assert cp._layer_b_probe_unreadable_count == before
