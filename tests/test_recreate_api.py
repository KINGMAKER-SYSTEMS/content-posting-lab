import pytest
from httpx import ASGITransport, AsyncClient
from app import app
from project_manager import get_project_recreate_dir
import routers.recreate as recreate_router
from routers.recreate import _PROMPT_GEN_SYSTEM

# A minimal valid PNG data URI for the happy-path / format-validation tests.
_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeUsage:
    total_tokens = 42


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, content, raises):
        self._content = content
        self._raises = raises

    async def create(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        return _FakeResp(self._content)


class _FakeOpenAI:
    """Stand-in for AsyncOpenAI exposing only .chat.completions.create."""

    def __init__(self, content="  a man walks toward the camera  ", raises=None):
        completions = _FakeCompletions(content, raises)
        self.chat = type("_Chat", (), {"completions": completions})()


@pytest.mark.anyio
async def test_list_recreate_jobs_empty():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/recreate/jobs", params={"project": "quick-test"})
        assert r.status_code == 200
        data = r.json()
        assert data["jobs"] == []


@pytest.mark.anyio
async def test_delete_nonexistent_job_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/fake-id", params={"project": "quick-test"})
        assert r.status_code == 404


@pytest.mark.anyio
async def test_delete_existing_job_removes_dir():
    job_dir = get_project_recreate_dir("quick-test") / "real-job"
    job_dir.mkdir(parents=True)
    (job_dir / "clip.mp4").write_bytes(b"x")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/real-job", params={"project": "quick-test"})
        assert r.status_code == 200
        assert r.json()["deleted"] is True
    assert not job_dir.exists()


@pytest.mark.anyio
async def test_delete_job_survives_rmtree_failure(monkeypatch):
    """A locked/permission-blocked dir must not 500 the DELETE route — the
    rmtree is best-effort (ignore_errors=True), so the route still returns 200."""
    import services.fsutil as fsutil

    job_dir = get_project_recreate_dir("quick-test") / "locked-job"
    job_dir.mkdir(parents=True)

    def boom(path, ignore_errors=False, **kwargs):
        # Mirror shutil.rmtree semantics: only raise when errors aren't ignored.
        if not ignore_errors:
            raise PermissionError("dir is locked")

    # The route now deletes through services.fsutil.safe_rmtree, which calls
    # shutil.rmtree WITHOUT ignore_errors and swallows the resulting OSError
    # (best-effort, never raises). Patch the underlying chokepoint so the route
    # still returns 200 on a locked dir.
    monkeypatch.setattr(fsutil.shutil, "rmtree", boom)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/locked-job", params={"project": "quick-test"})
        assert r.status_code == 200
        assert r.json()["deleted"] is True


def test_recreate_prompt_system_stays_minimal():
    system = _PROMPT_GEN_SYSTEM.lower()
    assert "one plain" in system
    assert "under 30 words" in system
    assert "style stacks" in system
    assert "2-4 sentence" not in system


# ── /generate-prompt error paths + happy path ─────────────────────────


@pytest.mark.anyio
async def test_generate_prompt_happy_path(monkeypatch):
    """Valid frames + working OpenAI client → 200 with stripped prompt text.
    The OpenAI client is patched so no real API call (or key) is needed."""
    monkeypatch.setattr(
        recreate_router, "_get_openai",
        lambda: _FakeOpenAI(content="  a man walks toward the camera  "),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/recreate/generate-prompt",
            json={"first_frame": _PNG_DATA_URI, "last_frame": _PNG_DATA_URI},
        )
    assert r.status_code == 200
    # Response is whitespace-stripped per the route's .strip().
    assert r.json()["prompt"] == "a man walks toward the camera"


@pytest.mark.anyio
async def test_generate_prompt_empty_frame_returns_400(monkeypatch):
    """Empty first_frame trips the explicit emptiness guard before any
    OpenAI work. Patch _get_openai so a misfire would be obvious (it isn't called)."""
    monkeypatch.setattr(
        recreate_router, "_get_openai",
        lambda: (_ for _ in ()).throw(AssertionError("OpenAI should not be reached")),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/recreate/generate-prompt",
            json={"first_frame": "", "last_frame": _PNG_DATA_URI},
        )
    assert r.status_code == 400
    assert "required" in r.json()["detail"].lower()


@pytest.mark.anyio
async def test_generate_prompt_non_data_uri_returns_400(monkeypatch):
    """A frame that isn't a data:image/... URI is rejected with 400 before
    any OpenAI call."""
    monkeypatch.setattr(
        recreate_router, "_get_openai",
        lambda: (_ for _ in ()).throw(AssertionError("OpenAI should not be reached")),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/recreate/generate-prompt",
            json={"first_frame": _PNG_DATA_URI, "last_frame": "https://example.com/x.png"},
        )
    assert r.status_code == 400
    assert "data:image" in r.json()["detail"]


@pytest.mark.anyio
async def test_generate_prompt_client_init_failure_returns_500(monkeypatch):
    """If the OpenAI client can't initialize (e.g. missing key), the route
    surfaces a 500 with the RuntimeError message — not an unhandled 502."""
    def _boom():
        raise RuntimeError("OPENAI_API_KEY not set")

    monkeypatch.setattr(recreate_router, "_get_openai", _boom)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/recreate/generate-prompt",
            json={"first_frame": _PNG_DATA_URI, "last_frame": _PNG_DATA_URI},
        )
    assert r.status_code == 500
    assert r.json()["detail"] == "OPENAI_API_KEY not set"


@pytest.mark.anyio
async def test_generate_prompt_openai_call_failure_returns_502(monkeypatch):
    """An exception during the OpenAI completion call maps to a 502 with the
    error detail (the status_code-prefixed branch when present)."""
    class _ApiError(Exception):
        status_code = 429

    monkeypatch.setattr(
        recreate_router, "_get_openai",
        lambda: _FakeOpenAI(raises=_ApiError("rate limited")),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/recreate/generate-prompt",
            json={"first_frame": _PNG_DATA_URI, "last_frame": _PNG_DATA_URI},
        )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "429" in detail
    assert "rate limited" in detail
