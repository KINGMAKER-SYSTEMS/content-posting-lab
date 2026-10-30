"""Shared isolation for source-dna registry scans in dirt-bike/BTL tests.

Retiring the real chase/BTL libraries deletes their recipes, so the Dossier and
executor tests that used those files as generic fixtures now run against an
isolated registry: a copy of the real recipes plus a non-retired dirt-bike
fixture (tests/fixtures/source-dna/). This keeps production files read-only and
gives every test the same curated set without a fake page leaking into the real
registry.
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "source-dna"


def isolate_source_dna_registry(monkeypatch, tmp_path) -> Path:
    import services.dossier_ingredients as ingredients
    import services.source_dna_registry as registry

    isolated = tmp_path / "source-dna"
    isolated.mkdir()
    for path in sorted(Path(registry.MANIFEST_DIR).glob("*.json")):
        (isolated / path.name).write_bytes(path.read_bytes())
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        (isolated / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(registry, "MANIFEST_DIR", isolated)
    monkeypatch.setattr(ingredients, "SOURCE_DNA_MANIFEST_DIR", isolated)
    return isolated
