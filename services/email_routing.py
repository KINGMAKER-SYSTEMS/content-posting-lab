"""
Cloudflare Email Routing API client.
Manages email forwarding rules via the CF REST API.

Env vars required:
  CF_API_TOKEN   — API token with Email Routing permissions
  CF_ZONE_ID     — Zone ID for the domain
  CF_ACCOUNT_ID  — Account ID
  CF_EMAIL_DOMAIN — Domain for email aliases (e.g. yourdomain.com)
"""

import os

import httpx

CF_API = "https://api.cloudflare.com/client/v4"


def _config() -> dict:
    """Return CF config from env. Raises ValueError if missing."""
    token = os.getenv("CF_API_TOKEN", "")
    zone = os.getenv("CF_ZONE_ID", "")
    account = os.getenv("CF_ACCOUNT_ID", "")
    domain = os.getenv("CF_EMAIL_DOMAIN", "")
    return {
        "token": token,
        "zone_id": zone,
        "account_id": account,
        "domain": domain,
        "configured": bool(token and zone and account and domain),
    }


def get_config() -> dict:
    return _config()


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def list_rules() -> list[dict]:
    """List all email routing rules for the zone."""
    cfg = _config()
    if not cfg["configured"]:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CF_API}/zones/{cfg['zone_id']}/email/routing/rules",
            headers=_headers(cfg["token"]),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])


async def create_rule(alias: str, destination: str) -> dict:
    """Create an email routing rule: alias@domain -> destination."""
    cfg = _config()
    if not cfg["configured"]:
        raise ValueError("CF Email Routing not configured")

    payload = {
        "actions": [
            {
                "type": "forward",
                "value": [destination],
            }
        ],
        "matchers": [
            {
                "type": "literal",
                "field": "to",
                "value": f"{alias}@{cfg['domain']}",
            }
        ],
        "enabled": True,
        "name": f"Roster: {alias}",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CF_API}/zones/{cfg['zone_id']}/email/routing/rules",
            headers=_headers(cfg["token"]),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {})


async def update_rule(rule_id: str, alias: str, destination: str, enabled: bool = True) -> dict:
    """Update an existing email routing rule."""
    cfg = _config()
    if not cfg["configured"]:
        raise ValueError("CF Email Routing not configured")

    payload = {
        "actions": [
            {
                "type": "forward",
                "value": [destination],
            }
        ],
        "matchers": [
            {
                "type": "literal",
                "field": "to",
                "value": f"{alias}@{cfg['domain']}",
            }
        ],
        "enabled": enabled,
        "name": f"Roster: {alias}",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{CF_API}/zones/{cfg['zone_id']}/email/routing/rules/{rule_id}",
            headers=_headers(cfg["token"]),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {})


async def delete_rule(rule_id: str) -> bool:
    """Delete an email routing rule."""
    cfg = _config()
    if not cfg["configured"]:
        raise ValueError("CF Email Routing not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{CF_API}/zones/{cfg['zone_id']}/email/routing/rules/{rule_id}",
            headers=_headers(cfg["token"]),
        )
        resp.raise_for_status()
        return True


async def list_destinations() -> list[dict]:
    """List verified destination addresses for the account."""
    cfg = _config()
    if not cfg["configured"]:
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CF_API}/accounts/{cfg['account_id']}/email/routing/addresses",
            headers=_headers(cfg["token"]),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])


async def add_destination(email: str) -> dict:
    """Add a new destination address (triggers verification email from CF)."""
    cfg = _config()
    if not cfg["configured"]:
        raise ValueError("CF Email Routing not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CF_API}/accounts/{cfg['account_id']}/email/routing/addresses",
            headers=_headers(cfg["token"]),
            json={"email": email},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {})
