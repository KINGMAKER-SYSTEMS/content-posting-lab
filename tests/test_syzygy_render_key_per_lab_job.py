"""A Syzygy render key is fresh per Lab job, so a stored FAILED render is never replayed
to a later Lab job for the same plan (burnt.mill.rd 10-31: every retry replayed the
04:42Z thread-exhaustion failure)."""
import pytest

from services import control_plane_slideshows as slides


PLAN = {"templateSlug": "clean-slideshow", "mediaKeys": ["lib/a.jpg"], "signature": "a" * 64}


async def _keys(monkeypatch, *lab_job_ids):
    seen = []

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None, timeout=60.0):
        seen.append(idempotency_key)
        return {"job_id": f"syz-{len(seen)}", "quality": "final"}

    monkeypatch.setattr(slides, "_request_json", fake_request_json)
    for lab_job_id in lab_job_ids:
        await slides.submit_syzygy_render(dict(PLAN), page_id="acct:p", recipe_version="v1",
                                          lab_job_id=lab_job_id)
    return seen


@pytest.mark.asyncio
async def test_new_lab_job_gets_a_new_render_key(monkeypatch):
    first, second = await _keys(monkeypatch, "cpl-0001", "cpl-0002")
    assert first != second
    assert first.startswith("cpl-syzygy-") and second.startswith("cpl-syzygy-")


@pytest.mark.asyncio
async def test_same_lab_job_keeps_one_render_key(monkeypatch):
    first, again = await _keys(monkeypatch, "cpl-0001", "cpl-0001")
    assert first == again


@pytest.mark.asyncio
async def test_without_a_lab_job_the_legacy_key_is_unchanged(monkeypatch):
    (legacy,) = await _keys(monkeypatch, None)
    assert legacy == slides.syzygy_idempotency_key("acct:p", "v1", "a" * 64, "render-v1")


@pytest.mark.asyncio
async def test_empty_lab_job_id_is_refused_before_any_post(monkeypatch):
    with pytest.raises(slides.SyzygyError):
        await _keys(monkeypatch, "")


def test_the_slideshow_runner_passes_its_lab_job_id():
    import inspect
    from routers import control_plane as cp
    src = inspect.getsource(cp._run_owned_syzygy_slideshow)
    assert "lab_job_id=job_id" in src
