"""Fresh Lab jobs must not share the image renderer's stored request identity."""
import copy

import pytest

from services import control_plane_image_video as image
from services import control_plane_slideshows as slides

FORMATS = [("acct:pen-pal3", "image-video"), ("acct:twin47996", "single-image-video")]


@pytest.mark.asyncio
@pytest.mark.parametrize("page_id,format_slug", FORMATS)
async def test_new_lab_jobs_get_distinct_image_render_keys(monkeypatch, page_id, format_slug):
    photo = image.image_video_plan(page_id)
    original = copy.deepcopy(photo)
    calls = []

    async def request(method, endpoint, **kwargs):
        calls.append((method, endpoint, copy.deepcopy(kwargs)))
        return {"job_id": "fake-render", "quality": "draft"}

    monkeypatch.setattr(slides, "_request_json", request)
    for lab_job_id in ("cpl-0001", "cpl-0001", "cpl-0002", None):
        assert await slides.submit_syzygy_render(
            {"photoPlan": photo}, page_id=page_id, recipe_version="v1",
            format_slug=format_slug, lab_job_id=lab_job_id,
        ) == ("fake-render", "draft")
    assert photo == original
    assert all(method == "POST" and endpoint == "/api/v1/render"
               for method, endpoint, _ in calls)
    expected = {"spec": image.build_image_video_spec(photo)}
    assert all(call[2]["body"] == expected and call[2]["timeout"] == 90.0 for call in calls)
    keys = [call[2]["idempotency_key"] for call in calls]
    assert keys[0] == keys[1]  # a retry of the same job keeps its identity
    assert keys[3] == image.image_video_idempotency_key(page_id, "v1", photo["signature"], "render-v1")
    assert keys[0] != keys[2], "new image Lab job still reuses the prior render key"
    assert keys[0] != keys[3]  # marked jobs do not collide with legacy callers


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_job_id", ["", 123, False, []])
async def test_invalid_job_identity_refuses_before_image_transport(monkeypatch, bad_job_id):
    calls = []

    async def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"job_id": "fake-render", "quality": "draft"}

    monkeypatch.setattr(slides, "_request_json", request)
    with pytest.raises(image.ImageVideoError, match="Lab job id"):
        await slides.submit_syzygy_render(
            {"photoPlan": image.image_video_plan("acct:pen-pal3")},
            page_id="acct:pen-pal3", recipe_version="v1", format_slug="image-video",
            lab_job_id=bad_job_id,
        )
    assert calls == []
