"""What the /api/roster HTTP surface may say about a page, and who may change it.

The roster cache (services/roster.py) holds account credentials for every
page: the Notion signup email and password, the forwarding address, and the
Cloudflare Email Routing alias/rule/destination. Other parts of the Lab read
them from disk. The /api/roster routes, however, answer an operator UI that
sends no credential at all and are reachable from the public internet
whenever APP_API_KEY is unset (as in production). So:

* Every row leaves through ``public_page`` -- an allowlist. A field that is
  not named in ``PUBLIC_PAGE_FIELDS`` never crosses, including fields added to
  the roster cache later.
* The whole response body then passes ``scrub_credentials``, which removes any
  credential-shaped key at any depth (including free-text ``notes``) under
  camelCase, kebab, dotted or upper-case spellings. It is a key-name backstop
  for a route that forgets ``public_page``, not a replacement for it: a
  credential value under an innocent key is only stopped by the allowlist.
* ``require_roster_auth`` guards routes no unauthenticated UI calls. It accepts
  the Control Plane bearer (CONTROL_PLANE_TOKEN) or the app key (APP_API_KEY)
  and fails closed (503) when neither is configured.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from typing import Any, Callable

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

# Safe roster row fields. Mirrors the ontology the Control Plane snapshot
# already carries (routers/control_plane.py ROSTER_SNAPSHOT_FIELDS) plus the
# UI bookkeeping the Distribution/Pipeline screens render.
PUBLIC_PAGE_FIELDS: tuple[str, ...] = (
    "integration_id",
    "name",
    "provider",
    "picture",
    "project",
    "drive_folder_url",
    "drive_folder_id",
    "source",
    "tiktok_url",
    "poster_name",
    "group",
    "group_label",
    "account_type",
    "notion_page_id",
    "status",
    "pipeline",
    "page_type",
    "content_niche",
    "content_engine",
    "automation_mode",
    "vault_url",
    "account_status",
    "archived",
    "sounds_reference",
    "go_live_date",
    "r2_prefix",
    "r2_bucket",
    "added_at",
    "updated_at",
    # Enrichment added by GET /api/roster/project/{name}.
    "has_staging_topic",
    "staging_topic_name",
)

# Keys that carry, or point at, an account credential (or free text that may
# hold one). A key is normalised first -- camelCase split, every separator
# folded to "_", lowercased -- so `signupEmail`, `SIGNUP-EMAIL`, `signup.email`
# and `signup_email` are the same key. Long unambiguous stems match anywhere
# in the normalised key; short stems (`pw`, `pin`, `otp`, ...) only as a whole
# `_`-delimited word, so `pwa_manifest` or `pinned` are not caught.
_CREDENTIAL_SUBSTRINGS = (
    "passw", "passcode", "passphrase", "secret", "token", "credential",
    "cookie", "session", "apikey", "api_key", "email", "e_mail", "mail_address",
    "fwd", "forward", "alias", "notes", "recovery", "backup_code",
    "login", "signin", "sign_in", "totp", "mfa", "2fa", "private_key",
    "access_key",
)
_CREDENTIAL_WORDS = frozenset({
    "pw", "pwd", "pass", "pin", "otp", "note", "auth", "mail",
})

# Keys that merely contain a credential word but name something else. Their
# values are still scrubbed recursively.
#   inventory_forwarded     dedup/telegram count, not an address
#   cookie_status           "valid"/"missing" upload-cookie state, not the cookie
#   cf_alias, notion_email_writeback
#                           pipeline setup step names; values are ok/reason dicts
_CREDENTIAL_KEY_ALLOW = frozenset({
    "inventory_forwarded",
    "cookie_status",
    "cf_alias",
    "notion_email_writeback",
})

_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


def _normalise_key(key: str) -> str:
    split = _CAMEL_RE.sub("_", key).lower()
    return _SEPARATOR_RE.sub("_", split).strip("_")


def is_credential_key(key: object) -> bool:
    """True when a response key names a credential and must not be serialised."""
    if not isinstance(key, str):
        return False
    norm = _normalise_key(key)
    if norm in _CREDENTIAL_KEY_ALLOW:
        return False
    if any(stem in norm for stem in _CREDENTIAL_SUBSTRINGS):
        return True
    # A trailing version digit does not change the word: `pass2`, `pin1`.
    return any(
        word in _CREDENTIAL_WORDS or word.rstrip("0123456789") in _CREDENTIAL_WORDS
        for word in norm.split("_")
    )


def public_page(page: dict[str, Any]) -> dict[str, Any]:
    """Reduce one roster row to the fields the HTTP surface may carry."""
    allowed = set(PUBLIC_PAGE_FIELDS)
    return {
        key: value
        for key, value in page.items()
        if key in allowed and not is_credential_key(key)
    }


def public_pages(pages: Any) -> list[dict[str, Any]]:
    return [public_page(page) for page in (pages or []) if isinstance(page, dict)]


def _is_presence_flag(key: object, value: Any) -> bool:
    """`has_email_alias: true` says a credential exists, not what it is."""
    return isinstance(key, str) and key.startswith("has_") and isinstance(value, bool)


def scrub_credentials(value: Any, keep: frozenset[str] = frozenset()) -> Any:
    """Return ``value`` with every credential-shaped dict key removed, at any depth.

    ``keep`` names keys a route is explicitly allowed to serialise (see
    ``allow_credential_keys``); boolean ``has_*`` presence flags always stay.
    """
    if isinstance(value, dict):
        return {
            key: scrub_credentials(item, keep)
            for key, item in value.items()
            if key in keep or _is_presence_flag(key, item) or not is_credential_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [scrub_credentials(item, keep) for item in value]
    return value


_ALLOWED_KEYS_ATTR = "__credential_keys_allowed__"


def allow_credential_keys(*keys: str) -> Callable:
    """Let one endpoint serialise the named credential-shaped keys.

    Only for values the caller already holds or just created in the same
    request (a freshly minted alias echoed to the operator who minted it, the
    destination inbox the operator typed) -- never a pre-existing roster
    credential. Every use is pinned by a test, so a new one is reviewed.
    """
    allowed = frozenset(keys)

    def mark(endpoint: Callable) -> Callable:
        setattr(endpoint, _ALLOWED_KEYS_ATTR, allowed)
        return endpoint

    return mark


def allowed_credential_keys(endpoint: Callable) -> frozenset[str]:
    return getattr(endpoint, _ALLOWED_KEYS_ATTR, frozenset())


class CredentialGuardRoute(APIRoute):
    """Backstop: strip credential-shaped keys from every JSON body a router returns.

    Routes already build their rows with public_page; this catches a route
    that forgets to, including routes added later. Install it with
    ``APIRouter(route_class=CredentialGuardRoute)``.
    """

    def get_route_handler(self):
        original = super().get_route_handler()
        keep = allowed_credential_keys(self.endpoint)

        async def guarded(request: Request) -> Response:
            response = await original(request)
            if not isinstance(response, JSONResponse):
                return response
            try:
                body = json.loads(response.body)
            except ValueError:
                return response
            cleaned = scrub_credentials(body, keep)
            if cleaned == body:
                return response
            return JSONResponse(
                content=cleaned,
                status_code=response.status_code,
                background=response.background,
            )

        return guarded


def _configured_tokens() -> list[bytes]:
    tokens = []
    for name in ("CONTROL_PLANE_TOKEN", "APP_API_KEY"):
        token = (os.getenv(name) or "").strip()
        if token:
            tokens.append(token.encode("utf-8"))
    return tokens


def require_roster_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: the caller must present a configured machine credential."""
    tokens = _configured_tokens()
    if not tokens:
        raise HTTPException(status_code=503, detail="Roster authentication is not configured")
    supplied: list[bytes] = []
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        supplied.append(authorization.removeprefix("Bearer ").strip().encode("utf-8"))
    if isinstance(x_api_key, str) and x_api_key.strip():
        supplied.append(x_api_key.strip().encode("utf-8"))
    for candidate in supplied:
        for token in tokens:
            if hmac.compare_digest(candidate, token):
                return None
    raise HTTPException(status_code=401, detail="Invalid or missing roster credential")
