def test_unified_server_health(sync_client):
    response = sync_client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert "providers" in payload


def test_projects_endpoint_available(sync_client):
    response = sync_client.get("/api/projects")
    assert response.status_code == 200
    payload = response.json()
    assert "projects" in payload
