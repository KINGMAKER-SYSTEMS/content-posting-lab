"""TOS imports require the private machine credential before reads or writes."""

import requests

import app as app_module
import project_manager


TOKEN = "server-only-control-plane-token"
PROJECT = "import-auth"
URL = "/api/burn/import-tos"


def test_import_refuses_missing_wrong_browser_and_unconfigured_token_before_side_effects(
    sync_client, monkeypatch, isolated_projects_root
):
    monkeypatch.setattr(app_module, "_APP_API_KEY", None)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unauthorized import reached a side effect")

    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(project_manager, "get_project_caption_dir", forbidden)
    project_dir = isolated_projects_root / PROJECT
    assert not project_dir.exists()

    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer browser-facing-key"},
        {"X-API-Key": TOKEN},
        {"X-API-Key": "browser-facing-key"},
    ):
        response = sync_client.post(URL, params={"project": PROJECT}, headers=headers)
        assert response.status_code == 401
        assert not project_dir.exists()

    monkeypatch.delenv("CONTROL_PLANE_TOKEN")
    response = sync_client.post(
        URL, params={"project": PROJECT}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 503
    assert not project_dir.exists()

    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "   ")
    response = sync_client.post(
        URL, params={"project": PROJECT}, headers={"Authorization": "Bearer    "}
    )
    assert response.status_code == 503
    assert not project_dir.exists()


def test_import_accepts_machine_token_with_app_key_unset(sync_client, monkeypatch, isolated_projects_root):
    monkeypatch.setattr(app_module, "_APP_API_KEY", None)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")

    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{
                "text_raw": "Exact caption", "username": "@creator",
                "video_url": "https://www.tiktok.com/@creator/video/12345",
                "views": 7, "sound_id": "sound-1", "song_title": "Song",
                "campaign_slug": "campaign-1",
            }]

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    response = sync_client.post(
        URL, params={"project": PROJECT}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json()["total_captions"] == 1
    assert len(calls) == 1
    assert calls[0][0] == "https://supabase.invalid/rest/v1/tos_captions"
    assert (isolated_projects_root / PROJECT / "captions" / "tos-campaign-1" / "captions.csv").exists()
