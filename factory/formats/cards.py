"""
Designed-card generators — the shots that replace the screenshot-a-blog slop.

The visual-director emits shot types; these produce the actual PNG frames for the four NEW
designed shots (the ones the legacy monolith had no real generator for, so it fell back to
scrolling an article):

    number_card  — a big stat/price/benchmark, hero number + label
    vs_card      — split-screen X vs Y comparison
    quote_card   — the take/verdict as a pull-quote
    diagram_card — a simple captioned mechanism panel (placeholder for a real motion diagram)

All use ImageMagick with REAL fonts on the brand gradient — no AI image text (that's the
fake-glyph slop), no blog screenshots. Brand: dark #0b1020→#08090b gradient, cyan #7FD2FF,
red #FF2B2F, TikTokSans/Montserrat. Each returns the output Path (1920x1080 PNG) or raises.

These are pure (no app imports) so they can be unit-tested and called from either the v2 runner
or adapted into the v1 monolith. Pass `assets_dir` + `fonts_dir` explicitly.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

BRAND_BG_TOP = "#0b1020"
BRAND_BG_BOT = "#08090b"
BRAND_CYAN = "#7FD2FF"
BRAND_RED = "#FF2B2F"
BRAND_WHITE = "#f2f4f7"
BRAND_SUB = "#8b9bd4"
WATERMARK = "AGENTICBUILDERNEWS"


def _font(fonts_dir: Path, *names: str) -> str:
    for n in names:
        p = fonts_dir / n
        if p.exists():
            return str(p)
    # fall back to a system font so generation never hard-fails on a missing brand font
    for sysf in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if Path(sysf).exists():
            return sysf
    return "Helvetica"


def _clean(s: str, n: int) -> str:
    """Normalize whitespace and truncate to <=n chars on a WORD boundary (never mid-word)."""
    s = re.sub(r'\s+', ' ', (s or "").strip())
    if len(s) <= n:
        return s
    cut = s[:n].rstrip()
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.strip()


def _run(cmd: str, out: Path) -> Path:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"card gen failed: {r.stderr[-200:] or r.stdout[-200:]}")
    return out


import random as _random

# The factory sets this once so every card's _base() can find the cinematic background pool without
# threading assets_dir through all 4 generators. (Cards stay pure/unit-testable; default None = gradient.)
_ASSETS_DIR = None


def _bg_pool(assets_dir=None):
    """Cinematic background images (generated via GPT-image) the cards composite text OVER instead of a
    flat blue slide. Lives in <assets>/card_backgrounds/. Empty => cards fall back to the gradient."""
    if assets_dir is None:
        return []
    d = Path(assets_dir) / "card_backgrounds"
    if not d.exists():
        return []
    return [p for p in d.glob("*.png") if not p.name.startswith("._")]


def _base(size="1920x1080", assets_dir=None) -> str:
    """The shared backdrop. Prefers a REAL cinematic GPT-image background (darkened for text legibility)
    so cards aren't a flat-blue PowerPoint slide; falls back to the brand gradient if none generated."""
    pool = _bg_pool(assets_dir or _ASSETS_DIR)
    if pool:
        bg = _random.choice(pool)
        # scale-to-fill + DARKEN (multiply a 58% black veil) so white card text stays readable over it,
        # then a subtle bottom-vignette. This is the anti-PowerPoint background.
        return (f'magick \\( {shlex.quote(str(bg))} -resize {size}^ -gravity center -extent {size} \\) '
                f'\\( -size {size} xc:"rgba(8,9,11,0.58)" \\) -compose over -composite '
                f'\\( -size {size} gradient:none-"rgba(0,0,0,0.5)" \\) -compose over -composite ')
    return (f'magick -size {size} gradient:"{BRAND_BG_TOP}"-"{BRAND_BG_BOT}" '
            f'\\( -size {size} radial-gradient:rgba\\(110,139,255,0.16\\)-none \\) '
            f'-compose over -composite ')


def number_card(value: str, label: str, name: str, assets_dir: Path, fonts_dir: Path,
                accent: str = BRAND_CYAN) -> Path:
    """A hero stat: huge number + a small label under it. For NUMBER scenes."""
    out = assets_dir / f"{name}_number.png"
    big = _font(fonts_dir, "TikTokSans16pt-Black.ttf", "Montserrat-ExtraBold.ttf")
    sub = _font(fonts_dir, "TikTokSans-Bold.ttf", "Montserrat-ExtraBold.ttf")
    val = _clean(value, 16)
    lab = _clean(label, 48).upper()
    # ADAPTIVE hero size: a fixed 300pt overflowed the frame for long values like '4,096 tokens'
    # (the number got clipped at both edges — caught on a real card). Scale down as the value gets
    # longer so it always fits inside ~1680px of the 1920 frame.
    n = len(val)
    psize = 300 if n <= 5 else (240 if n <= 8 else (180 if n <= 11 else 132))
    cmd = (_base() +
           f'-gravity center -font {shlex.quote(big)} '
           f'-fill black -annotate +6-30 {shlex.quote(val)} '                       # shadow
           f'-fill "{accent}" -pointsize {psize} -annotate +0-36 {shlex.quote(val)} '   # hero number (adaptive)
           f'-font {shlex.quote(sub)} -fill "{BRAND_WHITE}" -pointsize 54 -annotate +0+150 {shlex.quote(lab)} '
           f'-fill "{accent}" -draw "rectangle 860,250 1060,258" '                   # accent rule
           f'-font {shlex.quote(sub)} -fill "#3a4254" -pointsize 26 -gravity south -annotate +0+50 "{WATERMARK}" '
           f'{shlex.quote(str(out))}')
    return _run(cmd, out)


def vs_card(left: str, right: str, name: str, assets_dir: Path, fonts_dir: Path) -> Path:
    """Split-screen X vs Y. Left tinted cyan, right tinted red, 'VS' in the middle."""
    out = assets_dir / f"{name}_vs.png"
    disp = _font(fonts_dir, "TikTokSans16pt-Black.ttf", "Montserrat-ExtraBold.ttf")
    # shorter cap so a long label never runs under the centered VS badge
    L, R = _clean(left, 16), _clean(right, 16)
    cmd = (f'magick -size 1920x1080 gradient:"{BRAND_BG_TOP}"-"{BRAND_BG_BOT}" '
           # left + right tinted halves
           f'\\( -size 960x1080 xc:"rgba(127,210,255,0.10)" \\) -gravity west -compose over -composite '
           f'\\( -size 960x1080 xc:"rgba(255,43,47,0.10)" \\) -gravity east -compose over -composite '
           f'-fill "#1b2440" -draw "rectangle 956,0 964,1080" '                       # center divider
           # labels are CENTERED within their own half (west/east quadrant midpoints ≈ ±480px),
           # so neither can collide with the VS badge in the middle.
           f'-font {shlex.quote(disp)} -fill "{BRAND_WHITE}" -pointsize 76 '
           f'-gravity center -annotate -480+0 {shlex.quote(L)} '
           f'-gravity center -annotate +480+0 {shlex.quote(R)} '
           # VS badge: a filled circle so it reads as a deliberate centerpiece, not stray text
           f'-fill "{BRAND_BG_BOT}" -draw "circle 960,540 960,455" '
           f'-fill "{BRAND_CYAN}" -strokewidth 4 -stroke "{BRAND_CYAN}" '
           f'-draw "fill-opacity 0 circle 960,540 960,452" -strokewidth 0 '
           f'-gravity center -fill "{BRAND_CYAN}" -pointsize 92 -annotate +0+0 "VS" '
           f'-font {shlex.quote(disp)} -fill "#3a4254" -pointsize 26 -gravity south -annotate +0+50 "{WATERMARK}" '
           f'{shlex.quote(str(out))}')
    return _run(cmd, out)


def quote_card(quote: str, name: str, assets_dir: Path, fonts_dir: Path,
               accent: str = BRAND_CYAN) -> Path:
    """The take/verdict as a centered pull-quote with a big accent bar. For TAKE scenes."""
    out = assets_dir / f"{name}_quote.png"
    disp = _font(fonts_dir, "TikTokSans-Bold.ttf", "Montserrat-ExtraBold.ttf")
    # word-wrap to ~26 chars/line so it reads big and clean
    words = _clean(quote, 130).split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 26 and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    text = "\n".join(lines[:4])
    cmd = (_base() +
           f'-fill "{accent}" -draw "rectangle 360,300 380,780" '                    # left accent bar
           f'-gravity west -font {shlex.quote(disp)} -fill "{BRAND_WHITE}" -pointsize 76 '
           f'-interline-spacing 14 -annotate +420+0 {shlex.quote(text)} '
           f'-font {shlex.quote(disp)} -fill "#3a4254" -pointsize 26 -gravity south -annotate +0+50 "{WATERMARK}" '
           f'{shlex.quote(str(out))}')
    return _run(cmd, out)


def hook_card(hook_text: str, name: str, assets_dir: Path, fonts_dir: Path,
              accent: str = BRAND_RED) -> Path:
    """The FIRST-5-SECONDS hook frame — the most important visual in the whole episode.

    A big, bold, high-contrast statement card built from the cold-open's striking fact, designed to
    STOP the scroll (research: ~70% retention at 0:30 triggers promotion; 55% leave by 60s). Red
    accent by default for max energy, large centered text, a top 'eyebrow' kicker. Visually
    distinct from the calmer mid-video cards so the open reads as a deliberate hook."""
    out = assets_dir / f"{name}_hook.png"
    disp = _font(fonts_dir, "TikTokSans16pt-Black.ttf", "Montserrat-ExtraBold.ttf")
    # punchy: keep it short, wrap to ~18 chars/line so it reads HUGE
    words = _clean(hook_text, 90).split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 18 and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    text = "\n".join(lines[:3])
    cmd = (_base() +
           # energetic glow wash + a bold top kicker bar
           f'\\( -size 1920x1080 radial-gradient:rgba\\(255,43,47,0.10\\)-none \\) -compose over -composite '
           f'-gravity north -fill "{accent}" -draw "rectangle 810,210 1110,222" '
           f'-font {shlex.quote(disp)} -fill "{accent}" -pointsize 34 -annotate +0+150 "AGENTIC BUILDER NEWS" '
           # the hook line, huge, centered. A dark STROKE (not an offset shadow) gives clean contrast
           # on any background without the doubled-ghost the offset shadow produced.
           f'-gravity center -font {shlex.quote(disp)} -pointsize 104 -interline-spacing 8 '
           f'-stroke black -strokewidth 6 -fill "{BRAND_WHITE}" -annotate +0+0 {shlex.quote(text)} '
           f'-stroke none -fill "{BRAND_WHITE}" -annotate +0+0 {shlex.quote(text)} '
           f'-font {shlex.quote(disp)} -fill "#3a4254" -pointsize 26 -gravity south -annotate +0+50 "{WATERMARK}" '
           f'{shlex.quote(str(out))}')
    return _run(cmd, out)


def diagram_card(title: str, steps: list[str], name: str, assets_dir: Path, fonts_dir: Path,
                 accent: str = BRAND_CYAN) -> Path:
    """A simple captioned mechanism panel: title + up to 4 stacked step boxes with arrows.
    Placeholder for a real animated motion-diagram; still a designed frame, not a blog screenshot."""
    out = assets_dir / f"{name}_diagram.png"
    disp = _font(fonts_dir, "TikTokSans-Bold.ttf", "Montserrat-ExtraBold.ttf")
    ttl = _clean(title, 40)
    steps = [s for s in (steps or []) if s][:4] or ["input", "process", "output"]
    parts = [_base(),
             f'-gravity north -font {shlex.quote(disp)} -fill "{BRAND_WHITE}" -pointsize 64 '
             f'-annotate +0+90 {shlex.quote(ttl)} ']
    # stacked step boxes down the center
    y = 320
    for i, s in enumerate(steps):
        lab = _clean(s, 34)
        col = accent if i % 2 == 0 else BRAND_RED
        parts.append(f'-fill "rgba(127,210,255,0.08)" -draw "roundrectangle 560,{y} 1360,{y+110} 16,16" ')
        parts.append(f'-fill "{col}" -draw "rectangle 560,{y} 576,{y+110}" ')
        parts.append(f'-gravity northwest -font {shlex.quote(disp)} -fill "{BRAND_WHITE}" -pointsize 40 '
                     f'-annotate +620+{y+34} {shlex.quote(lab)} ')
        if i < len(steps) - 1:
            # a real drawn down-chevron in the gap between boxes (was a literal lowercase 'v' that
            # read like a typo). Centered at x=960, in the ~40px gap below this box.
            ay = y + 110 + 8   # just below the box bottom
            parts.append(f'-fill "{accent}" -draw "polygon 940,{ay} 980,{ay} 960,{ay+22}" ')
        y += 150
    parts.append(f'-font {shlex.quote(disp)} -fill "#3a4254" -pointsize 26 -gravity south -annotate +0+50 "{WATERMARK}" ')
    parts.append(shlex.quote(str(out)))
    return _run(" ".join(parts), out)
