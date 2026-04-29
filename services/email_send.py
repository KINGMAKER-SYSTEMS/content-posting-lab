"""
Pipeline → Cloudflare email forwarding destination resolver.

When a new page is minted, its CF email alias forwards to a destination
based on which pipeline owns it:

  Flow Stage    → jay@risingtidesent.com   (EMAIL_HANDOFF_TO_FLOW_STAGE)
  King Maker    → glitch@risingtidesent.com (EMAIL_HANDOFF_TO_KING_MAKER)

This means TikTok signup emails / verification codes land directly in
Jay's or Glitch's inbox — no extra outbound email infrastructure.

Both addresses MUST be added + verified in Cloudflare Email Routing →
Destination Addresses before mint will succeed for that pipeline.

Env vars (override defaults):
  EMAIL_HANDOFF_TO_FLOW_STAGE — defaults to jay@risingtidesent.com
  EMAIL_HANDOFF_TO_KING_MAKER — defaults to glitch@risingtidesent.com
"""

import os

DEFAULT_FLOW_STAGE = "jay@risingtidesent.com"
DEFAULT_KING_MAKER = "glitch@risingtidesent.com"


def destination_for_pipeline(pipeline: str | None) -> str | None:
    """Return the verified CF destination address for the given pipeline.

    Returns None if pipeline is empty/unknown — caller falls back to the
    first verified destination on the CF account.
    """
    p = (pipeline or "").strip().lower()
    if p == "flow stage":
        return os.getenv("EMAIL_HANDOFF_TO_FLOW_STAGE", DEFAULT_FLOW_STAGE).strip() or None
    if p in ("king maker tech", "king maker", "kingmaker", "kingmaker tech"):
        return os.getenv("EMAIL_HANDOFF_TO_KING_MAKER", DEFAULT_KING_MAKER).strip() or None
    return None
