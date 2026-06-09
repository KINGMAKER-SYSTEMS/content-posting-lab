import { AbsoluteFill, Img, interpolate, spring, useCurrentFrame, useVideoConfig, Easing } from "remotion";
import { useState } from "react";
import { abnTokens } from "../brand/abnTokens";

type KB = { startScale: number; endScale: number; startX: number; startY: number; endX: number; endY: number; easing?: string };
type Box = { x: number; y: number; w: number; h: number; color: string; opacity?: number; borderWidth?: number; label?: string; atSec?: number };

const ease = (e?: string) =>
  e === "easeIn" ? Easing.in(Easing.ease) :
  e === "easeOut" ? Easing.out(Easing.ease) :
  e === "linear" ? Easing.linear : Easing.inOut(Easing.ease);

export const KenBurnsArtifact: React.FC<{ src: string; kb?: KB; boxes?: Box[]; accent: string }> =
({ src, kb, boxes = [], accent }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();
  const k = kb || { startScale: 1, endScale: 1.08, startX: 0.5, startY: 0.5, endX: 0.5, endY: 0.5 };
  const scale = interpolate(frame, [0, durationInFrames], [k.startScale, k.endScale],
    { extrapolateRight: "clamp", easing: ease(k.easing) });
  const ox = interpolate(frame, [0, durationInFrames], [k.startX, k.endX], { extrapolateRight: "clamp" }) * 100;
  const oy = interpolate(frame, [0, durationInFrames], [k.startY, k.endY], { extrapolateRight: "clamp" }) * 100;
  const [failed, setFailed] = useState(false);
  return (
    <AbsoluteFill style={{ background: "#08090b", overflow: "hidden" }}>
      <AbsoluteFill style={{ transform: `scale(${scale})`, transformOrigin: `${ox}% ${oy}%` }}>
        {!failed && src
          ? <Img src={src} onError={() => setFailed(true)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          : <AbsoluteFill style={{ background: "linear-gradient(135deg,#0c0d10,#16181d)" }} />}
      </AbsoluteFill>
      {boxes.map((b, i) => {
        const trig = Math.round((b.atSec || 0) * fps);
        const local = frame - trig;
        if (local < 0) return null;
        // Smooth, no-bounce draw-in for the highlight box; eased opacity ramp on top of the
        // spring so the border never hard-appears on frame 0.
        const draw = spring({ frame: local, fps, config: { damping: 22, mass: 0.6, stiffness: 130 } });
        const drawOpacity = interpolate(local, [0, fps * 0.18], [0, 1], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic),
        });
        const glow = interpolate(Math.sin((frame / fps) * 3), [-1, 1], [10, 30]);
        // Label rises into place + fades, slightly staggered after the box draws.
        const labelSpring = spring({ frame: local - Math.round(fps * 0.08), fps, config: { damping: 24, mass: 0.6, stiffness: 130 } });
        const labelRise = interpolate(labelSpring, [0, 1], [10, 0]);
        return (
          <div key={i} style={{ position: "absolute", left: b.x * width, top: b.y * height,
            width: b.w * width, height: b.h * height, border: `${b.borderWidth || 3}px solid ${b.color || accent}`,
            borderRadius: 8, opacity: (b.opacity ?? 0.9) * drawOpacity, transform: `scale(${interpolate(draw, [0, 1], [0.94, 1])})`,
            willChange: "transform, opacity",
            boxShadow: `0 0 ${glow}px ${b.color || accent}, inset 0 0 ${glow / 2}px ${b.color || accent}` }}>
            {b.label && (
              <div style={{ position: "absolute", top: -42, left: 0, background: b.color || accent,
                color: abnTokens.colors.voidBlack, fontWeight: 800, padding: "4px 12px", borderRadius: 6, fontSize: 26,
                fontFamily: abnTokens.fonts.mono, opacity: labelSpring, transform: `translateY(${labelRise}px)`, whiteSpace: "nowrap" }}>{b.label}</div>
            )}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
