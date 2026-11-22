"""Rewrite a free-form video prompt into the structured form MiniMax H3 expects.

MiniMax's hosted H3 puts a prompt-rewriting stage in front of the model and
does not ship it with the open weights. Plain prose still renders, but in the
recipe lab the structured form was the only truck prompt that came out with no
invented lettering. This is our own small version of that stage: a local LLM
(Ollama) restates the prompt in H3's three labelled fields, turning every
"no X" into a description of what is there instead, because the local H3 graph
has no negative prompt and naming a thing tends to draw it.

    python tools/h3_prompt.py "handheld phone clip of a parked truck, no logos" --seconds 3

Nothing here is used by the app at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "projects" / "recipe-lab" / "_cache" / "h3_prompts.json"
REWRITER_VERSION = "3"

FIELDS = ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")

_SYSTEM = """You convert a video idea into the exact prompt format a text-to-video model was trained on.

Output exactly three fields, in this order, separated by one blank line, and nothing else:

integrated_multimodal_description: [Shot 1] <style>, <opening composition and everything visible, in timeline order>

overall_soundscape: <1 to 4 sentences of ambient and action sound>

non_diegetic_music: <1 to 3 sentences of background score, or N/A>

Rules for integrated_multimodal_description:
- Start with "[Shot 1] " followed by the visual style (for example "Live-action, cinematic," or "Live-action, handheld phone footage,") and the opening composition (shot size and angle).
- Use ONE shot. Never add "[Shot 2]" unless the idea explicitly asks for a cut.
- Describe only what can be seen. Every sentence must describe something that IS in the picture.
- NEVER write a negation. Do not use "no", "not", "without", "never", "free of", "absent", "unmarked", "unbadged", "debadged" or "unlit". Do not name anything that should be missing (logos, badges, emblems, lettering, text, decals, stickers, licence plates, people, watermarks). Replace each such instruction with a positive description of the surface or space as it should look, for example: "smooth plain painted metal", "a flat grille of plain horizontal bars", "dark smoked lamp lenses", "clear glass showing empty seats", "a flat blank bumper".
- Keep every concrete detail of subject, setting, light, colour and atmosphere from the idea.
- Say that the exposure is steady from the first frame when the idea asks for consistent brightness.
- Describe camera movement as an action using this vocabulary: static shot, push in, pull out, pan left, pan right, truck left, truck right, tilt up, tilt down, pedestal up, pedestal down, arc shot, tracking shot, POV, shake slightly. Add "with small amplitude" or "with large amplitude" and "at slow speed" or "at fast speed" when they matter. Example: "The camera pushes in with small amplitude at slow speed."
- A subject described as parked or still must be described as stationary for the whole shot.
- For a vehicle that the idea wants plain or with its lights off, say so in positive terms every time: its grille is a flat panel of plain horizontal bars, its lamp lenses are dark smoked glass, and its doors, fenders, bed sides and tailgate are smooth painted metal in one colour.

Rules for the other two fields:
- overall_soundscape: plain ambient sound that fits the scene. No dialogue.
- non_diegetic_music: write N/A unless the idea asks for music.

The clip is {seconds} seconds long. Write for that length: one continuous moment, no story beats that need more time."""

_NEGATIONS = re.compile(
    r"\b(no|not|without|never|unmarked|unbadged|debadged|unlit|free of|absent)\b", re.I)


def _cache_key(prompt: str, seconds: int, model: str) -> str:
    return hashlib.sha256(f"{REWRITER_VERSION}|{model}|{seconds}|{prompt}".encode()).hexdigest()


def problems(text: str) -> list[str]:
    """Why a rewrite is not usable, or an empty list."""
    found = []
    positions = [text.find(field) for field in FIELDS]
    if any(p < 0 for p in positions) or positions != sorted(positions):
        found.append("missing or out-of-order field labels")
    if any(text.count(field) != 1 for field in FIELDS) or "<" in text:
        found.append("repeated field labels or leftover markup")
    if "[Shot 1]" not in text:
        found.append("no [Shot 1]")
    if "[Shot 2]" in text:
        found.append("added a second shot")
    body = text.split(FIELDS[1])[0]
    negations = sorted({m.group(0).lower() for m in _NEGATIONS.finditer(body)})
    if negations:
        found.append("negations in the description: " + ", ".join(negations))
    return found


def _normalize(text: str) -> str:
    # Some local models leak a reasoning draft and then repeat the answer after
    # a closing think tag: the answer is whatever follows the last one.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    text = text.strip().strip("`").strip()
    start = text.find(FIELDS[0])
    if start > 0:
        text = text[start:]
    parts = []
    for index, field in enumerate(FIELDS):
        begin = text.find(field)
        if begin < 0:
            return text
        end = text.find(FIELDS[index + 1]) if index + 1 < len(FIELDS) else len(text)
        parts.append(field + " " + " ".join(text[begin + len(field):end].split()))
    return "\n\n".join(parts)


def rewrite_for_h3(prompt: str, seconds: int = 3, *, use_cache: bool = True) -> str:
    """Return the H3-format prompt, from cache when this exact request was rewritten before.

    Makes up to three attempts and raises if none passes ``problems``: a sweep
    must not quietly test a malformed prompt.
    """
    url = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("H3_REWRITER_MODEL") or "qwen3.8:27b-nvfp4"
    key = _cache_key(prompt, seconds, model)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if use_cache and key in cache:
        return cache[key]

    messages = [{"role": "system", "content": _SYSTEM.replace("{seconds}", str(seconds))},
                {"role": "user", "content": "Video idea:\n" + prompt}]
    last: list[str] = []
    for attempt in range(3):
        resp = httpx.post(f"{url}/api/chat", timeout=600, json={
            "model": model, "stream": False, "think": False,
            "options": {"temperature": 0.2 + 0.2 * attempt}, "messages": messages})
        resp.raise_for_status()
        text = _normalize(resp.json()["message"]["content"])
        last = problems(text)
        if not last:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            cache[key] = text
            CACHE.write_text(json.dumps(cache, indent=1))
            return text
        messages = messages[:2] + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "That breaks the rules: " + "; ".join(last) + ". Rewrite it correctly."}]
    raise RuntimeError("H3 rewrite failed: " + "; ".join(last))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("prompt")
    parser.add_argument("--seconds", type=int, default=3)
    parser.add_argument("--fresh", action="store_true", help="ignore the cache")
    args = parser.parse_args()
    print(rewrite_for_h3(args.prompt, args.seconds, use_cache=not args.fresh))


if __name__ == "__main__":
    main()
