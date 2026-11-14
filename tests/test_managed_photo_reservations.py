"""Exercise the direct photo reservation through the mounted Content Lab API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import pytest

from fastapi.testclient import TestClient

import routers.control_plane as cp
from services.control_plane_image_video import image_video_variant_plans
import tests.test_control_plane_slideshow_execution as slideshow
from tests.test_control_plane_image_video_execution import image_lab  # noqa: F401


RESERVE = "/api/control-plane/v1/photo-plan-reservations"
JOBS = "/api/control-plane/v1/jobs"
CAPABILITIES = "/api/control-plane/v1/capabilities"


def _post(client, path, publication, key, **changes):
    body = {**slideshow.job_body(1, publication), **changes}
    return client.post(path, json=body, headers=slideshow.headers(key))


def _capacity(client, publication):
    response = client.get(CAPABILITIES, headers=slideshow.headers())
    assert response.status_code == 200, response.text
    return next(
        (entry["maxQuantity"] for entry in response.json()["capabilities"]
         if entry["recipeVersion"] == publication["recipeVersion"]),
        0,
    )


def test_reserve_and_replay_without_starting_a_lab_job(image_lab, monkeypatch):
    client, _, publication, approved = image_lab
    started = []
    monkeypatch.setattr(cp, "_start_syzygy_slideshow", started.append)
    response = _post(client, RESERVE, publication, "managed-photo-reserve-first")
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt == {
        "schema": "content-lab.photo-plan-reservation.v1",
        "reservationId": receipt["reservationId"],
        "pageId": publication["pageId"],
        "recipeId": publication["recipeId"],
        "recipeVersion": publication["recipeVersion"],
        "recipeSpecHash": publication["recipeSpecHash"],
        "masterPagesHash": json.loads(publication["recipeSpecCanonical"])["masterPagesHash"],
        "sourceLibraryId": approved["libraryId"],
        "librarySnapshotHash": "c" * 64,
        "photoPlan": approved,
    }
    assert response.headers["Cache-Control"] == "private, no-store"
    assert not cp._load_jobs()["jobs"]
    assert started == []
    stored = cp._jobs_path().read_bytes()

    def unavailable(*args):
        raise AssertionError("an idempotent replay cannot resolve the library again")

    with monkeypatch.context() as patch:
        patch.setattr(cp, "_jobs_snapshot", None)
        patch.setattr(cp, "load_recipe_library", unavailable)
        replay = _post(client, RESERVE, publication, "managed-photo-reserve-first")
        assert replay.status_code == 200, replay.text
        assert replay.json() == receipt
        mismatch = _post(client, RESERVE, publication, "managed-photo-reserve-first",
                         policyHash="sha256:different")
        assert mismatch.status_code == 409
        assert cp._jobs_path().read_bytes() == stored

    ordinary = _post(client, JOBS, publication, "managed-photo-normal-after")
    assert ordinary.status_code == 200, ordinary.text
    job = cp._load_jobs()["jobs"][ordinary.json()["jobId"]]
    assert job["slideshowPlan"][0]["signature"] != approved["signature"]
    assert started == [job["jobId"]]


def test_reserved_plans_reduce_legacy_capacity_until_exhausted(image_lab):
    client, library, publication, approved = image_lab
    plans = image_video_variant_plans(approved["pageId"], library)
    assert len(plans) > 1
    assert _capacity(client, publication) == 1
    for index, expected in enumerate(plans):
        response = _post(client, RESERVE, publication, f"managed-photo-capacity-{index}")
        assert response.status_code == 200, response.text
        assert response.json()["photoPlan"] == expected
    assert _capacity(client, publication) == 0
    assert _post(client, JOBS, publication, "managed-photo-exhausted-lab").status_code == 409
    assert _post(client, RESERVE, publication, "managed-photo-exhausted-direct").status_code == 409


def test_reserved_photos_share_live_and_compacted_history(image_lab):
    client, _, publication, approved = image_lab
    legacy = _post(client, JOBS, publication, "managed-photo-normal-before")
    assert legacy.status_code == 200, legacy.text
    job_id = legacy.json()["jobId"]
    job = cp._load_jobs()["jobs"][job_id]
    assert job["slideshowPlan"][0]["signature"] == approved["signature"]
    live = _post(client, RESERVE, publication, "managed-photo-live")
    assert live.status_code == 200, live.text
    held = live.json()
    assert held["photoPlan"]["signature"] != approved["signature"]

    now = datetime.now(timezone.utc)
    cp._update_job(job_id, status="completed", completedAt=(now - timedelta(days=45)).isoformat())
    assert cp.run_compaction_once(cp._jobs_path(), now)["archived"] == 1
    assert job_id not in cp._load_jobs()["jobs"]
    archived = _post(client, RESERVE, publication, "managed-photo-archived")
    assert archived.status_code == 200, archived.text
    assert archived.json()["photoPlan"]["signature"] not in {
        approved["signature"], held["photoPlan"]["signature"],
    }
    assert _post(client, RESERVE, publication, "managed-photo-live").json() == held


def test_concurrent_reservations_and_a_legacy_job_use_different_plans(image_lab):
    client, _, publication, _ = image_lab
    requests = [
        (RESERVE, "managed-photo-same-concurrent"),
        (RESERVE, "managed-photo-same-concurrent"),
        (JOBS, "managed-photo-legacy-concurrent"),
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda args: _post(client, args[0], publication, args[1]), requests))
    assert [response.status_code for response in results] == [200, 200, 200]
    left, right, legacy = [response.json() for response in results]
    assert left == right
    job = cp._load_jobs()["jobs"][legacy["jobId"]]
    assert job["slideshowPlan"][0]["signature"] != left["photoPlan"]["signature"]


def test_bad_scope_and_unreadable_history_refuse_without_a_write(image_lab):
    client, _, publication, _ = image_lab
    body = slideshow.job_body(1, publication)
    headers = slideshow.headers("managed-photo-refusal")
    for modified in (
        {**headers, "Authorization": "Bearer invalid"},
        {**headers, "X-RT-Page-Id": "acct:foreign"},
        {**headers, "X-RT-Lane": "another-lane"},
    ):
        response = client.post(RESERVE, json=body, headers=modified)
        assert response.status_code in (400, 401, 403)
    assert _post(client, RESERVE, publication, "managed-photo-refusal", quantity=True).status_code == 400
    assert _post(client, RESERVE, publication, "managed-photo-refusal",
                 lockedRecipeId="coffee-slideshow:master").status_code == 409
    assert not cp._jobs_path().exists()

    cp._jobs_path().parent.mkdir(parents=True, exist_ok=True)
    broken = b'{"jobs":'
    cp._jobs_path().write_bytes(broken)
    response = _post(client, RESERVE, publication, "managed-photo-refusal")
    assert response.status_code == 503, response.text
    assert cp._jobs_path().read_bytes() == broken


def test_missing_archive_index_never_reopens_a_photo_plan(image_lab):
    client, _, publication, _ = image_lab
    path = cp._jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    archive = path.with_name(f"{path.name}.archive-202610.jsonl")
    archive.write_text('{"old":"reservation"}\n')
    response = _post(client, RESERVE, publication, "managed-photo-missing-index")
    assert response.status_code == 503, response.text
    assert not path.exists()


@pytest.mark.parametrize("corruption", (
    "invalid_signature", "other_scope_invalid_signature", "missing_scope", "empty_scope", "non_dict",
))
def test_malformed_archive_entry_never_reopens_a_photo_plan(image_lab, corruption):
    client, _, publication, approved = image_lab
    archive = cp.compaction.empty_index()
    entry = {
        "pageId": publication["pageId"],
        "sourceLibraryId": approved["libraryId"],
        "signature": "invalid",
    }
    if corruption == "missing_scope":
        entry.pop("sourceLibraryId")
    elif corruption == "empty_scope":
        entry["sourceLibraryId"] = ""
    elif corruption == "other_scope_invalid_signature":
        entry["pageId"] = "acct:other"
    elif corruption == "non_dict":
        entry = "invalid"
    archive["slideshow"] = [entry]
    path = cp._jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**cp._empty_jobs(), cp.compaction.ARCHIVE_INDEX_KEY: archive}))
    broken = path.read_bytes()

    for index, route in enumerate((RESERVE, JOBS)):
        response = _post(client, route, publication, f"managed-photo-{corruption}-{index}")
        assert response.status_code == 503, response.text
    response = client.get(CAPABILITIES, headers=slideshow.headers())
    assert response.status_code == 503, response.text
    assert path.read_bytes() == broken


def test_corrupt_receipt_does_not_release_a_photo_plan(image_lab):
    client, _, publication, _ = image_lab
    reserved = _post(client, RESERVE, publication, "managed-photo-before-corruption")
    assert reserved.status_code == 200, reserved.text
    store = cp._load_jobs()
    record = next(iter(store["managedPhotoReservations"].values()))
    record["receipt"]["photoPlan"]["signature"] = "f" * 64
    cp._jobs_path().write_text(json.dumps(store))
    broken = cp._jobs_path().read_bytes()
    for path in (RESERVE, JOBS):
        response = _post(client, path, publication, "managed-photo-after-corruption")
        assert response.status_code == 503, response.text
        assert cp._jobs_path().read_bytes() == broken


def test_hosted_app_routes_reservation_through_both_auth_boundaries(image_lab, monkeypatch):
    import app as app_module

    _, _, publication, _ = image_lab
    monkeypatch.setattr(app_module, "_APP_API_KEY", "fixture-app-key")
    client = TestClient(app_module.app)
    headers = slideshow.headers("managed-photo-full-app")
    body = slideshow.job_body(1, publication)
    assert client.post(RESERVE, json=body, headers=headers).status_code == 401
    assert client.post(RESERVE, json=body, headers={
        **headers, "Authorization": "Bearer fixture-app-key",
    }).status_code == 401
    response = client.post(RESERVE, json=body, headers={
        **headers, "X-API-Key": "fixture-app-key",
    })
    assert response.status_code == 200, response.text
    assert response.json()["recipeSpecHash"] == publication["recipeSpecHash"]
