"""Contract guard: the Lab's dossier-ingredients catalog must pass the live Worker validator.

HIGH-1 (Lab PR #340): for every listed pool-supplied page the catalog emitted a
lyric-video ``productionSelection`` with ``controls: {}``. The live Worker's
``validateDossierProductionSelection`` (contentLabClient.js) requires a timed
selection's controls to be exactly ``{lyricTimingSetId: <bounded string>}``, so
the whole catalog was rejected (CONTENT_LAB_SCHEMA_MISMATCH).

This test builds the real Lab catalog for EVERY listed page plus one unlisted
page, feeds it through the vendored Worker validator (unmodified, run in Node),
and asserts ACCEPTED. It fails on 12018e2d for every listed page (e.g.
chemically-lit and wildflower993) and passes on the fixed head.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from services.dossier_ingredients import build_dossier_ingredient_catalog
from services.pool_supplied_pages import load_pool_supplied_page_ids
from tests.master_pages_fixtures import master_pages

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "fixtures/worker-live-a963a12d/run_dossier_ingredients_contract.mjs"
VENDORED = HERE / "fixtures/worker-live-a963a12d/src/adapters/contentLabClient.js"

# sha256 of control-plane-worker/src/adapters/contentLabClient.js at the live
# twin a963a12d (worker-src-live-a963a12d.tgz). Refresh when the Worker validator
# changes (see fixtures/worker-live-a963a12d/README.md).
WORKER_VALIDATOR_SHA256 = "fbc02ac3e42840e9c755c7fb0b5f73babdc4177fe8aaac583cc868731a2462dd"

LISTED_PAGES = sorted(load_pool_supplied_page_ids())
UNLISTED_PAGE = "acct:unlisted-truck"

# Real niche/engine for the two pages the class-c review proved, plus a lyric
# default for the rest (the bug is niche-independent: the lyric-video format
# carries the pool-supplied timing for every listed page).
_NICHE_BY_PAGE = {
    "acct:chemically-lit": ("lyric-edits", "lyrics_slideshows"),
    "acct:wildflower993": ("ugc-slideshow", "sourced_slideshow"),
}


def _intent(page_id: str):
    content_niche, content_engine = _NICHE_BY_PAGE.get(
        page_id, ("lyric-edits", "lyrics_slideshows"),
    )
    return master_pages(
        page_id,
        handle="pool.test",
        content_niche=content_niche,
        content_engine=content_engine,
    )


def _node_available() -> bool:
    return shutil.which("node") is not None


def test_vendored_worker_validator_matches_live_sha():
    assert hashlib.sha256(VENDORED.read_bytes()).hexdigest() == WORKER_VALIDATOR_SHA256


@pytest.mark.skipif(not _node_available(), reason="node is not installed on this host")
def test_worker_accepts_catalog_for_every_listed_and_one_unlisted_page(tmp_path):
    # 12 original pages + 3 replacement Warner pages + Goldford sunsetsoul46 + Christmas tis.the.szn7 (11-03).
    assert len(LISTED_PAGES) == 17, LISTED_PAGES
    for page_id in [*LISTED_PAGES, UNLISTED_PAGE]:
        intent, revision = _intent(page_id)
        catalog = build_dossier_ingredient_catalog(page_id, intent, revision)
        path = tmp_path / f"{page_id.replace(':', '_')}.json"
        path.write_text(
            json.dumps(catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["node", str(RUNNER), str(path)], capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"{page_id} REJECTED\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert f"ACCEPTED {page_id}" in result.stdout
