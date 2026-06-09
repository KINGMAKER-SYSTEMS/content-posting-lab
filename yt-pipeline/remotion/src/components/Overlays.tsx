import { AbsoluteFill, spring, interpolate, useCurrentFrame, useVideoConfig, Easing } from "remotion";
import { abnGradient, abnTokens, typeStyle } from "../brand/abnTokens";

// KeywordCard DELETED — it was the floating "slop rectangle" no real channel uses; Episode.tsx
// stopped rendering it earlier and dead code shouldn't survive a token migration.

export const LowerThird: React.FC<{ headline: string; sourceUrl?: string; startSec: number; durationSec: number; accent: string }> =
({ headline, sourceUrl, startSec, durationSec, accent }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - Math.round(startSec * fps);
  const total = durationSec * fps;
  if (local < 0 || local > total) return null;

  // Slide-in: a soft, well-damped spring (no bounce) for a smooth glide, not a mechanical snap.
  const slide = spring({ frame: local, fps, config: abnTokens.motion.springs.glide });
  const x = interpolate(slide, [0, 1], [-90, 0]);
  // Decouple opacity from the slide position — fade in on its own eased curve so the
  // text reads in cleanly instead of being half-transparent while it travels.
  const enterOpacity = interpolate(local, [0, fps * 0.3], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  // Exit: slide back out a touch + eased fade.
  const exit = interpolate(local, [total - fps * 0.5, total], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.in(Easing.cubic),
  });
  const exitX = interpolate(exit, [0, 1], [-40, 0]);
  // The accent rule wipes in horizontally for a crisp, intentional reveal.
  const ruleW = interpolate(slide, [0, 1], [0, 80]);

  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "flex-start",
      padding: abnTokens.safeZones.longform.lowerThirdLeft,
      paddingBottom: abnTokens.safeZones.longform.lowerThirdBottom, zIndex: abnTokens.layers.lowerThirds }}>
      <div style={{ transform: `translateX(${x + exitX}px)`, opacity: Math.min(enterOpacity, exit), willChange: "transform, opacity" }}>
        <div style={{ background: accent, height: 7, width: ruleW, marginBottom: abnTokens.space.sm + 2,
          boxShadow: `0 0 14px ${accent}77` }} />
        <div style={{ ...typeStyle(abnTokens.type.lowerThird), color: abnTokens.colors.steelWhite,
          textShadow: "0 2px 14px #000", maxWidth: 1100 }}>{headline}</div>
        {/* source attribution speaks in the MONO voice (data register) — cyan, wide-tracked */}
        {sourceUrl && <div style={{ ...typeStyle(abnTokens.type.label), color: abnTokens.colors.signalCyan, marginTop: abnTokens.space.xs }}>
          {sourceUrl.replace(/^https?:\/\//, "").slice(0, 50)}</div>}
      </div>
    </AbsoluteFill>
  );
};

export const TitleCard: React.FC<{ headline: string; accent: string }> = ({ headline, accent }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  // Smooth, no-bounce spring entrance for the whole card.
  const p = spring({ frame, fps, config: abnTokens.motion.springs.settle });
  const cardScale = interpolate(p, [0, 1], [0.94, 1]);
  const enterOpacity = interpolate(frame, [0, fps * 0.4], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  // Eased fade-out near the end so the card never hard-cuts.
  const exitOpacity = interpolate(frame, [durationInFrames - fps * 0.4, durationInFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.in(Easing.cubic),
  });
  // Accent rule + headline stagger: rule wipes first, headline rises just after.
  const ruleW = interpolate(p, [0, 1], [0, 120]);
  const headRise = interpolate(
    spring({ frame: frame - Math.round(fps * 0.12), fps, config: abnTokens.motion.springs.settle }),
    [0, 1], [22, 0]
  );

  return (
    <AbsoluteFill style={{ background: abnGradient.darkField, justifyContent: "center", alignItems: "center", padding: abnTokens.space.stage }}>
      <div style={{ transform: `scale(${cardScale})`, opacity: Math.min(enterOpacity, exitOpacity), textAlign: "center", willChange: "transform, opacity" }}>
        <div style={{ background: accent, height: 8, width: ruleW, margin: `0 auto ${abnTokens.space.lg + 4}px`, boxShadow: `0 0 16px ${accent}88` }} />
        <div style={{ ...typeStyle(abnTokens.type.titleCard), color: abnTokens.colors.steelWhite,
          transform: `translateY(${headRise}px)` }}>
          {headline}</div>
      </div>
    </AbsoluteFill>
  );
};
