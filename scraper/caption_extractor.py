import asyncio
import base64
import logging
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

log = logging.getLogger("scraper.caption_extractor")

load_dotenv()

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        _client = AsyncOpenAI(api_key=key)
    return _client


SYSTEM_PROMPT = (
    "You are an OCR assistant. Extract ONLY the burned-in caption text "
    "visible on this TikTok video screenshot. The captions are typically "
    "large white or colored text overlaid directly on the video content, "
    "often with a black outline, shadow, or highlight background. "
    "They are the main hook/story text that creators add to their videos.\n\n"
    "IGNORE all TikTok UI elements: username, likes count, comments count, "
    "share button, description text at the bottom, sound name, hashtags, "
    "and any watermarks.\n\n"
    "Return ONLY the caption text exactly as it appears, preserving line "
    "breaks. If no burned-in caption is visible, return exactly: NO_CAPTION"
)


async def extract_caption(screenshot_bytes: bytes, _max_retries: int = 4) -> str:
    """Send a screenshot to GPT-4.1 vision and extract burned-in caption text.

    Returns the extracted caption string, or empty string if none found.
    Retries with exponential backoff on rate-limit (429) errors.
    """
    client = _get_client()
    b64 = base64.b64encode(screenshot_bytes).decode()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": "Extract the burned-in caption text from this TikTok video screenshot.",
                },
            ],
        },
    ]

    for attempt in range(_max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model="gpt-4.1",
                messages=messages,
                max_tokens=500,
                temperature=0.0,
            )
            text = resp.choices[0].message.content.strip()
            return "" if text == "NO_CAPTION" else text
        except RateLimitError as e:
            if attempt == _max_retries:
                raise
            wait = min(2 ** attempt * 2, 30)
            log.warning("Rate limited, retrying in %ds (attempt %d/%d)", wait, attempt + 1, _max_retries)
            await asyncio.sleep(wait)

    return ""
