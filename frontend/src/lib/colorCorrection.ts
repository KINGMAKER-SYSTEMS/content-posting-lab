import type { ColorCorrection } from '../types/api';

export const DEFAULT_COLOR_CORRECTION: ColorCorrection = {
  brightness: 0,
  contrast: 0,
  saturation: 0,
  sharpness: 0,
  shadow: 0,
  temperature: 0,
  tint: 0,
  fade: 0,
};

export function getColorCorrectionOrNull(cc: ColorCorrection): ColorCorrection | null {
  if (
    cc.brightness === 0 &&
    cc.contrast === 0 &&
    cc.saturation === 0 &&
    cc.sharpness === 0 &&
    cc.shadow === 0 &&
    cc.temperature === 0 &&
    cc.tint === 0 &&
    cc.fade === 0
  ) {
    return null;
  }
  return { ...cc };
}

// Mirrors services/ffmpeg.py:build_cc_filter's CSS-equivalent transforms so the
// live preview roughly matches the ffmpeg output. Intentionally approximate —
// the final download is authoritative.
export function applyCSSFilterPreview(cc: ColorCorrection): string {
  const b = Number.parseInt(String(cc.brightness), 10) || 0;
  const c = Number.parseInt(String(cc.contrast), 10) || 0;
  const s = Number.parseInt(String(cc.saturation), 10) || 0;
  const sd = Number.parseInt(String(cc.shadow), 10) || 0;
  const t = Number.parseInt(String(cc.temperature), 10) || 0;
  const ti = Number.parseInt(String(cc.tint), 10) || 0;
  const f = Number.parseInt(String(cc.fade), 10) || 0;

  let cssBrightness = 1 + b / 100;
  let cssContrast = 1 + c / 100;
  let cssSaturate = 1 + s / 100;

  if (f > 0) {
    const fade = f / 100;
    cssBrightness = Math.min(2, cssBrightness + fade * 0.4);
    cssContrast = Math.max(0.2, cssContrast - fade * 0.3);
    cssSaturate = Math.max(0.2, cssSaturate - fade * 0.4);
  }
  if (sd !== 0) cssBrightness += sd / 400;

  const parts: string[] = [];
  if (Math.abs(cssBrightness - 1) > 0.005) parts.push(`brightness(${cssBrightness.toFixed(3)})`);
  if (Math.abs(cssContrast - 1) > 0.005) parts.push(`contrast(${cssContrast.toFixed(3)})`);
  if (Math.abs(cssSaturate - 1) > 0.005) parts.push(`saturate(${cssSaturate.toFixed(3)})`);
  if (Math.abs(t) > 1) parts.push(t > 0 ? `sepia(${(t / 200).toFixed(3)})` : `hue-rotate(${(t / 5).toFixed(1)}deg)`);
  if (Math.abs(ti) > 1) parts.push(`hue-rotate(${(ti / 3).toFixed(1)}deg)`);

  return parts.length ? parts.join(' ') : 'none';
}
