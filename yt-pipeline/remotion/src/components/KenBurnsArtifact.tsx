import { AbsoluteFill, Img, interpolate, useCurrentFrame, useVideoConfig, Easing } from "remotion";
import { useState } from "react";

type KB = { startScale: number; endScale: number; startX: number; startY: number; endX: number; endY: number; easing?: string };

const ease = (e?: string) =>
  e === "easeIn" ? Easing.in(Easing.ease) :
  e === "easeOut" ? Easing.out(Easing.ease) :
  e === "linear" ? Easing.linear : Easing.inOut(Easing.ease);

// Highlight-box rendering 100% DELETED (was the `boxes` prop): the floating labeled rectangles
// were broken-looking clutter ("Rapid-MLX" boxing random title text). Keyword emphasis belongs
// in the karaoke captions. This component is now purely the Ken-Burns image move.
export const KenBurnsArtifact: React.FC<{ src: string; kb?: KB; boxes?: unknown; accent?: string }> =
({ src, kb }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
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
    </AbsoluteFill>
  );
};
