"""Tests for the Telegram Mini App API and per-poster content federation."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import services.content_requests as content_requests
import services.miniapp_auth as miniapp_auth
import services.roster as roster
import services.telegram as tg
from project_manager import get_project_video_dir
from services.miniapp_auth import (
    AuthError,
    parse_init_data,
    resolve_poster_from_request,
)


FAKE_TOKEN = "123456:FAKE-bot-token-for-tests"


@pytest.fixture(autouse=True)
def isolate_service_files(monkeypatch, tmp_path):
    """Point the telegram/roster/requests data files at a tmp dir."""
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(
        content_requests, "REQUESTS_PATH", tmp_path / "content_requests.json"
    )
    monkeypatch.setenv("MINIAPP_DEV_AUTH", "1")
    monkeypatch.delenv("MINIAPP_AGENT_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    yield


def _build_init_data(token: str, user: dict, auth_date: int | None = None) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAExample",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# ── initData validation (unit) ───────────────────────────────────────


def test_parse_init_data_valid():
    user = {"id": 555, "username": "seffra_tg", "first_name": "Seffra"}
    init_data = _build_init_data(FAKE_TOKEN, user)
    parsed = parse_init_data(init_data, bot_token=FAKE_TOKEN)
    assert parsed["user"]["id"] == 555
    assert parsed["user"]["username"] == "seffra_tg"


def test_parse_init_data_tampered_rejected():
    user = {"id": 555, "username": "seffra_tg"}
    init_data = _build_init_data(FAKE_TOKEN, user)
    tampered = init_data.replace("555", "999")
    with pytest.raises(AuthError):
        parse_init_data(tampered, bot_token=FAKE_TOKEN)


def test_parse_init_data_wrong_token_rejected():
    user = {"id": 555}
    init_data = _build_init_data(FAKE_TOKEN, user)
    with pytest.raises(AuthError):
        parse_init_data(init_data, bot_token="999:OTHER-token")


def test_parse_init_data_expired_rejected(monkeypatch):
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "60")
    user = {"id": 555}
    old = int(time.time()) - 3600
    init_data = _build_init_data(FAKE_TOKEN, user, auth_date=old)
    with pytest.raises(AuthError):
        parse_init_data(init_data, bot_token=FAKE_TOKEN)


# ── Dev-auth API flow ────────────────────────────────────────────────


def _seed_poster_with_page(project="acme-tiktok"):
    tg.set_poster("test-poster", {"name": "Test Poster", "chat_id": -100})
    roster.set_page(
        "acct:test-page",
        {
            "name": "ACME TikTok",
            "provider": "tiktok",
            "poster_name": "Test Poster",
            "project": project,
        },
    )


def test_me_returns_poster_and_pages(sync_client):
    _seed_poster_with_page()
    resp = sync_client.get(
        "/api/miniapp/me", headers={"X-Dev-Poster-Id": "test-poster"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["poster_id"] == "test-poster"
    assert body["page_count"] == 1
    assert body["pages"][0]["integration_id"] == "acct:test-page"


def test_me_requires_auth(sync_client):
    resp = sync_client.get("/api/miniapp/me")
    assert resp.status_code in (401, 403)


def test_videos_lists_rendered_videos(sync_client):
    _seed_poster_with_page(project="acme-tiktok")
    # Drop a rendered mp4 into the page's project video dir.
    vdir = get_project_video_dir("acme-tiktok") / "provider-a"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "job_0.mp4").write_bytes(b"\x00\x00")

    resp = sync_client.get(
        "/api/miniapp/videos", headers={"X-Dev-Poster-Id": "test-poster"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pages"][0]["video_count"] == 1
    assert len(body["videos"]) == 1
    v = body["videos"][0]
    assert v["page_id"] == "acct:test-page"
    assert v["url"] == "/projects/acme-tiktok/videos/provider-a/job_0.mp4"


def test_content_request_lifecycle(sync_client):
    _seed_poster_with_page()
    # File a request as the poster.
    created = sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "test-poster"},
        json={"text": "Need 5 more lyric videos", "page_id": "acct:test-page"},
    )
    assert created.status_code == 201
    req = created.json()
    assert req["status"] == "open"
    assert req["page_name"] == "ACME TikTok"
    rid = req["id"]

    # Poster sees their own request.
    listed = sync_client.get(
        "/api/miniapp/requests", headers={"X-Dev-Poster-Id": "test-poster"}
    )
    assert listed.status_code == 200
    assert any(r["id"] == rid for r in listed.json()["requests"])

    # Agent picks it up and fulfills it.
    agent_list = sync_client.get("/api/miniapp/agent/requests?status=open")
    assert agent_list.status_code == 200
    assert any(r["id"] == rid for r in agent_list.json()["requests"])

    patched = sync_client.patch(
        f"/api/miniapp/agent/requests/{rid}",
        json={"status": "fulfilled", "agent_note": "rendered + sent"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "fulfilled"
    assert patched.json()["fulfilled_at"] is not None


def test_empty_text_request_rejected(sync_client):
    _seed_poster_with_page()
    resp = sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "test-poster"},
        json={"text": "   "},
    )
    assert resp.status_code == 400


def test_agent_key_enforced_when_set(sync_client, monkeypatch):
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "s3cret")
    _seed_poster_with_page()
    denied = sync_client.get("/api/miniapp/agent/requests")
    assert denied.status_code == 401
    ok = sync_client.get(
        "/api/miniapp/agent/requests", headers={"X-Agent-Key": "s3cret"}
    )
    assert ok.status_code == 200


# ── initData binding flow (no dev bypass) ────────────────────────────


def test_initdata_resolves_bound_poster(sync_client, monkeypatch):
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    _seed_poster_with_page()
    tg.set_bot_token(FAKE_TOKEN)
    tg.bind_user_to_poster("test-poster", 777, "seffra_tg")

    init_data = _build_init_data(FAKE_TOKEN, {"id": 777, "username": "seffra_tg"})
    resp = sync_client.get(
        "/api/miniapp/me", headers={"X-Telegram-Init-Data": init_data}
    )
    assert resp.status_code == 200
    assert resp.json()["poster_id"] == "test-poster"


def test_initdata_unbound_user_forbidden(sync_client, monkeypatch):
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    _seed_poster_with_page()
    tg.set_bot_token(FAKE_TOKEN)

    init_data = _build_init_data(FAKE_TOKEN, {"id": 12345, "username": "nobody"})
    resp = sync_client.get(
        "/api/miniapp/me", headers={"X-Telegram-Init-Data": init_data}
    )
    assert resp.status_code == 403


# ── initData validation: more malformed shapes (unit) ────────────────


def test_parse_init_data_missing_hash():
    # Hand-built field set with no `hash` at all.
    with pytest.raises(AuthError) as ei:
        parse_init_data("auth_date=1&user=%7B%7D", bot_token=FAKE_TOKEN)
    assert "hash" in ei.value.message


def test_parse_init_data_empty_rejected():
    with pytest.raises(AuthError) as ei:
        parse_init_data("", bot_token=FAKE_TOKEN)
    assert ei.value.status_code == 401


def test_parse_init_data_garbage_rejected():
    # Random non-querystring junk: parses to nothing useful, has no hash.
    with pytest.raises(AuthError):
        parse_init_data("@@@not-a-querystring@@@", bot_token=FAKE_TOKEN)


def test_parse_init_data_no_bot_token_503():
    user = {"id": 1}
    init_data = _build_init_data(FAKE_TOKEN, user)
    with pytest.raises(AuthError) as ei:
        parse_init_data(init_data, bot_token="")
    assert ei.value.status_code == 503


def test_parse_init_data_no_auth_date_skips_expiry(monkeypatch):
    # Documented behavior: the expiry check only fires when auth_date is present
    # and non-zero. Validly signed initData with NO auth_date is accepted even
    # with a max-age set. Pin this so a future change can't silently flip it.
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "60")
    fields = {"user": json.dumps({"id": 1}, separators=(",", ":"))}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    parsed = parse_init_data(urlencode(fields), bot_token=FAKE_TOKEN)
    assert parsed["user"]["id"] == 1


def test_parse_init_data_max_age_zero_disables_expiry(monkeypatch):
    # MINIAPP_INITDATA_MAX_AGE=0 disables the expiry check (documented). A very
    # old but validly signed initData must still be accepted.
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "0")
    old = int(time.time()) - 10_000_000
    init_data = _build_init_data(FAKE_TOKEN, {"id": 1}, auth_date=old)
    parsed = parse_init_data(init_data, bot_token=FAKE_TOKEN)
    assert parsed["user"]["id"] == 1


def test_parse_init_data_garbage_max_age_falls_back(monkeypatch):
    # A non-integer MINIAPP_INITDATA_MAX_AGE must fall back to the 86400 default
    # (not crash), so a fresh initData still validates.
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "not-a-number")
    init_data = _build_init_data(FAKE_TOKEN, {"id": 1})
    parsed = parse_init_data(init_data, bot_token=FAKE_TOKEN)
    assert parsed["user"]["id"] == 1


def test_parse_init_data_bad_user_json_nulls_user():
    # Valid signature but `user` is not JSON → parsed user becomes None.
    fields = {"auth_date": str(int(time.time())), "user": "not-json"}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    parsed = parse_init_data(urlencode(fields), bot_token=FAKE_TOKEN)
    assert parsed["user"] is None


def test_initdata_no_user_field_rejected(sync_client, monkeypatch):
    # Validly signed initData carrying no `user` → cannot resolve a poster.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    _seed_poster_with_page()
    tg.set_bot_token(FAKE_TOKEN)

    fields = {"auth_date": str(int(time.time())), "query_id": "AAExample"}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    resp = sync_client.get(
        "/api/miniapp/me", headers={"X-Telegram-Init-Data": urlencode(fields)}
    )
    assert resp.status_code in (401, 403)


def test_authorization_tma_header_accepted(sync_client, monkeypatch):
    # initData may arrive via `Authorization: tma <initData>` instead of the header.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    _seed_poster_with_page()
    tg.set_bot_token(FAKE_TOKEN)
    tg.bind_user_to_poster("test-poster", 777, "seffra_tg")

    init_data = _build_init_data(FAKE_TOKEN, {"id": 777, "username": "seffra_tg"})
    resp = sync_client.get(
        "/api/miniapp/me", headers={"Authorization": f"tma {init_data}"}
    )
    assert resp.status_code == 200
    assert resp.json()["poster_id"] == "test-poster"


# ── Dev bypass gating ────────────────────────────────────────────────


def test_dev_bypass_ignored_when_disabled(sync_client, monkeypatch):
    # With MINIAPP_DEV_AUTH off, X-Dev-Poster-Id must NOT grant access.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    _seed_poster_with_page()
    resp = sync_client.get(
        "/api/miniapp/me", headers={"X-Dev-Poster-Id": "test-poster"}
    )
    assert resp.status_code in (401, 403)


def test_dev_bypass_unknown_poster_404(sync_client):
    # Dev auth is on (autouse fixture), but the poster id doesn't exist.
    resp = sync_client.get(
        "/api/miniapp/me", headers={"X-Dev-Poster-Id": "ghost-poster"}
    )
    assert resp.status_code == 404


# ── Agent-key validation failures ────────────────────────────────────


def test_agent_wrong_key_rejected(sync_client, monkeypatch):
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "s3cret")
    _seed_poster_with_page()
    resp = sync_client.get(
        "/api/miniapp/agent/requests", headers={"X-Agent-Key": "wrong"}
    )
    assert resp.status_code == 401


def test_agent_blank_key_rejected(sync_client, monkeypatch):
    # An empty/whitespace X-Agent-Key must be treated as missing, not as a
    # match against a configured key.
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "s3cret")
    _seed_poster_with_page()
    resp = sync_client.get(
        "/api/miniapp/agent/requests", headers={"X-Agent-Key": "   "}
    )
    assert resp.status_code == 401


def test_agent_patch_key_enforced(sync_client, monkeypatch):
    # The PATCH endpoint is gated too, not just the GET listing.
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "s3cret")
    _seed_poster_with_page()
    created = sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "test-poster"},
        json={"text": "need content"},
    )
    rid = created.json()["id"]

    denied = sync_client.patch(
        f"/api/miniapp/agent/requests/{rid}", json={"status": "in_progress"}
    )
    assert denied.status_code == 401

    ok = sync_client.patch(
        f"/api/miniapp/agent/requests/{rid}",
        headers={"X-Agent-Key": "s3cret"},
        json={"status": "in_progress"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "in_progress"


# ── Agent endpoint edge cases ────────────────────────────────────────


def test_agent_patch_unknown_request_404(sync_client):
    resp = sync_client.patch(
        "/api/miniapp/agent/requests/does-not-exist",
        json={"status": "fulfilled"},
    )
    assert resp.status_code == 404


def test_agent_patch_invalid_status_400(sync_client):
    _seed_poster_with_page()
    created = sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "test-poster"},
        json={"text": "need content"},
    )
    rid = created.json()["id"]
    resp = sync_client.patch(
        f"/api/miniapp/agent/requests/{rid}", json={"status": "bogus-status"}
    )
    assert resp.status_code == 400


# ── Request filtering edge cases ─────────────────────────────────────


def test_requests_status_filter(sync_client):
    _seed_poster_with_page()
    hdr = {"X-Dev-Poster-Id": "test-poster"}
    a = sync_client.post(
        "/api/miniapp/requests", headers=hdr, json={"text": "first"}
    ).json()
    sync_client.post("/api/miniapp/requests", headers=hdr, json={"text": "second"})
    # Move one request to in_progress.
    sync_client.patch(
        f"/api/miniapp/agent/requests/{a['id']}", json={"status": "in_progress"}
    )

    open_only = sync_client.get(
        "/api/miniapp/requests?status=open", headers=hdr
    ).json()["requests"]
    assert all(r["status"] == "open" for r in open_only)
    assert a["id"] not in {r["id"] for r in open_only}

    in_prog = sync_client.get(
        "/api/miniapp/requests?status=in_progress", headers=hdr
    ).json()["requests"]
    assert {r["id"] for r in in_prog} == {a["id"]}


def test_requests_scoped_to_calling_poster(sync_client):
    # Two posters; each sees only their own requests.
    _seed_poster_with_page()
    tg.set_poster("other-poster", {"name": "Other", "chat_id": -200})

    sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "test-poster"},
        json={"text": "mine"},
    )
    sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "other-poster"},
        json={"text": "theirs"},
    )

    mine = sync_client.get(
        "/api/miniapp/requests", headers={"X-Dev-Poster-Id": "test-poster"}
    ).json()["requests"]
    assert {r["text"] for r in mine} == {"mine"}


def test_agent_list_status_empty_returns_all(sync_client):
    # status="" (empty) on the agent listing means "all statuses".
    _seed_poster_with_page()
    hdr = {"X-Dev-Poster-Id": "test-poster"}
    a = sync_client.post(
        "/api/miniapp/requests", headers=hdr, json={"text": "one"}
    ).json()
    sync_client.patch(
        f"/api/miniapp/agent/requests/{a['id']}", json={"status": "fulfilled"}
    )
    sync_client.post("/api/miniapp/requests", headers=hdr, json={"text": "two"})

    all_reqs = sync_client.get(
        "/api/miniapp/agent/requests?status="
    ).json()["requests"]
    statuses = {r["status"] for r in all_reqs}
    assert {"fulfilled", "open"} <= statuses

    # Default (no status param) lists only open.
    open_default = sync_client.get(
        "/api/miniapp/agent/requests"
    ).json()["requests"]
    assert all(r["status"] == "open" for r in open_default)


def test_agent_list_poster_filter(sync_client):
    _seed_poster_with_page()
    tg.set_poster("other-poster", {"name": "Other", "chat_id": -200})
    sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "test-poster"},
        json={"text": "mine"},
    )
    sync_client.post(
        "/api/miniapp/requests",
        headers={"X-Dev-Poster-Id": "other-poster"},
        json={"text": "theirs"},
    )
    only_test = sync_client.get(
        "/api/miniapp/agent/requests?status=&poster_id=test-poster"
    ).json()["requests"]
    assert {r["poster_id"] for r in only_test} == {"test-poster"}


# ── resolve_poster_from_request (direct unit, critical auth path) ─────


def test_resolve_dev_bypass_returns_poster():
    # MINIAPP_DEV_AUTH=1 from the autouse fixture; dev poster id resolves.
    tg.set_poster("dev-p", {"name": "Dev", "chat_id": -1})
    poster = resolve_poster_from_request(None, dev_poster_id="dev-p")
    assert poster["name"] == "Dev"


def test_resolve_dev_bypass_unknown_poster_404():
    with pytest.raises(AuthError) as ei:
        resolve_poster_from_request(None, dev_poster_id="ghost")
    assert ei.value.status_code == 404


def test_resolve_dev_bypass_disabled_falls_through(monkeypatch):
    # With dev auth off, a dev_poster_id is ignored and we fall to initData,
    # which here is empty → 401. The bypass must NOT grant access.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    tg.set_poster("dev-p", {"name": "Dev", "chat_id": -1})
    with pytest.raises(AuthError) as ei:
        resolve_poster_from_request(None, dev_poster_id="dev-p")
    assert ei.value.status_code == 401


def test_resolve_dev_bypass_requires_poster_id(monkeypatch):
    # Dev auth on but no dev_poster_id → must not bypass; falls to initData.
    with pytest.raises(AuthError) as ei:
        resolve_poster_from_request(None, dev_poster_id=None)
    assert ei.value.status_code == 401


def test_resolve_initdata_bound_user(monkeypatch):
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    tg.set_poster("real-p", {"name": "Real", "chat_id": -2})
    tg.set_bot_token(FAKE_TOKEN)
    tg.bind_user_to_poster("real-p", 777, "seffra_tg")

    init_data = _build_init_data(FAKE_TOKEN, {"id": 777, "username": "seffra_tg"})
    poster = resolve_poster_from_request(init_data)
    assert poster["name"] == "Real"


def test_resolve_initdata_unbound_user_403(monkeypatch):
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    tg.set_bot_token(FAKE_TOKEN)
    init_data = _build_init_data(FAKE_TOKEN, {"id": 999, "username": "nobody"})
    with pytest.raises(AuthError) as ei:
        resolve_poster_from_request(init_data)
    assert ei.value.status_code == 403


def test_resolve_initdata_no_user_field_400ish(monkeypatch):
    # Validly signed but user-less initData → "no user" error.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    tg.set_bot_token(FAKE_TOKEN)
    fields = {"auth_date": str(int(time.time())), "query_id": "AAExample"}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(AuthError) as ei:
        resolve_poster_from_request(urlencode(fields))
    assert "no user" in ei.value.message


def test_resolve_initdata_user_without_id_or_username(monkeypatch):
    # user JSON present but carries neither id nor username → can't resolve.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    tg.set_bot_token(FAKE_TOKEN)
    init_data = _build_init_data(FAKE_TOKEN, {"first_name": "Anon"})
    with pytest.raises(AuthError) as ei:
        resolve_poster_from_request(init_data)
    assert "no user" in ei.value.message


def test_resolve_initdata_username_only_match(monkeypatch):
    # No telegram_user_ids entry, but username binding resolves the poster.
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    tg.set_poster("uname-p", {"name": "UnameOnly", "chat_id": -3})
    tg.set_bot_token(FAKE_TOKEN)
    tg.bind_user_to_poster("uname-p", 0, "seffra_tg")

    init_data = _build_init_data(FAKE_TOKEN, {"username": "seffra_tg"})
    poster = resolve_poster_from_request(init_data)
    assert poster["name"] == "UnameOnly"


def test_resolve_concurrent_dev_bypass_consistent():
    # The auth path reads JSON from disk on every call; hammering it
    # concurrently must always return the same poster (no torn reads).
    import concurrent.futures

    tg.set_poster("race-p", {"name": "Race", "chat_id": -9})

    def call():
        return resolve_poster_from_request(None, dev_poster_id="race-p")["name"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: call(), range(40)))
    assert set(results) == {"Race"}
