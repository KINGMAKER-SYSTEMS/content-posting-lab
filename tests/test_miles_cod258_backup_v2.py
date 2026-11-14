"""miles.of.memories77 gets its approved Cod_ggrs69U backup in a v2 library (11-13).

Miles' primary (2790d9fd, 600 s) is fully cut. Eric's 10-22 standing backup
assignment names Cod_ggrs69U ("Miami 4K - Night Drive") for Miles once the
primary has no fresh window. The page's only library so far is the ShipStream
projection shipstream-miles-of-memories77-ccf287410666dfef, which can hold one
master and no backups, so the backup lives in a new registered library:
pov-night-core-miles-of-memories77-v2. The projection is not edited; v2 carries
its primary row byte-for-byte plus the one banked backup (258441bc, bank share
3490-3974 s). The other three Miles bank masters have no verified bytes and are
not registered.

Committing the backup under the projection's own id would move its version
(a role-keyed registry file hashes its canonical primaries plus a newline, a
projection hashes compact bytes with none), so v2 is a new id, as BTL's v2 was
(#277).
"""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from services.control_plane_sources import CutUse, plan_source_cuts
from services.dossier_ingredients import _master_source_options, _pooled_library_ids
from services.shipstream_source_manifest import _master_from_page_master
from services.source_dna_registry import MANIFEST_DIR, parse_source_dna_manifest
from tests.source_dna_fixture import isolate_source_dna_registry
from tests.test_control_plane_source_execution import offline_shipstream_manifest  # noqa: F401 (fixture)
from tests.test_source_recut_variety import _recipe


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    isolate_source_dna_registry(monkeypatch, tmp_path)


DIR = Path(__file__).resolve().parents[1] / "recipes/source-dna"
V2 = "pov-night-core-miles-of-memories77-v2"
PROJECTION_ID = "shipstream-miles-of-memories77-ccf287410666dfef"
MILES_PAGE = "acct:rail:6880a7944b9f074c700d6218"
PRIMARY = "2790d9fde3ca39eef07bd20ae43bcae15ef84ffb3436ac79727d39a04e581760"
BACKUP = "258441bcbcb36448f65d356b9bc3daf4d36347c0631063e944c96aee8969d0e7"
UNVERIFIED_BANK_MASTERS = {
    # Bank 7e160cb2 Miles share 2047-3490: listed in the bank, bytes not verified.
    "61729396",
    "7fe1c869",
    "c1ea2606",
}

# ShipStream source-manifest.v1 ``master`` for Miles, registered 10-01.
# Its projection is exactly the live library id above (asserted below).
SHIPSTREAM_PAGE_MASTER = {
    "sha256": PRIMARY,
    "storageKey": f"vault/miles.of.memories77/masters/{PRIMARY}.mp4",
    "bytes": 179879310,
    "media": {
        "width": 1080, "height": 1920, "fps": 30, "durationSeconds": 600,
        "videoCodec": "h264", "pixelFormat": "yuv420p", "audioStreams": 0,
    },
    "originSourceId": "content-lab:cpl-5b253fe9d8bea08a",
    "originSourceUrl": (
        "https://shipstream.risingtidesviral.com/assets/vault/miles.of.memories77/"
        f"intake/{PRIMARY}.mp4"
    ),
    "originWindowSeconds": [0, 600],
    "registeredAt": "2026-10-01T14:46:27.052Z",
}

# The whole committed file, hashed as stored. Separate from the primary-only
# library version, which leaves the backup out by design.
FULL_MANIFEST_SHA256 = "ee436809901c515e1505be42e4dc46d9c7a2acedb33fd4f7b75cd22bb7aef8e4"


def _load():
    raw = (DIR / f"{V2}.json").read_bytes()
    return raw, parse_source_dna_manifest(raw, V2)


def _projection_row():
    return _master_from_page_master(SHIPSTREAM_PAGE_MASTER, "miles.of.memories77")


def test_projection_row_is_the_live_miles_library():
    row = _projection_row()
    identity = hashlib.sha256(json.dumps(
        {"pageId": MILES_PAGE, "format": "pov-night-core", "masters": [row]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    assert f"shipstream-miles-of-memories77-{identity[:16]}" == PROJECTION_ID


def test_v2_is_canonical_and_keeps_the_primary_byte_for_byte():
    raw, v2 = _load()
    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    assert raw == canonical.encode()
    assert (v2.page_id, v2.format_slug) == (MILES_PAGE, "pov-night-core")
    rows = json.loads(raw)["masters"]
    primaries = [row for row in rows if row.get("role", "primary") == "primary"]
    assert primaries == [_projection_row()]
    assert "role" not in primaries[0]


def test_v2_adds_exactly_the_approved_backup_with_its_actual_media():
    raw, v2 = _load()
    backups = [m for m in v2.masters if m.role == "backup"]
    assert [m.sha256 for m in backups] == [BACKUP]
    backup = backups[0]
    # 14519 frames at 30 fps: 483.966667 s; first frame at local PTS 0.033008 s.
    assert (backup.bytes, backup.duration_ms, backup.source_offset_ms) == (171_038_098, 483_967, 3_490_000)
    assert backup.origin == {
        "originSourceId": "Cod_ggrs69U", "originStartMs": 3_490_000, "originEndMs": 3_973_967,
    }
    assert backup.grade_override is None
    assert backup.provenance["sourceUrl"] == "https://www.youtube.com/watch?v=Cod_ggrs69U"
    assert "0.033008" in backup.provenance["authority"]
    assert "7e160cb2" in backup.provenance["authority"]
    # Outside the vault's masters/ prefix, so the Worker's newest-vault-master
    # follower can never import it as the page's primary.
    assert backup.storage_key == f"source-dna/pov-night-core/miles-of-memories77/{BACKUP}.mp4"
    assert not backup.storage_key.startswith("vault/")
    assert not any(m.sha256[:8] in UNVERIFIED_BANK_MASTERS for m in v2.masters)


def test_full_manifest_hash_is_recorded_apart_from_the_primary_version():
    raw, v2 = _load()
    full = hashlib.sha256(raw).hexdigest()
    assert full == FULL_MANIFEST_SHA256
    assert v2.sha256 != full, "the library version must leave the backup out"
    primary_only = json.loads(raw)
    primary_only["masters"] = [r for r in primary_only["masters"] if r.get("role", "primary") == "primary"]
    canonical = json.dumps(primary_only, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    assert v2.sha256 == hashlib.sha256(canonical.encode()).hexdigest()


def test_dossier_binds_v2_as_the_miles_pool():
    assert _pooled_library_ids("pov-night-core", MILES_PAGE) == {V2}
    options = _master_source_options("pov-night-core", MILES_PAGE)
    v2_option = next(option for option in options if option["libraryId"] == V2)
    assert [m["sha256"] for m in v2_option["masters"]] == [PRIMARY]
    assert [m["sha256"] for m in v2_option["backupMasters"]] == [BACKUP]


def test_backup_is_registered_once_and_only_for_miles():
    owners = []
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        if BACKUP in {m.sha256 for m in library.masters}:
            owners.append(library.library_id)
    assert owners == [V2]


def test_miles_plans_cuts_only_from_its_two_registered_masters():
    _, v2 = _load()
    recipe, _ = _recipe()
    recipe = replace(recipe, masters=v2.masters)
    served, history, cuts = set(), {}, []
    for run in range(20):
        for order, cut in enumerate(plan_source_cuts(recipe, 1, served, seed=f"miles-{run}", history=history)):
            served.add(cut.slot_id)
            history[cut.slot_id] = CutUse(float(run), order)
            cuts.append(cut)
    assert cuts
    assert {cut.master.sha256 for cut in cuts} <= {PRIMARY, BACKUP}
