import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiUrl, staticUrl } from '../lib/api';
import { EmptyState, LazyVideo, ProgressBar } from '../components';
import { AssignToPagesDialog } from '../components/AssignToPagesDialog';
import { useWorkflowStore } from '../stores/workflowStore';
import { captureTextOverlay as captureTextOverlayShared, fontFamilyName } from '../lib/textOverlay';
import type { TextOverlayConfig } from '../lib/textOverlay';
import type {
  BatchesResponse,
  BurnBatch,
  BurnResponse,
  CaptionSource,
  CaptionsResponse,
  ColorCorrection,
  FontInfo,
  FontsResponse,
  VideoFile,
  VideosResponse,
} from '../types/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';

const DEFAULT_LINE_HEIGHT = 1.08;
const DEFAULT_STROKE_WIDTH = 4;
const SNAP_THRESHOLD = 3;

const TIKTOK_COLOR_PRESETS: { name: string; hex: string }[] = [
  { name: 'White', hex: '#FFFFFF' },
  { name: 'Yellow', hex: '#FFFC00' },
  { name: 'Pastel Yellow', hex: '#FFF176' },
  { name: 'TikTok Red', hex: '#FE2C55' },
  { name: 'TikTok Cyan', hex: '#25F4EE' },
  { name: 'Blue', hex: '#5B9BD5' },
  { name: 'Green', hex: '#27AE60' },
  { name: 'Orange', hex: '#FF8A00' },
  { name: 'Pink', hex: '#FF3B5C' },
  { name: 'Purple', hex: '#7B2FBE' },
  { name: 'Black', hex: '#000000' },
];

const STROKE_COLOR_PRESETS: { name: string; hex: string }[] = [
  { name: 'None', hex: 'transparent' },
  { name: 'Black', hex: '#000000' },
  { name: 'White', hex: '#FFFFFF' },
];

const DEFAULT_COLOR_CORRECTION: ColorCorrection = {
  brightness: 0, contrast: 0, saturation: 0, sharpness: 0,
  shadow: 0, temperature: 0, tint: 0, fade: 0,
};

type QuickPosition = 'top' | 'center' | 'bottom';

const POSITION_Y_MAP: Record<QuickPosition, number> = {
  top: 15, center: 50, bottom: 85,
};

interface BurnPairState {
  videoPath: string;
  name: string;
  caption: string;
  x: number;
  y: number;
  fontSize: number;
  fontFile: string;
  maxWidthPct: number;
  lineHeight: number;
  strokeWidth: number;
  fontColor: string;
  strokeColor: string;
  colorCorrection: ColorCorrection | null;
  result: BurnResponse | null;
  burnedFile?: string;
  videoWidth?: number;
  videoHeight?: number;
  previewScale?: number;
}

interface DragState {
  index: number;
  startX: number;
  startY: number;
  startLayerY: number;
  startMaxWidthPct: number;
  rect: DOMRect;
}

function encodePathForUrl(path: string): string {
  return path.split('/').map((s) => encodeURIComponent(s)).join('/');
}

function formatBatchTime(created: number): string {
  const ts = created > 10_000_000_000 ? created : created * 1000;
  const d = new Date(ts);
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
}

function makeBatchId(project: string, label?: string): string {
  const now = new Date();
  const ts = `${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}`;
  const prefix = (label || project).toLowerCase().replace(/\s+/g, '-').slice(0, 30);
  const short = Math.random().toString(36).slice(2, 6);
  return `${prefix}-${ts}-${short}`;
}

function getColorCorrectionOrNull(cc: ColorCorrection): ColorCorrection | null {
  if (cc.brightness === 0 && cc.contrast === 0 && cc.saturation === 0 && cc.sharpness === 0 && cc.shadow === 0 && cc.temperature === 0 && cc.tint === 0 && cc.fade === 0) return null;
  return { ...cc };
}

function applyCSSFilterPreview(cc: ColorCorrection): string {
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

async function captureTextOverlay(pair: BurnPairState): Promise<string | null> {
  const config: TextOverlayConfig = {
    caption: pair.caption,
    x: pair.x,
    y: pair.y,
    fontSize: pair.fontSize,
    fontFile: pair.fontFile,
    maxWidthPct: pair.maxWidthPct,
    lineHeight: pair.lineHeight,
    strokeWidth: pair.strokeWidth,
    fontColor: pair.fontColor,
    strokeColor: pair.strokeColor,
    videoWidth: pair.videoWidth,
    videoHeight: pair.videoHeight,
  };
  return captureTextOverlayShared(config);
}

// ── Memoized pair card ──────────────────────────────────────────────

interface PairCardProps {
  pair: BurnPairState;
  index: number;
  selected: boolean;
  inlineEditing: boolean;
  toolbarOpen: boolean;
  snapGuide: { index: number; horizontal: boolean; vertical: boolean } | null;
  draggingIndex: number | null;
  encodedProjectName: string;
  availableFonts: FontInfo[];
  onSelect: (i: number) => void;
  onStartDrag: (e: React.PointerEvent<HTMLDivElement>, i: number) => void;
  onInlineEdit: (i: number) => void;
  onToggleToolbar: (i: number) => void;
  onCaptionChange: (i: number, caption: string) => void;
  onFontSizeChange: (i: number, v: number) => void;
  onFontFileChange: (i: number, fontFile: string) => void;
  onMaxWidthChange: (i: number, v: number) => void;
  onFontColorChange: (i: number, color: string) => void;
  onStrokeColorChange: (i: number, color: string) => void;
  onPositionChange: (i: number, y: number) => void;
  onColorCorrectionChange: (i: number, key: keyof ColorCorrection, val: number) => void;
  onDimensions: (i: number, w: number, h: number) => void;
  onWrapRef: (i: number, node: HTMLDivElement | null) => void;
}

const PairCard = memo(function PairCard({
  pair, index, selected, inlineEditing, toolbarOpen, snapGuide, draggingIndex,
  encodedProjectName, availableFonts,
  onSelect, onStartDrag, onInlineEdit, onToggleToolbar,
  onCaptionChange, onFontSizeChange, onFontFileChange, onMaxWidthChange,
  onFontColorChange, onStrokeColorChange, onPositionChange, onColorCorrectionChange,
  onDimensions, onWrapRef,
}: PairCardProps) {
  const [ccOpen, setCcOpen] = useState(false);
  const hasError = Boolean(pair.result && !pair.result.ok);
  const hasBurned = Boolean(pair.result?.ok && pair.burnedFile);
  const scale = pair.previewScale ?? 1;
  const previewFontPx = Math.max(6, Math.round(pair.fontSize * scale));
  const strokePx = Math.max(0.5, (pair.strokeWidth / 2) * scale);
  const videoSubdir = pair.videoPath.startsWith('clips/') ? '' : 'videos/';
  const videoSrc = hasBurned && pair.burnedFile
    ? staticUrl(`/projects/${encodedProjectName}/burned/${encodePathForUrl(pair.burnedFile)}`)
    : staticUrl(`/projects/${encodedProjectName}/${videoSubdir}${encodePathForUrl(pair.videoPath)}`);

  const pairCc = pair.colorCorrection ?? DEFAULT_COLOR_CORRECTION;
  const cardCssFilter = useMemo(() => applyCSSFilterPreview(pairCc), [pairCc]);

  // Current quick position based on pair.y
  const currentPos: QuickPosition | null = pair.y <= 20 ? 'top' : pair.y >= 80 ? 'bottom' : Math.abs(pair.y - 50) < 5 ? 'center' : null;

  return (
    <article
      className={`relative overflow-hidden rounded-[var(--border-radius)] border-2 bg-card transition-all ${
        hasBurned ? 'border-green-700 shadow-[3px_3px_0_0_var(--green-700,#15803d)]'
          : hasError ? 'border-destructive'
          : selected ? 'border-primary shadow-[4px_4px_0_0_var(--primary)]'
          : 'border-border hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[3px_3px_0_0_var(--border)]'
      }`}
      onClick={(e) => {
        const t = e.target as HTMLElement;
        if (t.closest('[data-text-layer]') || t.closest('[data-caption-edit]') || t.closest('[data-card-controls]') || t.closest('[data-card-toolbar]')) return;
        onSelect(index);
      }}
    >
          <div
            ref={(node) => { onWrapRef(index, node); }}
            className="relative aspect-[9/16] overflow-hidden bg-muted"
          >
            <LazyVideo
              src={`${videoSrc}#t=0.001`}
              selected={selected}
              style={{ filter: cardCssFilter }}
              className="block h-full w-full object-cover"
              onLoadedMetadata={(e) => onDimensions(index, (e.target as HTMLVideoElement).videoWidth, (e.target as HTMLVideoElement).videoHeight)}
            />

            <div className={`pointer-events-none absolute inset-x-0 top-1/2 z-20 h-px bg-primary/70 ${snapGuide && snapGuide.index === index && snapGuide.horizontal ? 'block' : 'hidden'}`} />
            <div className={`pointer-events-none absolute inset-y-0 left-1/2 z-20 w-px bg-primary/70 ${snapGuide && snapGuide.index === index && snapGuide.vertical ? 'block' : 'hidden'}`} />

            {!hasBurned ? (
              <div
                data-text-layer
                onPointerDown={(e) => onStartDrag(e, index)}
                onDoubleClick={(e) => { e.stopPropagation(); onInlineEdit(index); }}
                className={`absolute z-10 select-none text-center ${draggingIndex === index ? 'cursor-grabbing' : 'cursor-grab'} ${selected ? 'outline outline-2 outline-offset-4 outline-dashed outline-primary' : ''}`}
                style={{ left: '50%', top: `${pair.y}%`, transform: 'translate(-50%, -50%)', width: `${pair.maxWidthPct}%`, minHeight: '24px', fontFamily: `'${fontFamilyName(pair.fontFile)}', sans-serif` }}
              >
                {selected && !inlineEditing ? (
                  <div className="pointer-events-none absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-primary px-1.5 py-0.5 text-[8px] font-bold text-primary-foreground shadow-sm">
                    drag
                  </div>
                ) : null}
                {inlineEditing ? (
                  <textarea
                    value={pair.caption}
                    onChange={(e) => onCaptionChange(index, e.target.value)}
                    onBlur={() => onInlineEdit(-1)}
                    onKeyDown={(e) => { if (e.key === 'Escape') e.currentTarget.blur(); }}
                    autoFocus
                    rows={Math.max(2, pair.caption.split('\n').length + 1)}
                    className="w-full resize-none overflow-hidden border-none bg-transparent text-center font-bold outline-none"
                    style={{ fontSize: `${previewFontPx}px`, lineHeight: pair.lineHeight, color: pair.fontColor || '#FFFFFF', WebkitTextStroke: `${strokePx.toFixed(1)}px ${pair.strokeColor || '#000000'}`, paintOrder: 'stroke fill', whiteSpace: 'pre-wrap' }}
                  />
                ) : (
                  <span className="inline-block break-words font-bold" style={{ fontSize: `${previewFontPx}px`, lineHeight: pair.lineHeight, color: pair.fontColor || '#FFFFFF', WebkitTextStroke: `${strokePx.toFixed(1)}px ${pair.strokeColor || '#000000'}`, paintOrder: 'stroke fill', whiteSpace: 'pre-wrap' }}>
                    {pair.caption || '\u00A0'}
                  </span>
                )}
              </div>
            ) : null}

            {hasBurned ? <Badge variant="success" className="absolute right-2 top-2 z-30">Burned</Badge> : null}
            {hasError ? <Badge variant="error" className="absolute right-2 top-2 z-30" title={pair.result?.error || ''}>Error</Badge> : null}
          </div>

          <div className="flex flex-col gap-1.5 px-2.5 py-2">
            <div className="flex items-center justify-between">
              <div className="truncate text-xs text-muted-foreground" title={pair.name}>{pair.name}</div>
              {!hasBurned ? (
                <button
                  type="button"
                  data-card-controls
                  onClick={(e) => { e.stopPropagation(); onToggleToolbar(index); }}
                  className={`flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10px] font-medium transition-colors ${
                    toolbarOpen
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:border-primary hover:text-primary'
                  }`}
                >
                  <span>{toolbarOpen ? '▾' : '▸'}</span>
                  Edit
                </button>
              ) : null}
            </div>
            <Textarea
              data-caption-edit
              value={pair.caption}
              onChange={(e) => onCaptionChange(index, e.target.value)}
              rows={2}
              className="min-h-11 resize-y text-sm"
            />
          </div>

          {/* Per-card style toolbar */}
          {toolbarOpen && !hasBurned ? (
            <div data-card-toolbar className="border-t-2 border-border px-2.5 py-2.5 space-y-2.5">
              {/* Font + Size row */}
              <div className="flex gap-2">
                <div className="flex-1 min-w-0">
                  <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">Font</Label>
                  <Select value={pair.fontFile} onValueChange={(v: string) => onFontFileChange(index, v)}>
                    <SelectTrigger className="h-8 mt-0.5 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {availableFonts.map((f) => (
                        <SelectItem key={f.file} value={f.file}>{f.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="w-16">
                  <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">Size</Label>
                  <Input
                    type="number"
                    min={8}
                    max={120}
                    value={pair.fontSize}
                    onChange={(e) => onFontSizeChange(index, Number.parseInt(e.target.value, 10))}
                    className="h-8 mt-0.5 text-xs"
                  />
                </div>
              </div>

              {/* Text Width slider */}
              <div>
                <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">Text Width</Label>
                <div className="flex items-center gap-2 mt-0.5">
                  <input
                    type="range"
                    min={20}
                    max={95}
                    value={pair.maxWidthPct}
                    onChange={(e) => onMaxWidthChange(index, Number.parseInt(e.target.value, 10))}
                    className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-primary"
                  />
                  <span className="min-w-7 text-right text-[10px] font-bold tabular-nums text-foreground">{pair.maxWidthPct}%</span>
                </div>
              </div>

              {/* Font Color swatches */}
              <div>
                <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">Font Color</Label>
                <div className="flex flex-wrap gap-1 mt-0.5">
                  {TIKTOK_COLOR_PRESETS.map((c) => (
                    <button
                      key={c.hex}
                      type="button"
                      title={c.name}
                      onClick={() => onFontColorChange(index, c.hex)}
                      className={`h-5 w-5 rounded-full border-2 transition-all ${pair.fontColor === c.hex ? 'border-primary scale-110 ring-1 ring-primary/40' : 'border-border hover:scale-105'}`}
                      style={{ backgroundColor: c.hex, boxShadow: c.hex === '#FFFFFF' ? 'inset 0 0 0 1px rgba(0,0,0,0.1)' : undefined }}
                    />
                  ))}
                  <label
                    title="Custom color"
                    className={`relative flex h-5 w-5 cursor-pointer items-center justify-center rounded-full border-2 border-border bg-gradient-to-br from-red-400 via-yellow-300 to-blue-400 transition-all hover:scale-105 ${!TIKTOK_COLOR_PRESETS.some((c) => c.hex === pair.fontColor) ? 'border-primary scale-110 ring-1 ring-primary/40' : ''}`}
                  >
                    <input
                      type="color"
                      value={pair.fontColor}
                      onChange={(e) => onFontColorChange(index, e.target.value)}
                      className="absolute inset-0 cursor-pointer opacity-0"
                    />
                  </label>
                </div>
              </div>

              {/* Stroke Color swatches */}
              <div>
                <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">Stroke</Label>
                <div className="flex flex-wrap gap-1 mt-0.5">
                  {STROKE_COLOR_PRESETS.map((c) => (
                    <button
                      key={c.hex}
                      type="button"
                      title={c.name}
                      onClick={() => onStrokeColorChange(index, c.hex)}
                      className={`h-5 w-5 rounded-full border-2 transition-all ${pair.strokeColor === c.hex ? 'border-primary scale-110 ring-1 ring-primary/40' : 'border-border hover:scale-105'}`}
                      style={{ backgroundColor: c.hex === 'transparent' ? undefined : c.hex, boxShadow: c.hex === '#FFFFFF' ? 'inset 0 0 0 1px rgba(0,0,0,0.1)' : undefined }}
                    >
                      {c.hex === 'transparent' ? <span className="block h-full w-full rounded-full bg-muted relative overflow-hidden"><span className="absolute inset-0 flex items-center justify-center text-destructive text-[8px] font-bold">/</span></span> : null}
                    </button>
                  ))}
                  <label
                    title="Custom stroke"
                    className={`relative flex h-5 w-5 cursor-pointer items-center justify-center rounded-full border-2 border-border bg-gradient-to-br from-gray-600 via-gray-400 to-gray-200 transition-all hover:scale-105 ${!STROKE_COLOR_PRESETS.some((c) => c.hex === pair.strokeColor) ? 'border-primary scale-110 ring-1 ring-primary/40' : ''}`}
                  >
                    <input
                      type="color"
                      value={pair.strokeColor === 'transparent' ? '#000000' : pair.strokeColor}
                      onChange={(e) => onStrokeColorChange(index, e.target.value)}
                      className="absolute inset-0 cursor-pointer opacity-0"
                    />
                  </label>
                </div>
              </div>

              {/* Quick Position */}
              <div>
                <Label className="text-[10px] uppercase tracking-wide text-muted-foreground">Position</Label>
                <div className="flex gap-1.5 mt-0.5">
                  {(['top', 'center', 'bottom'] as QuickPosition[]).map((pos) => (
                    <button
                      key={pos}
                      type="button"
                      onClick={() => onPositionChange(index, POSITION_Y_MAP[pos])}
                      className={`rounded-md border px-2 py-0.5 text-[10px] font-medium capitalize transition-colors ${
                        currentPos === pos
                          ? 'border-primary bg-primary text-primary-foreground'
                          : 'border-border text-muted-foreground hover:border-primary hover:text-primary'
                      }`}
                    >
                      {pos}
                    </button>
                  ))}
                </div>
              </div>

              {/* Color Correction (collapsible) */}
              <div>
                <button
                  type="button"
                  onClick={() => setCcOpen((o) => !o)}
                  className="flex w-full items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground hover:text-foreground transition-colors"
                >
                  <span>{ccOpen ? '▾' : '▸'}</span>
                  Color Correction
                </button>
                {ccOpen ? (
                  <div className="mt-1.5 space-y-1">
                    {[
                      { key: 'brightness', label: 'Bright', min: -100, max: 100 },
                      { key: 'contrast', label: 'Contrast', min: -100, max: 100 },
                      { key: 'saturation', label: 'Satur.', min: -100, max: 100 },
                      { key: 'sharpness', label: 'Sharp', min: 0, max: 100 },
                      { key: 'shadow', label: 'Shadow', min: -100, max: 100 },
                      { key: 'temperature', label: 'Temp', min: -100, max: 100 },
                      { key: 'tint', label: 'Tint', min: -100, max: 100 },
                      { key: 'fade', label: 'Fade', min: 0, max: 100 },
                    ].map((slider) => {
                      const k = slider.key as keyof ColorCorrection;
                      const v = pairCc[k];
                      return (
                        <div key={slider.key} className="flex items-center gap-1.5">
                          <span className="min-w-[44px] text-[10px] text-muted-foreground">{slider.label}</span>
                          <input
                            type="range"
                            min={slider.min}
                            max={slider.max}
                            value={v}
                            onChange={(e) => onColorCorrectionChange(index, k, Number.parseInt(e.target.value, 10) || 0)}
                            className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-primary"
                          />
                          <span className="min-w-5 text-right text-[10px] font-bold tabular-nums text-foreground">{v}</span>
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
    </article>
  );
});

export function BurnPage() {
  const { activeProjectName, addNotification, burnSelection, clearBurnSelection, setBurnReadyCount } = useWorkflowStore();

  const [videos, setVideos] = useState<VideoFile[]>([]);
  const [captionSources, setCaptionSources] = useState<CaptionSource[]>([]);
  const [availableFonts, setAvailableFonts] = useState<FontInfo[]>([]);
  const [batches, setBatches] = useState<BurnBatch[]>([]);

  const [selectedCaptionSource, setSelectedCaptionSource] = useState('__paste');
  const [randomizeCaptions, setRandomizeCaptions] = useState(false);

  const [selectedFolders, setSelectedFolders] = useState<string[]>(() => {
    if (!activeProjectName) return [];
    // Try new key first
    try {
      const raw = localStorage.getItem(`burn:folders:${activeProjectName}`);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.every((v) => typeof v === 'string')) return parsed;
      }
    } catch {
      // fall through to legacy
    }
    // Legacy migration: single-folder string under `burn:folder:{project}`
    const legacy = localStorage.getItem(`burn:folder:${activeProjectName}`);
    if (legacy) {
      localStorage.removeItem(`burn:folder:${activeProjectName}`);
      return [legacy];
    }
    return [];
  });
  const [renamingFolder, setRenamingFolder] = useState<string | null>(null);
  const [renameFolderValue, setRenameFolderValue] = useState('');
  const [manualPaste, setManualPaste] = useState('');
  const [selectedFontFile, setSelectedFontFile] = useState('');
  const [defaultFontSize, setDefaultFontSize] = useState(32);
  const [defaultMaxWidth, setDefaultMaxWidth] = useState(80);
  const [fontColor, setFontColor] = useState('#FFFFFF');
  const [strokeColor, setStrokeColor] = useState('transparent');
  const [quickPosition, setQuickPosition] = useState<QuickPosition>('center');
  const [colorCorrection, setColorCorrection] = useState<ColorCorrection>(DEFAULT_COLOR_CORRECTION);

  const [pairs, setPairs] = useState<BurnPairState[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [inlineEditIndex, setInlineEditIndex] = useState<number | null>(null);
  const [snapGuide, setSnapGuide] = useState<{ index: number; horizontal: boolean; vertical: boolean } | null>(null);

  const [expandedToolbars, setExpandedToolbars] = useState<Set<number>>(new Set());

  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [burning, setBurning] = useState(false);
  const [progressVisible, setProgressVisible] = useState(false);
  const [progressValue, setProgressValue] = useState(0);
  const [progressLabel, setProgressLabel] = useState('');
  const [burnBatchId, setBurnBatchId] = useState<string | null>(null);
  const [batchLabel, setBatchLabel] = useState('');
  const [showExportBar, setShowExportBar] = useState(false);
  const [exportCount, setExportCount] = useState(0);
  const [assignBatchId, setAssignBatchId] = useState<string | null>(null);
  const [assignBatchCount, setAssignBatchCount] = useState(0);
  const [renamingBatchId, setRenamingBatchId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  const wrapRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const gridRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const fontStyleRef = useRef<HTMLStyleElement | null>(null);

  const groupedFolders = useMemo(() => {
    const m = new Map<string, VideoFile[]>();
    for (const v of videos) { const f = v.folder || '(root)'; m.set(f, [...(m.get(f) || []), v]); }
    return Array.from(m.entries()).map(([folder, list]) => ({
      folder, videos: list,
      label: `${(folder.split('/').pop() || folder).replaceAll('_', ' ').slice(0, 40)} (${list.length})`,
    }));
  }, [videos]);

  const multiSelectedVideos = useMemo(() => {
    if (selectedFolders.length === 0) return [];
    const set = new Set(selectedFolders);
    return videos
      .filter((v) => set.has(v.folder || '(root)'))
      .slice() // don't mutate source
      .sort((a, b) => (a.created ?? 0) - (b.created ?? 0));
  }, [videos, selectedFolders]);

  const isFolderRenameable = useCallback((folder: string): boolean => {
    if (!folder || folder === '(root)' || folder === 'clips') return false;
    // Virtual multi-job subfolders invented by backend — no disk equivalent
    for (const seg of folder.split('/')) {
      if (seg.startsWith('run_')) return false;
    }
    return true;
  }, []);

  const toggleFolder = useCallback((folder: string) => {
    setSelectedFolders((prev) => prev.includes(folder) ? prev.filter((f) => f !== folder) : [...prev, folder]);
  }, []);

  const selectedCaptionItems = useMemo(() => {
    if (selectedCaptionSource === '__paste') return manualPaste.split('\n').map((l) => l.trim()).filter(Boolean);
    const src = captionSources.find((s) => s.username === selectedCaptionSource);
    return src ? src.captions.map((r) => r.text) : [];
  }, [captionSources, manualPaste, selectedCaptionSource]);

  const showPasteManual = selectedCaptionSource === '__paste';
  const projectName = activeProjectName ?? '';
  const encodedProjectName = useMemo(() => encodeURIComponent(projectName), [projectName]);

  const applyPairsColorCorrection = useCallback((cc: ColorCorrection) => {
    setPairs((prev) => prev.map((p) => ({ ...p, colorCorrection: getColorCorrectionOrNull(cc) })));
  }, []);

  const loadBatches = useCallback(async () => {
    if (!projectName) { setBatches([]); return; }
    try {
      const r = await fetch(apiUrl(`/api/burn/batches?project=${encodeURIComponent(projectName)}`));
      if (!r.ok) throw new Error('Failed');
      const d = (await r.json()) as BatchesResponse;
      setBatches(d.batches ?? []);
    } catch { setBatches([]); }
  }, [projectName]);

  const loadData = useCallback(async () => {
    if (!activeProjectName) return;
    setIsLoading(true);
    setError(null);
    try {
      const pq = encodeURIComponent(activeProjectName);
      const [vR, cR, fR, bR] = await Promise.all([
        fetch(apiUrl(`/api/burn/videos?project=${pq}`)), fetch(apiUrl(`/api/burn/captions?project=${pq}`)),
        fetch(apiUrl('/api/burn/fonts')), fetch(apiUrl(`/api/burn/batches?project=${pq}`)),
      ]);
      if (!vR.ok || !cR.ok || !fR.ok || !bR.ok) throw new Error('Failed to load burn workspace data');

      const nv = ((await vR.json()) as VideosResponse).videos ?? [];
      const ns = ((await cR.json()) as CaptionsResponse).sources ?? [];
      const nf = ((await fR.json()) as FontsResponse).fonts ?? [];
      const nb = ((await bR.json()) as BatchesResponse).batches ?? [];

      setVideos(nv); setCaptionSources(ns); setAvailableFonts(nf); setBatches(nb);

      if (nf.length > 0) {
        setSelectedFontFile((c) => {
          if (c && nf.some((f) => f.file === c)) return c;
          return (nf.find((f) => f.file.includes('16pt-Bold.'))?.file ?? nf[0].file);
        });
      } else setSelectedFontFile('');

      const folders = new Map<string, VideoFile[]>();
      for (const v of nv) { const f = v.folder || '(root)'; folders.set(f, [...(folders.get(f) || []), v]); }
      const allFolders = Array.from(folders.keys());
      const knownFolders = new Set(allFolders);
      const pv = burnSelection.videoPaths.find((p) => nv.some((v) => v.path === p));
      const pf = pv ? (nv.find((v) => v.path === pv)?.folder || '(root)') : null;

      setSelectedFolders((curr) => {
        // Filter to folders that still exist on disk (drops stale renamed ones).
        const kept = curr.filter((f) => knownFolders.has(f));
        if (kept.length > 0) return kept;
        // Fallback: prior selection from store, else first available folder.
        if (pf && knownFolders.has(pf)) return [pf];
        return allFolders.length > 0 ? [allFolders[0]] : [];
      });
      setSelectedCaptionSource((c) => {
        if (c === '__paste' || ns.some((s) => s.username === c)) return c;
        if (burnSelection.captionSource && ns.some((s) => s.username === burnSelection.captionSource)) return burnSelection.captionSource;
        return '__paste';
      });
      if (burnSelection.videoPaths.length > 0 || burnSelection.captionSource) clearBurnSelection();
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load burn data';
      setError(msg); addNotification('error', msg);
      setVideos([]); setCaptionSources([]); setAvailableFonts([]); setBatches([]);
    } finally { setIsLoading(false); }
  }, [activeProjectName, addNotification, burnSelection.captionSource, burnSelection.videoPaths, clearBurnSelection]);

  const selectCard = useCallback((i: number) => { setInlineEditIndex(null); setSelectedIndex((p) => p === i ? -1 : i); }, []);
  const handleInlineEdit = useCallback((i: number) => { if (i < 0) setInlineEditIndex(null); else { setInlineEditIndex(i); setSelectedIndex(i); } }, []);
  const handleWrapRef = useCallback((i: number, node: HTMLDivElement | null) => { wrapRefs.current[i] = node; }, []);

  /** Compute the actual card width from the grid container.
   *  CSS grid `auto-fill, minmax(240px, 1fr)` means all cards are the same width.
   *  Reading the grid's width is reliable even when individual cards are off-screen. */
  const getCardWidth = useCallback((): number => {
    const grid = gridRef.current;
    if (!grid) return 240;
    const gridW = grid.offsetWidth;
    const gap = 16; // gap-4 = 1rem = 16px
    // Replicate CSS grid auto-fill logic: how many 240px cols fit?
    const cols = Math.max(1, Math.floor((gridW + gap) / (240 + gap)));
    return (gridW - gap * (cols - 1)) / cols;
  }, []);

  const refreshPairScales = useCallback(() => {
    const cardW = getCardWidth();
    setPairs((prev) => prev.map((p) => {
      const ns = cardW / (p.videoWidth || 432);
      if (Math.abs((p.previewScale ?? 0) - ns) < 0.005) return p;
      return { ...p, previewScale: ns };
    }));
  }, [getCardWidth]);

  useEffect(() => { void loadData(); }, [loadData]);
  useEffect(() => {
    if (!activeProjectName) return;
    try {
      localStorage.setItem(`burn:folders:${activeProjectName}`, JSON.stringify(selectedFolders));
    } catch {
      // localStorage full / disabled — ignore
    }
  }, [activeProjectName, selectedFolders]);
  useEffect(() => {
    const h: EventListener = () => { void loadData(); };
    window.addEventListener('burn:refresh-request', h);
    return () => { window.removeEventListener('burn:refresh-request', h); setBurnReadyCount(0); };
  }, [loadData, setBurnReadyCount]);

  useEffect(() => {
    if (!availableFonts.length) { if (fontStyleRef.current) { document.head.removeChild(fontStyleRef.current); fontStyleRef.current = null; } return; }
    if (fontStyleRef.current) document.head.removeChild(fontStyleRef.current);
    const el = document.createElement('style');
    el.setAttribute('data-burn-font-face', 'true');
    el.textContent = availableFonts.map((f) => `@font-face{font-family:'${fontFamilyName(f.file)}';src:url('${staticUrl(`/fonts/${f.file}`)}') format('truetype');font-weight:700;font-style:normal;}`).join('\n');
    document.head.appendChild(el);
    fontStyleRef.current = el;
    return () => { if (fontStyleRef.current) { document.head.removeChild(fontStyleRef.current); fontStyleRef.current = null; } };
  }, [availableFonts]);

  useEffect(() => { if (selectedFontFile) setPairs((p) => p.map((pair) => ({ ...pair, fontFile: selectedFontFile }))); }, [selectedFontFile]);
  useEffect(() => { if (pairs.length > 0) setPairs((p) => p.map((pair) => ({ ...pair, fontColor, strokeColor }))); }, [fontColor, strokeColor]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { applyPairsColorCorrection(colorCorrection); }, [applyPairsColorCorrection, colorCorrection]);
  // Auto-apply font size / line-height / stroke changes to all existing pairs
  // Auto-apply font size changes to all existing pairs
  useEffect(() => { if (pairs.length > 0) setPairs((p) => p.map((pair) => ({ ...pair, fontSize: defaultFontSize || 32 }))); }, [defaultFontSize]); // eslint-disable-line react-hooks/exhaustive-deps
  // Auto-apply text width changes to all existing pairs
  useEffect(() => { if (pairs.length > 0) setPairs((p) => p.map((pair) => ({ ...pair, maxWidthPct: defaultMaxWidth }))); }, [defaultMaxWidth]); // eslint-disable-line react-hooks/exhaustive-deps
  // Auto-apply quick position changes to all existing pairs
  useEffect(() => { if (pairs.length > 0) { const y = POSITION_Y_MAP[quickPosition] ?? 50; setPairs((p) => p.map((pair) => ({ ...pair, x: 50, y }))); } }, [quickPosition]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setBurnReadyCount(Math.min(multiSelectedVideos.length, selectedCaptionItems.length)); }, [multiSelectedVideos.length, selectedCaptionItems.length, setBurnReadyCount]);
  useEffect(() => { const h = () => refreshPairScales(); window.addEventListener('resize', h); return () => window.removeEventListener('resize', h); }, [refreshPairScales]);

  const handleAlignPreview = useCallback(() => {
    const nv = multiSelectedVideos;
    if (!nv.length) return;
    let captions = [...selectedCaptionItems];
    if (randomizeCaptions && captions.length > 1) {
      // Fisher-Yates shuffle
      for (let i = captions.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [captions[i], captions[j]] = [captions[j], captions[i]];
      }
    }
    const y = POSITION_Y_MAP[quickPosition] ?? 50;
    const cc = getColorCorrectionOrNull(colorCorrection);
    const np: BurnPairState[] = nv.map((v, i) => ({
      videoPath: v.path, name: v.name, caption: captions.length > 0 ? captions[i % captions.length] : '',
      x: 50, y, fontSize: defaultFontSize || 32, fontFile: selectedFontFile, maxWidthPct: defaultMaxWidth,
      lineHeight: DEFAULT_LINE_HEIGHT, strokeWidth: DEFAULT_STROKE_WIDTH,
      fontColor, strokeColor,
      colorCorrection: cc, result: null,
    }));
    wrapRefs.current = {};
    setPairs(np); setSelectedIndex(-1); setInlineEditIndex(null);
    setShowExportBar(false); setExportCount(0); setBurnBatchId(null);
    setProgressVisible(false); setProgressLabel(''); setProgressValue(0);
    requestAnimationFrame(() => refreshPairScales());
  }, [colorCorrection, defaultFontSize, defaultMaxWidth, fontColor, multiSelectedVideos, quickPosition, randomizeCaptions, refreshPairScales, selectedCaptionItems, selectedFontFile, strokeColor]);

  const handleQuickPosition = useCallback((pos: QuickPosition) => {
    setQuickPosition(pos);
    setPairs((p) => p.map((pair) => ({ ...pair, x: 50, y: POSITION_Y_MAP[pos] ?? 50 })));
  }, []);

  const handleApplyCurrentStyleToAll = useCallback(() => {
    const y = POSITION_Y_MAP[quickPosition] ?? 50;
    const cc = getColorCorrectionOrNull(colorCorrection);
    setPairs((p) => p.map((pair) => ({ ...pair, fontSize: defaultFontSize || 32, fontFile: selectedFontFile, x: 50, y, maxWidthPct: defaultMaxWidth, lineHeight: DEFAULT_LINE_HEIGHT, strokeWidth: DEFAULT_STROKE_WIDTH, fontColor, strokeColor, colorCorrection: cc })));
  }, [colorCorrection, defaultFontSize, defaultMaxWidth, fontColor, quickPosition, selectedFontFile, strokeColor]);

  const handlePairCaptionChange = useCallback((i: number, caption: string) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, caption } : pair));
  }, []);

  const handlePairFontSizeChange = useCallback((i: number, v: number) => {
    const size = Number.isNaN(v) ? 32 : Math.max(8, Math.min(120, v));
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, fontSize: size } : pair));
  }, []);

  const handlePairFontFileChange = useCallback((i: number, fontFile: string) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, fontFile } : pair));
  }, []);

  const handlePairMaxWidthChange = useCallback((i: number, v: number) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, maxWidthPct: Math.max(20, Math.min(95, v)) } : pair));
  }, []);

  const handlePairFontColorChange = useCallback((i: number, color: string) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, fontColor: color } : pair));
  }, []);

  const handlePairStrokeColorChange = useCallback((i: number, color: string) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, strokeColor: color } : pair));
  }, []);

  const handlePairPositionChange = useCallback((i: number, y: number) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, x: 50, y } : pair));
  }, []);

  const handlePairColorCorrectionChange = useCallback((i: number, key: keyof ColorCorrection, val: number) => {
    setPairs((p) => p.map((pair, idx) => {
      if (idx !== i) return pair;
      const base = pair.colorCorrection ?? { ...DEFAULT_COLOR_CORRECTION };
      const updated = { ...base, [key]: val };
      return { ...pair, colorCorrection: getColorCorrectionOrNull(updated) };
    }));
  }, []);

  const setPairDimensions = useCallback((i: number, vw: number, vh: number) => {
    const cardW = getCardWidth();
    const scale = cardW / (vw || 432);
    setPairs((p) => p.map((pair, idx) => idx !== i ? pair : { ...pair, videoWidth: vw, videoHeight: vh, previewScale: scale }));
  }, [getCardWidth]);

  const onDragMove = useCallback((e: PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;

    // Vertical → move y position (with center snap)
    let ny = d.startLayerY + (dy / d.rect.height) * 100;
    ny = Math.max(5, Math.min(95, ny));
    let sH = false;
    if (Math.abs(ny - 50) < SNAP_THRESHOLD) { ny = 50; sH = true; }

    // Horizontal → adjust wrap width (text density)
    // Full card-width drag = 60pct change in maxWidthPct
    // Drag right = narrower (compact), drag left = wider (flatten)
    const dxPct = (dx / d.rect.width) * 60;
    let newMaxW = d.startMaxWidthPct - dxPct;
    newMaxW = Math.max(20, Math.min(95, newMaxW));

    setSnapGuide({ index: d.index, horizontal: sH, vertical: false });
    setPairs((p) => p.map((pair, idx) => idx !== d.index ? pair : { ...pair, x: 50, y: ny, maxWidthPct: newMaxW }));
  }, []);

  const onDragEnd = useCallback(() => {
    dragRef.current = null; setSnapGuide(null);
    window.removeEventListener('pointermove', onDragMove);
    window.removeEventListener('pointerup', onDragEnd);
  }, [onDragMove]);

  const startDrag = useCallback((e: React.PointerEvent<HTMLDivElement>, i: number) => {
    if (inlineEditIndex === i) return;
    const w = wrapRefs.current[i];
    if (!w) return;
    e.preventDefault(); e.stopPropagation();
    const pair = pairs[i]; if (!pair) return;
    selectCard(i);
    dragRef.current = { index: i, startX: e.clientX, startY: e.clientY, startLayerY: pair.y, startMaxWidthPct: pair.maxWidthPct, rect: w.getBoundingClientRect() };
    window.addEventListener('pointermove', onDragMove);
    window.addEventListener('pointerup', onDragEnd);
  }, [inlineEditIndex, onDragEnd, onDragMove, pairs, selectCard]);

  /**
   * Submit a single burn item. The server queues it and returns immediately.
   * We then poll batch-status to track progress.
   */
  const submitBurnItem = useCallback(async (pair: BurnPairState, index: number, batchId: string, overlayPng: string | null): Promise<void> => {
    const r = await fetch(apiUrl('/api/burn/overlay'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: projectName, batchId, index, videoPath: pair.videoPath, overlayPng, colorCorrection: pair.colorCorrection }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
      throw new Error(d.error || `Submit failed (${r.status})`);
    }
  }, [projectName]);

  const handleBurnAll = useCallback(async () => {
    if (burning || pairs.length === 0 || !projectName) return;
    setBurning(true); setError(null); setProgressVisible(true); setProgressValue(0);
    setProgressLabel(`Rendering ${pairs.length} text overlays...`);
    const batchId = makeBatchId(projectName, batchLabel || undefined); setBurnBatchId(batchId);
    try {
      // Phase 1: Render all text overlays client-side
      const overlays = await Promise.all(pairs.map((p) => captureTextOverlay(p)));
      setProgressValue(10); setProgressLabel(`Submitting ${pairs.length} burn jobs...`);

      // Phase 2: Submit all burn requests (server returns immediately, processes in background)
      // Send in chunks of 8 to avoid overwhelming the browser connection pool
      const SUBMIT_CHUNK = 8;
      const submitErrors: string[] = [];
      for (let start = 0; start < pairs.length; start += SUBMIT_CHUNK) {
        const chunk = pairs.slice(start, start + SUBMIT_CHUNK).map((p, ci) => {
          const i = start + ci;
          return submitBurnItem(p, i, batchId, overlays[i]).catch((err: unknown) => {
            submitErrors.push(`#${i}: ${err instanceof Error ? err.message : String(err)}`);
          });
        });
        await Promise.all(chunk);
      }

      if (submitErrors.length === pairs.length) {
        // All submissions failed — no point polling
        setError(`All ${pairs.length} submissions failed: ${submitErrors.slice(0, 3).join(' | ')}`);
        addNotification('error', `All burns failed to submit`);
        return;
      }
      if (submitErrors.length > 0) {
        console.warn(`[burn] ${submitErrors.length} submit errors:`, submitErrors);
      }

      setProgressValue(20); setProgressLabel(`Burning 0/${pairs.length}...`);

      // Phase 3: Poll batch-status until all items are done/error
      const pollInterval = 2000; // 2 seconds
      const maxPolls = 600; // 20 minutes max
      let polls = 0;
      let finalResults: Record<string, { index: number; ok: boolean; file?: string; error?: string; status: string }> = {};

      while (polls < maxPolls) {
        await new Promise((resolve) => setTimeout(resolve, pollInterval));
        polls++;
        try {
          const statusRes = await fetch(apiUrl(`/api/burn/batch-status/${encodeURIComponent(batchId)}`));
          if (!statusRes.ok) continue;
          const status = await statusRes.json();
          finalResults = status.items || {};
          const doneCount = status.done || 0;
          const okCount = status.ok || 0;
          setProgressValue(20 + Math.round((doneCount / pairs.length) * 80));
          setProgressLabel(`Burned ${doneCount}/${pairs.length} (${okCount} OK)...`);

          if (doneCount >= pairs.length) break;
        } catch {
          // Poll failed, retry
        }
      }

      // Phase 4: Collect results
      const results: BurnResponse[] = pairs.map((_, i) => {
        const item = finalResults[String(i)];
        if (!item) return { index: i, ok: false, error: 'No status returned' };
        return { index: item.index ?? i, ok: !!item.ok, file: item.file, error: item.error };
      });

      const okCount = results.filter((r) => r.ok).length;
      const failCount = results.filter((r) => !r.ok).length;
      const errMsgs = results.filter((r) => !r.ok && r.error).map((r) => r.error!).slice(0, 3);

      setPairs(pairs.map((p, i) => {
        const r = results[i];
        return r?.ok && r.file ? { ...p, result: r, burnedFile: r.file } : { ...p, result: r };
      }));
      setExportCount(okCount); setProgressValue(100);
      setProgressLabel(`Done! ${okCount}/${pairs.length} burned.`);

      if (okCount > 0) {
        setShowExportBar(true);
        addNotification('success', `Burn complete: ${okCount}/${pairs.length}`);
      } else {
        setShowExportBar(false);
        const debugInfo = `ok=${okCount} fail=${failCount} | ${errMsgs.join(' | ') || 'no error messages'}`;
        setError(debugInfo);
        addNotification('error', `Burn 0/${pairs.length}`);
      }
      await loadBatches();
      window.dispatchEvent(new Event('projects:changed'));
      window.dispatchEvent(new Event('burn:refresh-request'));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`OUTER CATCH: ${msg}`);
      addNotification('error', `Burn crashed: ${msg}`);
    } finally { setBurning(false); }
  }, [addNotification, batchLabel, submitBurnItem, burning, loadBatches, pairs, projectName]);

  const handleRenameBatch = useCallback(async (batchId: string, label: string) => {
    if (!projectName || !label.trim()) return;
    try {
      const r = await fetch(apiUrl(`/api/burn/batches/${encodeURIComponent(batchId)}/rename?project=${encodeURIComponent(projectName)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label.trim() }),
      });
      if (!r.ok) throw new Error('Rename failed');
      setRenamingBatchId(null);
      await loadBatches();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Rename failed');
    }
  }, [projectName, loadBatches, addNotification]);

  const handleRenameSourceFolder = useCallback(async (oldFolder: string, newName: string) => {
    const trimmed = newName.trim();
    const oldLeaf = oldFolder.includes('/') ? (oldFolder.split('/').pop() || oldFolder) : oldFolder;
    if (!trimmed || trimmed === oldLeaf) {
      setRenamingFolder(null);
      setRenameFolderValue('');
      return;
    }
    if (!projectName) {
      setRenamingFolder(null);
      return;
    }
    try {
      const r = await fetch(apiUrl(`/api/burn/folders/rename?project=${encodeURIComponent(projectName)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: oldFolder, new_name: trimmed }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
        throw new Error(err.error || 'Rename failed');
      }
      const data = await r.json() as { ok: boolean; old_folder: string; new_folder: string };
      const newFolder = data.new_folder;

      // 1. Optimistically update selection so reload doesn't drop it
      setSelectedFolders((prev) => prev.map((f) => f === oldFolder ? newFolder : f));

      // 2. Rewrite in-memory pair videoPaths that point at the renamed folder.
      //    Handles both videos/ (no prefix) and clips/{job}/ (clips/ prefix preserved).
      setPairs((prev) => prev.map((p) => {
        // Match folder:
        //   - For root ('(root)' or ''): path has no '/' → not affected because rename blocked on root.
        //   - For clips/job → pair.videoPath starts with 'clips/job/' (clips prefix IS in path).
        //   - For videos folder 'foo' → pair.videoPath starts with 'foo/'.
        const oldPrefix = oldFolder === '(root)' ? '' : `${oldFolder}/`;
        if (!oldPrefix || !p.videoPath.startsWith(oldPrefix)) return p;
        const rest = p.videoPath.slice(oldPrefix.length);
        const newPrefix = newFolder === '(root)' ? '' : `${newFolder}/`;
        return { ...p, videoPath: `${newPrefix}${rest}` };
      }));

      setRenamingFolder(null);
      setRenameFolderValue('');
      addNotification('success', `Renamed to "${trimmed}"`);
      await loadData();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Rename failed');
      // Keep edit field open for retry
    }
  }, [projectName, loadData, addNotification]);

  const downloadBatchZip = useCallback((bId: string) => {
    if (!projectName) return;
    const a = document.createElement('a');
    a.href = apiUrl(`/api/burn/zip/${encodeURIComponent(bId)}?project=${encodeURIComponent(projectName)}`);
    a.download = `burned_${bId}.zip`; a.click();
  }, [projectName]);

  const hasPairs = pairs.length > 0;

  if (!activeProjectName) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState icon="📁" title="No Project Selected" description="Please select or create a project to start burning captions." />
      </div>
    );
  }

  return (
    <div className="grid h-full min-h-0 grid-cols-1 lg:grid-cols-[340px_minmax(0,1fr)]">
      {/* Sidebar */}
      <aside className="sticky top-0 flex h-screen flex-col overflow-y-auto border-b-2 border-r-2 border-border bg-card p-6 lg:border-b-0">
        <h2 className="text-xl font-heading text-foreground">Caption Burner</h2>
        <p className="mb-6 mt-1 text-xs text-muted-foreground">Pair videos with captions, drag to position, burn & download</p>

        <div className="mt-0 flex items-center justify-between">
          <Label>Video Folders</Label>
          <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
            <button
              type="button"
              onClick={() => setSelectedFolders(groupedFolders.map((g) => g.folder))}
              disabled={groupedFolders.length === 0}
              className="uppercase tracking-wide hover:text-primary disabled:opacity-40"
            >
              All
            </button>
            <span className="opacity-40">/</span>
            <button
              type="button"
              onClick={() => setSelectedFolders([])}
              disabled={selectedFolders.length === 0}
              className="uppercase tracking-wide hover:text-primary disabled:opacity-40"
            >
              None
            </button>
          </div>
        </div>
        <div className="mt-1 mb-1 text-[10px] text-muted-foreground">
          {selectedFolders.length}/{groupedFolders.length} selected
        </div>
        <div className="mb-4 min-h-[72px] max-h-56 shrink-0 overflow-y-auto rounded-md border-2 border-border bg-background">
          {groupedFolders.length === 0 ? (
            <div className="px-2 py-3 text-xs text-muted-foreground italic">No videos in project</div>
          ) : (
            groupedFolders.map((g) => {
              const checked = selectedFolders.includes(g.folder);
              const renameable = isFolderRenameable(g.folder);
              const editing = renamingFolder === g.folder;
              const leaf = g.folder === '(root)' ? '(root)' : (g.folder.split('/').pop() || g.folder);
              return (
                <div
                  key={g.folder}
                  className="group flex items-center gap-2 border-b border-border/60 px-2 py-1.5 last:border-b-0 hover:bg-accent/40"
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleFolder(g.folder)}
                    className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-primary"
                    aria-label={`Select folder ${g.label}`}
                  />
                  {editing ? (
                    <Input
                      autoFocus
                      value={renameFolderValue}
                      onChange={(e) => setRenameFolderValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          void handleRenameSourceFolder(g.folder, renameFolderValue);
                        } else if (e.key === 'Escape') {
                          e.preventDefault();
                          setRenamingFolder(null);
                          setRenameFolderValue('');
                        }
                      }}
                      onBlur={() => {
                        if (renameFolderValue.trim()) void handleRenameSourceFolder(g.folder, renameFolderValue);
                        else { setRenamingFolder(null); setRenameFolderValue(''); }
                      }}
                      className="h-6 flex-1 min-w-0 text-xs"
                    />
                  ) : (
                    <span
                      className="flex-1 min-w-0 truncate cursor-pointer text-xs"
                      title={g.folder || '(root)'}
                      onClick={() => toggleFolder(g.folder)}
                    >
                      {g.label}
                    </span>
                  )}
                  {renameable && !editing ? (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setRenamingFolder(g.folder);
                        setRenameFolderValue(leaf);
                      }}
                      className="shrink-0 opacity-0 group-hover:opacity-100 focus:opacity-100 text-muted-foreground hover:text-primary transition-opacity"
                      title="Rename folder"
                      aria-label={`Rename folder ${g.label}`}
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M17 3a2.828 2.828 0 114 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                      </svg>
                    </button>
                  ) : null}
                </div>
              );
            })
          )}
        </div>

        <Label>Caption Source</Label>
        <div className="mt-1 mb-2 flex flex-wrap gap-1.5">
          <button
            type="button"
            onClick={() => setSelectedCaptionSource('__paste')}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
              selectedCaptionSource === '__paste'
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border bg-muted text-muted-foreground hover:bg-accent'
            }`}
          >
            Paste
          </button>
          {captionSources.map((s) => (
            <button
              key={s.username}
              type="button"
              onClick={() => setSelectedCaptionSource(s.username)}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                selectedCaptionSource === s.username
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border bg-muted text-muted-foreground hover:bg-accent'
              }`}
            >
              @{s.username} ({s.count})
            </button>
          ))}
        </div>

        <div className="mb-3 flex items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={randomizeCaptions}
              onChange={(e) => setRandomizeCaptions(e.target.checked)}
              className="accent-primary"
            />
            Randomize caption order
          </label>
          <span className="text-[10px] text-muted-foreground/60">({selectedCaptionItems.length} captions)</span>
        </div>

        {showPasteManual ? (
          <div className="mb-4">
            <Label htmlFor="burn-paste">Captions (one per line)</Label>
            <Textarea
              id="burn-paste"
              value={manualPaste}
              onChange={(e) => setManualPaste(e.target.value)}
              placeholder={'First caption\nSecond caption\nThird caption'}
              className="mt-1 min-h-20 resize-y"
            />
          </div>
        ) : null}

        <Separator className="my-3" />
        <span className="mb-2 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Style Defaults</span>

        <div className="mb-4 flex gap-3">
          <div className="flex-1">
            <Label htmlFor="burn-font">Font</Label>
            <Select value={selectedFontFile} onValueChange={setSelectedFontFile}>
              <SelectTrigger id="burn-font" className="w-full mt-1">
                <SelectValue placeholder="Select font..." />
              </SelectTrigger>
              <SelectContent>
                {availableFonts.length === 0 ? (
                  <SelectItem value="__none" disabled>No fonts found</SelectItem>
                ) : (
                  availableFonts.map((f) => (
                    <SelectItem key={f.file} value={f.file}>{f.name}</SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </div>
          <div className="w-24">
            <Label htmlFor="burn-font-size">Size</Label>
            <Input
              id="burn-font-size"
              type="number"
              min={8}
              max={120}
              value={defaultFontSize}
              onChange={(e) => { const v = Number.parseInt(e.target.value, 10); setDefaultFontSize(Number.isNaN(v) ? 32 : Math.max(8, Math.min(120, v))); }}
              className="mt-1"
            />
          </div>
        </div>

        <Label>Text Width</Label>
        <div className="mb-4 mt-1 flex items-center gap-2">
          <input
            type="range"
            min={20}
            max={95}
            value={defaultMaxWidth}
            onChange={(e) => setDefaultMaxWidth(Number.parseInt(e.target.value, 10))}
            className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-primary"
          />
          <span className="min-w-8 text-right text-xs font-bold tabular-nums text-foreground">{defaultMaxWidth}%</span>
        </div>

        <Label>Font Color</Label>
        <div className="mb-2 mt-1 flex flex-wrap gap-1.5">
          {TIKTOK_COLOR_PRESETS.map((c) => (
            <button
              key={c.hex}
              type="button"
              title={c.name}
              onClick={() => setFontColor(c.hex)}
              className={`h-7 w-7 rounded-full border-2 transition-all ${fontColor === c.hex ? 'border-primary scale-110 ring-2 ring-primary/40' : 'border-border hover:scale-105'}`}
              style={{ backgroundColor: c.hex, boxShadow: c.hex === '#FFFFFF' ? 'inset 0 0 0 1px rgba(0,0,0,0.1)' : undefined }}
            />
          ))}
          <label
            title="Custom color"
            className={`relative flex h-7 w-7 cursor-pointer items-center justify-center rounded-full border-2 border-border bg-gradient-to-br from-red-400 via-yellow-300 to-blue-400 transition-all hover:scale-105 ${!TIKTOK_COLOR_PRESETS.some((c) => c.hex === fontColor) ? 'border-primary scale-110 ring-2 ring-primary/40' : ''}`}
          >
            <input
              type="color"
              value={fontColor}
              onChange={(e) => setFontColor(e.target.value)}
              className="absolute inset-0 cursor-pointer opacity-0"
            />
          </label>
        </div>

        <Label>Stroke Color</Label>
        <div className="mb-4 mt-1 flex flex-wrap gap-1.5">
          {STROKE_COLOR_PRESETS.map((c) => (
            <button
              key={c.hex}
              type="button"
              title={c.name}
              onClick={() => setStrokeColor(c.hex)}
              className={`h-7 w-7 rounded-full border-2 transition-all ${strokeColor === c.hex ? 'border-primary scale-110 ring-2 ring-primary/40' : 'border-border hover:scale-105'}`}
              style={{ backgroundColor: c.hex === 'transparent' ? undefined : c.hex, boxShadow: c.hex === '#FFFFFF' ? 'inset 0 0 0 1px rgba(0,0,0,0.1)' : undefined }}
            >
              {c.hex === 'transparent' ? <span className="block h-full w-full rounded-full bg-muted relative overflow-hidden"><span className="absolute inset-0 flex items-center justify-center text-destructive text-xs font-bold">/</span></span> : null}
            </button>
          ))}
          <label
            title="Custom stroke color"
            className={`relative flex h-7 w-7 cursor-pointer items-center justify-center rounded-full border-2 border-border bg-gradient-to-br from-gray-600 via-gray-400 to-gray-200 transition-all hover:scale-105 ${!STROKE_COLOR_PRESETS.some((c) => c.hex === strokeColor) ? 'border-primary scale-110 ring-2 ring-primary/40' : ''}`}
          >
            <input
              type="color"
              value={strokeColor === 'transparent' ? '#000000' : strokeColor}
              onChange={(e) => setStrokeColor(e.target.value)}
              className="absolute inset-0 cursor-pointer opacity-0"
            />
          </label>
        </div>

        <Label>Quick Position</Label>
        <div className="mb-4 mt-1 flex flex-wrap gap-2">
          {(['top', 'center', 'bottom'] as QuickPosition[]).map((pos) => (
            <Button
              key={pos}
              type="button"
              variant={quickPosition === pos ? 'default' : 'outline'}
              size="sm"
              onClick={() => handleQuickPosition(pos)}
              className="capitalize"
            >
              {pos}
            </Button>
          ))}
        </div>

        <Separator className="my-3" />
        <span className="mb-2 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Color Correction</span>

        <div className="space-y-1.5">
          {[
            { key: 'brightness', label: 'Brightness', min: -100, max: 100 },
            { key: 'contrast', label: 'Contrast', min: -100, max: 100 },
            { key: 'saturation', label: 'Saturation', min: -100, max: 100 },
            { key: 'sharpness', label: 'Sharpness', min: 0, max: 100 },
            { key: 'shadow', label: 'Shadow', min: -100, max: 100 },
            { key: 'temperature', label: 'Temperature', min: -100, max: 100 },
            { key: 'tint', label: 'Tint', min: -100, max: 100 },
            { key: 'fade', label: 'Fade', min: 0, max: 100 },
          ].map((slider) => {
            const k = slider.key as keyof ColorCorrection;
            const v = colorCorrection[k];
            return (
              <div key={slider.key} className="flex items-center gap-2">
                <span className="min-w-[72px] text-xs text-muted-foreground">{slider.label}</span>
                <input
                  type="range"
                  min={slider.min}
                  max={slider.max}
                  value={v}
                  onChange={(e) => setColorCorrection((prev) => ({ ...prev, [k]: Number.parseInt(e.target.value, 10) || 0 }))}
                  className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-primary"
                />
                <span className="min-w-7 text-right text-xs font-bold tabular-nums text-foreground">{v}</span>
              </div>
            );
          })}
        </div>

        <Button variant="ghost" size="sm" onClick={() => setColorCorrection(DEFAULT_COLOR_CORRECTION)} className="mt-2 mb-3">
          Reset All
        </Button>

        <Separator className="my-3" />
        <span className="mb-2 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Past Batches</span>
        <ScrollArea className="mb-4 max-h-40 flex-shrink-0">
          {batches.length === 0 ? (
            <div className="text-xs text-muted-foreground italic">No batches yet</div>
          ) : (
            <div className="space-y-1.5 pr-1">
              {batches.map((b) => (
                <Card key={b.id} className="py-0">
                  <CardContent className="space-y-1 py-2">
                    <div className="flex items-center justify-between">
                      {renamingBatchId === b.id ? (
                        <Input
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => { if (e.key === 'Enter') void handleRenameBatch(b.id, renameValue); if (e.key === 'Escape') setRenamingBatchId(null); }}
                          onBlur={() => { if (renameValue.trim()) void handleRenameBatch(b.id, renameValue); else setRenamingBatchId(null); }}
                          autoFocus
                          className="h-6 text-xs flex-1 mr-1"
                        />
                      ) : (
                        <button
                          type="button"
                          onClick={() => { setRenamingBatchId(b.id); setRenameValue(b.label || b.id); }}
                          className="text-xs text-muted-foreground text-left truncate hover:text-primary transition-colors"
                          title="Click to rename"
                        >
                          <strong className="text-foreground">{b.label || b.id}</strong> · {b.count} clips
                        </button>
                      )}
                      <Button size="xs" onClick={() => downloadBatchZip(b.id)}>ZIP</Button>
                    </div>
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] text-muted-foreground">{formatBatchTime(b.created)}</span>
                      <Button
                        size="xs"
                        variant="ghost"
                        className="ml-auto h-5 px-1.5 text-[10px]"
                        onClick={() => {
                          setAssignBatchId(b.id);
                          setAssignBatchCount(b.count ?? 0);
                        }}
                      >
                        Assign
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </ScrollArea>

        <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
          <span>Videos</span><strong className="text-foreground">{multiSelectedVideos.length}</strong>
        </div>
        <div className="mb-3 flex items-center justify-between text-xs text-muted-foreground">
          <span>Captions</span><strong className="text-foreground">{selectedCaptionItems.length}</strong>
        </div>

        <Button onClick={handleAlignPreview} disabled={multiSelectedVideos.length === 0 || isLoading} className="w-full">
          Align & Preview
        </Button>

        {hasPairs ? (
          <Button variant="outline" onClick={handleApplyCurrentStyleToAll} className="mt-2 w-full">
            Apply Current Style to All
          </Button>
        ) : null}

        {progressVisible ? (
          <ProgressBar value={progressValue} label={progressLabel} color={progressValue >= 100 ? 'success' : 'primary'} showValue className="mt-3" />
        ) : null}

        {hasPairs ? (
          <>
            <Input
              value={batchLabel}
              onChange={(e) => setBatchLabel(e.target.value)}
              placeholder="Batch label (optional)"
              className="mt-3"
              disabled={burning}
            />
            <Button onClick={handleBurnAll} disabled={burning} className="mt-2 w-full" variant={burning ? 'secondary' : 'default'}>
              {burning ? 'Burning...' : 'Burn All'}
            </Button>
          </>
        ) : null}

        {burnBatchId && exportCount > 0 ? (
          <div className="mt-3 flex flex-col gap-2">
            <Button
              onClick={() => { setAssignBatchId(burnBatchId); setAssignBatchCount(exportCount); }}
              className="w-full"
            >
              Assign to Pages
            </Button>
            <Button variant="outline" onClick={() => downloadBatchZip(burnBatchId)} className="w-full">
              Download All Burned
            </Button>
          </div>
        ) : null}

        {error ? (
          <Card className="mt-3 border-destructive bg-red-50">
            <CardContent className="max-h-48 overflow-y-auto py-2 text-xs text-red-800 whitespace-pre-wrap break-all select-all">{error}</CardContent>
          </Card>
        ) : null}
      </aside>

      {/* Main content */}
      <main className="min-h-0 overflow-y-auto bg-background p-6">
        {!hasPairs ? (
          <div className="flex h-[60vh] items-center justify-center">
            <EmptyState icon="🔥" title="No Pairs Yet" description="Pick a video folder and caption source, then hit Align & Preview." />
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <span className="text-xl font-heading text-foreground">Paired Clips</span>
              <Badge variant="secondary">{pairs.length} pairs</Badge>
            </div>

            {showExportBar && burnBatchId ? (
              <Card>
                <CardContent className="flex items-center justify-between py-3">
                  <span className="text-sm text-muted-foreground">{exportCount} burned</span>
                  <div className="flex items-center gap-2">
                    <Button size="sm" onClick={() => { setAssignBatchId(burnBatchId); setAssignBatchCount(exportCount); }}>
                      Assign to Pages
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => downloadBatchZip(burnBatchId)}>Download</Button>
                  </div>
                </CardContent>
              </Card>
            ) : null}

            <div ref={gridRef} className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
              {pairs.map((pair, index) => (
                <PairCard
                  key={`${pair.videoPath}-${index}`}
                  pair={pair}
                  index={index}
                  selected={selectedIndex === index}
                  inlineEditing={inlineEditIndex === index}
                  toolbarOpen={expandedToolbars.has(index)}
                  snapGuide={snapGuide}
                  draggingIndex={dragRef.current?.index ?? null}
                  encodedProjectName={encodedProjectName}
                  availableFonts={availableFonts}
                  onSelect={selectCard}
                  onStartDrag={startDrag}
                  onInlineEdit={handleInlineEdit}
                  onToggleToolbar={(i) => setExpandedToolbars((prev) => { const next = new Set(prev); if (next.has(i)) next.delete(i); else next.add(i); return next; })}
                  onCaptionChange={handlePairCaptionChange}
                  onFontSizeChange={handlePairFontSizeChange}
                  onFontFileChange={handlePairFontFileChange}
                  onMaxWidthChange={handlePairMaxWidthChange}
                  onFontColorChange={handlePairFontColorChange}
                  onStrokeColorChange={handlePairStrokeColorChange}
                  onPositionChange={handlePairPositionChange}
                  onColorCorrectionChange={handlePairColorCorrectionChange}
                  onDimensions={setPairDimensions}
                  onWrapRef={handleWrapRef}
                />
              ))}
            </div>
          </div>
        )}
      </main>

      {/* Assign to Pages dialog */}
      <AssignToPagesDialog
        open={assignBatchId !== null}
        onOpenChange={(open) => { if (!open) setAssignBatchId(null); }}
        batchId={assignBatchId ?? ''}
        projectName={projectName}
        videoCount={assignBatchCount}
      />
    </div>
  );
}
