"""Deploy/shipping contracts for the Scrambler fix round (MED-2/MED-3)."""
from __future__ import annotations

import json
from pathlib import Path

from services import json_store

REPO = Path(__file__).resolve().parents[1]


def test_json_store_annotations_are_lazy_strings():
    """Python 3.9 cannot evaluate ``Path | str`` at def time; the future import
    stores annotations as strings so the module imports under 3.9 too."""
    assert json_store.lock_for.__annotations__["path"] == "Path | str"


def test_dockerfile_ships_tools_directory():
    """The daily report CLI (tools/scrambler_report.py) and the feed sync must
    ship in the production image, not only in the checkout."""
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "COPY tools/ ./tools/" in dockerfile


def test_retired_pov_dirt_bike_is_network_wide():
    """The pov-dirt-bike retirement covers BOTH bound masters network-wide.

    The 10-29 01:18 fingerprint finding (LEAD-STATE-originality.md) bound
    chase.miles.4l c434bf96 == soul.revival83 9ff3fe74 (103 s shared run), so
    9ff3fe74 is retired network-wide, not chase-only as the brief once said.
    """
    retired = json.loads((REPO / "recipes/originality/retired-origins.json").read_text())
    entry = next(e for e in retired if e.get("origin_id") == "pov-dirt-bike")
    shas = set(entry["master_sha256s"])
    assert "c434bf9678fbaa20b9b081c68260cca75eb3dd109ddc1cb82df556ec59ae5bd5" in shas
    assert "9ff3fe749cdcf268967bc922af66857a3bace3fd38eaab4180315b77bd6be15c" in shas
    assert all(len(s) == 64 and all(c in "0123456789abcdef" for c in s) for s in shas)
