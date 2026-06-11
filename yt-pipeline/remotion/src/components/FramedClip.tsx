import { AbsoluteFill, OffthreadVideo, interpolate, spring, useCurrentFrame, useVideoConfig, Easing } from "remotion";
import { abnGradient, abnTokens, typeStyle } from "../brand/abnTokens";
import { KenBurnsArtifact } from "./KenBurnsArtifact";

// FramedClip — the "layered theatrics" shot treatment. Instead of a full-bleed static hold,
// the shot's media floats as a FRAMED clip (screen recording / press clipping) over an animated
// background layer. Renders for any Shot with `frame: "clip"`:
//   bg layer (animated b-roll mp4 OR breathing darkField gradient)
//     → dark veil (only over video bg, so the frame pops)
//       → framed media (video or Ken-Burns image) with hairline border + deep shadow
//         → source chip (mono/data voice) inside the frame's bottom-left edge
// The global vignette + karaoke captions in Episode.tsx composite ABOVE all of this.

type KB = { startScale: number; endScale: number; startX: number; startY: number; endX: number; endY: number; easing?: string };

// hex token → rgba(...) so every translucent surface stays derived from the palette atoms
// (no invented colors — DESIGN.md palette rule).
const rgba = (hex: string, a: number) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};

export const FramedClip: React.FC<{
  src: string;
  isVideo: boolean;
  shotIndex: number;
  bgSrc?: string;
  sourceChip?: string;
  kenBurns?: KB;
  muteSource?: boolean;
  startFromFrames?: number;
}> = ({ src, isVideo, shotIndex, bgSrc, sourceChip, kenBurns, muteSource, startFromFrames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Entrance per the motion grammar: `settle` spring drives the scale (0.96→1), opacity is
  // DECOUPLED on its own eased ramp (enter = easeOut cubic, ~0.3s) — nothing hard-appears.
  const settle = spring({ frame, fps, config: abnTokens.motion.springs.settle });
  const scale = interpolate(settle, [0, 1], [0.96, 1]);
  const opacity = interpolate(frame, [0, fps * abnTokens.motion.enterSec], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic),
  });

  // Per-shot organic variation: alternate a slight vertical offset + rotation by shot index so
  // consecutive framed shots don't land on the exact same pixels (kills the "template" feel).
  const flip = shotIndex % 2 === 0 ? -1 : 1;
  const offsetYPct = 2 * flip;   // -2% / +2%
  const rotateDeg = 0.6 * flip;  // -0.6deg / +0.6deg

  // Slow ambient breath for the gradient fallback (~4.4s cycle) — DESIGN.md: static backdrops
  // read dead; scenes need ambient decoratives with slow drift.
  const breathe = Math.sin((frame / fps) * Math.PI * 0.45);

  return (
    <AbsoluteFill style={{ background: abnTokens.colors.voidBlack }}>
      {/* ── BACKGROUND LAYER ─────────────────────────────────────────────────────── */}
      {bgSrc ? (
        <AbsoluteFill style={{ zIndex: abnTokens.layers.footage }}>
          {/* NOTE: OffthreadVideo has no `loop` prop, and <Loop> needs the bg's exact duration
              up front. In practice shots run 4–7s while bg b-roll clips run ~8s+, so the clip
              never exhausts inside its Sequence — we deliberately play it un-looped and let the
              Sequence duration clamp it. If shorter bg clips ever enter the library, wrap this
              in <Loop durationInFrames={bgDurationInFrames}> with a measured duration. */}
          <OffthreadVideo src={bgSrc} muted style={{ width: "100%", height: "100%", objectFit: "cover" }} />
          {/* dark veil so the framed clip pops off the moving footage */}
          <AbsoluteFill style={{ background: rgba(abnTokens.colors.voidBlack, 0.55) }} />
        </AbsoluteFill>
      ) : (
        <AbsoluteFill style={{ background: abnGradient.darkField, zIndex: abnTokens.layers.footage }}>
          {/* breathing accent glows (signal-blue + forge-red, the two brand voices at whisper
              opacity) so the fallback never reads as a flat static slide */}
          <AbsoluteFill style={{
            background:
              `radial-gradient(circle at ${30 + breathe * 4}% ${72 - breathe * 3}%, ${rgba(abnTokens.colors.signalBlue, 0.07 + 0.02 * breathe)}, transparent 34%), ` +
              `radial-gradient(circle at ${74 - breathe * 3}% ${24 + breathe * 4}%, ${rgba(abnTokens.colors.forgeRed, 0.05 + 0.015 * breathe)}, transparent 30%)`,
          }} />
        </AbsoluteFill>
      )}

      {/* ── FRAMED CLIP ──────────────────────────────────────────────────────────── */}
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div style={{
          position: "relative",
          width: "78%",
          aspectRatio: "16 / 9",
          transform: `translateY(${offsetYPct}%) rotate(${rotateDeg}deg) scale(${scale})`,
          opacity,
          willChange: "transform, opacity",
          borderRadius: abnTokens.radius.lg,
          border: `1px solid ${rgba(abnTokens.colors.steelWhite, 0.10)}`,
          boxShadow: "0 40px 90px rgba(0,0,0,0.55)",
          background: abnTokens.colors.panelBlack,
        }}>
          {/* media well — clips the media to the frame radius */}
          <div style={{ position: "absolute", inset: 0, borderRadius: abnTokens.radius.lg, overflow: "hidden" }}>
            {isVideo
              ? <OffthreadVideo src={src} muted={muteSource !== false} startFrom={startFromFrames || 0}
                  style={{ width: "100%", height: "100%", objectFit: "cover" }} />
              /* images keep their Ken Burns move INSIDE the frame — the frame floats, the
                 artifact still breathes within it */
              : <KenBurnsArtifact src={src} kb={kenBurns} />}
          </div>
          {/* source chip — data voice (mono, signalCyan), anchored inside the frame's
              bottom-left edge */}
          {sourceChip && (
            <div style={{
              position: "absolute",
              left: abnTokens.space.lg,
              bottom: abnTokens.space.lg,
              ...typeStyle(abnTokens.type.label),
              color: abnTokens.colors.signalCyan,
              background: rgba(abnTokens.colors.panelBlack, 0.85),
              borderRadius: abnTokens.radius.sm,
              padding: "6px 14px",
            }}>
              {sourceChip}
            </div>
          )}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
