"""Property tests for the /api/roster response-shape guard (services/roster_public.py).

Seeded random generation (no extra dependency): arbitrary roster rows and
arbitrary nested JSON bodies, salted with credential-shaped keys in many
spellings, must never come out of the serialiser carrying one. Plus a drift
guard: every field services.roster.set_page writes is classified as either
public or private, so a new credential column cannot slip out by default.
"""

import random
import string

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import services.roster as roster
from services import json_store
from services.roster_public import (
    PUBLIC_PAGE_FIELDS,
    is_credential_key,
    public_page,
    scrub_credentials,
)
from routers import roster as roster_router

# Fields set_page persists that must never be served over HTTP.
PRIVATE_PAGE_FIELDS = frozenset({
    "signup_email",
    "password",
    "fwd_address",
    "email_alias",
    "email_rule_id",
    "fwd_destination",
    "notes",
})

CREDENTIAL_STEMS = (
    "password", "passwd", "secret", "token", "credential", "cookie", "session",
    "api_key", "apikey", "signup_email", "signupEmail", "fwd", "fwd_address",
    "forwarding_address", "forward_to", "email_alias", "emailAlias",
    "email_rule_id", "emailRuleId", "alias", "totp", "mfa", "2fa",
)


def _credential_variant(rng: random.Random) -> str:
    stem = rng.choice(CREDENTIAL_STEMS)
    stem = rng.choice([stem, stem.upper(), stem.title()])
    prefix = rng.choice(["", "account_", "tiktok", "x_", "old_"])
    suffix = rng.choice(["", "_2", "_hash", "Value", "_id"])
    return f"{prefix}{stem}{suffix}"


def _random_key(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase + "_") for _ in range(rng.randint(1, 12)))


def _random_scalar(rng: random.Random):
    return rng.choice([None, True, 0, 1.5, "", "x" * rng.randint(1, 8)])


def _random_json(rng: random.Random, depth: int = 0):
    if depth > 3 or rng.random() < 0.3:
        return _random_scalar(rng)
    if rng.random() < 0.5:
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    out = {}
    for _ in range(rng.randint(0, 6)):
        key = _credential_variant(rng) if rng.random() < 0.4 else _random_key(rng)
        out[key] = _random_json(rng, depth + 1)
    return out


def _keys_anywhere(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys_anywhere(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys_anywhere(item)


def test_every_credential_spelling_is_recognised():
    rng = random.Random(1)
    for _ in range(2000):
        key = _credential_variant(rng)
        assert is_credential_key(key), key
    for key in PRIVATE_PAGE_FIELDS:
        assert is_credential_key(key) or key == "notes", key


def test_public_fields_contain_no_credential_key():
    assert not [f for f in PUBLIC_PAGE_FIELDS if is_credential_key(f)]
    assert not PRIVATE_PAGE_FIELDS & set(PUBLIC_PAGE_FIELDS)


@pytest.mark.parametrize("seed", range(25))
def test_public_page_property(seed):
    rng = random.Random(seed)
    for _ in range(200):
        page = {}
        for field in rng.sample(PUBLIC_PAGE_FIELDS, rng.randint(0, len(PUBLIC_PAGE_FIELDS))):
            page[field] = _random_json(rng)
        for field in rng.sample(sorted(PRIVATE_PAGE_FIELDS), rng.randint(0, len(PRIVATE_PAGE_FIELDS))):
            page[field] = "leak"
        for _ in range(rng.randint(0, 5)):
            page[_credential_variant(rng) if rng.random() < 0.6 else _random_key(rng)] = "leak?"
        out = public_page(page)
        assert set(out) <= set(PUBLIC_PAGE_FIELDS)
        assert not PRIVATE_PAGE_FIELDS & set(out)
        assert not [k for k in out if is_credential_key(k)]
        # Nothing public is lost.
        for field in PUBLIC_PAGE_FIELDS:
            if field in page:
                assert out[field] == page[field]


@pytest.mark.parametrize("seed", range(25))
def test_scrub_credentials_property(seed):
    rng = random.Random(1000 + seed)
    for _ in range(200):
        body = _random_json(rng)
        cleaned = scrub_credentials(body)
        assert not [k for k in _keys_anywhere(cleaned) if is_credential_key(k)]
        # Idempotent, and a body with nothing to strip is returned unchanged.
        assert scrub_credentials(cleaned) == cleaned


def test_scrub_keeps_dedup_counters():
    body = {"entries": [{"inventory_forwarded": 2, "inventory_pending": 1, "topic_id": 5}]}
    assert scrub_credentials(body) == body


def test_every_persisted_roster_field_is_classified(monkeypatch, tmp_path):
    """Drift guard: a new set_page field must be declared public or private."""
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    entry = roster.set_page("probe", {})
    unclassified = set(entry) - set(PUBLIC_PAGE_FIELDS) - PRIVATE_PAGE_FIELDS
    assert not unclassified, (
        f"services/roster.py set_page writes {sorted(unclassified)}; add each to "
        "PUBLIC_PAGE_FIELDS (services/roster_public.py) or PRIVATE_PAGE_FIELDS here"
    )
    # And every private field really is persisted (the list is not stale).
    assert PRIVATE_PAGE_FIELDS <= set(entry)


def test_route_guard_backstops_a_route_that_forgets_public_page():
    """Any route on the roster router's route class is scrubbed, even one added later."""
    guarded = APIRouter(route_class=roster_router._CredentialGuardRoute)

    @guarded.get("/raw")
    async def raw():
        return {
            "page": {"integration_id": "p", "password": "leak", "signup_email": "leak"},
            "pages": [{"name": "n", "email_rule_id": "leak", "fwd_destination": "leak"}],
            "count": 1,
        }

    app = FastAPI()
    app.include_router(guarded)
    r = TestClient(app).get("/raw")
    assert r.status_code == 200
    assert r.json() == {"page": {"integration_id": "p"}, "pages": [{"name": "n"}], "count": 1}
    assert "leak" not in r.text


def test_roster_router_uses_the_guard_route_class():
    assert roster_router.router.routes
    for route in roster_router.router.routes:
        assert isinstance(route, roster_router._CredentialGuardRoute), route.path
