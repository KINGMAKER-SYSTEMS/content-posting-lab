import { AbsoluteFill, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig, Easing } from "remotion";
import { useMemo, Fragment } from "react";
import { abnTokens, typeStyle } from "../brand/abnTokens";

type Word = { w: string; s: number; e: number };
const STROKE =
  "3px 3px 0 #000,-3px -3px 0 #000,3px -3px 0 #000,-3px 3px 0 #000,0 4px 8px rgba(0,0,0,.6)";

// group words into ~5-word phrase pages
function pages(words: Word[]) {
  const out: { start: number; end: number; toks: Word[] }[] = [];
  let cur: Word[] = [];
  for (const w of words) {
    cur.push(w);
    if (cur.length >= 5) {
      out.push({ start: cur[0].s, end: cur[cur.length - 1].e, toks: cur });
      cur = [];
    }
  }
  if (cur.length) out.push({ start: cur[0].s, end: cur[cur.length - 1].e, toks: cur });
  return out;
}

const Page: React.FC<{ pg: { start: number; toks: Word[] }; accent: string; durFrames: number }> = ({ pg, accent, durFrames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const absSec = pg.start + frame / fps;

  // Page-level entrance: soft spring up + fade (no hard pop). Springs settle ~12f.
  const enter = spring({ frame, fps, config: abnTokens.motion.springs.caption });
  const enterY = interpolate(enter, [0, 1], [26, 0]);
  const enterOpacity = interpolate(enter, [0, 1], [0, 1]);
  // Page-level exit: ease out over the last ~8 frames so pages don't snap off-screen.
  const FADE = Math.min(8, Math.max(3, Math.round(durFrames * 0.18)));
  const exitOpacity = interpolate(frame, [durFrames - FADE, durFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.in(Easing.cubic),
  });
  const opacity = Math.min(enterOpacity, exitOpacity);

  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "center",
      paddingBottom: abnTokens.safeZones.longform.captionBottom, zIndex: abnTokens.layers.captions }}>
      {/* DISPLAY font (TikTok Sans, now genuinely loaded via brand/fonts.ts) to MATCH the designed
          cards + lower-thirds — one typeface channel-wide. type.caption bakes the video rules:
          1.25 lineHeight (dark-bg halo compensation) + slight-tight tracking. */}
      <div style={{ ...typeStyle(abnTokens.type.caption),
        textAlign: "center", whiteSpace: "pre-wrap", maxWidth: "82%",
        textShadow: STROKE,
        background: "rgba(8,11,15,0.62)", padding: `${abnTokens.space.md}px ${abnTokens.space.xl}px`,
        borderRadius: abnTokens.radius.md,
        boxShadow: "0 0 40px 30px rgba(8,11,15,0.45)",
        opacity, transform: `translateY(${enterY}px)`, willChange: "transform, opacity" }}>
        {pg.toks.map((t, i) => {
          // Smooth per-word karaoke highlight: instead of a hard on/off scale+color swap,
          // ramp an `lit` value 0→1 across a short window around the word's start so the
          // active word eases up in scale and cross-fades cyan instead of popping.
          const RAMP = 0.09; // seconds to ease the highlight in
          // Build a STRICTLY-monotonic inputRange. Whisper alignment can emit t.s === t.e (zero-width
          // words) or near-equal stamps, and Remotion's interpolate throws on a non-increasing range.
          // Force each point at least 1ms above the previous.
          const EPS = 0.001;
          let r0 = t.s - RAMP, r1 = t.s, r2 = t.e, r3 = t.e + 0.06;
          r1 = Math.max(r1, r0 + EPS);
          r2 = Math.max(r2, r1 + EPS);
          r3 = Math.max(r3, r2 + EPS);
          const lit = interpolate(absSec, [r0, r1, r2, r3], [0, 1, 1, 0], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: Easing.out(Easing.cubic),
          });
          const scale = interpolate(lit, [0, 1], [1, 1.08]);
          // cross-fade white → accent. lerp via opacity-stacked spans would be heavier; a
          // mix-friendly approach: layer accent text over white and fade the accent in.
          // Render each word as an inline-block (needed for the per-word scale) followed by a REAL
          // space text node. Margin alone let adjacent words visually touch when a glyph overhangs its
          // box (caught on a real frame: "Microsoft'sScout" fused). The explicit space guarantees a gap.
          return (
            <Fragment key={i}>
              <span style={{ position: "relative", display: "inline-block",
                // .09em: at the karaoke crossover BOTH adjacent words sit at scale 1.08 and each
                // overhangs ~3px into the shared gap — .06em let them visually kiss at 68px.
                margin: "0 .09em", transform: `scale(${scale})`, willChange: "transform" }}>
                <span style={{ color: "#fff" }}>{t.w.trim()}</span>
                <span style={{ position: "absolute", left: 0, top: 0, color: accent,
                  opacity: lit, textShadow: `0 0 ${interpolate(lit, [0, 1], [0, 18])}px ${accent}66` }}
                  aria-hidden>{t.w.trim()}</span>
              </span>
              {i < pg.toks.length - 1 ? " " : ""}
            </Fragment>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

export const KaraokeCaptions: React.FC<{ words: Word[]; accent: string }> = ({ words, accent }) => {
  const { fps } = useVideoConfig();
  const pg = useMemo(() => pages(words || []), [words]);
  return (
    <AbsoluteFill>
      {pg.map((p, i) => {
        const sf = Math.round(p.start * fps);
        const ef = Math.round((pg[i + 1] ? pg[i + 1].start : p.end + 0.4) * fps);
        const dur = ef - sf;
        if (dur <= 0) return null;
        return (
          <Sequence key={i} from={sf} durationInFrames={dur}>
            <Page pg={p} accent={accent} durFrames={dur} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
