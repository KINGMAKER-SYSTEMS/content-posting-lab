# Leptos frontend spike — findings & verdict

**Question:** could we rebuild the entire React frontend in Leptos and make it
look EXACTLY the same?

**Short answer:** technically yes, and this spike proves it — but it's a
multi-week, high-risk rewrite that delivers *zero* user-facing value and would
block the cutover that's the whole point of the Rust rewrite. **Recommendation:
keep React for cutover.** Treat Leptos as a possible *later* all-Rust initiative,
done page-by-page, never as part of getting the Rust backend live.

## What the spike proves (working artifact)

`leptos-home-spike/` is a real Leptos 0.7 → WASM app reproducing the **Home**
dashboard. It builds and runs:
- `trunk build` → WASM, clean (36s cold, ~2s incremental).
- Renders the brand look **pixel-faithfully**: dark chrome (#060606/#141414),
  the magenta→purple gradient title, stat cards, colored pipeline badges,
  project cards with the active-magenta border — all from the same design tokens
  in `frontend/src/index.css`.
- **Live reactivity** (real WASM, not static): "+ New Project" prepends a card
  and the derived totals update; per-card delete; active-card selection — all via
  Leptos signals + a `<For>` loop + derived signals (the 1:1 replacements for
  React `useState`/`useMemo`/`.map`).

So: Leptos is viable, the toolchain works here, and identical-look is achievable.

## The catch that decides it: the styling toolchain stays JavaScript

The "exact same look" does **not** come from React — it comes from **Tailwind v4
+ shadcn + the brand theme** (`@import "tailwindcss"` etc. in index.css). Leptos
replaces the *component logic*, not the *CSS pipeline*. To reproduce the look you
still run the **Node + Tailwind v4 + shadcn** build over the Leptos markup
(Tailwind scans `.rs` class strings the same way it scans `.tsx`). So:

> "Rewrite the frontend in Leptos" ≠ "all Rust, no Node."
> You keep npm + Tailwind + PostCSS. You only delete React.

That removes most of the *motivation* (single-language stack) while keeping the
JS build's cost.

## What the full rewrite actually costs

Every one of ~18 pages rewrites from React into Leptos, including the heavy ones:
- `Generate.tsx` (1700 lines), `Burn.tsx` (1783), `Clipper.tsx` (1269),
  `Distribution.tsx` + 6 tab files — thousands of lines of hooks/effects/stores.
- **shadcn/ui** — every primitive (Button, Card, Dialog, Dropdown, Toast, Input,
  Badge, …) is a React/Radix component with no Leptos equivalent → hand-rebuild
  each with matching a11y + classes. (This spike hand-wrote the trivial ones;
  Dialog/Dropdown/Toast with focus-trap + portals are the real work.)
- **The canvas caption editor** in Burn (`captureTextOverlay`) renders the
  browser PNG the ffmpeg burn pipeline *depends on* — reimplement via `web-sys`
  canvas + pointer events.
- Zustand `workflowStore` → Leptos stores/context; `react-router` →
  `leptos_router`; the WS/SSE hooks → `gloo-net`/`web-sys`.

Rough estimate: **multi-week**, front-loaded risk on the 3–4 complex pages, and a
long tail of shadcn parity. Bundle: this one page is 1.4 MB debug / 274 KB
gzipped (≈150 KB gzipped with release + wasm-opt) — the Leptos runtime is a fixed
cost comparable to React's, so no win there either.

## Why it's the wrong thing *now*

The rewrite's value is getting **off the clunky Python onto the clean Rust
backend**. That backend is done and deployable against the existing React app
today. Gating cutover behind a UI rewrite whose best-case outcome is "same app,
now in WASM" trades a real win (Rust live) for months of risk and no user-visible
change. You already made this call once ("react is so complex at this point") —
nothing since changed the math.

## If you ever do want it (the sane path)

1. Ship the Rust backend + React cutover first. Prove it live.
2. Then, *incrementally*, behind the same Tailwind CSS: port one page at a time,
   simplest first (Home ✓ — this spike), each shipped independently. Never
   big-bang.
3. Build the shadcn-parity primitive library once, up front, as its own crate.

This spike is that step-1 artifact for Home. It's isolated (not a workspace
member; its WASM deps never touch the server build) and safe to delete.
