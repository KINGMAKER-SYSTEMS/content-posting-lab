import { Composition, CalculateMetadataFunction } from "remotion";
import { Episode } from "./Episode";
import { abnTokens } from "./brand/abnTokens";
import "./brand/fonts"; // side-effect: registers TikTok Sans with the renderer before any frame draws

const calc: CalculateMetadataFunction<any> = ({ props }) => {
  const fps = props.fps || 30;
  const segFrames = (props.segments || []).reduce(
    (a: number, s: any) => a + Math.round((s.durationSec || 0) * fps), 0);
  // segment→segment fades are 10-18f; sting adds ~2.2s + its 14f fade. Use a small net overlap
  // estimate; slight overshoot is safe (TransitionSeries clamps), undershoot cuts the last segment.
  const n = (props.segments || []).length;
  const segOverlaps = Math.max(0, n - 1) * 12;             // avg of 10/18 transitions
  // MUST mirror Episode.tsx: sting renders at 1.2s with a 9f fade. This was still budgeting the
  // old 2.2s/14f sting, which padded ~1s of dead black onto the end of every episode.
  const sting = props.logo ? Math.round(fps * abnTokens.motion.logoStingSec) - 9 : 0;
  return { durationInFrames: Math.max(1, segFrames - segOverlaps + sting), fps,
           width: props.width || 1920, height: props.height || 1080 };
};

export const RemotionRoot = () => (
  <Composition id="Episode" component={Episode as any}
    defaultProps={{ fps: 30, width: 1920, height: 1080, accent: abnTokens.colors.signalCyan, segments: [] } as any}
    calculateMetadata={calc} fps={30} width={1920} height={1080} durationInFrames={1} />
);
