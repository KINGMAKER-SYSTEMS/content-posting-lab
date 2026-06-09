// ABN "Forge Signal" design tokens — the single source of truth for the Remotion compositor.
// Renderer-agnostic contract (palette / type / motion grammar) lives in remotion/DESIGN.md;
// this file is the typed projection of it. Components must consume tokens, not magic numbers.

export const abnTokens = {
  brand: {
    id: "agentic-builder-news",
    displayName: "Agentic Builder News",
    shortName: "ABN",
    systemName: "Forge Signal",
  },

  // Two-voice palette, on purpose: forge RED is the editorial/alert voice, signal CYAN is the
  // data/source voice. Cyan-on-dark is a known AI-design tell — we keep it ONLY because the whole
  // brand (logo lockup, card library, thumbnails) is already built on the red/cyan duo; what kills
  // the tell is discipline: one accent voice per element, neutrals tinted toward blue, never both
  // accents on the same element.
  colors: {
    voidBlack: "#030405",
    nearBlack: "#080B0F",
    panelBlack: "#0D1117",
    forgeRed: "#FF2B2F",
    forgeRedDark: "#9F0711",
    signalCyan: "#7FD2FF",
    signalBlue: "#16A7FF",
    steelWhite: "#F4F7FB",
    steelMid: "#8F99A7",
    warningGold: "#FFCD38",
    bodyInk: "#CBD7E1",
    success: "#39E58C",
  },

  // One expressive family (TikTok Sans, the brand face — same TTFs ImageMagick burns onto cards)
  // + one receding mono for data/source attribution. NO second sans: Inter/Montserrat are gone
  // from the chain — pairing two sans-serifs is friction without hierarchy, and Inter is the
  // canonical AI-default font. Families here must be LOADED via brand/fonts.ts, never assumed.
  fonts: {
    display: "TikTok Sans Display, Arial Black, sans-serif", // 36pt optical cut, 800/900 only
    text: "TikTok Sans Text, Arial, sans-serif",             // 16pt optical cut, 600/700
    mono: "JetBrains Mono, SF Mono, Menlo, monospace",       // data, source labels, tagline
  },

  // Type scale for the 1920×1080 frame. Video rules baked in: extreme weight contrast (900 vs 400,
  // not 700 vs 400), tracking tighter than web on display sizes (encoding eats letter detail),
  // +0.05–0.1 lineHeight on dark backgrounds (light-on-dark halo effect), tabular numerals on data.
  type: {
    titleCard:   { size: 96, weight: 900, lineHeight: 1.08, tracking: "-0.025em", family: "display" },
    caption:     { size: 68, weight: 800, lineHeight: 1.25, tracking: "-0.01em",  family: "display", maxLines: 2 },
    lowerThird:  { size: 56, weight: 900, lineHeight: 1.1,  tracking: "-0.02em",  family: "display", maxChars: 54 },
    statValue:   { size: 150, weight: 900, lineHeight: 0.95, tracking: "-0.03em", family: "display", numeric: "tabular-nums" },
    label:       { size: 26, weight: 700, lineHeight: 1.2,  tracking: "0.06em",   family: "mono" },   // source urls, badges
    micro:       { size: 22, weight: 400, lineHeight: 1.2,  tracking: "0.04em",   family: "mono" },   // tagline, watermarks
  },

  // 4px-base spacing scale. Padding/margins/offsets snap to these — no invented gaps.
  space: { xs: 4, sm: 8, md: 16, lg: 24, xl: 32, xxl: 48, gutter: 80, stage: 120 },

  radius: { sm: 8, md: 14, lg: 20 },

  // Motion grammar. THREE springs cover every entrance in the show — components must not invent
  // their own configs (we had five inconsistent damping values across four files).
  motion: {
    springs: {
      glide:  { damping: 26, mass: 0.9, stiffness: 120 },  // lateral slides (lower thirds)
      settle: { damping: 24, mass: 0.8, stiffness: 110 },  // scale/rise settles (cards, logo)
      pop:    { damping: 11, mass: 0.5, stiffness: 170 },  // snappy badge pops (tiny overshoot ok)
      caption:{ damping: 200, mass: 0.6, stiffness: 140 }, // overdamped caption page rise
    },
    // Eased opacity always decoupled from the spring (springs start ~0 but opacity needs its own
    // clean ramp): enter = Easing.out(cubic), exit = Easing.in(cubic), x-fade = inOut(cubic).
    enterSec: 0.3,
    enterFastSec: 0.18,
    exitSec: 0.45,
    fadeNormalFrames: 10,   // story→story cut
    fadeActBreakFrames: 18, // cold-open exit + into final/CTA segment
    logoStingSec: 1.2,
    lowerThirdSec: 3.5,
    cutIntervalSec: { min: 4, max: 7 },
  },

  // Layer stack (z-order contract): footage 0 → fx/vignette 10 → captions 30 → lower thirds 40 →
  // pops/badges 50 → branding/sting 60. Nothing renders above branding.
  layers: { footage: 0, fx: 10, captions: 30, lowerThirds: 40, pops: 50, branding: 60 },

  canvas: {
    longform: { width: 1920, height: 1080, fps: 30 },
    thumbnail: { width: 1280, height: 720 },
    shorts: { width: 1080, height: 1920 },
    banner: {
      width: 2560,
      height: 1440,
      youtubeSafeArea: { x: 281, y: 489, width: 1546, height: 423 },
    },
  },

  safeZones: {
    thumbnail: {
      marginX: 102,
      marginY: 72,
      avoidBottomRight: { width: 220, height: 110 },
      avoidBottomLeft: { width: 150, height: 110 },
    },
    longform: {
      captionBottom: 150,
      lowerThirdLeft: 80,
      lowerThirdBottom: 260,
      bugTop: 44,
      bugRight: 52,
    },
    shorts: {
      topDeadZone: 180,
      bottomDeadZone: 300,
      centerSafeWidth: 860,
    },
  },

  assets: {
    root: "/agenticnews-assets/brand/abn-forge-signal",
    logoPrimary: "/agenticnews-assets/brand/abn-forge-signal/assets/source/abn-logo-primary.png",
    channelBanner: "/agenticnews-assets/brand/abn-forge-signal/assets/png/youtube-channel-banner.png",
    lowerThirdTemplate: "/agenticnews-assets/brand/abn-forge-signal/assets/png/lower-third-topic.png",
    topRightBug: "/agenticnews-assets/brand/abn-forge-signal/assets/png/top-right-bug.png",
    breakingStrap: "/agenticnews-assets/brand/abn-forge-signal/assets/png/breaking-strap.png",
    chapterTitleCard: "/agenticnews-assets/brand/abn-forge-signal/assets/png/chapter-title-card.png",
    endScreen: "/agenticnews-assets/brand/abn-forge-signal/assets/png/end-screen.png",
    shortsCover: "/agenticnews-assets/brand/abn-forge-signal/assets/png/shorts-cover.png",
  },

  thumbnailFormulas: {
    forgeAlert: {
      id: "forge-alert",
      bestFor: "breaking model/tool/platform change",
      hookExamples: ["AI SHIFT", "MCP TRAP", "FREE AGENTS", "TOOLS BROKE"],
      template: "/agenticnews-assets/brand/abn-forge-signal/assets/png/thumbnail-template-forge-alert.png",
    },
    realTest: {
      id: "real-test",
      bestFor: "live build, tool test, benchmark replacement",
      hookExamples: ["REAL TEST", "BUILT IT", "FAILED LIVE", "NO BENCHMARK"],
      template: "/agenticnews-assets/brand/abn-forge-signal/assets/png/thumbnail-template-tool-test.png",
    },
    buildBrief: {
      id: "build-brief",
      bestFor: "weekly recap, list video, short cover",
      hookExamples: ["BUILD BRIEF", "3 SHIPS", "STEAL THIS", "NEW STACK"],
      template: "/agenticnews-assets/brand/abn-forge-signal/assets/png/thumbnail-template-build-brief.png",
    },
  },
} as const;

export type AbnTokens = typeof abnTokens;

// Resolve a type token to a CSS style object (keeps components one-liners).
export const typeStyle = (t: { size: number; weight: number; lineHeight: number; tracking: string; family: string; numeric?: string }) => ({
  fontFamily: abnTokens.fonts[t.family as keyof typeof abnTokens.fonts],
  fontSize: t.size,
  fontWeight: t.weight,
  lineHeight: t.lineHeight,
  letterSpacing: t.tracking,
  ...(t.numeric ? { fontVariantNumeric: t.numeric } : {}),
});

export const abnGradient = {
  darkField:
    "radial-gradient(circle at 82% 18%, rgba(22,167,255,0.09), transparent 18%), radial-gradient(circle at 18% 78%, rgba(255,43,47,0.08), transparent 22%), linear-gradient(135deg, #111923 0%, #030405 46%, #000000 100%)",
  glass:
    "linear-gradient(135deg, rgba(21,32,49,0.88) 0%, rgba(5,7,10,0.86) 100%)",
  forge:
    "linear-gradient(180deg, #ff6468 0%, #ff2b2f 42%, #9f0711 100%)",
};
