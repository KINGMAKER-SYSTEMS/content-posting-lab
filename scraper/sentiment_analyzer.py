"""Mood / sentiment analyzer for caption text.

Classifies captions into music-aligned mood tags so the caption bank can be
filtered by vibe when syncing captions to songs for bulk video production.
"""

import asyncio
import json
import os
from typing import Literal

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

load_dotenv()

# 5 mood categories aligned to music sentiment — the goal is matching
# caption tone to song vibe so nothing contradicts the track.
MOOD_TAGS = [
    "sad",
    "hype",
    "love",
    "funny",
    "chill",
]

MoodTag = Literal[
    "sad",
    "hype",
    "love",
    "funny",
    "chill",
]

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        _client = AsyncOpenAI(api_key=key)
    return _client


_SYSTEM_PROMPT = (
    "You are a mood/sentiment classifier for short-form video captions (TikTok style). "
    "The purpose is to match caption tone to song/music mood so they don't contradict each other.\n\n"
    "For each caption, pick exactly ONE mood tag from this list:\n"
    f"{', '.join(MOOD_TAGS)}\n\n"
    "Definitions:\n"
    "- sad: heartbreak, loss, missing someone, pain, longing, emotional vulnerability, breakup energy\n"
    "- hype: motivational, confident, flexing, aggressive, energetic, empowering, pump-up\n"
    "- love: romantic, devotion, partner appreciation, loyalty, affection, deep connection\n"
    "- funny: humor, sarcasm, jokes, absurd observations, lighthearted, playful\n"
    "- chill: laid-back, unbothered, reflective, wholesome, relatable, peaceful, grateful\n\n"
    "Respond with ONLY valid JSON. No markdown fences."
)


async def analyze_mood(caption: str, _max_retries: int = 3) -> MoodTag:
    """Classify a single caption into a mood tag.

    Returns one of the MOOD_TAGS strings.
    Falls back to 'relatable' if classification fails.
    """
    if not caption or not caption.strip():
        return "chill"

    client = _get_client()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f'Classify this caption:\n"{caption.strip()}"\n\n'
                'Return JSON: {"mood": "<tag>"}'
            ),
        },
    ]

    for attempt in range(_max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                max_tokens=50,
                temperature=0.0,
            )
            text = resp.choices[0].message.content.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(text)
            mood = data.get("mood", "chill").lower().strip()
            if mood in MOOD_TAGS:
                return mood  # type: ignore[return-value]
            return "chill"
        except RateLimitError:
            if attempt == _max_retries:
                return "chill"
            wait = min(2**attempt * 2, 15)
            await asyncio.sleep(wait)
        except (json.JSONDecodeError, KeyError, AttributeError):
            return "chill"

    return "chill"


async def analyze_moods_batch(captions: list[str]) -> list[MoodTag]:
    """Classify multiple captions concurrently.

    Uses a single batch GPT call for efficiency when there are many captions.
    Falls back to individual calls if the batch approach fails.
    """
    if not captions:
        return []

    # For small batches, just run individually
    if len(captions) <= 3:
        return list(await asyncio.gather(*[analyze_mood(c) for c in captions]))

    # For larger batches, use a single prompt with numbered captions
    client = _get_client()
    numbered = "\n".join(f"{i+1}. \"{c.strip()}\"" for i, c in enumerate(captions) if c.strip())
    if not numbered:
        return ["chill"] * len(captions)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Classify each caption below:\n{numbered}\n\n"
                'Return JSON: {"moods": ["tag1", "tag2", ...]} in the same order.'
            ),
        },
    ]

    try:
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            max_tokens=len(captions) * 20 + 50,
            temperature=0.0,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        moods = data.get("moods", [])
        result: list[MoodTag] = []
        for i in range(len(captions)):
            if i < len(moods) and moods[i] in MOOD_TAGS:
                result.append(moods[i])
            else:
                result.append("chill")
        return result
    except Exception:
        # Fallback to individual analysis
        return list(await asyncio.gather(*[analyze_mood(c) for c in captions]))
