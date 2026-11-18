"""Lab-side red-first test for the paired BOOKED add (drop into tests/ beside test_finished_clips.py;
#164 shape = test_custody_scope + test_unbooked_sha_refused_at_route). Red on the parent (every new sha
refused), green once routers/finished_clips.py carries the add. The spec file is LAB-WORKER-SET-SPEC.json
from lanes/fanout-1117/booked-add (copy it next to this test or point BOOKED_ADD_SPEC at it)."""
import hashlib
import json
import os

import pytest

from routers import finished_clips

SPEC = json.load(open(os.environ.get("BOOKED_ADD_SPEC", os.path.join(os.path.dirname(__file__), "LAB-WORKER-SET-SPEC.json"))))
CUL, BUR = "acct:culture-core-1", "acct:burnt-mill-rd"
NEW = {CUL: [s for s in SPEC["sets"][CUL]], BUR: [s for s in SPEC["sets"][BUR]]}


def test_booked_equals_paired_spec_exactly():
    # L52706 rule: Lab and Worker BOOKED identical per page, additions only
    assert set(finished_clips.BOOKED) == {CUL, BUR}
    for page in (CUL, BUR):
        assert sorted(finished_clips.BOOKED[page]) == SPEC["sets"][page]
        assert len(finished_clips.BOOKED[page]) == SPEC["counts"][page]


@pytest.mark.parametrize("page", [CUL, BUR])
def test_every_spec_member_is_booked_on_its_page_only(page):
    other = BUR if page == CUL else CUL
    for sha in SPEC["sets"][page]:
        assert finished_clips._booked(page, sha)
        assert not finished_clips._booked(other, sha)
        assert not finished_clips._booked("acct:other", sha)
        assert not finished_clips._booked(page, sha.upper())


def test_drifted_164_identities_present():
    for sha in ("34d449d33eba2aa42dd85f03973927eec7fb21c24e498ba6bc2cb928217e7429", "1be6ec6be64302cc085ebb9c3138c7c58b1b1804c662bb40f00a28555840ed83",
                "3fdefd5a8bab937f8f41138035455d416b99255147dcc3312979febf50069217", "600ccf3f8c951f48830842b7563d09da647630e2e3a7609ca2e1260675eba218"):
        assert finished_clips._booked(CUL, sha)
    assert finished_clips._booked(BUR, "eb0539ff7cb0e2c47945fa737055fc8c183200b8c5de769751bb71aeed107d92")


def test_non_member_still_refused():
    for page in (CUL, BUR):
        assert not finished_clips._booked(page, "0" * 64)
        assert not finished_clips._booked(page, "f" * 64)


def _route_client(tmp_path, monkeypatch):
    # built the way tests/test_finished_clips.py builds its client (module-local, no shared fixture)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setattr(finished_clips, "_root", lambda: tmp_path)
    monkeypatch.setattr(finished_clips, "_probe", lambda path, audio: {"width": 1080, "height": 1920, "codec": "h264", "durationSeconds": 20, "hasAudio": audio})
    app = FastAPI(); app.include_router(finished_clips.router)
    return TestClient(app)


def _post(client, page, body, sha):
    data = {"pageId": page, "publicationId": "publication-1", "masterPagesHash": "sha256:" + "a" * 64,
            "bookedDateTime": "2026-11-16T11:00:00-05:00", "bookedSha256": sha, "bytes": len(body), "hasAudio": False,
            "treatment": {"textMode": "blank", "captionMode": "in_app"}}
    return client.post("/v1/finished-clips", content=body,
                       headers={"Authorization": "Bearer test-token", "X-RT-Page-Id": page, "Idempotency-Key": "request-1",
                                "X-RT-Finished-Clip-Booking": json.dumps(data)})


def test_unbooked_new_identity_refused_at_route(tmp_path, monkeypatch):
    # route-level twin of test_unbooked_sha_refused_at_route for a sha that is NOT in the spec
    client = _route_client(tmp_path, monkeypatch)
    body = b"non-custody synthetic bytes"
    sha = hashlib.sha256(body).hexdigest()
    for page in (CUL, BUR):
        response = _post(client, page, body, sha)
        assert response.status_code == 400, (page, response.status_code, response.text)
        assert response.json()["detail"] == "clip_not_booked"


def test_spec_member_passes_booked_gate_on_own_page_only(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    body = b"bytes whose sha is not the booked sha"
    for page, other in ((CUL, BUR), (BUR, CUL)):
        for sha in SPEC["sets"][page]:
            r = _post(client, page, body, sha)
            assert not (r.status_code == 400 and r.json().get("detail") == "clip_not_booked"), (page, sha)
            r2 = _post(client, other, body, sha)
            assert r2.status_code == 400 and r2.json()["detail"] == "clip_not_booked", (other, sha)
