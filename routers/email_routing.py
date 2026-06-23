"""
Cloudflare Email Routing router.
Proxies CF Email Routing API for creating/managing forwarding rules
and destination addresses.
"""

from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.email_routing import (
    add_destination,
    create_rule,
    delete_rule,
    get_config,
    list_destinations,
    list_rules,
)
from services.roster import get_page, set_page


def _destination_email(d: dict) -> str:
    return (d.get("email") or "").strip().lower()


router = APIRouter()


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


@router.delete("/rules/{rule_id}")
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
            raise HTTPException(
                status_code=409,
                detail=(
                    f"rule_id {rule_id} does not belong to page {integration_id} "
                    f"(linked rule is {page.get('email_rule_id')})"
                ),
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


@router.post("/auto-create")
async def auto_create_for_page(req: AutoCreateRequest):
    """Auto-generate an email alias for a roster page and create the CF rule.

    Generates alias from account_name (lowercased, alphanumeric + hyphens).
    """
    cfg = _require_configured()

    # Sanitize account name to valid email local part
    alias = "".join(c if c.isalnum() or c in "-_." else "-" for c in req.account_name.lower()).strip("-.")
    if not alias:
        raise HTTPException(status_code=400, detail="Invalid account name for email alias")

    full_alias = f"{alias}@{cfg['domain']}"

    # Check if alias already exists
    existing_rules = await list_rules()
    for rule in existing_rules:
        for matcher in rule.get("matchers", []):
            if matcher.get("value") == full_alias:
                raise HTTPException(status_code=409, detail=f"Alias {full_alias} already exists")

    # Reject destinations CF hasn't verified — a rule pointing at an unverified
    # address silently drops mail, breaking the sale-intake handoff.
    destinations = await list_destinations()
    verified = {
        _destination_email(d)
        for d in destinations
        if d.get("verified")  # CF returns an ISO timestamp (truthy) when verified, null otherwise
    }
    if req.destination.strip().lower() not in verified:
        raise HTTPException(
            status_code=422,
            detail=f"Destination {req.destination} is not a verified Cloudflare destination",
        )

    with _cf_upstream():
        rule = await create_rule(alias, req.destination)

    # Link to roster page
    page = get_page(req.integration_id)
    if page:
        set_page(req.integration_id, {
            "email_alias": full_alias,
            "email_rule_id": rule.get("id", ""),
            "fwd_destination": req.destination,
        })

    return {
        "rule": rule,
        "alias": full_alias,
        "page": get_page(req.integration_id),
    }


# ── Destinations ─────────────────────────────────────────────────────────────


@router.get("/destinations")
async def get_destinations():
    """List verified destination addresses."""
    _require_configured()
    with _cf_upstream():
        destinations = await list_destinations()
    return {"destinations": destinations}


class AddDestinationRequest(BaseModel):
    email: str


@router.post("/destinations")
async def add_destination_address(req: AddDestinationRequest):
    """Add a new destination address (triggers CF verification email)."""
    _require_configured()
    with _cf_upstream():
        dest = await add_destination(req.email)
    return {"destination": dest}
