# ABN "Forge Signal" — Design Contract for the Frame

> frame.md-style design spec, written for VIDEO (not web). Every renderer in the factory reads
> this: the Remotion compositor (`src/brand/abnTokens.ts` is the typed projection), the
> ImageMagick card renderer (`factory/formats/cards.py`), Lottie generation (text-to-lottie
> skill), and any hyperframes/html-video experiment. Atoms are sacred; composition is free.

## Identity

Agentic Builder News (ABN) — daily AI-builder news. Register: **technical wire-service urgency**.
A terminal that learned broadcast design, not a startup landing page. Every frame should read as
"newsroom for people who ship agents," never "AI-generated explainer."

## Palette (atoms — never invent colors per-element)

| Token | Hex | Voice |
|---|---|---|
| voidBlack | `#030405` | absolute backdrop |
| nearBlack | `#080B0F` | stage background |
| panelBlack | `#0D1117` | video letterbox panels |
| forgeRed | `#FF2B2F` | **editorial/alert voice** — breaking, warnings, the forge |
| forgeRedDark | `#9F0711` | red gradient floor |
| signalCyan | `#7FD2FF` | **data/source voice** — attribution, karaoke highlight, the signal |
| signalBlue | `#16A7FF` | cyan's deep end (glows, gradients) |
| steelWhite | `#F4F7FB` | primary text (never pure #fff) |
| steelMid | `#8F99A7` | secondary text |
| bodyInk | `#CBD7E1` | long-body text |
| warningGold | `#FFCD38` | sparing tertiary accent |

Rules: dark theme always. ONE accent voice per element — red speaks editorially, cyan speaks
data; they never co-brand a single element. Neutrals are blue-tinted (already in the atoms), no
dead gray, no pure black/white. Cyan-on-dark is a known AI tell — we keep it because the entire
existing brand (logo, card library, thumbnails) is built on the red/cyan duo; the discipline
above is what separates it from the default.

## Typography

- **Display:** TikTok Sans, 36pt optical cut, weights 800/900 ONLY. Headlines, captions, cards.
- **Text:** TikTok Sans, 16pt optical cut, 600/700. Mid-size UI labels.
- **Mono:** JetBrains Mono (fallback SF Mono/Menlo), 400/700. Source URLs, data readouts, taglines.
- **BANNED:** Inter, Montserrat, Roboto, Open Sans, Poppins, and any second sans-serif. One
  expressive family + one receding mono. Two sans-serifs = friction without hierarchy.
- Weight contrast must be extreme (900 vs 400, never 700 vs 400).
- Tracking tighter than web on display sizes (encoding eats letter detail): −0.02 to −0.03em.
  Mono runs wide: +0.04 to +0.06em.
- Dark-background compensation: +0.05–0.1 lineHeight vs light-bg values (light-on-dark halos).
- `tabular-nums` on any stacked numerals (stats, counters, timers).
- Fixed reading time: text on screen 3s must be readable in 2 — fewer words, larger type.
- Fonts MUST be loaded from the TTFs in `public/fonts/` (`src/brand/fonts.ts`). The same TTFs
  drive ImageMagick cards — identical glyphs across every surface. Never assume a font exists.

### Type scale (1920×1080 frame)

| Role | Size | Weight | Tracking | Line height |
|---|---|---|---|---|
| statValue | 150 | 900 | −0.03em | 0.95 |
| titleCard | 96 | 900 | −0.025em | 1.08 |
| caption | 68 | 800 | −0.01em | 1.25 |
| lowerThird | 56 | 900 | −0.02em | 1.1 |
| label (mono) | 26 | 700 | +0.06em | 1.2 |
| micro (mono) | 22 | 400 | +0.04em | 1.2 |

## Space, radius, layers

- Spacing snaps to the 4px scale: 4 / 8 / 16 / 24 / 32 / 48; gutter 80; stage padding 120.
- Radius: 8 / 14 / 20. Nothing fully-rounded except dots.
- Layer stack (z): footage 0 → fx/vignette 10 → captions 30 → lower thirds 40 → pops 50 →
  branding/sting 60. Nothing renders above branding.

## Motion grammar

Three springs cover every entrance — do NOT invent per-component configs:

- `glide` (damping 26, mass 0.9, stiffness 120) — lateral slides (lower thirds)
- `settle` (damping 24, mass 0.8, stiffness 110) — scale/rise settles (cards, logo)
- `pop` (damping 11, mass 0.5, stiffness 170) — snappy badges, tiny overshoot allowed
- `caption` (damping 200, mass 0.6, stiffness 140) — overdamped caption page rise

Opacity is ALWAYS decoupled from the spring: enter `easeOut(cubic)` ~0.3s, exit `easeIn(cubic)`
~0.45s, crossfades `easeInOut(cubic)`. Nothing hard-appears or hard-cuts; everything enters and
exits through an eased ramp. Cuts every 4–7s. Story→story fade 10f; act-break fade 18f (after
cold-open, into CTA). Logo sting 1.2s. In video, TIME is hierarchy — the first element to enter
is the most important; stagger entrances ~0.12s, never simultaneous.

## Frame composition

- Every shot carries the radial vignette (center→edge to 22% black) above footage, below text.
- Designed cards composite text over REAL cinematic backgrounds (GPT-image pool on the SSD) with
  a 58% black veil — never flat gradient slides. "This isn't a PowerPoint."
- Background depth: scenes need 2–4 ambient decoratives (accent-tinted glows, hairline rules,
  grain) with slow drift/breath — static backdrops read dead. Use the darkField gradient as floor.
- Lead the eye somewhere: asymmetric layouts by default; centered only for solemn/branded beats.
- Safe zones: captions bottom-150px center; lower thirds left-80/bottom-260; bug top-44/right-52.

## Banned moves (AI-design tells)

Gradient text (`background-clip: text`), left-edge accent stripes on cards, identical card grids,
floating "keyword cards"/highlight rectangles over footage, two sans-serifs, weight 400-vs-700
"contrast", web-size type (<20px equivalents), purple-to-blue gradients, flat blue-gradient text
slides, everything-centered-equal-weight, pure #000/#fff.
