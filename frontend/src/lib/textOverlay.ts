/**
 * Shared text overlay rendering utility.
 * Generates a PNG overlay with styled caption text using Canvas API.
 * Used by both Burn and Slideshow pages.
 */

export interface TextOverlayConfig {
  caption: string;
  x: number;           // 0-100 percentage
  y: number;           // 0-100 percentage
  fontSize: number;    // px (at preview resolution)
  fontFile: string;    // font filename e.g. "TikTokSans16pt-Bold.ttf"
  maxWidthPct: number; // 0-100 percentage of canvas width
  lineHeight?: number; // multiplier (default 1.08)
  strokeWidth?: number; // px at preview scale (default 4)
  /** Preview container dimensions (used for scaling) */
  videoWidth?: number;
  videoHeight?: number;
}

export function fontFamilyName(file: string): string {
  return `tt-${file.replace(/\.(ttf|otf)$/i, '')}`;
}

/**
 * Calculate CSS translateX% so text overlay stays within the container bounds.
 */
export function getTextTranslateX(x: number, maxWidthPct: number): number {
  const halfW = maxWidthPct / 2;
  if (x < halfW) {
    return -(x / maxWidthPct) * 100;
  } else if (x > 100 - halfW) {
    return -(1 - (100 - x) / maxWidthPct) * 100;
  }
  return -50;
}

/**
 * Render a caption text overlay as a transparent PNG.
 * Returns base64 data URI string, or null if caption is empty.
 *
 * Renders at 2x supersampled resolution (2160x3840) for crisp text.
 * FFmpeg scales the overlay to match the actual video dimensions.
 */
export async function captureTextOverlay(config: TextOverlayConfig): Promise<string | null> {
  const w = config.videoWidth || 432;
  const h = config.videoHeight || 768;
  const caption = config.caption.trim();
  if (!caption) return null;

  const TARGET_W = 1080;
  const TARGET_H = 1920;
  const SUPERSAMPLE = 2;
  const renderW = TARGET_W * SUPERSAMPLE;
  const renderH = TARGET_H * SUPERSAMPLE;
  const scaleX = renderW / w;
  const scaleY = renderH / h;

  const fontFamily = `'${fontFamilyName(config.fontFile)}', sans-serif`;
  const fontSize = config.fontSize;
  const renderFontSize = fontSize * scaleY;
  try {
    await document.fonts.load(`700 ${Math.ceil(renderFontSize)}px ${fontFamily}`);
  } catch { /* proceed with fallback font if load fails */ }

  const canvas = document.createElement('canvas');
  canvas.width = renderW;
  canvas.height = renderH;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';

  const rawCx = (config.x / 100) * renderW;
  const cy = (config.y / 100) * renderH;
  const maxWidth = (config.maxWidthPct / 100) * renderW;
  const cx = Math.max(maxWidth / 2, Math.min(renderW - maxWidth / 2, rawCx));

  ctx.font = `700 ${renderFontSize}px ${fontFamily}`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  // Word-wrap: respect explicit \n line breaks, then wrap within maxWidth
  const paragraphs = caption.split('\n');
  const lines: string[] = [];
  for (const para of paragraphs) {
    const words = para.split(/\s+/).filter(Boolean);
    if (words.length === 0) { lines.push(''); continue; }
    let currentLine = '';
    for (const word of words) {
      const testLine = currentLine ? `${currentLine} ${word}` : word;
      if (ctx.measureText(testLine).width > maxWidth && currentLine) {
        lines.push(currentLine);
        currentLine = word;
      } else {
        currentLine = testLine;
      }
    }
    if (currentLine) lines.push(currentLine);
  }

  const lhMultiplier = config.lineHeight ?? 1.08;
  const lineHeight = renderFontSize * lhMultiplier;
  const totalHeight = lines.length * lineHeight;
  const startY = cy - totalHeight / 2 + lineHeight / 2;

  const userStroke = config.strokeWidth ?? 4;
  const renderStroke = userStroke * Math.max(scaleX, scaleY);

  for (let i = 0; i < lines.length; i++) {
    const ly = startY + i * lineHeight;

    ctx.strokeStyle = 'black';
    ctx.lineWidth = renderStroke;
    ctx.lineJoin = 'round';
    ctx.miterLimit = 2;
    ctx.strokeText(lines[i], cx, ly);

    ctx.fillStyle = 'white';
    ctx.fillText(lines[i], cx, ly);
  }

  return canvas.toDataURL('image/png');
}
