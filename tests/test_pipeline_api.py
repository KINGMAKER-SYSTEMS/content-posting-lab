"""Tests for routers/pipeline.py — the page-intake / account-setup handoff.

Focus is on `_mint_random_alias`, the heart of the intake flow (it mints the
Cloudflare email alias every new TikTok account is keyed on). The router had no
test file, so the failure modes that matter at the sale-handoff desk —
CloudFlare not configured, no verified destination, unverified destination,
alias collisions, and CF API failures — were entirely unguarded.

We patch the module-level service handles that `routers.pipeline` imports
(`cf_get_config`, `cf_list_rules`, `cf_create_rule`, `destination_for_pipeline`)
plus the one direct `httpx.AsyncClient` call used to fetch verified
destinations.
"""

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from routers import pipeline


# ── fake httpx client for the verified-destinations GET ──────────────────────


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=None, response=httpx.Response(self.status_code)
            )

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient(...) as an async context manager.

    `get_result` is either a payload dict (returned from .get) or an Exception
    instance to raise from .get (simulates a network/transport failure).
    """

    def __init__(self, get_result):
        self._get_result = get_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *a, **k):
        if isinstance(self._get_result, Exception):
            raise self._get_result
        return _FakeResp(self._get_result)


def _patch_httpx(monkeypatch, get_result):
    """Make `httpx.AsyncClient(...)` (as used inside pipeline.py) return our fake."""
    fake = _FakeAsyncClient(get_result)
    monkeypatch.setattr(pipeline.httpx, "AsyncClient", lambda *a, **k: fake)


def _verified(*emails):
    return {"result": [{"email": e, "verified": True} for e in emails]}


CFG = {
    "configured": True,
    "account_id": "acct123",
    "token": "tok",
    "domain": "risingtidesviral.com",
}


def _patch_cfg(monkeypatch, cfg=CFG):
    monkeypatch.setattr(pipeline, "cf_get_config", lambda: dict(cfg))


def _run(coro):
    return asyncio.run(coro)


# ── configuration / destination guards ───────────────────────────────────────


def test_mint_raises_503_when_cf_not_configured(monkeypatch):
    monkeypatch.setattr(pipeline, "cf_get_config", lambda: {"configured": False})
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias())
    assert ei.value.status_code == 503
    assert "not configured" in ei.value.detail


def test_mint_raises_503_when_no_verified_destination(monkeypatch):
    _patch_cfg(monkeypatch)
    # CF returns destinations but none verified.
    _patch_httpx(monkeypatch, {"result": [{"email": "x@y.com", "verified": False}]})
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias())
    assert ei.value.status_code == 503
    assert "verified destination" in ei.value.detail.lower()


def test_mint_502_when_destinations_fetch_fails(monkeypatch):
    _patch_cfg(monkeypatch)
    _patch_httpx(monkeypatch, httpx.ConnectError("network down"))
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias())
    assert ei.value.status_code == 502
    assert "destinations fetch failed" in ei.value.detail.lower()


def test_mint_409_when_override_destination_unverified(monkeypatch):
    _patch_cfg(monkeypatch)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias(destination_override="stranger@evil.com"))
    assert ei.value.status_code == 409
    assert "not verified" in ei.value.detail.lower()


def test_mint_409_when_pipeline_default_destination_unverified(monkeypatch):
    """destination_for_pipeline resolves a default inbox; if CF hasn't verified
    it, the mint must 409 instead of silently routing into the void."""
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(
        pipeline, "destination_for_pipeline", lambda p: "henry@risingtidesent.com"
    )
    # CF verified list does NOT include henry@...
    _patch_httpx(monkeypatch, _verified("someoneelse@risingtidesent.com"))
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias(pipeline="King Maker Tech"))
    assert ei.value.status_code == 409


# ── alias resolution: collisions + sanitization ──────────────────────────────


def test_mint_409_when_desired_local_collides(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))
    # Existing rule already owns the alias the user wants.
    taken = "samb-truck-04@risingtidesviral.com"

    async def _list():
        return [{"matchers": [{"value": taken}]}]

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias(desired_local="samb-truck-04"))
    assert ei.value.status_code == 409
    assert "already taken" in ei.value.detail.lower()


def test_mint_all_punctuation_desired_local_floors_to_unknown(monkeypatch):
    """A desired_local with no usable chars slugs to the 'unknown' sentinel
    (never empty), so the 400 'must contain alphanumeric' guard in the router
    is effectively dead — the mint proceeds with unknown@domain. This test
    pins that real behavior so a future refactor of `_slug_alias`'s floor is
    caught instead of silently changing what alias gets minted."""
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))

    async def _list():
        return []

    minted = {}

    async def _create(local, dest):
        minted["local"] = local
        return {"id": "rule-floor"}

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)
    monkeypatch.setattr(pipeline, "cf_create_rule", _create)
    out = _run(pipeline._mint_random_alias(desired_local="---"))
    assert out["alias"] == "unknown@risingtidesviral.com"
    assert minted["local"] == "unknown"


def test_mint_502_when_rules_list_fails(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))

    async def _boom():
        raise RuntimeError("cf 500")

    monkeypatch.setattr(pipeline, "cf_list_rules", _boom)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias(desired_local="ok-name"))
    assert ei.value.status_code == 502
    assert "rules list failed" in ei.value.detail.lower()


def test_mint_500_when_random_alias_collides_every_time(monkeypatch):
    """Force every generated random local to collide so the retry loop is
    exhausted and we get the 500 'no free alias' path."""
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))
    # Pin the random generator to one fixed local-part...
    monkeypatch.setattr(pipeline, "_random_alias_local", lambda: "acct-fixed")
    collide = "acct-fixed@risingtidesviral.com"

    async def _list():
        return [{"matchers": [{"value": collide}]}]

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)

    created = []

    async def _create(local, dest):  # must never be reached
        created.append(local)
        return {"id": "should-not-happen"}

    monkeypatch.setattr(pipeline, "cf_create_rule", _create)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias())
    assert ei.value.status_code == 500
    assert created == []  # never tried to create a colliding rule


def test_mint_502_when_rule_create_fails(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))

    async def _list():
        return []

    async def _create(local, dest):
        raise RuntimeError("cf create 500")

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)
    monkeypatch.setattr(pipeline, "cf_create_rule", _create)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline._mint_random_alias(desired_local="ok-name"))
    assert ei.value.status_code == 502
    assert "rule create failed" in ei.value.detail.lower()


# ── happy paths ──────────────────────────────────────────────────────────────


def test_mint_happy_desired_local_uses_chosen_name_and_routes_destination(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))

    async def _list():
        return []

    seen = {}

    async def _create(local, dest):
        seen["local"] = local
        seen["dest"] = dest
        return {"id": "rule-xyz"}

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)
    monkeypatch.setattr(pipeline, "cf_create_rule", _create)

    out = _run(pipeline._mint_random_alias(desired_local="SamB Truck 04"))
    # slug lowercases + replaces non-alnum with '-'
    assert out["alias"] == "samb-truck-04@risingtidesviral.com"
    assert out["rule_id"] == "rule-xyz"
    # no override / no pipeline dest -> falls back to the first verified address
    assert out["destination"] == "henry@risingtidesent.com"
    assert seen["local"] == "samb-truck-04"
    assert seen["dest"] == "henry@risingtidesent.com"


def test_mint_happy_random_local_when_no_desired(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(pipeline, "destination_for_pipeline", lambda p: None)
    _patch_httpx(monkeypatch, _verified("henry@risingtidesent.com"))
    monkeypatch.setattr(pipeline, "_random_alias_local", lambda: "acct-abcd1234")

    async def _list():
        return []

    async def _create(local, dest):
        return {"id": "rule-rand"}

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)
    monkeypatch.setattr(pipeline, "cf_create_rule", _create)

    out = _run(pipeline._mint_random_alias())
    assert out["alias"] == "acct-abcd1234@risingtidesviral.com"
    assert out["rule_id"] == "rule-rand"


def test_mint_destination_override_takes_priority(monkeypatch):
    _patch_cfg(monkeypatch)
    # pipeline default would route to henry, but override must win.
    monkeypatch.setattr(
        pipeline, "destination_for_pipeline", lambda p: "henry@risingtidesent.com"
    )
    _patch_httpx(
        monkeypatch, _verified("henry@risingtidesent.com", "jay@risingtidesent.com")
    )
    monkeypatch.setattr(pipeline, "_random_alias_local", lambda: "acct-zzzz")

    async def _list():
        return []

    chosen = {}

    async def _create(local, dest):
        chosen["dest"] = dest
        return {"id": "r"}

    monkeypatch.setattr(pipeline, "cf_list_rules", _list)
    monkeypatch.setattr(pipeline, "cf_create_rule", _create)

    out = _run(pipeline._mint_random_alias(destination_override="jay@risingtidesent.com"))
    assert out["destination"] == "jay@risingtidesent.com"
    assert chosen["dest"] == "jay@risingtidesent.com"


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_slug_alias_sanitizes_and_floors_to_unknown():
    assert pipeline._slug_alias("SamB Truck 04") == "samb-truck-04"
    assert pipeline._slug_alias("  Keep.Dots_and-Dashes ") == "keep.dots_and-dashes"
    # all-invalid -> floor sentinel
    assert pipeline._slug_alias("@@@") == "unknown"


def test_random_alias_local_shape():
    a = pipeline._random_alias_local()
    assert a.startswith("acct-")
    assert len(a) == len("acct-") + 8
    assert a[5:].isalnum()


# ── submit_intake config / input guards ──────────────────────────────────────
# These are the 503 (Notion unconfigured) and 400 (blank username) doors at the
# top of the /intake endpoint — the regression surface the ticket calls out.
# A flip of notion_configured() returning True while NOTION_API_KEY is unset
# would let a half-configured endpoint create orphaned rows; pin the guard.


def test_submit_intake_503_when_notion_not_configured(monkeypatch):
    monkeypatch.setattr(pipeline, "notion_configured", lambda: False)
    req = pipeline.IntakeRequest(account_username="newhandle")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.submit_intake(req))
    assert ei.value.status_code == 503
    assert "notion not configured" in ei.value.detail.lower()


def test_submit_intake_400_when_username_blank(monkeypatch):
    # Notion is configured; the username guard must still fire on whitespace.
    monkeypatch.setattr(pipeline, "notion_configured", lambda: True)
    req = pipeline.IntakeRequest(account_username="   ")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.submit_intake(req))
    assert ei.value.status_code == 400
    assert "account_username is required" in ei.value.detail


# ── run_setup guards ─────────────────────────────────────────────────────────
# run_setup must (a) 404 unknown pages and (b) 409 a page with no email rather
# than re-minting (which historically clobbered real signup addresses). Both
# paths are independent of Notion/CF being live, so they're cheap to pin.


def test_run_setup_404_when_page_missing(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: None)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.run_setup("acct:ghost"))
    assert ei.value.status_code == 404
    assert "not found" in ei.value.detail.lower()


def test_run_setup_409_when_page_has_no_email(monkeypatch):
    # Page exists but carries neither an email_alias nor a signup_email.
    page = {"name": "Some Page", "pipeline": "King Maker Tech"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline, "cf_get_config", lambda: dict(CFG))
    # set_page must not be needed on this path, but stub it to be safe.
    monkeypatch.setattr(pipeline, "set_page", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.run_setup("acct:no-email"))
    assert ei.value.status_code == 409
    assert "no email" in ei.value.detail.lower()


def test_run_setup_does_not_remint_when_email_missing(monkeypatch):
    """The 409 must come from the guard, not from a silent re-mint attempt —
    Run Setup is explicitly forbidden from minting (it has clobbered real
    signup emails). If _mint_random_alias is ever wired back in here, this
    fails loudly."""
    page = {"name": "Some Page", "pipeline": "Flow Stage"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline, "cf_get_config", lambda: dict(CFG))
    monkeypatch.setattr(pipeline, "set_page", lambda *a, **k: None)

    async def _no_mint(*a, **k):
        raise AssertionError("run_setup must never mint an email")

    monkeypatch.setattr(pipeline, "_mint_random_alias", _no_mint)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.run_setup("acct:no-email"))
    assert ei.value.status_code == 409


def test_transition_400_when_invalid_status(monkeypatch):
    """transition_status validates against PIPELINE_STAGES before touching
    Notion — an invalid status must 400, never reach update_page_status."""
    req = pipeline.TransitionRequest(status="Bogus Stage")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.transition_status("acct:x", req))
    assert ei.value.status_code == 400
    assert "invalid status" in ei.value.detail.lower()


# ── get_workspace ────────────────────────────────────────────────────────────
# The King Maker operator console. Every external dependency (R2, poster
# resolution, cookie status) is optional and must degrade gracefully — an R2
# list failure or a missing poster must NOT 500 the whole workspace, it must
# surface as a field. The only hard error is a missing page (404).


def test_workspace_404_when_page_missing(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: None)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.get_workspace("acct:ghost"))
    assert ei.value.status_code == 404


def test_workspace_degrades_when_r2_unconfigured_and_no_poster(monkeypatch):
    """No R2 prefix, R2 not configured, no poster resolved, cookie lookup
    throws — the workspace must still return a complete skeleton with defaults
    rather than blowing up."""
    page = {"name": "Some Page", "pipeline": "King Maker Tech"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: False)
    monkeypatch.setattr(pipeline, "resolve_poster_for_page", lambda p: None)

    def _boom(name):
        raise RuntimeError("cookie store down")

    monkeypatch.setattr(pipeline, "get_cookie_status", _boom)

    ws = _run(pipeline.get_workspace("acct:x"))
    assert ws["r2"]["configured"] is False
    assert ws["r2"]["object_count"] == 0
    assert ws["telegram"]["topic_present"] is False
    assert ws["telegram"]["poster_id"] is None
    # cookie lookup threw -> stays at the default "missing", never propagates
    assert ws["cookie_status"] == "missing"


def test_workspace_r2_list_error_surfaces_as_field(monkeypatch):
    """When R2 is configured and the page has a prefix but listing objects
    throws, the error is captured on workspace['r2']['error'] — not raised."""
    page = {"name": "P", "r2_prefix": "accounts/acct-x/", "r2_bucket": "b"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)

    def _boom(iid, max_keys=500):
        raise RuntimeError("r2 list 500")

    monkeypatch.setattr(pipeline.r2, "list_account_objects", _boom)
    monkeypatch.setattr(pipeline, "resolve_poster_for_page", lambda p: None)
    monkeypatch.setattr(pipeline, "get_cookie_status", lambda name: "valid")

    ws = _run(pipeline.get_workspace("acct:x"))
    assert "r2 list 500" in ws["r2"]["error"]
    assert ws["r2"]["object_count"] == 0
    assert ws["cookie_status"] == "valid"


def test_workspace_includes_poster_topic_when_resolved(monkeypatch):
    page = {"name": "P", "r2_prefix": "accounts/acct-x/", "r2_bucket": "b"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    monkeypatch.setattr(
        pipeline.r2, "list_account_objects", lambda iid, max_keys=500: [{"key": "a"}, {"key": "b"}]
    )
    poster = {
        "poster_id": "p1",
        "name": "Glitch",
        "chat_id": -100,
        "topics": {"acct:x": {"topic_id": 42, "topic_name": "P"}},
    }
    monkeypatch.setattr(pipeline, "resolve_poster_for_page", lambda p: poster)
    monkeypatch.setattr(pipeline, "get_cookie_status", lambda name: "valid")

    ws = _run(pipeline.get_workspace("acct:x"))
    assert ws["r2"]["object_count"] == 2
    assert ws["telegram"]["poster_id"] == "p1"
    assert ws["telegram"]["topic_present"] is True
    assert ws["telegram"]["topic_id"] == 42


# ── presign_upload ───────────────────────────────────────────────────────────
# The browser-direct-to-R2 upload door. Three guard rails: 404 unknown page,
# 503 R2 unconfigured, 400 page with no R2 prefix (setup never ran). Only when
# all three pass do we hand back a presigned URL.


def test_presign_404_when_page_missing(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: None)
    req = pipeline.PresignUploadRequest(filename="v.mp4")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.presign_upload("acct:ghost", req))
    assert ei.value.status_code == 404


def test_presign_503_when_r2_unconfigured(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"r2_prefix": "x/"})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: False)
    req = pipeline.PresignUploadRequest(filename="v.mp4")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.presign_upload("acct:x", req))
    assert ei.value.status_code == 503
    assert "r2 not configured" in ei.value.detail.lower()


def test_presign_400_when_no_r2_prefix(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"name": "P"})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    req = pipeline.PresignUploadRequest(filename="v.mp4")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.presign_upload("acct:x", req))
    assert ei.value.status_code == 400
    assert "no r2 prefix" in ei.value.detail.lower()


def test_presign_happy_sanitizes_filename_and_returns_url(monkeypatch):
    """Path separators in the filename must be neutralized so a caller can't
    escape the account prefix (e.g. '../../etc'). The key is built from the
    sanitized name."""
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"r2_prefix": "accounts/acct-x/"})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    monkeypatch.setattr(
        pipeline.r2, "account_key", lambda iid, name: f"accounts/{iid}/{name}"
    )
    monkeypatch.setattr(pipeline.r2, "presign_put", lambda key, content_type: f"https://r2/{key}")

    req = pipeline.PresignUploadRequest(filename="a/b\\c.mp4")
    out = _run(pipeline.presign_upload("acct:x", req))
    assert out["filename"] == "a_b_c.mp4"
    assert "a_b_c.mp4" in out["key"]
    assert out["url"].startswith("https://r2/")


# ── forward_r2_to_topic ──────────────────────────────────────────────────────
# Streams an R2 object to the poster's Telegram topic. The interesting failure
# modes the ticket calls out: page missing (404), R2 unconfigured (503), no
# poster resolved (400), no telegram topic (400), R2 object missing (404).


def test_forward_404_when_page_missing(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: None)
    req = pipeline.ForwardToTopicRequest(r2_key="k")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.forward_r2_to_topic("acct:ghost", req))
    assert ei.value.status_code == 404


def test_forward_503_when_r2_unconfigured(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"name": "P"})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: False)
    req = pipeline.ForwardToTopicRequest(r2_key="k")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.forward_r2_to_topic("acct:x", req))
    assert ei.value.status_code == 503


def test_forward_400_when_no_poster(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"name": "P", "poster_name": ""})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    monkeypatch.setattr(pipeline, "resolve_poster_for_page", lambda p: None)
    req = pipeline.ForwardToTopicRequest(r2_key="k")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.forward_r2_to_topic("acct:x", req))
    assert ei.value.status_code == 400
    assert "no poster" in ei.value.detail.lower()


def test_forward_400_when_no_telegram_topic(monkeypatch):
    """Poster resolved but has no topic for this page (setup never created
    one) — must 400, never attempt a send."""
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"name": "P"})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    monkeypatch.setattr(
        pipeline,
        "resolve_poster_for_page",
        lambda p: {"chat_id": -100, "topics": {}},
    )
    req = pipeline.ForwardToTopicRequest(r2_key="k")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.forward_r2_to_topic("acct:x", req))
    assert ei.value.status_code == 400
    assert "no telegram topic" in ei.value.detail.lower()


def test_forward_404_when_r2_object_missing(monkeypatch):
    """All routing resolves, but the R2 key the caller passed doesn't exist —
    head() returns None and we 404 the object before downloading anything."""
    monkeypatch.setattr(pipeline, "get_page", lambda iid: {"name": "P"})
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    monkeypatch.setattr(
        pipeline,
        "resolve_poster_for_page",
        lambda p: {"chat_id": -100, "topics": {"acct:x": {"topic_id": 7}}},
    )
    monkeypatch.setattr(pipeline.r2, "head", lambda key: None)
    req = pipeline.ForwardToTopicRequest(r2_key="missing.mp4")
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.forward_r2_to_topic("acct:x", req))
    assert ei.value.status_code == 404
    assert "r2 object not found" in ei.value.detail.lower()


# ── page_health ──────────────────────────────────────────────────────────────
# Derived dashboard metrics. Like the workspace, every external call is
# best-effort: an R2 count failure or a cookie-status throw must be swallowed
# (the field keeps its default) rather than propagating. Only a missing page
# 404s.


def test_health_404_when_page_missing(monkeypatch):
    monkeypatch.setattr(pipeline, "get_page", lambda iid: None)
    with pytest.raises(HTTPException) as ei:
        _run(pipeline.page_health("acct:ghost"))
    assert ei.value.status_code == 404


def test_health_swallows_r2_and_cookie_errors(monkeypatch):
    """R2 count throws and cookie lookup throws — both must be caught so the
    health blob still returns with its defaults intact."""
    page = {"name": "P", "r2_prefix": "accounts/acct-x/"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)

    def _boom_count(iid):
        raise RuntimeError("r2 down")

    def _boom_cookie(name):
        raise RuntimeError("cookie down")

    monkeypatch.setattr(pipeline.r2, "count_account_objects", _boom_count)
    monkeypatch.setattr(pipeline, "resolve_poster_for_page", lambda p: None)
    monkeypatch.setattr(pipeline, "get_cookie_status", _boom_cookie)

    h = _run(pipeline.page_health("acct:x"))
    assert h["r2_count"] == 0
    assert h["cookie_status"] == "missing"
    assert h["has_r2_prefix"] is True
    assert h["telegram_topic_present"] is False


def test_health_reports_counts_and_topic(monkeypatch):
    page = {"name": "P", "r2_prefix": "accounts/acct-x/", "email_alias": "a@b.com"}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: dict(page))
    monkeypatch.setattr(pipeline.r2, "is_configured", lambda: True)
    monkeypatch.setattr(pipeline.r2, "count_account_objects", lambda iid: 37)
    monkeypatch.setattr(
        pipeline,
        "resolve_poster_for_page",
        lambda p: {"topics": {"acct:x": {"topic_name": "My Topic"}}},
    )
    monkeypatch.setattr(pipeline, "get_cookie_status", lambda name: "valid")

    h = _run(pipeline.page_health("acct:x"))
    assert h["r2_count"] == 37
    assert h["telegram_topic_present"] is True
    assert h["telegram_topic_name"] == "My Topic"
    assert h["has_email_alias"] is True
    assert h["cookie_status"] == "valid"
