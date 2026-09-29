"""
Cloudflare Email Routing router.
Proxies CF Email Routing API for creating/managing forwarding rules
and destination addresses.

Credential boundary: roster rows leave through public_page and every JSON
body through the CredentialGuardRoute scrub. The alias an auto-create just
minted and the team's verified destination inboxes (which the operator picks
from to mint) are the only credential-shaped keys served, via
allow_credential_keys; a page's own alias/rule/signup/password never are.

Every route that can change where a page's mail goes -- adding a destination,
deleting a forwarding rule, (re)creating a page alias -- requires the machine
credential (``require_control_plane_auth``). Unauthenticated, those three
routes chained into an account takeover: add and verify an attacker address,
delete a page's rule, recreate the same alias forwarding to the attacker, and
receive the page's TikTok password-reset mail.
"""

import logging
import os
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.email_routing import (
    add_destination,
    create_rule,
    delete_rule,
    get_config,
    list_destinations,
    list_rules,
)
from services.roster import get_page, list_all_pages, set_page
from services.roster_public import (
    CredentialGuardRoute,
    allow_credential_keys,
    public_page,
    require_control_plane_auth,
)

logger = logging.getLogger(__name__)


def _public_or_none(page: dict | None) -> dict | None:
    return public_page(page) if page else None


def _destination_email(d: dict) -> str:
    return (d.get("email") or "").strip().lower()


def _allowed_destination_domains() -> set[str]:
    """Domains from EMAIL_DESTINATION_DOMAINS (comma-separated); empty set = unrestricted."""
    raw = os.getenv("EMAIL_DESTINATION_DOMAINS") or ""
    return {
        part.strip().lower().lstrip("@")
        for part in raw.split(",")
        if part.strip().lstrip("@")
    }


def _normalise_destination(email: str) -> str:
    """Return the one canonical form of a destination address, or 400.

    Stripped, ASCII only (no Unicode that lowercases into an allowed domain,
    e.g. the Kelvin sign), no whitespace, exactly one '@' with a non-empty
    local part and domain, lowercased. Callers validate AND send this value,
    so what the allowlist checks is exactly what Cloudflare receives.
    """
    address = (email or "").strip()
    if not address.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in address):
        raise HTTPException(status_code=400, detail="Destination must be a plain ASCII email address")
    if address.count("@") != 1:
        raise HTTPException(status_code=400, detail="Destination must contain exactly one '@'")
    local, _, domain = address.partition("@")
    if not local or not domain:
        raise HTTPException(status_code=400, detail="Destination must have a local part and a domain")
    return address.lower()


def _require_allowed_destination(email: str) -> str:
    """Normalise a forwarding destination and enforce EMAIL_DESTINATION_DOMAINS.

    Returns the normalised address, which is the value to send to Cloudflare.
    Exact domain match (no subdomains). Unset means no restriction beyond auth,
    which is logged so the gap stays visible.
    """
    address = _normalise_destination(email)
    allowed = _allowed_destination_domains()
    if not allowed:
        logger.warning(
            "EMAIL_DESTINATION_DOMAINS is unset; email forwarding destinations are "
            "not domain-restricted (auth still required)"
        )
        return address
    domain = address.partition("@")[2]
    if domain not in allowed:
        raise HTTPException(
            status_code=403,
            detail="Destination domain is not in EMAIL_DESTINATION_DOMAINS",
        )
    return address


router = APIRouter(route_class=CredentialGuardRoute)


def _require_configured():
    cfg = get_config()
    if not cfg["configured"]:
        raise HTTPException(status_code=503, detail="CF Email Routing not configured")
    return cfg


@contextmanager
def _cf_upstream():
    """Translate any failure from the Cloudflare API client into a 502.

    Centralizes the bare-exception → HTTP 502 conversion shared by every
    endpoint that proxies the CF Email Routing API.
    """
    try:
        yield
    except HTTPException:
        # Already a deliberate HTTP error — don't mask it as a 502.
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Status ───────────────────────────────────────────────────────────────────


@router.get("/status")
async def email_status():
    """Check if CF Email Routing is configured."""
    cfg = get_config()
    return {
        "configured": cfg["configured"],
        "domain": cfg["domain"] if cfg["configured"] else None,
    }


# ── Rules ────────────────────────────────────────────────────────────────────


@router.delete("/rules/{rule_id}", dependencies=[Depends(require_control_plane_auth)])
async def delete_email_rule(rule_id: str, integration_id: str | None = None):
    """Delete an email routing rule and optionally unlink from roster page."""
    _require_configured()

    # When an integration_id is supplied, the caller is asking us to unlink a
    # specific page. Validate that the rule actually belongs to that page BEFORE
    # deleting anything — otherwise a mismatched rule_id/integration_id pair
    # silently nukes the wrong CF rule and leaves the page's real rule orphaned.
    page = None
    if integration_id:
        page = get_page(integration_id)
        if page and page.get("email_rule_id") not in (None, "", rule_id):
            # Generic on purpose: never echo the page's linked rule id.
            raise HTTPException(
                status_code=409,
                detail="rule_id does not belong to this page; nothing was deleted",
            )

    with _cf_upstream():
        await delete_rule(rule_id)

    # Unlink from roster page
    if integration_id and page:
        set_page(integration_id, {
            "email_alias": None,
            "email_rule_id": None,
            "fwd_destination": None,
        })

    return {"deleted": True}


# ── Auto-create for roster page ──────────────────────────────────────────────


class AutoCreateRequest(BaseModel):
    integration_id: str
    account_name: str
    destination: str
    # Explicitly allow re-pointing a page whose roster row already records a
    # different alias/destination. Never implied.
    replace: bool = False


def _same_forwarding(page: dict, full_alias: str, destination: str) -> bool:
    return (
        (page.get("email_alias") or "").strip().lower() == full_alias.lower()
        and (page.get("fwd_destination") or "").strip().lower() == destination
    )


def _refuse_silent_repoint(req: AutoCreateRequest, full_alias: str, destination: str) -> None:
    """409 when this would re-point mail a roster row already owns, unless replace.

    Covers both the named page (it already has an alias) and any other roster
    page recorded with this same alias -- the alias is derived from
    account_name, so a caller could otherwise target a victim page's alias
    under an unrelated integration_id. Recreating the identical
    alias -> destination pair is allowed (healing a deleted rule).
    """
    if req.replace:
        return
    page = get_page(req.integration_id)
    if page and (page.get("email_alias") or "").strip():
        if not _same_forwarding(page, full_alias, destination):
            raise HTTPException(
                status_code=409,
                detail="Page already has an email alias; pass replace=true to re-point it",
            )
    for other in list_all_pages():
        if (other.get("email_alias") or "").strip().lower() != full_alias.lower():
            continue
        if not _same_forwarding(other, full_alias, destination):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Alias is recorded for another roster page with a different "
                    "destination; pass replace=true to re-point it"
                ),
            )


@router.post("/auto-create", dependencies=[Depends(require_control_plane_auth)])
@allow_credential_keys("alias")  # the alias this request just created
async def auto_create_for_page(req: AutoCreateRequest):
    """Auto-generate an email alias for a roster page and create the CF rule.

    Generates alias from account_name (lowercased, alphanumeric + hyphens).
    Machine credential required. Refuses destinations outside
    EMAIL_DESTINATION_DOMAINS (when set) and refuses to silently re-point an
    alias a roster page already records unless ``replace`` is true.
    """
    cfg = _require_configured()
    destination = _require_allowed_destination(req.destination)

    # Sanitize account name to valid email local part
    alias = "".join(c if c.isalnum() or c in "-_." else "-" for c in req.account_name.lower()).strip("-.")
    if not alias:
        raise HTTPException(status_code=400, detail="Invalid account name for email alias")

    full_alias = f"{alias}@{cfg['domain']}"

    _refuse_silent_repoint(req, full_alias, destination)

    # Check if alias already exists
    existing_rules = await list_rules()
    for rule in existing_rules:
        for matcher in rule.get("matchers", []):
            if matcher.get("value") == full_alias:
                # Generic: never echo the alias (existence oracle).
                raise HTTPException(status_code=409, detail="Alias already exists")

    # Reject destinations CF hasn't verified — a rule pointing at an unverified
    # address silently drops mail, breaking the sale-intake handoff.
    destinations = await list_destinations()
    verified = {
        _destination_email(d)
        for d in destinations
        if d.get("verified")  # CF returns an ISO timestamp (truthy) when verified, null otherwise
    }
    if destination not in verified:
        raise HTTPException(
            status_code=422,
            detail=f"Destination {destination} is not a verified Cloudflare destination",
        )

    with _cf_upstream():
        rule = await create_rule(alias, destination)

    # Link to roster page
    page = get_page(req.integration_id)
    if page:
        set_page(req.integration_id, {
            "email_alias": full_alias,
            "email_rule_id": rule.get("id", ""),
            "fwd_destination": destination,
        })

    return {
        "rule": rule,
        "alias": full_alias,
        "page": _public_or_none(get_page(req.integration_id)),
    }


# ── Destinations ─────────────────────────────────────────────────────────────


@router.get("/destinations", dependencies=[Depends(require_control_plane_auth)])
@allow_credential_keys("email")  # team inboxes the operator mints aliases to
async def get_destinations():
    """List destination addresses (the team's real inboxes).

    Machine credential required: anonymously this served every forwarding
    inbox address to the public internet. The Distribution Email tab reads it
    without a credential and now shows an empty list; Pipeline mint-alias
    reads destinations server-side and is unaffected.
    """
    _require_configured()
    with _cf_upstream():
        destinations = await list_destinations()
    return {"destinations": destinations}


class AddDestinationRequest(BaseModel):
    email: str


@router.post("/destinations", dependencies=[Depends(require_control_plane_auth)])
@allow_credential_keys("email")  # the address this request just added
async def add_destination_address(req: AddDestinationRequest):
    """Add a new destination address (triggers CF verification email).

    Machine credential required; refuses domains outside
    EMAIL_DESTINATION_DOMAINS when that allowlist is set.
    """
    _require_configured()
    email = _require_allowed_destination(req.email)
    with _cf_upstream():
        dest = await add_destination(email)
    return {"destination": dest}
