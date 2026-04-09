import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWorkflowStore } from '../stores/workflowStore';
import { apiUrl } from '../lib/api';
import { captureTextOverlay, fontFamilyName, getTextTranslateX } from '../lib/textOverlay';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import {
  Upload, Trash2, Play, Image as ImageIcon, Film, Music,
  Layers, Square, CheckSquare, Volume2, Sparkles, Save,
  Download, ArrowRight,
} from 'lucide-react';
import type {
  SlideshowImage, SlideshowRender, SlideshowRenderJob,
  SlideshowAudioFile, SlideshowProjectVideo, FontInfo,
  CaptionSource, BatchJobStatus, SlideshowFormat,
} from '../types/api';

type QuickPosition = 'top' | 'center' | 'bottom';
const POSITION_Y: Record<QuickPosition, number> = { top: 15, center: 50, bottom: 85 };
const MOOD_TAGS = ['sad', 'hype', 'love', 'funny', 'chill'] as const;
type SlideshowMode = 'fan-page' | 'meme';

export function SlideshowPage() {
  const { activeProjectName, addNotification, primeBurnSelection } = useWorkflowStore();
  const navigate = useNavigate();

  // ── Mode ──
  const [mode, setMode] = useState<SlideshowMode>('fan-page');

  // Image gallery
  const [images, setImages] = useState<SlideshowImage[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // Block 1 state (shared between modes)
  const [b1Images, setB1Images] = useState<Set<string>>(new Set());
  const [b1Duration, setB1Duration] = useState(8);
  const [b1ShuffleSpeed, setB1ShuffleSpeed] = useState(0.4);
  const [b1Caption, setB1Caption] = useState('');
  const [b1FontFile, setB1FontFile] = useState('TikTokSans16pt-Bold.ttf');
  const [b1FontSize, setB1FontSize] = useState(32);
  const [b1TextX, setB1TextX] = useState(50);
  const [b1TextY, setB1TextY] = useState(50);
  const [b1MaxWidthPct] = useState(80);

  // Block 2 state (fan-page only)
  const [b2SourceType, setB2SourceType] = useState<'image' | 'video'>('image');
  const [b2Source, setB2Source] = useState('');
  const [b2Duration, setB2Duration] = useState(3);
  const [projectVideos, setProjectVideos] = useState<SlideshowProjectVideo[]>([]);

  // Audio state (shared)
  const [audioFiles, setAudioFiles] = useState<SlideshowAudioFile[]>([]);
  const [selectedAudio, setSelectedAudio] = useState('');
  const [uploadingAudio, setUploadingAudio] = useState(false);
  const audioFileRef = useRef<HTMLInputElement>(null);

  // Fonts (fan-page only)
  const [fonts, setFonts] = useState<FontInfo[]>([]);

  // Fan-page render state
  const [renders, setRenders] = useState<SlideshowRender[]>([]);
  const [rendering, setRendering] = useState(false);
  const [job, setJob] = useState<SlideshowRenderJob | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Meme mode state ──
  const [captionSources, setCaptionSources] = useState<CaptionSource[]>([]);
  const [selectedCaptionSource, setSelectedCaptionSource] = useState('');
  const [moodFilter, setMoodFilter] = useState<string | null>(null);
  const [batchSize, setBatchSize] = useState(5);
  const [batchJob, setBatchJob] = useState<BatchJobStatus | null>(null);
  const [memeRendering, setMemeRendering] = useState(false);
  const memePollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Formats state ──
  const [formats, setFormats] = useState<SlideshowFormat[]>([]);
  const [formatName, setFormatName] = useState('');
  const [showFormatSave, setShowFormatSave] = useState(false);

  // ── Derived ──
  const filteredCaptions = useMemo(() => {
    const src = captionSources.find((s) => s.username === selectedCaptionSource);
    if (!src) return [];
    if (!moodFilter) return src.captions;
    return src.captions.filter((c) => c.mood === moodFilter);
  }, [captionSources, selectedCaptionSource, moodFilter]);

  const effectiveBatchSize = Math.min(batchSize, filteredCaptions.length || batchSize);

  const memeOutputs = useMemo(() => {
    if (!batchJob) return [];
    return (batchJob.items ?? []).filter((it) => it.status === 'complete' && it.output);
  }, [batchJob]);

  // ── Data fetching ──

  const fetchImages = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/images?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) { const data = await res.json(); setImages(data.images ?? []); }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchRenders = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/renders?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) { const data = await res.json(); setRenders(data.renders ?? []); }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchAudio = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/audio?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) { const data = await res.json(); setAudioFiles(data.audio ?? []); }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchProjectVideos = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/project-videos?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) { const data = await res.json(); setProjectVideos(data.videos ?? []); }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchFonts = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/burn/fonts'));
      if (res.ok) { const data = await res.json(); setFonts(data.fonts ?? []); }
    } catch { /* ignore */ }
  }, []);

  const fetchCaptions = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/captions?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) { const data = await res.json(); setCaptionSources(data.sources ?? []); }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchFormats = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/formats?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) { const data = await res.json(); setFormats(data.formats ?? []); }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  // Reset on project change
  useEffect(() => {
    setB1Images(new Set());
    setB2Source('');
    setSelectedAudio('');
    setJob(null);
    setBatchJob(null);
    setRendering(false);
    setMemeRendering(false);
    setSelectedCaptionSource('');
    setMoodFilter(null);
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
    if (memePollingRef.current) { clearInterval(memePollingRef.current); memePollingRef.current = null; }
    void fetchImages();
    void fetchRenders();
    void fetchAudio();
    void fetchProjectVideos();
    void fetchFonts();
    void fetchCaptions();
    void fetchFormats();
  }, [activeProjectName, fetchImages, fetchRenders, fetchAudio, fetchProjectVideos, fetchFonts, fetchCaptions, fetchFormats]);

  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
      if (memePollingRef.current) clearInterval(memePollingRef.current);
    };
  }, []);

  // ── Image upload ──

  const handleUpload = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0 || !activeProjectName) return;
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('project', activeProjectName);
      for (let i = 0; i < files.length; i++) formData.append('files', files[i]);
      const res = await fetch(apiUrl('/api/slideshow/upload'), { method: 'POST', body: formData });
      if (!res.ok) throw new Error(`Upload failed (${res.status})`);
      addNotification('success', `Uploaded ${files.length} image(s)`);
      void fetchImages();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }, [activeProjectName, addNotification, fetchImages]);

  const handleDeleteImage = useCallback(async (filename: string) => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(
        apiUrl(`/api/slideshow/images/${encodeURIComponent(filename)}?project=${encodeURIComponent(activeProjectName)}`),
        { method: 'DELETE' },
      );
      if (!res.ok) throw new Error(`Delete failed (${res.status})`);
      setB1Images((prev) => { const n = new Set(prev); n.delete(filename); return n; });
      if (b2Source === filename) setB2Source('');
      void fetchImages();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Delete failed');
    }
  }, [activeProjectName, addNotification, fetchImages, b2Source]);

  // ── Audio upload ──

  const handleAudioUpload = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0 || !activeProjectName) return;
    setUploadingAudio(true);
    try {
      const formData = new FormData();
      formData.append('project', activeProjectName);
      formData.append('file', files[0]);
      const res = await fetch(apiUrl('/api/slideshow/audio/upload'), { method: 'POST', body: formData });
      if (!res.ok) throw new Error(`Upload failed (${res.status})`);
      const data = await res.json();
      addNotification('success', `Uploaded audio: ${files[0].name}`);
      setSelectedAudio(data.name);
      void fetchAudio();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Audio upload failed');
    } finally {
      setUploadingAudio(false);
      if (audioFileRef.current) audioFileRef.current.value = '';
    }
  }, [activeProjectName, addNotification, fetchAudio]);

  const handleDeleteAudio = useCallback(async (filename: string) => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(
        apiUrl(`/api/slideshow/audio/${encodeURIComponent(filename)}?project=${encodeURIComponent(activeProjectName)}`),
        { method: 'DELETE' },
      );
      if (!res.ok) throw new Error(`Delete failed`);
      if (selectedAudio === filename) setSelectedAudio('');
      void fetchAudio();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Delete failed');
    }
  }, [activeProjectName, addNotification, fetchAudio, selectedAudio]);

  // ── Block 1 image selection ──

  const toggleB1Image = useCallback((name: string) => {
    setB1Images((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  }, []);

  const addAllImages = useCallback(() => { setB1Images(new Set(images.map((i) => i.name))); }, [images]);
  const clearB1Images = useCallback(() => { setB1Images(new Set()); }, []);

  // ── Fan-page Render ──

  const handleRender = useCallback(async () => {
    if (!activeProjectName || b1Images.size === 0 || !b2Source) return;
    setRendering(true);
    setJob(null);

    try {
      let overlayPng: string | undefined;
      if (b1Caption.trim()) {
        const png = await captureTextOverlay({
          caption: b1Caption, x: b1TextX, y: b1TextY,
          fontSize: b1FontSize, fontFile: b1FontFile,
          maxWidthPct: b1MaxWidthPct, videoWidth: 432, videoHeight: 768,
        });
        if (png) overlayPng = png;
      }

      const body = {
        project: activeProjectName,
        block1: { images: Array.from(b1Images), duration: b1Duration, shuffle_speed: b1ShuffleSpeed, overlay_png: overlayPng },
        block2: { source: b2Source, source_type: b2SourceType, duration: b2Duration },
        audio: selectedAudio || undefined,
      };

      const res = await fetch(apiUrl('/api/slideshow/render-v2'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`Render request failed (${res.status})`);
      const data = await res.json();
      const newJobId = data.job_id as string;

      pollingRef.current = setInterval(async () => {
        try {
          const pollRes = await fetch(apiUrl(`/api/slideshow/job/${newJobId}`));
          if (!pollRes.ok) return;
          const pollData = (await pollRes.json()) as SlideshowRenderJob;
          setJob(pollData);
          if (pollData.status === 'complete' || pollData.status === 'error' || pollData.status === 'not_found') {
            if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
            setRendering(false);
            if (pollData.status === 'complete') { addNotification('success', 'Slideshow render complete!'); void fetchRenders(); }
            else if (pollData.status === 'error') { addNotification('error', pollData.message || 'Render failed'); }
          }
        } catch { /* ignore */ }
      }, 1000);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Render failed');
      setRendering(false);
    }
  }, [activeProjectName, b1Images, b1Duration, b1ShuffleSpeed, b1Caption, b1FontFile, b1FontSize, b1TextX, b1TextY, b1MaxWidthPct, b2Source, b2SourceType, b2Duration, selectedAudio, addNotification, fetchRenders]);

  // ── Meme Batch Render ──

  const handleMemeRender = useCallback(async () => {
    if (!activeProjectName || b1Images.size === 0 || !selectedCaptionSource || effectiveBatchSize < 1) return;
    setMemeRendering(true);
    setBatchJob(null);

    try {
      const body = {
        project: activeProjectName,
        images: Array.from(b1Images),
        batch_size: effectiveBatchSize,
        duration: b1Duration,
        shuffle_speed: b1ShuffleSpeed,
        audio: selectedAudio || undefined,
      };

      const res = await fetch(apiUrl('/api/slideshow/render-meme'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`Meme render failed (${res.status})`);
      const data = await res.json();
      const newJobId = data.job_id as string;

      memePollingRef.current = setInterval(async () => {
        try {
          const pollRes = await fetch(apiUrl(`/api/slideshow/job/${newJobId}`));
          if (!pollRes.ok) return;
          const pollData = (await pollRes.json()) as BatchJobStatus;
          setBatchJob(pollData);
          if (pollData.status === 'complete' || pollData.status === 'error') {
            if (memePollingRef.current) { clearInterval(memePollingRef.current); memePollingRef.current = null; }
            setMemeRendering(false);
            if (pollData.status === 'complete') {
              const ok = (pollData.items ?? []).filter((it) => it.status === 'complete').length;
              addNotification('success', `Batch complete: ${ok}/${pollData.batch_size} rendered`);
              void fetchRenders();
            } else {
              addNotification('error', pollData.message || 'Batch render failed');
            }
          }
        } catch { /* ignore */ }
      }, 1000);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Batch render failed');
      setMemeRendering(false);
    }
  }, [activeProjectName, b1Images, b1Duration, b1ShuffleSpeed, selectedAudio, selectedCaptionSource, effectiveBatchSize, addNotification, fetchRenders]);

  // ── Use in Burn ──

  const sendToBurn = useCallback(() => {
    const outputs = memeOutputs.map((it) => `videos/slideshow/${it.output}`);
    if (outputs.length === 0) { addNotification('info', 'No videos to send.'); return; }
    primeBurnSelection({ videoPaths: outputs, captionSource: selectedCaptionSource || null });
    navigate('/burn');
  }, [memeOutputs, selectedCaptionSource, primeBurnSelection, navigate, addNotification]);

  // ── Delete render ──

  const handleDeleteRender = useCallback(async (filename: string) => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(
        apiUrl(`/api/slideshow/renders/${encodeURIComponent(filename)}?project=${encodeURIComponent(activeProjectName)}`),
        { method: 'DELETE' },
      );
      if (!res.ok) throw new Error(`Delete failed`);
      void fetchRenders();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Delete failed');
    }
  }, [activeProjectName, addNotification, fetchRenders]);

  // ── Format save/load ──

  const handleSaveFormat = useCallback(async () => {
    if (!activeProjectName || !formatName.trim()) return;
    const config: Record<string, unknown> = {
      images: Array.from(b1Images),
      duration: b1Duration,
      shuffle_speed: b1ShuffleSpeed,
      audio: selectedAudio || null,
    };
    if (mode === 'meme') {
      config.caption_source = selectedCaptionSource;
      config.caption_mood = moodFilter;
      config.batch_size = batchSize;
    } else {
      config.b1_caption = b1Caption;
      config.b1_font_file = b1FontFile;
      config.b1_font_size = b1FontSize;
      config.b1_text_x = b1TextX;
      config.b1_text_y = b1TextY;
      config.block2 = { source: b2Source, source_type: b2SourceType, duration: b2Duration };
    }

    try {
      const res = await fetch(apiUrl('/api/slideshow/formats'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: activeProjectName, name: formatName.trim(), mode, config }),
      });
      if (!res.ok) throw new Error(`Save failed (${res.status})`);
      addNotification('success', `Format "${formatName.trim()}" saved`);
      setShowFormatSave(false);
      setFormatName('');
      void fetchFormats();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Save failed');
    }
  }, [activeProjectName, formatName, mode, b1Images, b1Duration, b1ShuffleSpeed, selectedAudio, selectedCaptionSource, moodFilter, batchSize, b1Caption, b1FontFile, b1FontSize, b1TextX, b1TextY, b2Source, b2SourceType, b2Duration, addNotification, fetchFormats]);

  const handleLoadFormat = useCallback((fmt: SlideshowFormat) => {
    const c = fmt.config;
    setMode(fmt.mode);
    if (Array.isArray(c.images)) setB1Images(new Set(c.images as string[]));
    if (typeof c.duration === 'number') setB1Duration(c.duration);
    if (typeof c.shuffle_speed === 'number') setB1ShuffleSpeed(c.shuffle_speed);
    if (typeof c.audio === 'string') setSelectedAudio(c.audio);
    else setSelectedAudio('');

    if (fmt.mode === 'meme') {
      if (typeof c.caption_source === 'string') setSelectedCaptionSource(c.caption_source);
      if (typeof c.caption_mood === 'string') setMoodFilter(c.caption_mood);
      else setMoodFilter(null);
      if (typeof c.batch_size === 'number') setBatchSize(c.batch_size);
    } else {
      if (typeof c.b1_caption === 'string') setB1Caption(c.b1_caption);
      if (typeof c.b1_font_file === 'string') setB1FontFile(c.b1_font_file);
      if (typeof c.b1_font_size === 'number') setB1FontSize(c.b1_font_size);
      if (typeof c.b1_text_x === 'number') setB1TextX(c.b1_text_x);
      if (typeof c.b1_text_y === 'number') setB1TextY(c.b1_text_y);
      const b2 = c.block2 as Record<string, unknown> | undefined;
      if (b2) {
        if (typeof b2.source === 'string') setB2Source(b2.source);
        if (b2.source_type === 'image' || b2.source_type === 'video') setB2SourceType(b2.source_type);
        if (typeof b2.duration === 'number') setB2Duration(b2.duration);
      }
    }
    addNotification('info', `Loaded format: ${fmt.name}`);
  }, [addNotification]);

  const handleDeleteFormat = useCallback(async (name: string) => {
    if (!activeProjectName) return;
    try {
      const safeName = name.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 64);
      const res = await fetch(
        apiUrl(`/api/slideshow/formats/${encodeURIComponent(safeName)}?project=${encodeURIComponent(activeProjectName)}`),
        { method: 'DELETE' },
      );
      if (!res.ok) throw new Error('Delete failed');
      void fetchFormats();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Delete failed');
    }
  }, [activeProjectName, addNotification, fetchFormats]);

  // ── Computed ──
  const totalDuration = mode === 'fan-page' ? b1Duration + b2Duration : b1Duration;
  const canRenderFanPage = b1Images.size > 0 && b2Source && !rendering;
  const canRenderMeme = b1Images.size > 0 && selectedCaptionSource && effectiveBatchSize > 0 && !memeRendering;

  // No project selected
  if (!activeProjectName) {
    return (
      <div className="p-6">
        <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-8 text-center shadow-[2px_2px_0_0_var(--border)]">
          <Film className="mx-auto mb-3 h-10 w-10 text-muted-foreground" />
          <h2 className="text-lg font-heading text-foreground mb-1">No Project Selected</h2>
          <p className="text-sm text-muted-foreground">Select or create a project to start building slideshows.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3 flex-wrap">
        <Film className="h-6 w-6 text-primary" />
        <h1 className="text-xl font-heading text-foreground">Slideshow</h1>
        <Badge variant="secondary" className="text-xs">{activeProjectName}</Badge>

        {/* Mode toggle */}
        <div className="flex gap-1 ml-4">
          <Button
            variant={mode === 'fan-page' ? 'default' : 'outline'}
            size="sm" className="h-7 px-3 text-xs"
            onClick={() => setMode('fan-page')}
          >
            <Layers className="mr-1 h-3 w-3" /> Fan Page
          </Button>
          <Button
            variant={mode === 'meme' ? 'default' : 'outline'}
            size="sm" className="h-7 px-3 text-xs"
            onClick={() => setMode('meme')}
          >
            <Sparkles className="mr-1 h-3 w-3" /> Meme
          </Button>
        </div>

        {/* Format controls */}
        <div className="ml-auto flex items-center gap-2">
          {formats.length > 0 && (
            <select
              onChange={(e) => {
                const fmt = formats.find((f) => f.name === e.target.value);
                if (fmt) handleLoadFormat(fmt);
                e.target.value = '';
              }}
              defaultValue=""
              className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
            >
              <option value="" disabled>Load Format...</option>
              {formats.map((f) => (
                <option key={f.name} value={f.name}>{f.name} ({f.mode})</option>
              ))}
            </select>
          )}
          {showFormatSave ? (
            <div className="flex gap-1">
              <input
                type="text" value={formatName} onChange={(e) => setFormatName(e.target.value)}
                placeholder="Format name..."
                className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground w-32"
                onKeyDown={(e) => { if (e.key === 'Enter') void handleSaveFormat(); if (e.key === 'Escape') setShowFormatSave(false); }}
                autoFocus
              />
              <Button variant="default" size="sm" className="h-7 px-2 text-xs" onClick={() => void handleSaveFormat()} disabled={!formatName.trim()}>
                Save
              </Button>
              <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => setShowFormatSave(false)}>
                Cancel
              </Button>
            </div>
          ) : (
            <Button variant="outline" size="sm" className="h-7 px-3 text-xs" onClick={() => setShowFormatSave(true)}>
              <Save className="mr-1 h-3 w-3" /> Save Format
            </Button>
          )}
          <span className="text-xs text-muted-foreground">
            Total: {totalDuration.toFixed(1)}s
          </span>
        </div>
      </div>

      {/* Saved formats list (collapsible) */}
      {formats.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {formats.map((f) => (
            <div key={f.name} className="group flex items-center gap-1 rounded-full border border-border bg-muted px-2.5 py-1 text-xs">
              <button className="font-medium text-foreground hover:text-primary" onClick={() => handleLoadFormat(f)}>
                {f.name}
              </button>
              <Badge variant="secondary" className="text-[9px] px-1 py-0">{f.mode}</Badge>
              <button
                className="text-muted-foreground hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                onClick={() => void handleDeleteFormat(f.name)}
              >
                <Trash2 className="h-2.5 w-2.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* ── Column 1: Image Gallery ── */}
        <div className="space-y-4">
          {/* Upload */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Upload className="h-4 w-4" /> Upload Images
            </h2>
            <input
              ref={fileRef} type="file" accept="image/*" multiple
              onChange={(e) => handleUpload(e.target.files)}
              className="block w-full text-sm text-foreground file:mr-3 file:rounded-[var(--border-radius)] file:border-2 file:border-border file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-bold file:text-foreground file:cursor-pointer hover:file:bg-muted"
            />
            {uploading && <Badge variant="info" className="mt-2 text-xs">Uploading...</Badge>}
          </div>

          {/* Gallery */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-heading text-foreground flex items-center gap-2">
                <ImageIcon className="h-4 w-4" /> Gallery
                {images.length > 0 && <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{images.length}</Badge>}
              </h2>
              {images.length > 0 && (
                <div className="flex gap-1">
                  <Button variant="outline" size="sm" className="h-6 px-2 text-[10px]" onClick={addAllImages}>Add All</Button>
                  {b1Images.size > 0 && (
                    <Button variant="outline" size="sm" className="h-6 px-2 text-[10px]" onClick={clearB1Images}>Clear</Button>
                  )}
                </div>
              )}
            </div>

            {images.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-6">Upload images to get started.</p>
            ) : (
              <div className="grid grid-cols-3 gap-2">
                {images.map((img) => {
                  const selected = b1Images.has(img.name);
                  return (
                    <div
                      key={img.name}
                      className={`group relative rounded-[var(--border-radius)] border-2 bg-muted overflow-hidden shadow-[2px_2px_0_0_var(--border)] cursor-pointer transition-all ${
                        selected ? 'border-primary ring-2 ring-primary/30' : 'border-border hover:border-muted-foreground'
                      }`}
                      onClick={() => toggleB1Image(img.name)}
                    >
                      <img
                        src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(img.name)}`)}
                        alt={img.name} className="w-full aspect-[9/16] object-cover"
                      />
                      <div className="absolute top-1 left-1">
                        {selected
                          ? <CheckSquare className="h-4 w-4 text-primary drop-shadow-md" />
                          : <Square className="h-4 w-4 text-white/60 drop-shadow-md opacity-0 group-hover:opacity-100 transition-opacity" />}
                      </div>
                      <button
                        className="absolute top-1 right-1 opacity-0 group-hover:opacity-100 transition-opacity bg-black/50 rounded p-0.5"
                        onClick={(e) => { e.stopPropagation(); handleDeleteImage(img.name); }}
                        title="Delete"
                      >
                        <Trash2 className="h-3 w-3 text-white" />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* ── Column 2: Block 1 config ── */}
        <div className="space-y-4">
          {/* Shuffle config (shared) */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Layers className="h-4 w-4" /> {mode === 'meme' ? 'Shuffle Config' : 'Block 1: Shuffle'}
              <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
                {b1Images.size} image{b1Images.size !== 1 ? 's' : ''}
              </Badge>
            </h2>

            {b1Images.size === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-4">
                Click images in the gallery to select them for the shuffle.
              </p>
            ) : (
              <>
                <div className="flex flex-wrap gap-1 mb-4">
                  {Array.from(b1Images).map((name) => (
                    <div key={name} className="relative group">
                      <img
                        src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(name)}`)}
                        alt={name} className="h-12 w-8 rounded border-2 border-border object-cover"
                      />
                      <button
                        className="absolute -top-1 -right-1 bg-red-500 rounded-full p-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
                        onClick={() => toggleB1Image(name)}
                      >
                        <Trash2 className="h-2 w-2 text-white" />
                      </button>
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-2 gap-3 mb-4">
                  <div>
                    <Label className="text-[10px] font-bold text-muted-foreground">Duration (sec)</Label>
                    <input
                      type="number" min={1} max={60} step={1} value={b1Duration}
                      onChange={(e) => setB1Duration(parseFloat(e.target.value) || 8)}
                      className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
                    />
                  </div>
                  <div>
                    <Label className="text-[10px] font-bold text-muted-foreground">Shuffle Speed (sec)</Label>
                    <input
                      type="number" min={0.2} max={1.0} step={0.05} value={b1ShuffleSpeed}
                      onChange={(e) => setB1ShuffleSpeed(parseFloat(e.target.value) || 0.4)}
                      className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
                    />
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Caption Controls (fan-page only) */}
          {mode === 'fan-page' && (
            <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
              <h2 className="text-sm font-heading text-foreground mb-3">Caption Overlay</h2>

              <textarea
                value={b1Caption} onChange={(e) => setB1Caption(e.target.value)}
                placeholder="Enter caption text (optional)..." rows={3}
                className="w-full rounded-[var(--border-radius)] border-2 border-border bg-muted px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground resize-none"
              />

              <div className="grid grid-cols-2 gap-3 mt-3">
                <div>
                  <Label className="text-[10px] font-bold text-muted-foreground">Font</Label>
                  <select
                    value={b1FontFile} onChange={(e) => setB1FontFile(e.target.value)}
                    className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
                  >
                    {fonts.map((f) => (<option key={f.file} value={f.file}>{f.name}</option>))}
                  </select>
                </div>
                <div>
                  <Label className="text-[10px] font-bold text-muted-foreground">Font Size</Label>
                  <input
                    type="number" min={8} max={120} step={1} value={b1FontSize}
                    onChange={(e) => setB1FontSize(parseInt(e.target.value) || 32)}
                    className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
                  />
                </div>
              </div>

              <div className="mt-3">
                <Label className="text-[10px] font-bold text-muted-foreground">Position</Label>
                <div className="flex gap-2 mt-1">
                  {(['top', 'center', 'bottom'] as QuickPosition[]).map((pos) => (
                    <Button
                      key={pos}
                      variant={b1TextY === POSITION_Y[pos] ? 'default' : 'outline'}
                      size="sm" className="h-7 px-3 text-xs capitalize flex-1"
                      onClick={() => { setB1TextX(50); setB1TextY(POSITION_Y[pos]); }}
                    >
                      {pos}
                    </Button>
                  ))}
                </div>
              </div>

              {b1Caption.trim() && b1Images.size > 0 && (
                <div className="mt-3 relative mx-auto" style={{ width: 160, height: 284 }}>
                  <img
                    src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(Array.from(b1Images)[0])}`)}
                    alt="preview" className="w-full h-full object-cover rounded-[var(--border-radius)] border-2 border-border"
                  />
                  <div
                    className="absolute pointer-events-none"
                    style={{
                      left: `${b1TextX}%`, top: `${b1TextY}%`,
                      transform: `translate(${getTextTranslateX(b1TextX, b1MaxWidthPct)}%, -50%)`,
                      width: `${b1MaxWidthPct}%`, textAlign: 'center',
                      fontFamily: `'${fontFamilyName(b1FontFile)}', sans-serif`,
                      fontSize: `${b1FontSize * (160 / 432)}px`, fontWeight: 700,
                      color: 'white', WebkitTextStroke: `${1}px black`,
                      paintOrder: 'stroke fill', lineHeight: 1.2, wordBreak: 'break-word',
                      textShadow: '1px 1px 2px rgba(0,0,0,0.5)',
                    }}
                  >
                    {b1Caption}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── Column 3: Mode-specific controls ── */}
        <div className="space-y-4">
          {mode === 'fan-page' ? (
            <>
              {/* Block 2 */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
                  <Square className="h-4 w-4" /> Block 2: Static
                </h2>

                <div className="flex gap-2 mb-3">
                  <Button variant={b2SourceType === 'image' ? 'default' : 'outline'} size="sm" className="h-7 px-3 text-xs flex-1"
                    onClick={() => { setB2SourceType('image'); setB2Source(''); }}>
                    <ImageIcon className="mr-1 h-3 w-3" /> Image
                  </Button>
                  <Button variant={b2SourceType === 'video' ? 'default' : 'outline'} size="sm" className="h-7 px-3 text-xs flex-1"
                    onClick={() => { setB2SourceType('video'); setB2Source(''); }}>
                    <Film className="mr-1 h-3 w-3" /> Video
                  </Button>
                </div>

                {b2SourceType === 'image' ? (
                  images.length > 0 ? (
                    <div className="grid grid-cols-3 gap-2 max-h-48 overflow-y-auto">
                      {images.map((img) => (
                        <div key={img.name}
                          className={`rounded-[var(--border-radius)] border-2 overflow-hidden cursor-pointer transition-all ${
                            b2Source === img.name ? 'border-primary ring-2 ring-primary/30' : 'border-border hover:border-muted-foreground'
                          }`}
                          onClick={() => setB2Source(img.name)}>
                          <img src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(img.name)}`)}
                            alt={img.name} className="w-full aspect-[9/16] object-cover" />
                        </div>
                      ))}
                    </div>
                  ) : (<p className="text-xs text-muted-foreground text-center py-4">Upload images first.</p>)
                ) : (
                  projectVideos.length > 0 ? (
                    <select value={b2Source} onChange={(e) => setB2Source(e.target.value)}
                      className="w-full rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1.5 text-xs font-bold text-foreground">
                      <option value="">Select a video...</option>
                      {projectVideos.map((v) => (<option key={v.name} value={v.name}>{v.name}</option>))}
                    </select>
                  ) : (<p className="text-xs text-muted-foreground text-center py-4">No project videos found.</p>)
                )}

                <div className="mt-3">
                  <Label className="text-[10px] font-bold text-muted-foreground">Duration (sec)</Label>
                  <input type="number" min={1} max={30} step={0.5} value={b2Duration}
                    onChange={(e) => setB2Duration(parseFloat(e.target.value) || 3)}
                    className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground" />
                </div>

                {b2Source && b2SourceType === 'image' && (
                  <div className="mt-3 mx-auto" style={{ width: 120 }}>
                    <img src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(b2Source)}`)}
                      alt="Block 2 preview" className="w-full rounded-[var(--border-radius)] border-2 border-border" />
                  </div>
                )}
              </div>

              {/* Audio */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
                  <Music className="h-4 w-4" /> Audio Track
                </h2>
                <input ref={audioFileRef} type="file" accept=".mp3,.wav,.m4a,.aac,.ogg"
                  onChange={(e) => handleAudioUpload(e.target.files)}
                  className="block w-full text-sm text-foreground file:mr-3 file:rounded-[var(--border-radius)] file:border-2 file:border-border file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-bold file:text-foreground file:cursor-pointer hover:file:bg-muted" />
                {uploadingAudio && <Badge variant="info" className="mt-2 text-xs">Uploading...</Badge>}
                {audioFiles.length > 0 && (
                  <div className="mt-3 space-y-2">
                    {audioFiles.map((af) => (
                      <div key={af.name}
                        className={`flex items-center gap-2 rounded-[var(--border-radius)] border-2 px-2 py-1.5 cursor-pointer transition-all ${
                          selectedAudio === af.name ? 'border-primary bg-primary/5' : 'border-border bg-muted hover:border-muted-foreground'
                        }`}
                        onClick={() => setSelectedAudio(selectedAudio === af.name ? '' : af.name)}>
                        <Volume2 className={`h-3 w-3 flex-shrink-0 ${selectedAudio === af.name ? 'text-primary' : 'text-muted-foreground'}`} />
                        <span className="text-xs font-bold text-foreground truncate flex-1">{af.name}</span>
                        <button className="opacity-0 group-hover:opacity-100 hover:opacity-100 transition-opacity"
                          onClick={(e) => { e.stopPropagation(); handleDeleteAudio(af.name); }}>
                          <Trash2 className="h-3 w-3 text-red-500" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Fan-page Render */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
                  <Film className="h-4 w-4" /> Render
                </h2>
                <div className="text-xs text-muted-foreground space-y-1 mb-3">
                  <p>Block 1: {b1Images.size} images, {b1Duration}s, {b1ShuffleSpeed}s/frame{b1Caption.trim() ? ' + caption' : ''}</p>
                  <p>Block 2: {b2Source || 'none'} ({b2SourceType}), {b2Duration}s</p>
                  <p>Audio: {selectedAudio || 'none'}</p>
                  <p className="font-bold text-foreground">Total: {totalDuration.toFixed(1)}s</p>
                </div>
                <Button onClick={handleRender} disabled={!canRenderFanPage} className="w-full">
                  <Play className="mr-2 h-4 w-4" />
                  {rendering ? 'Rendering...' : 'Render Slideshow'}
                </Button>

                {job && (job.status === 'pending' || job.status === 'running') && (
                  <div className="mt-3 space-y-2">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-bold text-foreground">{job.message || 'Processing...'}</span>
                      <span className="font-bold text-muted-foreground">{Math.round(job.progress)}%</span>
                    </div>
                    <div className="h-3 w-full rounded-[var(--border-radius)] border-2 border-border bg-muted overflow-hidden">
                      <div className="h-full bg-primary transition-all duration-300" style={{ width: `${job.progress}%` }} />
                    </div>
                  </div>
                )}
                {job?.status === 'complete' && (
                  <div className="mt-3 rounded-[var(--border-radius)] border-2 border-border bg-green-50 px-3 py-2">
                    <p className="text-xs font-bold text-green-800">Render complete!</p>
                  </div>
                )}
                {job?.status === 'error' && (
                  <div className="mt-3 rounded-[var(--border-radius)] border-2 border-border bg-red-50 px-3 py-2">
                    <p className="text-xs font-bold text-red-800">{job.message || 'Render failed.'}</p>
                  </div>
                )}
              </div>
            </>
          ) : (
            /* ── MEME MODE COLUMN ── */
            <>
              {/* Caption Source */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
                  <Sparkles className="h-4 w-4" /> Caption Source
                </h2>

                {captionSources.length === 0 ? (
                  <p className="text-xs text-muted-foreground text-center py-4">
                    No caption banks found. Scrape TikTok profiles on the Captions tab first.
                  </p>
                ) : (
                  <>
                    <div className="flex flex-wrap gap-1.5 mb-3">
                      {captionSources.map((s) => (
                        <button key={s.username} type="button"
                          onClick={() => { setSelectedCaptionSource(s.username); setMoodFilter(null); }}
                          className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                            selectedCaptionSource === s.username
                              ? 'border-primary bg-primary text-primary-foreground'
                              : 'border-border bg-muted text-muted-foreground hover:bg-accent'
                          }`}>
                          @{s.username}
                          <span className="ml-1 opacity-60">({s.count})</span>
                        </button>
                      ))}
                    </div>

                    {/* Mood filter */}
                    {selectedCaptionSource && (
                      <div className="mb-3">
                        <Label className="text-[10px] font-bold text-muted-foreground mb-1 block">Mood Filter</Label>
                        <div className="flex flex-wrap gap-1">
                          <button type="button"
                            onClick={() => setMoodFilter(null)}
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors ${
                              !moodFilter ? 'border-primary bg-primary text-primary-foreground' : 'border-border bg-muted text-muted-foreground hover:bg-accent'
                            }`}>
                            All
                          </button>
                          {MOOD_TAGS.map((tag) => (
                            <button key={tag} type="button"
                              onClick={() => setMoodFilter(moodFilter === tag ? null : tag)}
                              className={`rounded-full border px-2 py-0.5 text-[10px] font-medium capitalize transition-colors ${
                                moodFilter === tag ? 'border-primary bg-primary text-primary-foreground' : 'border-border bg-muted text-muted-foreground hover:bg-accent'
                              }`}>
                              {tag}
                            </button>
                          ))}
                        </div>
                        <p className="text-[10px] text-muted-foreground mt-1">
                          {filteredCaptions.length} caption{filteredCaptions.length !== 1 ? 's' : ''} available
                        </p>
                      </div>
                    )}
                  </>
                )}
              </div>

              {/* Batch Config */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3">Batch Config</h2>
                <div>
                  <Label className="text-[10px] font-bold text-muted-foreground">How many to render</Label>
                  <input type="number" min={1} max={50} step={1} value={batchSize}
                    onChange={(e) => setBatchSize(Math.max(1, Math.min(50, parseInt(e.target.value) || 1)))}
                    className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground" />
                  {filteredCaptions.length > 0 && batchSize > filteredCaptions.length && (
                    <p className="text-[10px] text-amber-600 mt-1">
                      Capped to {filteredCaptions.length} (available captions)
                    </p>
                  )}
                </div>
              </div>

              {/* Audio */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
                  <Music className="h-4 w-4" /> Audio Track
                </h2>
                <input ref={audioFileRef} type="file" accept=".mp3,.wav,.m4a,.aac,.ogg"
                  onChange={(e) => handleAudioUpload(e.target.files)}
                  className="block w-full text-sm text-foreground file:mr-3 file:rounded-[var(--border-radius)] file:border-2 file:border-border file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-bold file:text-foreground file:cursor-pointer hover:file:bg-muted" />
                {uploadingAudio && <Badge variant="info" className="mt-2 text-xs">Uploading...</Badge>}
                {audioFiles.length > 0 && (
                  <div className="mt-3 space-y-2">
                    {audioFiles.map((af) => (
                      <div key={af.name}
                        className={`flex items-center gap-2 rounded-[var(--border-radius)] border-2 px-2 py-1.5 cursor-pointer transition-all ${
                          selectedAudio === af.name ? 'border-primary bg-primary/5' : 'border-border bg-muted hover:border-muted-foreground'
                        }`}
                        onClick={() => setSelectedAudio(selectedAudio === af.name ? '' : af.name)}>
                        <Volume2 className={`h-3 w-3 flex-shrink-0 ${selectedAudio === af.name ? 'text-primary' : 'text-muted-foreground'}`} />
                        <span className="text-xs font-bold text-foreground truncate flex-1">{af.name}</span>
                        <button className="opacity-0 group-hover:opacity-100 hover:opacity-100 transition-opacity"
                          onClick={(e) => { e.stopPropagation(); handleDeleteAudio(af.name); }}>
                          <Trash2 className="h-3 w-3 text-red-500" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Meme Render */}
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
                <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
                  <Sparkles className="h-4 w-4" /> Render Batch
                </h2>
                <div className="text-xs text-muted-foreground space-y-1 mb-3">
                  <p>Images: {b1Images.size} selected, {b1Duration}s, {b1ShuffleSpeed}s/frame</p>
                  <p>Captions: {selectedCaptionSource ? `@${selectedCaptionSource}` : 'none'}{moodFilter ? ` (${moodFilter})` : ''}</p>
                  <p>Batch: {effectiveBatchSize} video{effectiveBatchSize !== 1 ? 's' : ''}</p>
                  <p>Audio: {selectedAudio || 'none'}</p>
                </div>

                <Button onClick={handleMemeRender} disabled={!canRenderMeme} className="w-full">
                  <Play className="mr-2 h-4 w-4" />
                  {memeRendering ? `Rendering...` : `Render ${effectiveBatchSize} Meme${effectiveBatchSize !== 1 ? 's' : ''}`}
                </Button>

                {/* Batch progress */}
                {batchJob && (batchJob.status === 'pending' || batchJob.status === 'running') && (
                  <div className="mt-3 space-y-2">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-bold text-foreground">{batchJob.message || 'Processing...'}</span>
                      <span className="font-bold text-muted-foreground">{batchJob.completed}/{batchJob.batch_size}</span>
                    </div>
                    <div className="h-3 w-full rounded-[var(--border-radius)] border-2 border-border bg-muted overflow-hidden">
                      <div className="h-full bg-primary transition-all duration-300"
                        style={{ width: `${batchJob.batch_size > 0 ? (batchJob.completed / batchJob.batch_size) * 100 : 0}%` }} />
                    </div>
                  </div>
                )}

                {batchJob?.status === 'complete' && memeOutputs.length > 0 && (
                  <div className="mt-3 space-y-3">
                    <div className="flex items-center justify-between">
                      <p className="text-xs font-bold text-green-800">
                        {memeOutputs.length} video{memeOutputs.length !== 1 ? 's' : ''} ready
                      </p>
                      <Button variant="default" size="sm" className="h-7 px-3 text-xs" onClick={sendToBurn}>
                        <ArrowRight className="mr-1 h-3 w-3" /> Use in Burn
                      </Button>
                    </div>
                    <div className="grid grid-cols-2 gap-2 max-h-64 overflow-y-auto">
                      {memeOutputs.map((item) => (
                        <div key={item.index} className="rounded-[var(--border-radius)] border-2 border-border bg-muted overflow-hidden">
                          <video
                            src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(item.output!)}`)}
                            controls className="w-full aspect-[9/16]"
                          />
                          <div className="p-1 flex items-center justify-between">
                            <span className="text-[10px] text-muted-foreground truncate">{item.output}</span>
                            <a href={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(item.output!)}`)}
                              download className="inline-flex">
                              <Download className="h-3 w-3 text-muted-foreground hover:text-foreground" />
                            </a>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {batchJob?.status === 'error' && (
                  <div className="mt-3 rounded-[var(--border-radius)] border-2 border-border bg-red-50 px-3 py-2">
                    <p className="text-xs font-bold text-red-800">{batchJob.message || 'Batch render failed.'}</p>
                  </div>
                )}
              </div>
            </>
          )}

          {/* Completed Renders (shared between modes) */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Film className="h-4 w-4" /> Completed Renders
              {renders.length > 0 && <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{renders.length}</Badge>}
            </h2>

            {renders.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-4">No renders yet.</p>
            ) : (
              <div className="space-y-2">
                {renders.map((render) => (
                  <div key={render.name} className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-3">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-bold text-foreground truncate flex-1 mr-2">{render.name}</span>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <a href={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(render.name)}`)}
                          download className="inline-flex">
                          <Button variant="outline" size="sm" className="h-7 px-2 text-xs">Download</Button>
                        </a>
                        <Button variant="outline" size="sm" className="h-7 px-2 text-xs text-red-600 hover:bg-red-50"
                          onClick={() => handleDeleteRender(render.name)}>
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                    <video
                      src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(render.name)}`)}
                      controls className="w-full rounded-[var(--border-radius)] border-2 border-border"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
