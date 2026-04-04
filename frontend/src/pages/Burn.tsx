import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiUrl, staticUrl } from '../lib/api';
import { EmptyState, LazyVideo, ProgressBar } from '../components';
import { useWorkflowStore } from '../stores/workflowStore';
import { captureTextOverlay as captureTextOverlayShared, fontFamilyName, getTextTranslateX } from '../lib/textOverlay';
import type { TextOverlayConfig } from '../lib/textOverlay';
import type {
  BatchesResponse,
  BurnBatch,
  BurnResponse,
  CaptionBankResponse,
  CaptionCategory,
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

const TEXT_STROKE_PX = 1.5;
const SNAP_THRESHOLD = 3;

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
  startLayerX: number;
  startLayerY: number;
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
    videoWidth: pair.videoWidth,
    videoHeight: pair.videoHeight,
  };
  return captureTextOverlayShared(config);
}

export function BurnPage() {
  const { activeProjectName, addNotification, burnSelection, clearBurnSelection, setBurnReadyCount } = useWorkflowStore();

  const [videos, setVideos] = useState<VideoFile[]>([]);
  const [captionSources, setCaptionSources] = useState<CaptionSource[]>([]);
  const [availableFonts, setAvailableFonts] = useState<FontInfo[]>([]);
  const [batches, setBatches] = useState<BurnBatch[]>([]);

  const [bankCategories, setBankCategories] = useState<CaptionCategory[]>([]);
  const [selectedCaptionSource, setSelectedCaptionSource] = useState('__paste');
  const [selectedMoodFilter, setSelectedMoodFilter] = useState<string>('');
  const [randomizeCaptions, setRandomizeCaptions] = useState(false);
  const [newCategoryName, setNewCategoryName] = useState('');
  const [showNewCategory, setShowNewCategory] = useState(false);
  const [showImportPicker, setShowImportPicker] = useState(false);
  const [importTargetCategoryId, setImportTargetCategoryId] = useState('');

  const [selectedFolder, setSelectedFolder] = useState(() => {
    if (!activeProjectName) return '';
    return localStorage.getItem(`burn:folder:${activeProjectName}`) || '';
  });
  const [manualPaste, setManualPaste] = useState('');
  const [selectedFontFile, setSelectedFontFile] = useState('');
  const [defaultFontSize, setDefaultFontSize] = useState(32);
  const [quickPosition, setQuickPosition] = useState<QuickPosition>('center');
  const [colorCorrection, setColorCorrection] = useState<ColorCorrection>(DEFAULT_COLOR_CORRECTION);

  const [pairs, setPairs] = useState<BurnPairState[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [inlineEditIndex, setInlineEditIndex] = useState<number | null>(null);
  const [snapGuide, setSnapGuide] = useState<{ index: number; horizontal: boolean; vertical: boolean } | null>(null);

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
  const [sendingToTelegram, setSendingToTelegram] = useState<string | null>(null);

  const wrapRefs = useRef<Record<number, HTMLDivElement | null>>({});
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

  const selectedFolderVideos = useMemo(() => {
    if (!selectedFolder) return [];
    return videos.filter((v) => (v.folder || '(root)') === selectedFolder);
  }, [videos, selectedFolder]);

  const selectedCaptionItems = useMemo(() => {
    if (selectedCaptionSource === '__paste') return manualPaste.split('\n').map((l) => l.trim()).filter(Boolean);

    // Cross-pollinated mood bank: all captions across all categories with this mood
    if (selectedCaptionSource.startsWith('mood:')) {
      const mood = selectedCaptionSource.slice(5);
      const items: string[] = [];
      const seen = new Set<string>();
      for (const cat of bankCategories) {
        for (const c of cat.captions) {
          const entry = typeof c === 'string' ? { text: c, mood: null } : c;
          if (entry.mood === mood && entry.text && !seen.has(entry.text)) {
            seen.add(entry.text);
            items.push(entry.text);
          }
        }
      }
      return items;
    }

    // Check bank categories (prefixed with 'bank:')
    if (selectedCaptionSource.startsWith('bank:')) {
      const catId = selectedCaptionSource.slice(5);
      const cat = bankCategories.find((c) => c.id === catId);
      if (!cat) return [];
      let captions = cat.captions.map((c) => typeof c === 'string' ? { text: c, mood: null } : c);
      if (selectedMoodFilter) {
        captions = captions.filter((c) => c.mood === selectedMoodFilter);
      }
      return captions.map((c) => c.text);
    }
    const src = captionSources.find((s) => s.username === selectedCaptionSource);
    return src ? src.captions.map((r) => r.text) : [];
  }, [bankCategories, captionSources, manualPaste, selectedCaptionSource, selectedMoodFilter]);

  const cssFilterPreview = useMemo(() => applyCSSFilterPreview(colorCorrection), [colorCorrection]);
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

  const loadBankCategories = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/api/caption-bank/'));
      if (!r.ok) throw new Error('Failed');
      const d = (await r.json()) as CaptionBankResponse;
      setBankCategories(d.categories ?? []);
    } catch { setBankCategories([]); }
  }, []);

  const handleCreateCategory = useCallback(async () => {
    const name = newCategoryName.trim();
    if (!name) return;
    try {
      const r = await fetch(apiUrl('/api/caption-bank/categories'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) throw new Error('Failed to create category');
      setNewCategoryName('');
      setShowNewCategory(false);
      await loadBankCategories();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Failed to create category');
    }
  }, [newCategoryName, loadBankCategories, addNotification]);

  const handleDeleteCategory = useCallback(async (catId: string) => {
    try {
      const r = await fetch(apiUrl(`/api/caption-bank/categories/${catId}`), { method: 'DELETE' });
      if (!r.ok) throw new Error('Failed');
      if (selectedCaptionSource === `bank:${catId}`) setSelectedCaptionSource('__paste');
      await loadBankCategories();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Failed to delete category');
    }
  }, [selectedCaptionSource, loadBankCategories, addNotification]);

  const handleImportScraped = useCallback(async (categoryId: string, username: string) => {
    try {
      const r = await fetch(apiUrl('/api/caption-bank/import'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: projectName, username, categoryId }),
      });
      if (!r.ok) throw new Error('Failed to import');
      const d = await r.json();
      addNotification('success', `Imported ${d.added} captions`);
      setShowImportPicker(false);
      await loadBankCategories();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Import failed');
    }
  }, [projectName, loadBankCategories, addNotification]);

  const loadData = useCallback(async () => {
    if (!activeProjectName) return;
    setIsLoading(true);
    setError(null);
    try {
      const pq = encodeURIComponent(activeProjectName);
      const [vR, cR, fR, bR, bankR] = await Promise.all([
        fetch(apiUrl(`/api/burn/videos?project=${pq}`)), fetch(apiUrl(`/api/burn/captions?project=${pq}`)),
        fetch(apiUrl('/api/burn/fonts')), fetch(apiUrl(`/api/burn/batches?project=${pq}`)),
        fetch(apiUrl('/api/caption-bank/')),
      ]);
      if (!vR.ok || !cR.ok || !fR.ok || !bR.ok) throw new Error('Failed to load burn workspace data');

      const nv = ((await vR.json()) as VideosResponse).videos ?? [];
      const ns = ((await cR.json()) as CaptionsResponse).sources ?? [];
      const nf = ((await fR.json()) as FontsResponse).fonts ?? [];
      const nb = ((await bR.json()) as BatchesResponse).batches ?? [];
      if (bankR.ok) {
        const bankData = (await bankR.json()) as CaptionBankResponse;
        setBankCategories(bankData.categories ?? []);
      }

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
      const pv = burnSelection.videoPaths.find((p) => nv.some((v) => v.path === p));
      const pf = pv ? (nv.find((v) => v.path === pv)?.folder || '(root)') : null;

      setSelectedFolder((c) => {
        if (c && allFolders.includes(c)) return c;
        if (pf && allFolders.includes(pf)) return pf;
        return allFolders[0] ?? '';
      });
      setSelectedCaptionSource((c) => {
        if (c === '__paste' || c.startsWith('bank:') || ns.some((s) => s.username === c)) return c;
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

  const refreshPairScales = useCallback(() => {
    setPairs((prev) => prev.map((p, i) => {
      const w = wrapRefs.current[i];
      if (!w) return p;
      const ns = w.offsetWidth / (p.videoWidth || 432);
      if (Math.abs((p.previewScale ?? 0) - ns) < 0.005) return p;
      return { ...p, previewScale: ns };
    }));
  }, []);

  useEffect(() => { void loadData(); }, [loadData]);
  useEffect(() => {
    if (activeProjectName && selectedFolder) {
      localStorage.setItem(`burn:folder:${activeProjectName}`, selectedFolder);
    }
  }, [activeProjectName, selectedFolder]);
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
  useEffect(() => { applyPairsColorCorrection(colorCorrection); }, [applyPairsColorCorrection, colorCorrection]);
  // Auto-apply font size changes to all existing pairs
  useEffect(() => { if (pairs.length > 0) setPairs((p) => p.map((pair) => ({ ...pair, fontSize: defaultFontSize || 32 }))); }, [defaultFontSize]); // eslint-disable-line react-hooks/exhaustive-deps
  // Auto-apply quick position changes to all existing pairs
  useEffect(() => { if (pairs.length > 0) { const y = POSITION_Y_MAP[quickPosition] ?? 50; setPairs((p) => p.map((pair) => ({ ...pair, x: 50, y }))); } }, [quickPosition]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setBurnReadyCount(Math.min(selectedFolderVideos.length, selectedCaptionItems.length)); }, [selectedFolderVideos.length, selectedCaptionItems.length, setBurnReadyCount]);
  useEffect(() => { const h = () => refreshPairScales(); window.addEventListener('resize', h); return () => window.removeEventListener('resize', h); }, [refreshPairScales]);

  const handleAlignPreview = useCallback(() => {
    if (!selectedFolder) return;
    const nv = videos.filter((v) => (v.folder || '(root)') === selectedFolder);
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
      x: 50, y, fontSize: defaultFontSize || 32, fontFile: selectedFontFile, maxWidthPct: 80,
      colorCorrection: cc, result: null,
    }));
    wrapRefs.current = {};
    setPairs(np); setSelectedIndex(-1); setInlineEditIndex(null);
    setShowExportBar(false); setExportCount(0); setBurnBatchId(null);
    setProgressVisible(false); setProgressLabel(''); setProgressValue(0);
    requestAnimationFrame(() => refreshPairScales());
  }, [colorCorrection, defaultFontSize, quickPosition, randomizeCaptions, refreshPairScales, selectedCaptionItems, selectedFolder, selectedFontFile, videos]);

  const handleQuickPosition = useCallback((pos: QuickPosition) => {
    setQuickPosition(pos);
    setPairs((p) => p.map((pair) => ({ ...pair, x: 50, y: POSITION_Y_MAP[pos] ?? 50 })));
  }, []);

  const handleApplyCurrentStyleToAll = useCallback(() => {
    const y = POSITION_Y_MAP[quickPosition] ?? 50;
    const cc = getColorCorrectionOrNull(colorCorrection);
    setPairs((p) => p.map((pair) => ({ ...pair, fontSize: defaultFontSize || 32, fontFile: selectedFontFile, x: 50, y, colorCorrection: cc })));
  }, [colorCorrection, defaultFontSize, quickPosition, selectedFontFile]);

  const handlePairCaptionChange = useCallback((i: number, caption: string) => {
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, caption } : pair));
  }, []);

  const handlePairFontSizeChange = useCallback((i: number, v: number) => {
    const size = Number.isNaN(v) ? 32 : Math.max(8, Math.min(120, v));
    setPairs((p) => p.map((pair, idx) => idx === i ? { ...pair, fontSize: size } : pair));
  }, []);

  const setPairDimensions = useCallback((i: number, vw: number, vh: number) => {
    const w = wrapRefs.current[i];
    const scale = w ? w.offsetWidth / (vw || 432) : undefined;
    setPairs((p) => p.map((pair, idx) => idx !== i ? pair : { ...pair, videoWidth: vw, videoHeight: vh, previewScale: scale }));
  }, []);

  const onDragMove = useCallback((e: PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    let nx = d.startLayerX + (dx / d.rect.width) * 100;
    let ny = d.startLayerY + (dy / d.rect.height) * 100;
    nx = Math.max(5, Math.min(95, nx));
    ny = Math.max(5, Math.min(95, ny));
    let sH = false, sV = false;
    if (Math.abs(nx - 50) < SNAP_THRESHOLD) { nx = 50; sV = true; }
    if (Math.abs(ny - 50) < SNAP_THRESHOLD) { ny = 50; sH = true; }
    setSnapGuide({ index: d.index, horizontal: sH, vertical: sV });
    setPairs((p) => p.map((pair, idx) => idx !== d.index ? pair : { ...pair, x: nx, y: ny }));
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
    dragRef.current = { index: i, startX: e.clientX, startY: e.clientY, startLayerX: pair.x, startLayerY: pair.y, rect: w.getBoundingClientRect() };
    window.addEventListener('pointermove', onDragMove);
    window.addEventListener('pointerup', onDragEnd);
  }, [inlineEditIndex, onDragEnd, onDragMove, pairs, selectCard]);

  const burnOnServer = useCallback(async (pair: BurnPairState, index: number, batchId: string, overlayPng: string | null): Promise<BurnResponse> => {
    const r = await fetch(apiUrl('/api/burn/overlay'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: projectName, batchId, index, videoPath: pair.videoPath, overlayPng, colorCorrection: pair.colorCorrection }),
    });
    const d = (await r.json()) as BurnResponse & { error?: string };
    if (!r.ok || !d.ok) throw new Error(d.error || `Burn failed (${r.status})`);
    return d;
  }, [projectName]);

  const handleBurnAll = useCallback(async () => {
    if (burning || pairs.length === 0 || !projectName) return;
    setBurning(true); setError(null); setProgressVisible(true); setProgressValue(0);
    setProgressLabel(`Rendering ${pairs.length} text overlays...`);
    const batchId = makeBatchId(projectName, batchLabel || undefined); setBurnBatchId(batchId);
    try {
      const overlays = await Promise.all(pairs.map((p) => captureTextOverlay(p)));
      setProgressValue(15); setProgressLabel(`Burning ${pairs.length} videos (server)...`);
      let done = 0;
      const results = await Promise.all(pairs.map((p, i) =>
        burnOnServer(p, i, batchId, overlays[i]).then((r) => { done++; setProgressValue(15 + Math.round((done / pairs.length) * 85)); setProgressLabel(`Burned ${done}/${pairs.length}...`); return r; })
          .catch((err: unknown) => { done++; setProgressValue(15 + Math.round((done / pairs.length) * 85)); return { index: i, ok: false, error: err instanceof Error ? err.message : 'Burn failed' } as BurnResponse; })
      ));
      setPairs(pairs.map((p, i) => {
        const r = results[i];
        return r?.ok && r.file ? { ...p, result: r, burnedFile: r.file } : { ...p, result: r };
      }));
      const sc = results.filter((r) => r.ok).length;
      setExportCount(sc); setProgressValue(100); setProgressLabel(`Done! ${sc}/${pairs.length} burned.`);
      if (sc > 0) { setShowExportBar(true); addNotification('success', `Burn complete: ${sc}/${pairs.length}`); }
      else { setShowExportBar(false); addNotification('error', 'Burn finished with no successful outputs'); }
      await loadBatches();
      window.dispatchEvent(new Event('projects:changed'));
      window.dispatchEvent(new Event('burn:refresh-request'));
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Burn failed';
      setError(msg); addNotification('error', msg);
    } finally { setBurning(false); }
  }, [addNotification, burnOnServer, burning, loadBatches, pairs, projectName]);

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

        <Label htmlFor="burn-folder">Video Folder</Label>
        <Select value={selectedFolder} onValueChange={setSelectedFolder}>
          <SelectTrigger id="burn-folder" className="w-full mt-1 mb-4">
            <SelectValue placeholder="Select folder..." />
          </SelectTrigger>
          <SelectContent>
            {groupedFolders.length === 0 ? (
              <SelectItem value="__none" disabled>No videos in project</SelectItem>
            ) : (
              groupedFolders.map((g) => (
                <SelectItem key={g.folder} value={g.folder}>{g.label}</SelectItem>
              ))
            )}
          </SelectContent>
        </Select>

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
          {bankCategories.map((cat) => (
            <button
              key={cat.id}
              type="button"
              onClick={() => setSelectedCaptionSource(`bank:${cat.id}`)}
              className={`group relative rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                selectedCaptionSource === `bank:${cat.id}`
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border bg-muted text-muted-foreground hover:bg-accent'
              }`}
            >
              {cat.name} ({cat.count})
              <span
                onClick={(e) => { e.stopPropagation(); handleDeleteCategory(cat.id); }}
                className="ml-1 hidden cursor-pointer opacity-60 hover:opacity-100 group-hover:inline"
              >&times;</span>
            </button>
          ))}
          <button
            type="button"
            onClick={() => setShowNewCategory((v) => !v)}
            className="rounded-full border border-dashed border-border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
            title="Add category"
          >+</button>
        </div>

        {showNewCategory && (
          <div className="mb-2 flex gap-1.5">
            <Input
              value={newCategoryName}
              onChange={(e) => setNewCategoryName(e.target.value)}
              placeholder="Category name..."
              className="h-7 text-xs"
              onKeyDown={(e) => { if (e.key === 'Enter') void handleCreateCategory(); }}
            />
            <Button size="sm" className="h-7 px-2 text-xs" onClick={() => void handleCreateCategory()}>Add</Button>
          </div>
        )}

        {captionSources.length > 0 && bankCategories.length > 0 && (
          <div className="mb-2">
            {!showImportPicker ? (
              <button
                type="button"
                onClick={() => setShowImportPicker(true)}
                className="text-[11px] text-muted-foreground underline hover:text-foreground"
              >Import from scraped</button>
            ) : (
              <div className="rounded border border-border bg-muted/50 p-2 text-xs">
                <div className="mb-1 font-medium">Import scraped captions into:</div>
                <Select value={importTargetCategoryId} onValueChange={setImportTargetCategoryId}>
                  <SelectTrigger className="h-7 w-full text-xs mb-1.5">
                    <SelectValue placeholder="Select category..." />
                  </SelectTrigger>
                  <SelectContent>
                    {bankCategories.map((c) => (
                      <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="mb-1 font-medium">From:</div>
                <div className="flex flex-col gap-1">
                  {captionSources.map((s) => (
                    <button
                      key={s.username}
                      type="button"
                      disabled={!importTargetCategoryId}
                      onClick={() => void handleImportScraped(importTargetCategoryId, s.username)}
                      className="rounded border border-border px-2 py-0.5 text-left text-xs hover:bg-accent disabled:opacity-40"
                    >@{s.username} ({s.count})</button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => setShowImportPicker(false)}
                  className="mt-1.5 text-[10px] text-muted-foreground underline"
                >Cancel</button>
              </div>
            )}
          </div>
        )}

        {bankCategories.length > 0 && (
          <>
            <Label className="mt-1">Mood Filter</Label>
            <div className="mt-1 mb-2 flex flex-wrap gap-1.5">
              {['sad', 'hype', 'love', 'funny', 'chill'].map((mood) => {
                const moodCount = bankCategories.reduce((sum, cat) =>
                  sum + cat.captions.filter((c) => (typeof c === 'string' ? null : c.mood) === mood).length, 0);
                if (moodCount === 0) return null;
                const isActive = selectedCaptionSource === `mood:${mood}`;
                const isFilter = selectedMoodFilter === mood;
                return (
                  <button
                    key={mood}
                    type="button"
                    onClick={() => {
                      if (isActive) { setSelectedCaptionSource('__paste'); setSelectedMoodFilter(''); }
                      else if (selectedCaptionSource.startsWith('bank:')) { setSelectedMoodFilter(isFilter ? '' : mood); }
                      else { setSelectedCaptionSource(`mood:${mood}`); setSelectedMoodFilter(''); }
                    }}
                    className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                      isActive || isFilter
                        ? 'border-primary bg-primary text-primary-foreground'
                        : 'border-border bg-muted text-muted-foreground hover:bg-accent'
                    }`}
                  >
                    {mood} ({moodCount})
                  </button>
                );
              })}
              {(selectedMoodFilter || selectedCaptionSource.startsWith('mood:')) && (
                <button
                  type="button"
                  onClick={() => { setSelectedMoodFilter(''); if (selectedCaptionSource.startsWith('mood:')) setSelectedCaptionSource('__paste'); }}
                  className="rounded-full border border-border px-2 py-1 text-[10px] text-muted-foreground hover:bg-accent"
                >clear</button>
              )}
            </div>
          </>
        )}

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
                      <span className="text-xs text-muted-foreground">
                        <strong className="text-foreground">{b.label || b.id}</strong> · {b.count} clips
                      </span>
                      <Button size="xs" onClick={() => downloadBatchZip(b.id)}>ZIP</Button>
                    </div>
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] text-muted-foreground">{formatBatchTime(b.created)}</span>
                      <Button
                        size="xs"
                        variant="ghost"
                        className="ml-auto h-5 px-1.5 text-[10px]"
                        disabled={sendingToTelegram === b.id}
                        onClick={async () => {
                          // Quick telegram send — uses first roster page
                          const rosterPages = useWorkflowStore.getState().rosterPages;
                          if (!rosterPages.length) {
                            addNotification('error', 'No roster pages configured. Set up pages in Telegram tab first.');
                            return;
                          }
                          setSendingToTelegram(b.id);
                          try {
                            const res = await fetch(apiUrl('/api/telegram/send-batch'), {
                              method: 'POST',
                              headers: { 'Content-Type': 'application/json' },
                              body: JSON.stringify({ integration_id: rosterPages[0].integration_id, batch_id: b.id, project: projectName }),
                            });
                            const data = await res.json();
                            if (!res.ok) throw new Error(data.error || data.detail || 'Send failed');
                            addNotification('success', `Sent ${data.sent}/${data.total} to Telegram`);
                          } catch (err) {
                            addNotification('error', err instanceof Error ? err.message : 'Telegram send failed');
                          } finally { setSendingToTelegram(null); }
                        }}
                      >
                        {sendingToTelegram === b.id ? '...' : 'TG'}
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </ScrollArea>

        <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
          <span>Videos</span><strong className="text-foreground">{selectedFolderVideos.length}</strong>
        </div>
        <div className="mb-3 flex items-center justify-between text-xs text-muted-foreground">
          <span>Captions</span><strong className="text-foreground">{selectedCaptionItems.length}</strong>
        </div>

        <Button onClick={handleAlignPreview} disabled={!selectedFolder || selectedFolderVideos.length === 0 || isLoading} className="w-full">
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
          <Button variant="outline" onClick={() => downloadBatchZip(burnBatchId)} className="mt-3 w-full">
            Download All Burned
          </Button>
        ) : null}

        {error ? (
          <Card className="mt-3 border-destructive bg-red-50">
            <CardContent className="py-2 text-xs text-red-800">{error}</CardContent>
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
                  <Button size="sm" onClick={() => downloadBatchZip(burnBatchId)}>Download All</Button>
                </CardContent>
              </Card>
            ) : null}

            <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
              {pairs.map((pair, index) => {
                const selected = selectedIndex === index;
                const hasError = Boolean(pair.result && !pair.result.ok);
                const hasBurned = Boolean(pair.result?.ok && pair.burnedFile);
                const scale = pair.previewScale ?? 1;
                const previewFontPx = Math.max(6, Math.round(pair.fontSize * scale));
                const strokePx = Math.max(0.5, TEXT_STROKE_PX * scale);
                const videoSubdir = pair.videoPath.startsWith('clips/') ? '' : 'videos/';
                const videoSrc = hasBurned && pair.burnedFile
                  ? staticUrl(`/projects/${encodedProjectName}/burned/${encodePathForUrl(pair.burnedFile)}`)
                  : staticUrl(`/projects/${encodedProjectName}/${videoSubdir}${encodePathForUrl(pair.videoPath)}`);

                return (
                  <article
                    key={`${pair.videoPath}-${index}`}
                    className={`relative overflow-hidden rounded-[var(--border-radius)] border-2 bg-card transition-all ${
                      hasBurned ? 'border-green-700 shadow-[3px_3px_0_0_var(--green-700,#15803d)]'
                        : hasError ? 'border-destructive'
                        : selected ? 'border-primary shadow-[4px_4px_0_0_var(--primary)]'
                        : 'border-border hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[3px_3px_0_0_var(--border)]'
                    }`}
                    onClick={(e) => {
                      const t = e.target as HTMLElement;
                      if (t.closest('[data-text-layer]') || t.closest('[data-caption-edit]') || t.closest('[data-card-controls]')) return;
                      selectCard(index);
                    }}
                  >
                    <div
                      ref={(node) => { wrapRefs.current[index] = node; }}
                      className="relative aspect-[9/16] overflow-hidden bg-muted"
                    >
                      <LazyVideo
                        src={`${videoSrc}#t=0.001`}
                        selected={selected}
                        style={{ filter: cssFilterPreview }}
                        className="block h-full w-full object-cover"
                        onLoadedMetadata={(e) => setPairDimensions(index, (e.target as HTMLVideoElement).videoWidth, (e.target as HTMLVideoElement).videoHeight)}
                      />

                      <div className={`pointer-events-none absolute inset-x-0 top-1/2 z-20 h-px bg-primary/70 ${snapGuide && snapGuide.index === index && snapGuide.horizontal ? 'block' : 'hidden'}`} />
                      <div className={`pointer-events-none absolute inset-y-0 left-1/2 z-20 w-px bg-primary/70 ${snapGuide && snapGuide.index === index && snapGuide.vertical ? 'block' : 'hidden'}`} />

                      {!hasBurned ? (
                        <div
                          data-text-layer
                          onPointerDown={(e) => startDrag(e, index)}
                          onDoubleClick={(e) => { e.stopPropagation(); setInlineEditIndex(index); setSelectedIndex(index); }}
                          className={`absolute z-10 select-none text-center ${dragRef.current?.index === index ? 'cursor-grabbing' : 'cursor-grab'} ${selected ? 'outline outline-2 outline-offset-4 outline-dashed outline-primary' : ''}`}
                          style={{ left: `${pair.x}%`, top: `${pair.y}%`, transform: `translate(${getTextTranslateX(pair.x, pair.maxWidthPct)}%, -50%)`, maxWidth: `${pair.maxWidthPct}%`, minWidth: '40px', minHeight: '24px', fontFamily: `'${fontFamilyName(pair.fontFile)}', sans-serif` }}
                        >
                          {/* Drag handle indicator — visible when selected */}
                          {selected && !inlineEditIndex ? (
                            <div className="pointer-events-none absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-primary px-1.5 py-0.5 text-[8px] font-bold text-primary-foreground shadow-sm">
                              drag
                            </div>
                          ) : null}
                          {inlineEditIndex === index ? (
                            <textarea
                              value={pair.caption}
                              onChange={(e) => handlePairCaptionChange(index, e.target.value)}
                              onBlur={() => setInlineEditIndex(null)}
                              onKeyDown={(e) => { if (e.key === 'Escape') e.currentTarget.blur(); }}
                              autoFocus
                              rows={Math.max(2, pair.caption.split('\n').length + 1)}
                              className="w-full resize-none overflow-hidden border-none bg-transparent text-center font-bold text-white outline-none"
                              style={{ fontSize: `${previewFontPx}px`, lineHeight: 1.2, WebkitTextStroke: `${strokePx.toFixed(1)}px black`, paintOrder: 'stroke fill', whiteSpace: 'pre-wrap' }}
                            />
                          ) : (
                            <span className="inline-block break-words font-bold text-white" style={{ fontSize: `${previewFontPx}px`, lineHeight: 1.2, WebkitTextStroke: `${strokePx.toFixed(1)}px black`, paintOrder: 'stroke fill', whiteSpace: 'pre-wrap' }}>
                              {pair.caption || '\u00A0'}
                            </span>
                          )}
                        </div>
                      ) : null}

                      {hasBurned ? <Badge variant="success" className="absolute right-2 top-2 z-30">Burned</Badge> : null}
                      {hasError ? <Badge variant="error" className="absolute right-2 top-2 z-30" title={pair.result?.error || ''}>Error</Badge> : null}
                    </div>

                    <div className="flex flex-col gap-1.5 px-2.5 py-2">
                      <div className="truncate text-xs text-muted-foreground" title={pair.name}>{pair.name}</div>
                      <Textarea
                        data-caption-edit
                        value={pair.caption}
                        onChange={(e) => handlePairCaptionChange(index, e.target.value)}
                        rows={2}
                        className="min-h-11 resize-y text-sm"
                      />
                    </div>

                    {selected ? (
                      <div data-card-controls className="flex items-center gap-2 border-t-2 border-border px-2.5 py-2">
                        <Label htmlFor={`pair-size-${index}`} className="text-[11px] uppercase tracking-wide text-muted-foreground">Size</Label>
                        <Input
                          id={`pair-size-${index}`}
                          type="number"
                          min={8}
                          max={120}
                          value={pair.fontSize}
                          onChange={(e) => handlePairFontSizeChange(index, Number.parseInt(e.target.value, 10))}
                          className="w-[60px]"
                        />
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
