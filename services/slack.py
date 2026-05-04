"""
Slack webhook service — posts messages to configured Slack incoming webhooks.

Used by the Pipeline tab to notify owners when a page completes setup:
  - Flow Stage  → Jay   (SLACK_WEBHOOK_URL_FLOW_STAGE, falls back to SLACK_WEBHOOK_URL)
  - King Maker  → Glitch (SLACK_WEBHOOK_URL_KING_MAKER, falls back to SLACK_WEBHOOK_URL)

Env vars:
  SLACK_WEBHOOK_URL_FLOW_STAGE   — Jay's webhook (Flow Stage handoffs)
  SLACK_WEBHOOK_URL_KING_MAKER   — Glitch's webhook (King Maker handoffs)
  SLACK_WEBHOOK_URL              — Legacy fallback if pipeline-specific not set
"""

import os
from typing import Any

import httpx


def is_configured() -> bool:
    """True if at least one webhook env var is set."""
    return bool(
        os.getenv("SLACK_WEBHOOK_URL")
        or os.getenv("SLACK_WEBHOOK_URL_FLOW_STAGE")
        or os.getenv("SLACK_WEBHOOK_URL_KING_MAKER")
    )


def _webhook_for_pipeline(pipeline: str | None) -> str:
    """Return the webhook URL to use for this pipeline's handoff notification.

    Falls back to SLACK_WEBHOOK_URL if the pipeline-specific one isn't set,
    so a single-channel rollout still works.
    """
    p = (pipeline or "").strip().lower()
    if p == "flow stage":
        return (
            os.getenv("SLACK_WEBHOOK_URL_FLOW_STAGE", "").strip()
            or os.getenv("SLACK_WEBHOOK_URL", "").strip()
        )
    if p in ("king maker tech", "king maker", "kingmaker", "kingmaker tech"):
        return (
            os.getenv("SLACK_WEBHOOK_URL_KING_MAKER", "").strip()
            or os.getenv("SLACK_WEBHOOK_URL", "").strip()
        )
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()


async def post_message(
    text: str,
    blocks: list[dict] | None = None,
    *,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Post a message to a Slack webhook.

    If `webhook_url` is provided, posts there. Otherwise uses SLACK_WEBHOOK_URL
    as the legacy default.

    Returns {ok: bool, error: str | None}.
    Non-fatal — if no webhook is configured, returns ok=False with reason.
    """
    url = (webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")).strip()
    if not url:
        return {"ok": False, "error": "SLACK_WEBHOOK_URL not configured"}

    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 200 and resp.status_code < 300:
                return {"ok": True, "error": None}
            return {
                "ok": False,
                "error": f"Slack returned {resp.status_code}: {resp.text[:200]}",
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def post_pipeline_handoff(page: dict[str, Any]) -> dict[str, Any]:
    """Send a pipeline handoff bundle to Slack.

    Routes to Jay (Flow Stage) or Glitch (King Maker) based on the page's
    `pipeline` value. Both webhooks fall back to SLACK_WEBHOOK_URL if their
    pipeline-specific one isn't set.

    The bundle includes everything the recipient needs to start work:
    handle, email, password, poster, sounds reference, notes, Notion link.
    """
    pipeline = (page.get("pipeline") or "").strip()
    webhook_url = _webhook_for_pipeline(pipeline)
    if not webhook_url:
        return {"ok": False, "error": f"No Slack webhook configured for pipeline '{pipeline}'"}

    if pipeline.lower() == "flow stage":
        emoji = "🚀"
        owner = "Jay"
        title = "Flow Stage handoff"
    elif pipeline.lower() in ("king maker tech", "king maker", "kingmaker", "kingmaker tech"):
        emoji = "👑"
        owner = "Glitch"
        title = "King Maker handoff"
    else:
        emoji = "📦"
        owner = "team"
        title = f"{pipeline or 'Pipeline'} handoff"

    handle = page.get("name") or page.get("integration_id", "unknown")
    email = page.get("email_alias") or page.get("signup_email") or "(none)"
    password = page.get("password") or "(check Notion)"
    poster = page.get("poster_name") or "(unassigned)"
    sounds = page.get("sounds_reference") or ""
    notes = page.get("notes") or ""
    page_type = page.get("page_type") or ""
    group = page.get("group_label") or page.get("group") or ""
    notion_pid = (page.get("notion_page_id") or "").replace("-", "")
    notion_url = (
        f"https://www.notion.so/{notion_pid}" if notion_pid else None
    )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {title}: {handle}",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"For *{owner}* — verification codes go to *henry@risingtidesent.com*; ping Henry for the login code."}
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Handle:*\n`{handle}`"},
                {"type": "mrkdwn", "text": f"*Email:*\n`{email}`"},
                {"type": "mrkdwn", "text": f"*Password:*\n`{password}`"},
                {"type": "mrkdwn", "text": f"*Poster:*\n{poster}"},
            ],
        },
    ]

    if page_type or group:
        blocks.append({
            "type": "section",
            "fields": [
                *([{"type": "mrkdwn", "text": f"*Page type:*\n{page_type}"}] if page_type else []),
                *([{"type": "mrkdwn", "text": f"*Group:*\n{group}"}] if group else []),
            ],
        })

    if sounds:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Sounds reference:*\n{sounds}"},
        })

    if notes:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Notes:*\n{notes}"},
        })

    if notion_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open in Notion", "emoji": True},
                    "url": notion_url,
                    "style": "primary",
                }
            ],
        })

    fallback_text = (
        f"{title}: {handle}\n"
        f"Email: {email}\n"
        f"Password: {password}\n"
        f"Poster: {poster}"
    )
    return await post_message(fallback_text, blocks, webhook_url=webhook_url)


# Back-compat alias — older callers may still import this name
post_flow_stage_handoff = post_pipeline_handoff
