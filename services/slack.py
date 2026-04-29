"""
Slack webhook service — posts messages to a configured Slack incoming webhook.

Used by the Pipeline tab to notify Jay when a Flow Stage page completes setup
and is ready for him to plug into the Flow Stage tooling externally.

Env var:
  SLACK_WEBHOOK_URL — Incoming Webhook URL from Slack (apps → Incoming Webhooks)
"""

import os
from typing import Any

import httpx


def is_configured() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL"))


async def post_message(text: str, blocks: list[dict] | None = None) -> dict[str, Any]:
    """Post a message to the configured Slack webhook.

    Returns {ok: bool, error: str | None}.
    Non-fatal — if Slack isn't configured, returns ok=False with reason.
    """
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
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


async def post_flow_stage_handoff(page: dict[str, Any]) -> dict[str, Any]:
    """Send the Flow Stage handoff bundle to Slack.

    Includes everything Jay needs to plug a new page into Flow Stage:
    handle, email, password, poster, sounds reference, notes, Notion link.
    """
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

    # Build a richer Block Kit message
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚀 Flow Stage handoff: {handle}",
                "emoji": True,
            },
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
        f"Flow Stage handoff: {handle}\n"
        f"Email: {email}\n"
        f"Password: {password}\n"
        f"Poster: {poster}"
    )
    return await post_message(fallback_text, blocks)
