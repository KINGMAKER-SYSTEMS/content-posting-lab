"""
Cloudflare Email Routing router.
Proxies CF Email Routing API for creating/managing forwarding rules
and destination addresses.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.email_routing import (
    add_destination,
    create_rule,
    delete_rule,
    get_config,
    list_destinations,
    list_rules,
    update_rule,
)
from services.roster import set_page, get_page

router = APIRouter()


def _require_configured():
    cfg = get_config()
    if not cfg["configured"]:
        raise HTTPException(status_code=503, detail="CF Email Routing not configured")
    return cfg


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


@router.get("/rules")
async def get_rules():
    """List all email routing rules."""
    _require_configured()
    try:
        rules = await list_rules()
        return {"rules": rules}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class CreateRuleRequest(BaseModel):
    alias: str
    destination: str
    integration_id: str | None = None


@router.post("/rules")
async def create_email_rule(req: CreateRuleRequest):
    """Create a new email routing rule and optionally link to roster page."""
    cfg = _require_configured()
    try:
        rule = await create_rule(req.alias, req.destination)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Link to roster page if integration_id provided
    if req.integration_id:
        page = get_page(req.integration_id)
        if page:
            set_page(req.integration_id, {
                "email_alias": f"{req.alias}@{cfg['domain']}",
                "email_rule_id": rule.get("id", ""),
                "fwd_destination": req.destination,
            })

    return {"rule": rule}


class UpdateRuleRequest(BaseModel):
    alias: str
    destination: str
    enabled: bool = True


@router.put("/rules/{rule_id}")
async def update_email_rule(rule_id: str, req: UpdateRuleRequest):
    """Update an existing email routing rule."""
    _require_configured()
    try:
        rule = await update_rule(rule_id, req.alias, req.destination, req.enabled)
        return {"rule": rule}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/rules/{rule_id}")
async def delete_email_rule(rule_id: str, integration_id: str | None = None):
    """Delete an email routing rule and optionally unlink from roster page."""
    _require_configured()
    try:
        await delete_rule(rule_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Unlink from roster page
    if integration_id:
        page = get_page(integration_id)
        if page:
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

    try:
        rule = await create_rule(alias, req.destination)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

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
    try:
        destinations = await list_destinations()
        return {"destinations": destinations}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class AddDestinationRequest(BaseModel):
    email: str


@router.post("/destinations")
async def add_destination_address(req: AddDestinationRequest):
    """Add a new destination address (triggers CF verification email)."""
    _require_configured()
    try:
        dest = await add_destination(req.email)
        return {"destination": dest}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
