"""
Pipeline → Cloudflare email forwarding destination resolver.

ALL new page email aliases forward to a single intake address — Henry
handles the TikTok account setup (verification codes, login flows, 2FA),
then either keeps it or hands off to Jay/Glitch for ongoing operations.

Centralizing on one inbox means:
  - Henry can always log in to any account during setup, no per-pipeline routing
  - Verification codes never get lost between Jay's / Glitch's inboxes
  - Handoff to Flow Stage / King Maker is a separate manual step, not coupled
    to the email mint

Env var (override default):
  EMAIL_HANDOFF_DEFAULT — defaults to henry@risingtidesent.com
"""

import os

DEFAULT_DESTINATION = "henry@risingtidesent.com"


def destination_for_pipeline(pipeline: str | None) -> str | None:
    """Return the verified CF destination address for new aliases.

    Pipeline arg is accepted for backwards compatibility but ignored —
    all aliases now route to a single intake inbox regardless of pipeline.
    """
    return os.getenv("EMAIL_HANDOFF_DEFAULT", DEFAULT_DESTINATION).strip() or None
