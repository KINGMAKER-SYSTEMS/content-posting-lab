"""Property tests for the /api/roster response-shape guard (services/roster_public.py).

Seeded random generation (no extra dependency): arbitrary roster rows and
arbitrary nested JSON bodies, salted with credential-shaped keys in many
spellings, must never come out of the serialiser carrying one. Plus a drift
guard: every field services.roster.set_page writes is classified as either
public or private, so a new credential column cannot slip out by default.
"""

import random
import re
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

# Credential concepts as WORD LISTS, written independently of the matcher in
# services/roster_public.py (several were missed by the first version of it:
# pwd, pw, passcode, recovery code, backup code, notes, plain email, login).
CREDENTIAL_CONCEPTS = (
    ("password",), ("passwd",), ("pwd",), ("pw",), ("pass",), ("passcode",),
    ("passphrase",), ("pin",), ("secret",), ("client", "secret"), ("token",),
    ("access", "token"), ("refresh", "token"), ("credential",), ("credentials",),
    ("cookie",), ("cookies",), ("session",), ("session", "id"), ("api", "key"),
    ("apikey",), ("auth",), ("otp",), ("totp",), ("mfa",), ("2fa",),
    ("recovery", "code"), ("recovery", "codes"), ("recovery", "email"),
    ("backup", "code"), ("backup", "codes"), ("email",), ("e", "mail"),
    ("mail",), ("signup", "email"), ("login", "email"), ("login",),
    ("sign", "in", "email"), ("user", "email"), ("fwd",), ("fwd", "address"),
    ("forward", "to"), ("forwarding", "address"), ("forward", "destination"),
    ("email", "alias"), ("alias",), ("email", "rule", "id"), ("notes",),
    ("note",), ("private", "key"), ("access", "key"), ("secret", "key"),
)

# The same idea for keys the roster surface legitimately returns: styling a
# public field must never make it look like a credential.
SAFE_KEYS = tuple(PUBLIC_PAGE_FIELDS) + (
    "added", "updated", "total_in_notion", "errors", "pages", "page", "removed",
    "removed_names", "inventory_merged", "topics_cleaned", "remaining",
    "total_pages", "duplicate_names", "duplicates", "count", "entries",
    "has_topic", "topic_id", "topic_name", "inventory_total",
    "inventory_pending", "has_project", "has_drive", "deleted", "configured",
    # /api/pipeline and /api/email non-credential keys.
    "key", "r2_key", "file_id", "message_id", "topic_id", "chat_id", "url",
    "filename", "cookie_status", "cf_alias", "notion_email_writeback",
    "slack_handoff", "status_flip", "poster_assign", "telegram_topic",
    "r2_prefix", "notion_r2_writeback", "object_count", "destination",
    "domain", "rule", "steps", "completed", "reason", "skipped",
)

# The first shipped matcher, kept only to prove the generator below is not
# tautological: it must produce spellings this one misses.
_FIRST_MATCHER = re.compile(
    r"passw|secret|token|credential|cookie|session|api_?key|signup_?email|fwd"
    r"|forward(ing)?_?(address|destination|to)|email_?(alias|rule)|alias"
    r"|(^|_)otp($|_)|2fa|mfa|totp",
    re.IGNORECASE,
)


def _style(words, rng: random.Random) -> str:
    style = rng.randrange(8)
    if style == 0:
        return "_".join(words)
    if style == 1:
        return words[0] + "".join(w.title() for w in words[1:])
    if style == 2:
        return "".join(w.title() for w in words)
    if style == 3:
        return "-".join(words)
    if style == 4:
        return "_".join(words).upper()
    if style == 5:
        return ".".join(words)
    if style == 6:
        return " ".join(words)
    return "__".join(w.upper() if rng.random() < 0.5 else w for w in words)


def _credential_variant(rng: random.Random) -> str:
    words = list(rng.choice(CREDENTIAL_CONCEPTS))
    if rng.random() < 0.5:
        words.insert(0, rng.choice(["account", "tiktok", "x", "old", "user", "notion"]))
    if rng.random() < 0.5:
        words.append(rng.choice(["2", "hash", "value", "raw", "v1"]))
    return _style(words, rng)


def _safe_variant(rng: random.Random) -> str:
    return _style(rng.choice(SAFE_KEYS).split("_"), rng)


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
    for _ in range(5000):
        key = _credential_variant(rng)
        assert is_credential_key(key), key
    for key in PRIVATE_PAGE_FIELDS:
        assert is_credential_key(key), key


def test_credential_generator_is_not_tautological():
    """The generator must reach spellings the first matcher missed."""
    rng = random.Random(2)
    missed_by_first = {
        key for key in (_credential_variant(rng) for _ in range(5000))
        if not _FIRST_MATCHER.search(key)
    }
    assert len(missed_by_first) > 100
    for key in ("notes", "pwd", "recovery_code", "backupCodes", "loginEmail", "passcode"):
        assert not _FIRST_MATCHER.search(key)
        assert is_credential_key(key), key


def test_styled_safe_keys_are_never_scrubbed():
    rng = random.Random(3)
    for _ in range(5000):
        key = _safe_variant(rng)
        assert not is_credential_key(key), key


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


def test_scrub_removes_notes_at_any_depth():
    body = {"notes": "backup code 123456", "pages": [{"name": "n", "Notes": "x", "meta": {"note": "y"}}]}
    assert scrub_credentials(body) == {"pages": [{"name": "n", "meta": {}}]}


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


# ── /api/pipeline and /api/email use the same guard ──────────────────────────


def test_pipeline_and_email_routers_use_the_guard_route_class():
    from routers import email_routing, pipeline
    from services.roster_public import CredentialGuardRoute

    for module in (pipeline, email_routing):
        assert module.router.routes
        for route in module.router.routes:
            assert isinstance(route, CredentialGuardRoute), (module.__name__, route.path)


def test_credential_key_allowances_are_pinned():
    """Every route that may serialise a credential-shaped key is listed here.

    Adding one is a security decision: it must echo only what the request
    itself created or the operator already holds, never a stored credential.
    """
    from routers import email_routing, pipeline, roster as roster_routes
    from services.roster_public import allowed_credential_keys

    found = {}
    for prefix, module in (("/api/pipeline", pipeline), ("/api/email", email_routing), ("/api/roster", roster_routes)):
        for route in module.router.routes:
            keys = allowed_credential_keys(route.endpoint)
            if keys:
                for method in route.methods:
                    found[(method, prefix + route.path)] = keys
    assert found == {
        ("POST", "/api/pipeline/mint-alias"): frozenset({"alias"}),
        ("POST", "/api/pipeline/intake"): frozenset({"email_alias", "fwd_destination"}),
        ("POST", "/api/email/auto-create"): frozenset({"alias"}),
        ("GET", "/api/email/destinations"): frozenset({"email"}),
        ("POST", "/api/email/destinations"): frozenset({"email"}),
    }


def test_presence_flags_survive_the_scrub_but_values_do_not():
    body = {"has_email_alias": True, "has_password": False, "has_email_alias_value": "x@y", "password": "p"}
    assert scrub_credentials(body) == {"has_email_alias": True, "has_password": False}
