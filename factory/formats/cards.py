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


def _base(size="1920x1080") -> str:
    """The shared brand backdrop: dark gradient + soft radial glow."""
    return (f'magick -size {size} gradient:"{BRAND_BG_TOP}"-"{BRAND_BG_BOT}" '
            f'\\( -size {size} radial-gradient:rgba\\(110,139,255,0.16\\)-none \\) '
            f'-compose over -composite ')


def number_card(value: str, label: str, name: str, assets_dir: Path, fonts_dir: Path,
                accent: str = BRAND_CYAN) -> Path:
    """A hero stat: huge number + a small label under it. For NUMBER scenes."""
    out = assets_dir / f"{name}_number.png"
    big = _font(fonts_dir, "TikTokSans16pt-Black.ttf", "Montserrat-ExtraBold.ttf")
    sub = _font(fonts_dir, "TikTokSans-Bold.ttf", "Montserrat-ExtraBold.ttf")
    val = _clean(value, 14)
    lab = _clean(label, 48).upper()
    cmd = (_base() +
           f'-gravity center -font {shlex.quote(big)} '
           f'-fill black -annotate +6-30 {shlex.quote(val)} '                       # shadow
           f'-fill "{accent}" -pointsize 300 -annotate +0-36 {shlex.quote(val)} '   # hero number
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
            parts.append(f'-gravity north -fill "{accent}" -pointsize 44 -annotate +0+{y+108} "v" ')
        y += 150
    parts.append(f'-font {shlex.quote(disp)} -fill "#3a4254" -pointsize 26 -gravity south -annotate +0+50 "{WATERMARK}" ')
    parts.append(shlex.quote(str(out)))
    return _run(" ".join(parts), out)
