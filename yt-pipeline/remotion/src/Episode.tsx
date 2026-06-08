import { AbsoluteFill, Sequence, Audio, OffthreadVideo, interpolate, spring, useCurrentFrame, useVideoConfig, Img, Easing } from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { KenBurnsArtifact } from "./components/KenBurnsArtifact";
import { KaraokeCaptions } from "./components/KaraokeCaptions";
import { KeywordCard, LowerThird, TitleCard } from "./components/Overlays";
import { abnGradient, abnTokens } from "./brand/abnTokens";

// assets are served by the lab over HTTP; remotion headless reads them via the URL.
const ASSET_BASE = (typeof process !== "undefined" && process.env && process.env.ABN_ASSET_BASE) || "http://localhost:8000";
const f = (path: string) => {
  if (!path) return path;
  if (path.startsWith("http")) return path;
  if (path.startsWith("/agenticnews-assets/")) return ASSET_BASE + path;
  if (path.startsWith("/")) return ASSET_BASE + "/agenticnews-assets/" + path.split("/").pop();
  return path;
};

const SegmentRenderer: React.FC<{ seg: any; accent: string }> = ({ seg, accent }) => {
  const shots = seg.shots || [];
  return (
    <AbsoluteFill style={{ background: abnTokens.colors.nearBlack }}>
      {/* shot layer: each shot is its own sub-sequence inside the segment */}
      {shots.map((sh: any, i: number) => {
        const { fps } = useVideoConfig();
        const from = Math.round((sh.startSec || 0) * fps);
        const dur = Math.max(1, Math.round(((sh.endSec || 0) - (sh.startSec || 0)) * fps));
        const boxes = sh.highlight ? [{ ...sh.highlight, atSec: 0.2 }] : [];
        const isCard = (sh.src || "").includes("_card");
        const isVideo = sh.type === "broll" || sh.type === "code" || sh.type === "screenrec";
        return (
          <Sequence key={i} from={from} durationInFrames={dur}>
            {isVideo
              ? <AbsoluteFill style={{ background: abnTokens.colors.panelBlack }}><OffthreadVideo src={f(sh.src)} muted={sh.muteSource !== false} startFrom={Math.round((sh.clipStartSec || 0) * fps)} style={{ width: "100%", height: "100%", objectFit: "contain", transform: sh.kenBurns ? `scale(${(sh.kenBurns.startScale||1)})` : undefined }} /></AbsoluteFill>
              : isCard
              ? <TitleCard headline={seg.title?.slice(0, 60) || ""} accent={accent} />
              : <KenBurnsArtifact src={f(sh.src)} kb={sh.kenBurns} boxes={boxes} accent={accent} />}
          </Sequence>
        );
      })}
      {/* global vignette (per EDITING-STYLE-GUIDE): radial center→edge to 22% black. Sits above the
          footage but below text — focuses the eye + kills the flat-slide look on every shot. */}
      <AbsoluteFill style={{ background: "radial-gradient(ellipse at center, rgba(0,0,0,0) 45%, rgba(0,0,0,0.22) 100%)", pointerEvents: "none" }} />
      {/* caption layer */}
      <KaraokeCaptions words={seg.wordTimestamps || []} accent={accent} />
      {/* keyword pops */}
      {(seg.keywordPops || []).map((k: any, i: number) => (
        <KeywordCard key={i} text={k.word} atSec={k.atSec ?? k.s ?? 0} durationSec={k.durationSec} color={k.color || accent} />
      ))}
      {/* lower thirds */}
      {(seg.lowerThirds || []).map((l: any, i: number) => (
        <LowerThird key={i} headline={l.headline || seg.title} sourceUrl={l.sourceUrl}
          startSec={l.startSec} durationSec={l.durationSec} accent={accent} />
      ))}
      {/* segment VO */}
      {seg.audio?.vo?.src && <Audio src={f(seg.audio.vo.src)} />}
    </AbsoluteFill>
  );
};

// 2.5s branded intro: logo springs in with a glow pulse, channel name fades up, then fades out.
const LogoSting: React.FC<{ logo: string; accent: string }> = ({ logo, accent }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  // Soft, no-bounce spring entrance — logo eases up from 0.6→1 and fades in together.
  const s = spring({ frame, fps, config: { damping: 18, mass: 0.8, stiffness: 120 } });
  const scale = interpolate(s, [0, 1], [0.62, 1]);
  const logoOpacity = interpolate(frame, [0, fps * 0.35], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic) });
  // Gentle breathing glow pulse.
  const glow = interpolate(Math.sin((frame / fps) * Math.PI * 1.2), [-1, 1], [12, 34]);
  // Tagline eases up + rises, instead of a linear cut-in.
  const nameSpring = spring({ frame: frame - Math.round(fps * 0.5), fps, config: { damping: 22, mass: 0.7, stiffness: 110 } });
  const nameOpacity = interpolate(nameSpring, [0, 1], [0, 1]);
  const nameRise = interpolate(nameSpring, [0, 1], [14, 0]);
  const fadeOut = interpolate(frame, [durationInFrames - fps * 0.4, durationInFrames], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.in(Easing.cubic) });
  // The brand logo is a FULL LOCKUP (Agentic Builder NEWS + anvil mark baked in), so we present it
  // large and DON'T re-print the channel name — just the logo + tagline. (No duplicate wordmark.)
  return (
    <AbsoluteFill style={{ background: abnGradient.darkField, justifyContent: "center", alignItems: "center", opacity: fadeOut }}>
      <Img src={f(logo)} style={{ width: 760, maxWidth: "62%", height: "auto", transform: `scale(${scale})`, opacity: logoOpacity, filter: `drop-shadow(0 0 ${glow}px ${accent})`, willChange: "transform, opacity" }} />
      <div style={{ marginTop: 18, fontFamily: abnTokens.fonts.mono, fontSize: 22, letterSpacing: "0.04em", color: accent, opacity: nameOpacity, transform: `translateY(${nameRise}px)` }}>the agentic builder brief</div>
    </AbsoluteFill>
  );
};

export const Episode: React.FC<any> = (props) => {
  const { fps } = useVideoConfig();
  const segs = props.segments || [];
  const accent = props.accent || abnTokens.colors.signalCyan;
  return (
    <AbsoluteFill style={{ background: abnTokens.colors.nearBlack }}>
      <TransitionSeries>
        {/* SNAPPY 1.2s branded logo sting — hook-first: the directive wants the first 5s to BE the
            hook, so the logo intro is kept short (was 2.2s) and hands off to the hook card fast
            (lands by ~1.3s) for maximum opening impact. */}
        {props.logo ? [
          <TransitionSeries.Sequence key="sting" durationInFrames={Math.round(fps * 1.2)}>
            <LogoSting logo={props.logo} accent={accent} />
          </TransitionSeries.Sequence>,
          <TransitionSeries.Transition key="sting-t" presentation={fade()} timing={linearTiming({ durationInFrames: 9, easing: Easing.inOut(Easing.cubic) })} />,
        ] : []}
        {segs.flatMap((seg: any, i: number) => {
          const dur = Math.max(1, Math.round((seg.durationSec || 0) * fps));
          const nodes: any[] = [
            <TransitionSeries.Sequence key={`s${i}`} durationInFrames={dur}>
              <SegmentRenderer seg={seg} accent={accent} />
            </TransitionSeries.Sequence>,
          ];
          if (i < segs.length - 1) {
            // Per EDITING-STYLE-GUIDE: 16f everywhere feels mushy for a hard-cutting channel. Use a
            // tight 10f fade for normal story→story cuts; reserve a longer 18f act-break fade only
            // after the cold-open (seg 0 → 1) and into the final/CTA segment.
            const isActBreak = i === 0 || i === segs.length - 2;
            nodes.push(
              <TransitionSeries.Transition key={`t${i}`} presentation={fade()}
                timing={linearTiming({ durationInFrames: isActBreak ? 18 : 10, easing: Easing.inOut(Easing.cubic) })} />
            );
          }
          return nodes;
        })}
      </TransitionSeries>
      {/* music bed is now mixed POST-render via ffmpeg sidechaincompress (real ducking under VO),
          not baked here. Only render it in Remotion if explicitly requested (legacy path). */}
      {props.musicBedRender && <MusicBed src={f(props.musicBedRender)} />}
      {/* whoosh SFX on each segment boundary (sound design per style guide) */}
      {props.sfx && segs.slice(1).map((_: any, i: number) => {
        const startFrame = segs.slice(0, i + 1).reduce((a: number, s: any) => a + Math.round((s.durationSec || 0) * fps), 0) - 8;
        return <Sequence key={`sfx${i}`} from={Math.max(0, startFrame)} durationInFrames={Math.round(fps * 0.5)}><Audio src={f(props.sfx)} volume={0.35} /></Sequence>;
      })}
    </AbsoluteFill>
  );
};

// Ducked music bed: low under VO, gentle fade in/out, loops. ~-26 LUFS feel.
const MusicBed: React.FC<{ src: string }> = ({ src }) => {
  const { fps, durationInFrames } = useVideoConfig();
  return (
    <Audio
      src={src}
      loop
      volume={(f) => {
        const fadeIn = interpolate(f, [0, fps], [0, 0.085], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
        const fadeOut = interpolate(f, [durationInFrames - fps * 1.5, durationInFrames], [0.085, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
        return Math.min(fadeIn, fadeOut);
      }}
    />
  );
};
