"""
Pipeline router — page sale handoff operations.

Reads roster pages grouped by Notion `Status`, exposes per-stage actions
(setup, transition, health). Notion is the canonical source; all status
changes also write back to Notion.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import r2
from services.slack import post_flow_stage_handoff, is_configured as slack_configured
from services.email_send import destination_for_pipeline
from services.email_routing import (
    create_rule as cf_create_rule,
    get_config as cf_get_config,
    list_rules as cf_list_rules,
)
from services.notion_pages import (
    create_intake_page,
    is_configured as notion_configured,
    sync_into_roster,
    update_page_drive_folder,
    update_page_email_fields,
    update_page_status,
)
from services.poster_router import resolve_poster_for_page
from services.roster import get_page, list_all_pages, set_page
from services.telegram import (
    assign_page_to_poster,
    get_poster,
)
from services.upload import get_cookie_status

logger = logging.getLogger(__name__)

router = APIRouter()


# Default password set on new intake rows. Eric/Glitch uses this when
# creating the TikTok account. We'll figure out per-account password
# management later.
DEFAULT_INTAKE_PASSWORD = "Risingtides123$"


# Pipeline stages — must match the Notion Status select values exactly.
PIPELINE_STAGES = [
    "New — Pending Setup",
    "In Production",
    "Delivered to Poster",
    "Live",
    "Complete",
]


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _slug_alias(username: str) -> str:
    s = username.lower().strip()
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in s).strip("-.")
    return cleaned or "unknown"


def _random_alias_local() -> str:
    """Generate a random local-part for a CF email alias.

    We use this instead of a username-based slug because the TikTok handle
    may not be available — the user picks their handle on TikTok AFTER the
    email is minted. The email is the immutable identity; the handle can
    differ.

    Format: `acct-{8 chars}` (e.g. `acct-7gx2k4mz`)
    """
    import secrets
    import string
    rand = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    return f"acct-{rand}"


async def _mint_random_alias(
    pipeline: str | None = None,
    destination_override: str | None = None,
    desired_local: str | None = None,
) -> dict[str, Any]:
    """Mint a CF email alias.

    If `desired_local` is given, uses that as the local-part (e.g. "samb-truck-04"
    becomes "samb-truck-04@risingtidesviral.com"). If it collides with an
    existing alias, returns 409. Otherwise generates a random "acct-XXXX" local.

    Destination routing:
      - If destination_override is provided, use it (must be verified on CF)
      - Else if pipeline is "Flow Stage" → EMAIL_HANDOFF_TO_FLOW_STAGE
      - Else if pipeline is "King Maker Tech" → EMAIL_HANDOFF_TO_KING_MAKER
      - Else fall back to the first verified destination on the account
    """
    cfg = cf_get_config()
    if not cfg["configured"]:
        raise HTTPException(
            status_code=503,
            detail="CF Email Routing not configured",
        )

    # Pull verified destinations (we always need this list for validation)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/email/routing/addresses",
                headers={"Authorization": f"Bearer {cfg['token']}"},
            )
            resp.raise_for_status()
            dests = resp.json().get("result", [])
        verified = [d for d in dests if d.get("verified")]
        if not verified:
            raise HTTPException(
                status_code=503,
                detail="No verified destination addresses on Cloudflare",
            )
        verified_emails = {d["email"].lower() for d in verified}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CF destinations fetch failed: {exc}")

    # Resolve destination
    desired = destination_override or destination_for_pipeline(pipeline)
    if desired:
        if desired.lower() not in verified_emails:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Destination '{desired}' is not verified on Cloudflare. "
                    f"Add it at Cloudflare → Email → Email Routing → Destination Addresses, "
                    f"verify the link in the inbox, then try again."
                ),
            )
        destination = desired
    else:
        destination = verified[0]["email"]

    # Try a few times in the (very rare) case of collision
    try:
        existing_rules = await cf_list_rules()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CF rules list failed: {exc}")

    existing_aliases = {
        m.get("value")
        for r in existing_rules
        for m in r.get("matchers", [])
    }

    # Resolve the local-part: user-chosen (sanitized) takes priority,
    # else generate a random one with retry-on-collision.
    if desired_local and desired_local.strip():
        alias_local = _slug_alias(desired_local)
        if not alias_local:
            raise HTTPException(
                status_code=400,
                detail="Email name must contain at least one alphanumeric character",
            )
        full_alias = f"{alias_local}@{cfg['domain']}"
        if full_alias in existing_aliases:
            raise HTTPException(
                status_code=409,
                detail=f"Email '{full_alias}' is already taken — pick a different name",
            )
    else:
        alias_local = _random_alias_local()
        full_alias = f"{alias_local}@{cfg['domain']}"
        attempts = 0
        while full_alias in existing_aliases and attempts < 5:
            alias_local = _random_alias_local()
            full_alias = f"{alias_local}@{cfg['domain']}"
            attempts += 1

        if full_alias in existing_aliases:
            raise HTTPException(
                status_code=500,
                detail="Couldn't find a free random alias after 5 tries (very rare — try again)",
            )

    try:
        rule = await cf_create_rule(alias_local, destination)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CF rule create failed: {exc}")

    return {
        "alias": full_alias,
        "rule_id": rule.get("id", ""),
        "destination": destination,
    }


async def _mint_email_for_username(username: str, pipeline: str | None = None) -> dict[str, Any]:
    """[LEGACY] Mint a CF email alias from a username slug. Kept for the
    Run Setup chain's fallback path on legacy/migrated pages that didn't
    go through the new intake flow. New intakes should use _mint_random_alias.

    Forwarding destination routes by pipeline (Flow Stage → Jay,
    King Maker → Glitch) — same as _mint_random_alias.
    """
    cfg = cf_get_config()
    if not cfg["configured"]:
        raise HTTPException(
            status_code=503,
            detail="CF Email Routing not configured",
        )

    alias_local = _slug_alias(username)
    full_alias = f"{alias_local}@{cfg['domain']}"

    # Pull verified destinations
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}/email/routing/addresses",
                headers={"Authorization": f"Bearer {cfg['token']}"},
            )
            resp.raise_for_status()
            dests = resp.json().get("result", [])
        verified = [d for d in dests if d.get("verified")]
        if not verified:
            raise HTTPException(
                status_code=503,
                detail="No verified destination addresses on Cloudflare",
            )
        verified_emails = {d["email"].lower() for d in verified}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CF destinations fetch failed: {exc}")

    desired = destination_for_pipeline(pipeline)
    if desired:
        if desired.lower() not in verified_emails:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Destination '{desired}' is not verified on Cloudflare. "
                    f"Add it at Cloudflare → Email → Email Routing → Destination Addresses, "
                    f"verify the link, then try again."
                ),
            )
        destination = desired
    else:
        destination = verified[0]["email"]

    # Collision check
    try:
        existing_rules = await cf_list_rules()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CF rules list failed: {exc}")

    if any(
        m.get("value") == full_alias
        for r in existing_rules
        for m in r.get("matchers", [])
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Alias {full_alias} already exists on Cloudflare",
        )

    # Create the rule
    try:
        rule = await cf_create_rule(alias_local, destination)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CF rule create failed: {exc}")

    return {
        "alias": full_alias,
        "rule_id": rule.get("id", ""),
        "destination": destination,
    }


# ── Stages overview ──────────────────────────────────────────────────────────


class IntakeRequest(BaseModel):
    account_username: str
    # Email alias was already minted in step 1 (mint-alias) before the TikTok
    # signup. Frontend passes it back here so we don't re-mint on submit.
    email_alias: str | None = None
    fwd_destination: str | None = None
    label_artist: str | None = None
    pipeline_choice: str | None = None  # "Flow Stage" | "King Maker Tech"
    page_type: str | None = None        # "Lyric page" | "UGC page" | "Artist burner page"
    sounds_reference: str | None = None
    notes: str | None = None
    poster: str | None = None
    go_live_date: str | None = None     # ISO date "YYYY-MM-DD"
    group: str | None = None            # "ATLANTIC" | "WARNER" | "INTERNAL"
    group_label: str | None = None      # "Sam Barber (Atlantic)" / "Warner UGC" / etc
    account_type: str | None = None     # "TRUCK" | "POV" | etc


class MintAliasRequest(BaseModel):
    # Optional — routes the forwarding destination based on which pipeline
    # owns this account. Flow Stage → Jay, King Maker → Glitch.
    pipeline: str | None = None
    # Optional — if provided, this becomes the local-part of the email
    # (e.g. "samb-truck-04" → "samb-truck-04@risingtidesviral.com").
    # If omitted, a random "acct-XXXXXXXX" local is generated.
    desired_local: str | None = None


class MintAliasResponse(BaseModel):
    alias: str
    destination: str


@router.post("/mint-alias")
async def mint_random_alias_endpoint(req: MintAliasRequest | None = None) -> MintAliasResponse:
    """Step 1 of intake: mint a CF email alias before user goes to TikTok.

    This decouples the email from the TikTok handle — the user picks whatever
    handle is available on TikTok; we just need to give them an email to sign
    up with. The forwarding destination is chosen based on pipeline so that
    TikTok verification emails go to the right person (Jay vs Glitch).

    The user can also specify a custom name (`desired_local`) so emails are
    human-readable in the inbox (e.g. "samb-truck-04@..." instead of random).
    """
    pipeline = (req.pipeline if req else None) or None
    desired_local = (req.desired_local if req else None) or None
    info = await _mint_random_alias(pipeline=pipeline, desired_local=desired_local)
    return MintAliasResponse(alias=info["alias"], destination=info["destination"])


@router.post("/intake")
async def submit_intake(req: IntakeRequest):
    """Create a new Master Pages row from the intake form.

    Sets Status = 'New — Pending Setup' so it lands in lane 1 of the Pipeline.
    Then re-syncs the roster so the new card shows up immediately.
    """
    if not notion_configured():
        raise HTTPException(
            status_code=503,
            detail="Notion not configured — set NOTION_API_KEY and NOTION_PAGES_DB in .env",
        )
    if not req.account_username.strip():
        raise HTTPException(status_code=400, detail="account_username is required")

    # Email alias is expected to have been minted in step 1 (POST /mint-alias)
    # before the user did the TikTok signup. If it's missing here, mint one
    # now as a fallback (covers cases where step 1 was skipped or failed).
    email_alias = (req.email_alias or "").strip()
    fwd_destination = (req.fwd_destination or "").strip()
    rule_id = ""

    if not email_alias:
        try:
            fallback = await _mint_random_alias(pipeline=req.pipeline_choice)
            email_alias = fallback["alias"]
            fwd_destination = fallback["destination"]
            rule_id = fallback["rule_id"]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Email mint failed: {exc}")

    try:
        created = await create_intake_page(
            account_username=req.account_username,
            label_artist=req.label_artist,
            pipeline_choice=req.pipeline_choice,
            page_type=req.page_type,
            sounds_reference=req.sounds_reference,
            notes=req.notes,
            poster=req.poster,
            go_live_date=req.go_live_date,
            group=req.group,
            group_label=req.group_label,
            account_type=req.account_type,
            email=email_alias,
            fwd_address=email_alias,  # alias forwards itself
            password=DEFAULT_INTAKE_PASSWORD,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Notion API error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Intake submit failed: {exc}")

    # Sync immediately so the new row appears in Pipeline lane 1.
    # The roster sync also picks up the email_alias / fwd_destination.
    try:
        sync_result = await sync_into_roster()
    except Exception as exc:
        return {
            "ok": True,
            "notion_page_id": created.get("id"),
            "email_alias": email_alias,
            "fwd_destination": fwd_destination or email_alias,
            "synced": False,
            "sync_error": str(exc),
        }

    # Also persist the CF rule_id locally so Run Setup can skip remint
    iid_slug = f"acct:{_slug_alias(req.account_username)}"
    try:
        from services.roster import set_page
        set_page(iid_slug, {
            "email_alias": email_alias,
            "email_rule_id": rule_id,
            "fwd_destination": fwd_destination or email_alias,
        })
    except Exception:
        # Non-fatal — Notion has the email, sync will hydrate the rest
        pass

    return {
        "ok": True,
        "notion_page_id": created.get("id"),
        "email_alias": email_alias,
        "fwd_destination": fwd_destination or email_alias,
        "synced": True,
        "added": sync_result.get("added", 0),
        "updated": sync_result.get("updated", 0),
    }


@router.get("/stages")
async def get_stages():
    """Return all pages grouped by Notion `status`. Order matches PIPELINE_STAGES."""
    pages = list_all_pages()
    by_status: dict[str, list[dict]] = {s: [] for s in PIPELINE_STAGES}
    unassigned: list[dict] = []

    for page in pages:
        status = (page.get("status") or "").strip()
        if status in by_status:
            by_status[status].append(page)
        elif status:
            # Unknown status — bucket separately so user can spot data drift
            unassigned.append({**page, "_unknown_status": status})
        # No status at all = legacy/active page that didn't come through the
        # sale-handoff intake. Skip — the Pipeline tab is ONLY for pages
        # currently moving through the onboarding lifecycle. Operational
        # accounts live on the Roster tab.

    return {
        "stages": [
            {"status": s, "count": len(by_status[s]), "pages": by_status[s]}
            for s in PIPELINE_STAGES
        ],
        "unassigned": unassigned,
        "total_pages": len(pages),
    }


# ── Setup chain (the big one) ────────────────────────────────────────────────


class SetupRequest(BaseModel):
    # Optional override — usually the page already has poster_name from Notion
    poster_id: str | None = None


@router.post("/{integration_id}/setup")
async def run_setup(integration_id: str, req: SetupRequest | None = None):
    """Setup chain for a `New — Pending Setup` page.

    SHARED steps (both pipelines):
      1. mint Cloudflare email alias
      2. write email back to Notion

    Then branches on the page's `Pipeline` value:

    Flow Stage (Jay's external system handles delivery):
      3. flip Notion status -> 'In Production'
      DONE — Notion row + email is the handoff. Jay plugs it into Flow Stage.

    King Maker (this app handles delivery via R2 + Telegram):
      3. assign page to poster (looks up poster_name from Notion)
      4. create Telegram topic in poster's group
      5. create R2 prefix for content storage
      6. write R2 location back to Notion
      7. flip Notion status -> 'In Production'

    Each step wrapped — failures abort with partial-result reporting.
    """
    page = get_page(integration_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {integration_id} not found")

    pipeline = (page.get("pipeline") or "").strip()

    result: dict[str, Any] = {
        "integration_id": integration_id,
        "pipeline": pipeline,
        "steps": {},
        "completed": False,
    }
    req = req or SetupRequest()

    # ── Step 1: Cloudflare email alias ───────────────────────────────────
    # Email is normally minted at intake (so user can sign up TikTok immediately).
    # This step is a fallback for legacy pages or pages that didn't go through intake.
    if page.get("email_alias"):
        result["steps"]["cf_alias"] = {"ok": True, "skipped": True, "alias": page["email_alias"]}
    else:
        try:
            email_info = await _mint_email_for_username(
                page.get("name") or integration_id,
                pipeline=page.get("pipeline"),
            )
            set_page(integration_id, {
                "email_alias": email_info["alias"],
                "email_rule_id": email_info["rule_id"],
                "fwd_destination": email_info["destination"],
            })
            result["steps"]["cf_alias"] = {
                "ok": True,
                "alias": email_info["alias"],
                "destination": email_info["destination"],
            }
        except HTTPException as e:
            result["steps"]["cf_alias"] = {"ok": False, "reason": str(e.detail)}
            raise

    # ── Step 2: Write email back to Notion (shared — both pipelines need this) ─
    fresh = get_page(integration_id) or {}
    notion_pid = fresh.get("notion_page_id")
    if notion_pid and notion_configured():
        try:
            email_to_write = fresh.get("email_alias")
            fwd_to_write = fresh.get("fwd_destination")
            if email_to_write or fwd_to_write:
                await update_page_email_fields(
                    notion_pid,
                    email=email_to_write,
                    fwd_address=email_to_write,
                )
                result["steps"]["notion_email_writeback"] = {"ok": True}
            else:
                result["steps"]["notion_email_writeback"] = {"ok": True, "skipped": True, "reason": "no email to write"}
        except Exception as exc:
            result["steps"]["notion_email_writeback"] = {"ok": False, "reason": str(exc)}
    else:
        result["steps"]["notion_email_writeback"] = {"ok": True, "skipped": True, "reason": "no notion_page_id or notion not configured"}

    # ─────────────────────────────────────────────────────────────────────
    # PIPELINE BRANCH
    # ─────────────────────────────────────────────────────────────────────

    if pipeline == "Flow Stage":
        # Flow Stage handles delivery externally via Jay's tooling.
        # Notion row + email is the handoff. Plus we ping Slack so Jay
        # knows immediately without watching Notion.
        fresh_for_slack = get_page(integration_id) or {}

        # ── Slack handoff (optional) ─────────────────────────────────
        if slack_configured():
            try:
                slack_result = await post_flow_stage_handoff(fresh_for_slack)
                result["steps"]["slack_handoff"] = slack_result
            except Exception as exc:
                result["steps"]["slack_handoff"] = {"ok": False, "error": str(exc)}
        else:
            result["steps"]["slack_handoff"] = {
                "ok": False,
                "skipped": True,
                "reason": "SLACK_WEBHOOK_URL not configured",
            }

        # NOTE: Jay gets notified via the Cloudflare email forwarding
        # destination — TikTok verification emails sent to the alias
        # land in jay@risingtidesent.com directly. No separate handoff
        # email needed.

        # ── Status flip ──────────────────────────────────────────────
        if notion_pid and notion_configured():
            try:
                await update_page_status(notion_pid, "In Production")
                set_page(integration_id, {"status": "In Production"})
                result["steps"]["status_flip"] = {"ok": True, "new_status": "In Production"}
            except Exception as exc:
                result["steps"]["status_flip"] = {"ok": False, "reason": str(exc)}
                return result
        else:
            set_page(integration_id, {"status": "In Production"})
            result["steps"]["status_flip"] = {"ok": True, "new_status": "In Production", "local_only": True}

        result["completed"] = True
        result["page"] = get_page(integration_id)
        return result

    # ── King Maker path (this app handles delivery via R2 + Telegram) ───
    # If pipeline is empty/unset, default to King Maker (full setup) for safety.

    # ── Step 3 (KM): Assign page to poster ──────────────────────────────
    poster_id = req.poster_id
    if not poster_id:
        resolved = resolve_poster_for_page(fresh)
        if resolved:
            poster_id = resolved.get("poster_id")

    if not poster_id:
        result["steps"]["poster_assign"] = {
            "ok": False,
            "reason": f"No poster could be resolved (poster_name on page: '{fresh.get('poster_name', '')}')",
        }
        return result

    poster = get_poster(poster_id)
    if not poster:
        result["steps"]["poster_assign"] = {"ok": False, "reason": f"Poster {poster_id} not found"}
        return result

    try:
        assign_page_to_poster(poster_id, integration_id)
        result["steps"]["poster_assign"] = {
            "ok": True,
            "poster_id": poster_id,
            "poster_name": poster.get("name"),
        }
    except Exception as exc:
        result["steps"]["poster_assign"] = {"ok": False, "reason": str(exc)}
        return result

    # ── Step 4 (KM): Create Telegram topic in poster's group ────────────
    try:
        from services.telegram import set_poster_topic
        from telegram_bot import create_forum_topic, get_bot

        if get_bot() is None:
            result["steps"]["telegram_topic"] = {"ok": False, "reason": "Telegram bot not configured"}
            return result

        chat_id = poster.get("chat_id")
        if not chat_id:
            result["steps"]["telegram_topic"] = {"ok": False, "reason": "Poster has no chat_id"}
            return result

        existing_topics = poster.get("topics", {})
        if integration_id in existing_topics:
            result["steps"]["telegram_topic"] = {
                "ok": True,
                "skipped": True,
                "topic_id": existing_topics[integration_id]["topic_id"],
            }
        else:
            topic_name = fresh.get("name") or integration_id
            topic_id = await create_forum_topic(chat_id, topic_name)
            set_poster_topic(poster_id, integration_id, topic_id, topic_name)
            result["steps"]["telegram_topic"] = {
                "ok": True,
                "topic_id": topic_id,
                "topic_name": topic_name,
            }
    except Exception as exc:
        result["steps"]["telegram_topic"] = {"ok": False, "reason": str(exc)}
        return result

    # ── Step 5 (KM): Create R2 prefix ───────────────────────────────────
    fresh = get_page(integration_id) or {}
    if fresh.get("r2_prefix"):
        result["steps"]["r2_prefix"] = {"ok": True, "skipped": True, "prefix": fresh["r2_prefix"]}
    elif not r2.is_configured():
        result["steps"]["r2_prefix"] = {"ok": False, "reason": "R2 not configured"}
        # Non-fatal — King Maker can still operate w/o R2 if topic exists
    else:
        try:
            r2_info = r2.create_account_prefix(integration_id)
            set_page(integration_id, {
                "r2_prefix": r2_info["prefix"],
                "r2_bucket": r2_info["bucket"],
            })
            result["steps"]["r2_prefix"] = {
                "ok": True,
                "prefix": r2_info["prefix"],
                "bucket": r2_info["bucket"],
            }
        except Exception as exc:
            result["steps"]["r2_prefix"] = {"ok": False, "reason": str(exc)}

    # ── Step 6 (KM): Write R2 location to Notion ────────────────────────
    fresh = get_page(integration_id) or {}
    if notion_pid and notion_configured() and fresh.get("r2_prefix"):
        try:
            r2_loc = f"r2://{fresh.get('r2_bucket', '')}/{fresh['r2_prefix']}"
            await update_page_drive_folder(notion_pid, r2_loc)
            result["steps"]["notion_r2_writeback"] = {"ok": True}
        except Exception as exc:
            result["steps"]["notion_r2_writeback"] = {"ok": False, "reason": str(exc)}

    # ── Step 7 (KM): Flip Notion status ─────────────────────────────────
    if notion_pid and notion_configured():
        try:
            await update_page_status(notion_pid, "In Production")
            set_page(integration_id, {"status": "In Production"})
            result["steps"]["status_flip"] = {"ok": True, "new_status": "In Production"}
        except Exception as exc:
            result["steps"]["status_flip"] = {"ok": False, "reason": str(exc)}
            return result
    else:
        set_page(integration_id, {"status": "In Production"})
        result["steps"]["status_flip"] = {"ok": True, "new_status": "In Production", "local_only": True}

    # NOTE: Glitch gets notified via the Cloudflare email forwarding
    # destination — TikTok verification emails sent to the alias land
    # in glitch@risingtidesent.com directly. He also has the Pipeline
    # tab to open the workspace and start dropping content.

    result["completed"] = True
    result["page"] = get_page(integration_id)
    return result


# ── Manual transition ────────────────────────────────────────────────────────


class TransitionRequest(BaseModel):
    status: str


@router.post("/{integration_id}/transition")
async def transition_status(integration_id: str, req: TransitionRequest):
    """Manually flip a page's status. Writes to roster and Notion."""
    if req.status not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(PIPELINE_STAGES)}",
        )

    page = get_page(integration_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {integration_id} not found")

    # Update Notion first (more likely to fail)
    notion_pid = page.get("notion_page_id")
    if notion_pid and notion_configured():
        try:
            await update_page_status(notion_pid, req.status)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Notion update failed: {exc}")

    set_page(integration_id, {"status": req.status, "updated_at": _today_utc_iso()})
    return {"ok": True, "page": get_page(integration_id)}


# ── King Maker workspace ─────────────────────────────────────────────────────


@router.get("/{integration_id}/workspace")
async def get_workspace(integration_id: str):
    """Full workspace state for a King Maker page.

    Returns everything Glitch needs to operate on this page:
    - page metadata
    - R2 prefix + list of all objects (videos)
    - assigned poster + telegram topic
    - cookie status
    - counts vs target
    """
    page = get_page(integration_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {integration_id} not found")

    workspace: dict[str, Any] = {
        "page": page,
        "r2": {
            "configured": r2.is_configured(),
            "prefix": page.get("r2_prefix"),
            "bucket": page.get("r2_bucket"),
            "objects": [],
            "object_count": 0,
            "target": 100,
        },
        "telegram": {
            "topic_present": False,
            "topic_id": None,
            "topic_name": None,
            "poster_id": None,
            "poster_name": None,
            "chat_id": None,
        },
        "cookie_status": "missing",
    }

    # R2 objects
    if page.get("r2_prefix") and r2.is_configured():
        try:
            objs = r2.list_account_objects(integration_id, max_keys=500)
            workspace["r2"]["objects"] = objs
            workspace["r2"]["object_count"] = len(objs)
        except Exception as exc:
            workspace["r2"]["error"] = str(exc)

    # Telegram poster + topic
    poster = resolve_poster_for_page(page)
    if poster:
        workspace["telegram"]["poster_id"] = poster.get("poster_id")
        workspace["telegram"]["poster_name"] = poster.get("name")
        workspace["telegram"]["chat_id"] = poster.get("chat_id")
        topic = poster.get("topics", {}).get(integration_id)
        if topic:
            workspace["telegram"]["topic_present"] = True
            workspace["telegram"]["topic_id"] = topic.get("topic_id")
            workspace["telegram"]["topic_name"] = topic.get("topic_name")

    # Cookie status
    try:
        workspace["cookie_status"] = get_cookie_status(page.get("name") or integration_id)
    except Exception:
        pass

    return workspace


class PresignUploadRequest(BaseModel):
    filename: str
    content_type: str = "video/mp4"


@router.post("/{integration_id}/upload-presign")
async def presign_upload(integration_id: str, req: PresignUploadRequest):
    """Generate a presigned PUT URL so the browser can upload a video
    directly to R2 (bypassing the Railway edge proxy and its size limit).

    Returns {url, key} — frontend PUTs the file to `url`, then optionally
    calls /forward-to-topic with the resulting key.
    """
    page = get_page(integration_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {integration_id} not found")
    if not r2.is_configured():
        raise HTTPException(status_code=503, detail="R2 not configured")
    if not page.get("r2_prefix"):
        raise HTTPException(
            status_code=400,
            detail="Page has no R2 prefix — run setup first",
        )

    safe_name = req.filename.replace("/", "_").replace("\\", "_") or "video.mp4"
    key = r2.account_key(integration_id, safe_name)
    url = r2.presign_put(key, content_type=req.content_type)

    return {"url": url, "key": key, "filename": safe_name}


class ForwardToTopicRequest(BaseModel):
    r2_key: str
    caption: str | None = None


@router.post("/{integration_id}/forward-to-topic")
async def forward_r2_to_topic(integration_id: str, req: ForwardToTopicRequest):
    """Forward an R2 object to the page's Telegram topic.

    Streams the R2 object through the backend → telegram bot → poster's
    forum topic. Used by King Maker pages once Glitch has uploaded a video
    and wants it to land in the poster's content folder.
    """
    page = get_page(integration_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {integration_id} not found")

    if not r2.is_configured():
        raise HTTPException(status_code=503, detail="R2 not configured")

    poster = resolve_poster_for_page(page)
    if not poster:
        raise HTTPException(
            status_code=400,
            detail=f"No poster resolved for page (poster_name='{page.get('poster_name', '')}')",
        )

    chat_id = poster.get("chat_id")
    topic = poster.get("topics", {}).get(integration_id)
    if not chat_id or not topic:
        raise HTTPException(
            status_code=400,
            detail="Page has no telegram topic in poster's group — run setup first",
        )

    # Verify object exists
    head = r2.head(req.r2_key)
    if head is None:
        raise HTTPException(status_code=404, detail=f"R2 object not found: {req.r2_key}")

    # Download R2 object to tmp file (telegram bot needs a local path)
    import tempfile
    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_fwd_"))
    filename = req.r2_key.rsplit("/", 1)[-1] or "video.mp4"
    tmp_path = tmp_dir / filename

    try:
        r2.download_to_path(req.r2_key, tmp_path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"R2 download failed: {exc}")

    # Forward via the telegram bot
    try:
        from telegram_bot import send_media_to_topic, get_bot

        if get_bot() is None:
            raise HTTPException(status_code=503, detail="Telegram bot not configured")

        msg = await send_media_to_topic(
            chat_id=chat_id,
            topic_id=topic["topic_id"],
            file_path=str(tmp_path),
            caption=req.caption,
        )

        return {
            "ok": True,
            "r2_key": req.r2_key,
            "topic_id": topic["topic_id"],
            "message_id": msg.get("message_id"),
            "file_id": msg.get("file_id"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Forward failed: {exc}")
    finally:
        # Clean up tmp file
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass


# ── Per-page health check ────────────────────────────────────────────────────


@router.get("/{integration_id}/health")
async def page_health(integration_id: str):
    """Derived health metrics for a single page."""
    page = get_page(integration_id)
    if not page:
        raise HTTPException(status_code=404, detail=f"Page {integration_id} not found")

    health: dict[str, Any] = {
        "integration_id": integration_id,
        "r2_count": 0,
        "r2_target": 100,
        "telegram_topic_present": False,
        "telegram_topic_name": None,
        "cookie_status": "missing",
        "has_email_alias": bool(page.get("email_alias")),
        "has_r2_prefix": bool(page.get("r2_prefix")),
    }

    # R2 object count
    if page.get("r2_prefix") and r2.is_configured():
        try:
            health["r2_count"] = r2.count_account_objects(integration_id)
        except Exception:
            pass

    # Telegram topic
    poster = resolve_poster_for_page(page)
    if poster:
        topic = poster.get("topics", {}).get(integration_id)
        if topic:
            health["telegram_topic_present"] = True
            health["telegram_topic_name"] = topic.get("topic_name")

    # Cookie status (autouploader)
    try:
        health["cookie_status"] = get_cookie_status(page.get("name") or integration_id)
    except Exception:
        pass

    return health
