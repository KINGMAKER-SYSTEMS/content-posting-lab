import { useState, useEffect, useRef, useCallback } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { apiUrl } from '../lib/api';
import { captureTextOverlay, fontFamilyName, getTextTranslateX } from '../lib/textOverlay';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import {
  Upload, Trash2, Play, Image as ImageIcon, Film, Music,
  Layers, Square, CheckSquare, Volume2,
} from 'lucide-react';
import type {
  SlideshowImage, SlideshowRender, SlideshowRenderJob,
  SlideshowAudioFile, SlideshowProjectVideo, FontInfo,
} from '../types/api';

type QuickPosition = 'top' | 'center' | 'bottom';
const POSITION_Y: Record<QuickPosition, number> = { top: 15, center: 50, bottom: 85 };

export function SlideshowPage() {
  const { activeProjectName, addNotification } = useWorkflowStore();

  // Image gallery
  const [images, setImages] = useState<SlideshowImage[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // Block 1 state
  const [b1Images, setB1Images] = useState<Set<string>>(new Set());
  const [b1Duration, setB1Duration] = useState(8);
  const [b1ShuffleSpeed, setB1ShuffleSpeed] = useState(0.4);
  const [b1Caption, setB1Caption] = useState('');
  const [b1FontFile, setB1FontFile] = useState('TikTokSans16pt-Bold.ttf');
  const [b1FontSize, setB1FontSize] = useState(32);
  const [b1TextX, setB1TextX] = useState(50);
  const [b1TextY, setB1TextY] = useState(50);
  const [b1MaxWidthPct] = useState(80);

  // Block 2 state
  const [b2SourceType, setB2SourceType] = useState<'image' | 'video'>('image');
  const [b2Source, setB2Source] = useState('');
  const [b2Duration, setB2Duration] = useState(3);
  const [projectVideos, setProjectVideos] = useState<SlideshowProjectVideo[]>([]);

  // Audio state
  const [audioFiles, setAudioFiles] = useState<SlideshowAudioFile[]>([]);
  const [selectedAudio, setSelectedAudio] = useState('');
  const [uploadingAudio, setUploadingAudio] = useState(false);
  const audioFileRef = useRef<HTMLInputElement>(null);

  // Fonts
  const [fonts, setFonts] = useState<FontInfo[]>([]);

  // Render state
  const [renders, setRenders] = useState<SlideshowRender[]>([]);
  const [rendering, setRendering] = useState(false);
  const [job, setJob] = useState<SlideshowRenderJob | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Data fetching ──

  const fetchImages = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/images?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) {
        const data = await res.json();
        setImages(data.images ?? []);
      }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchRenders = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/renders?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) {
        const data = await res.json();
        setRenders(data.renders ?? []);
      }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchAudio = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/audio?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) {
        const data = await res.json();
        setAudioFiles(data.audio ?? []);
      }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchProjectVideos = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/project-videos?project=${encodeURIComponent(activeProjectName)}`));
      if (res.ok) {
        const data = await res.json();
        setProjectVideos(data.videos ?? []);
      }
    } catch { /* ignore */ }
  }, [activeProjectName]);

  const fetchFonts = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/burn/fonts'));
      if (res.ok) {
        const data = await res.json();
        setFonts(data.fonts ?? []);
      }
    } catch { /* ignore */ }
  }, []);

  // Reset on project change
  useEffect(() => {
    setB1Images(new Set());
    setB2Source('');
    setSelectedAudio('');
    setJob(null);
    setRendering(false);
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
    void fetchImages();
    void fetchRenders();
    void fetchAudio();
    void fetchProjectVideos();
    void fetchFonts();
  }, [activeProjectName, fetchImages, fetchRenders, fetchAudio, fetchProjectVideos, fetchFonts]);

  useEffect(() => { return () => { if (pollingRef.current) clearInterval(pollingRef.current); }; }, []);

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

  const addAllImages = useCallback(() => {
    setB1Images(new Set(images.map((i) => i.name)));
  }, [images]);

  const clearB1Images = useCallback(() => { setB1Images(new Set()); }, []);

  // ── Render ──

  const handleRender = useCallback(async () => {
    if (!activeProjectName || b1Images.size === 0 || !b2Source) return;
    setRendering(true);
    setJob(null);

    try {
      // Generate text overlay PNG if caption provided
      let overlayPng: string | undefined;
      if (b1Caption.trim()) {
        const png = await captureTextOverlay({
          caption: b1Caption,
          x: b1TextX,
          y: b1TextY,
          fontSize: b1FontSize,
          fontFile: b1FontFile,
          maxWidthPct: b1MaxWidthPct,
          videoWidth: 432,
          videoHeight: 768,
        });
        if (png) overlayPng = png;
      }

      const body = {
        project: activeProjectName,
        block1: {
          images: Array.from(b1Images),
          duration: b1Duration,
          shuffle_speed: b1ShuffleSpeed,
          overlay_png: overlayPng,
        },
        block2: {
          source: b2Source,
          source_type: b2SourceType,
          duration: b2Duration,
        },
        audio: selectedAudio || undefined,
      };

      const res = await fetch(apiUrl('/api/slideshow/render-v2'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
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
            if (pollData.status === 'complete') {
              addNotification('success', 'Slideshow render complete!');
              void fetchRenders();
            } else if (pollData.status === 'error') {
              addNotification('error', pollData.message || 'Render failed');
            }
          }
        } catch { /* ignore */ }
      }, 1000);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Render failed');
      setRendering(false);
    }
  }, [activeProjectName, b1Images, b1Duration, b1ShuffleSpeed, b1Caption, b1FontFile, b1FontSize, b1TextX, b1TextY, b1MaxWidthPct, b2Source, b2SourceType, b2Duration, selectedAudio, addNotification, fetchRenders]);

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

  const totalDuration = b1Duration + b2Duration;
  const canRender = b1Images.size > 0 && b2Source && !rendering;

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
      <div className="flex items-center gap-3">
        <Film className="h-6 w-6 text-primary" />
        <h1 className="text-xl font-heading text-foreground">Slideshow</h1>
        <Badge variant="secondary" className="text-xs">{activeProjectName}</Badge>
        <span className="text-xs text-muted-foreground ml-auto">
          Total: {totalDuration.toFixed(1)}s
        </span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* ── Column 1: Image Gallery ── */}
        <div className="space-y-4">
          {/* Upload */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Upload className="h-4 w-4" /> Upload Images
            </h2>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
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
                  <Button variant="outline" size="sm" className="h-6 px-2 text-[10px]" onClick={addAllImages}>
                    Add All
                  </Button>
                  {b1Images.size > 0 && (
                    <Button variant="outline" size="sm" className="h-6 px-2 text-[10px]" onClick={clearB1Images}>
                      Clear
                    </Button>
                  )}
                </div>
              )}
            </div>

            {images.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-6">
                Upload images to get started.
              </p>
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
                        alt={img.name}
                        className="w-full aspect-[9/16] object-cover"
                      />
                      {/* Selection indicator */}
                      <div className="absolute top-1 left-1">
                        {selected
                          ? <CheckSquare className="h-4 w-4 text-primary drop-shadow-md" />
                          : <Square className="h-4 w-4 text-white/60 drop-shadow-md opacity-0 group-hover:opacity-100 transition-opacity" />
                        }
                      </div>
                      {/* Delete button */}
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

        {/* ── Column 2: Block 1 (Shuffle + Caption) ── */}
        <div className="space-y-4">
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Layers className="h-4 w-4" /> Block 1: Shuffle
              <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
                {b1Images.size} image{b1Images.size !== 1 ? 's' : ''}
              </Badge>
            </h2>

            {b1Images.size === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-4">
                Click images in the gallery to select them for the shuffle block.
              </p>
            ) : (
              <>
                {/* Selected thumbnails */}
                <div className="flex flex-wrap gap-1 mb-4">
                  {Array.from(b1Images).map((name) => (
                    <div key={name} className="relative group">
                      <img
                        src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(name)}`)}
                        alt={name}
                        className="h-12 w-8 rounded border-2 border-border object-cover"
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

                {/* Controls */}
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

          {/* Caption Controls */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3">Caption Overlay</h2>

            <textarea
              value={b1Caption}
              onChange={(e) => setB1Caption(e.target.value)}
              placeholder="Enter caption text (optional)..."
              rows={3}
              className="w-full rounded-[var(--border-radius)] border-2 border-border bg-muted px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground resize-none"
            />

            <div className="grid grid-cols-2 gap-3 mt-3">
              <div>
                <Label className="text-[10px] font-bold text-muted-foreground">Font</Label>
                <select
                  value={b1FontFile}
                  onChange={(e) => setB1FontFile(e.target.value)}
                  className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
                >
                  {fonts.map((f) => (
                    <option key={f.file} value={f.file}>{f.name}</option>
                  ))}
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

            {/* Quick position */}
            <div className="mt-3">
              <Label className="text-[10px] font-bold text-muted-foreground">Position</Label>
              <div className="flex gap-2 mt-1">
                {(['top', 'center', 'bottom'] as QuickPosition[]).map((pos) => (
                  <Button
                    key={pos}
                    variant={b1TextY === POSITION_Y[pos] ? 'default' : 'outline'}
                    size="sm"
                    className="h-7 px-3 text-xs capitalize flex-1"
                    onClick={() => { setB1TextX(50); setB1TextY(POSITION_Y[pos]); }}
                  >
                    {pos}
                  </Button>
                ))}
              </div>
            </div>

            {/* Preview */}
            {b1Caption.trim() && b1Images.size > 0 && (
              <div className="mt-3 relative mx-auto" style={{ width: 160, height: 284 }}>
                <img
                  src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(Array.from(b1Images)[0])}`)}
                  alt="preview"
                  className="w-full h-full object-cover rounded-[var(--border-radius)] border-2 border-border"
                />
                <div
                  className="absolute pointer-events-none"
                  style={{
                    left: `${b1TextX}%`,
                    top: `${b1TextY}%`,
                    transform: `translate(${getTextTranslateX(b1TextX, b1MaxWidthPct)}%, -50%)`,
                    width: `${b1MaxWidthPct}%`,
                    textAlign: 'center',
                    fontFamily: `'${fontFamilyName(b1FontFile)}', sans-serif`,
                    fontSize: `${b1FontSize * (160 / 432)}px`,
                    fontWeight: 700,
                    color: 'white',
                    WebkitTextStroke: `${1}px black`,
                    paintOrder: 'stroke fill',
                    lineHeight: 1.2,
                    wordBreak: 'break-word',
                    textShadow: '1px 1px 2px rgba(0,0,0,0.5)',
                  }}
                >
                  {b1Caption}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ── Column 3: Block 2 + Audio + Render ── */}
        <div className="space-y-4">
          {/* Block 2 */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Square className="h-4 w-4" /> Block 2: Static
            </h2>

            {/* Source type toggle */}
            <div className="flex gap-2 mb-3">
              <Button
                variant={b2SourceType === 'image' ? 'default' : 'outline'}
                size="sm" className="h-7 px-3 text-xs flex-1"
                onClick={() => { setB2SourceType('image'); setB2Source(''); }}
              >
                <ImageIcon className="mr-1 h-3 w-3" /> Image
              </Button>
              <Button
                variant={b2SourceType === 'video' ? 'default' : 'outline'}
                size="sm" className="h-7 px-3 text-xs flex-1"
                onClick={() => { setB2SourceType('video'); setB2Source(''); }}
              >
                <Film className="mr-1 h-3 w-3" /> Video
              </Button>
            </div>

            {/* Source picker */}
            {b2SourceType === 'image' ? (
              images.length > 0 ? (
                <div className="grid grid-cols-3 gap-2 max-h-48 overflow-y-auto">
                  {images.map((img) => (
                    <div
                      key={img.name}
                      className={`rounded-[var(--border-radius)] border-2 overflow-hidden cursor-pointer transition-all ${
                        b2Source === img.name ? 'border-primary ring-2 ring-primary/30' : 'border-border hover:border-muted-foreground'
                      }`}
                      onClick={() => setB2Source(img.name)}
                    >
                      <img
                        src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(img.name)}`)}
                        alt={img.name}
                        className="w-full aspect-[9/16] object-cover"
                      />
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground text-center py-4">Upload images first.</p>
              )
            ) : (
              projectVideos.length > 0 ? (
                <select
                  value={b2Source}
                  onChange={(e) => setB2Source(e.target.value)}
                  className="w-full rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1.5 text-xs font-bold text-foreground"
                >
                  <option value="">Select a video...</option>
                  {projectVideos.map((v) => (
                    <option key={v.name} value={v.name}>{v.name}</option>
                  ))}
                </select>
              ) : (
                <p className="text-xs text-muted-foreground text-center py-4">No project videos found.</p>
              )
            )}

            <div className="mt-3">
              <Label className="text-[10px] font-bold text-muted-foreground">Duration (sec)</Label>
              <input
                type="number" min={1} max={30} step={0.5} value={b2Duration}
                onChange={(e) => setB2Duration(parseFloat(e.target.value) || 3)}
                className="w-full mt-1 rounded-[var(--border-radius)] border-2 border-border bg-muted px-2 py-1 text-xs font-bold text-foreground"
              />
            </div>

            {/* Preview */}
            {b2Source && b2SourceType === 'image' && (
              <div className="mt-3 mx-auto" style={{ width: 120 }}>
                <img
                  src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(b2Source)}`)}
                  alt="Block 2 preview"
                  className="w-full rounded-[var(--border-radius)] border-2 border-border"
                />
              </div>
            )}
          </div>

          {/* Audio */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Music className="h-4 w-4" /> Audio Track
            </h2>

            <input
              ref={audioFileRef}
              type="file"
              accept=".mp3,.wav,.m4a,.aac,.ogg"
              onChange={(e) => handleAudioUpload(e.target.files)}
              className="block w-full text-sm text-foreground file:mr-3 file:rounded-[var(--border-radius)] file:border-2 file:border-border file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-bold file:text-foreground file:cursor-pointer hover:file:bg-muted"
            />
            {uploadingAudio && <Badge variant="info" className="mt-2 text-xs">Uploading...</Badge>}

            {audioFiles.length > 0 && (
              <div className="mt-3 space-y-2">
                {audioFiles.map((af) => (
                  <div
                    key={af.name}
                    className={`flex items-center gap-2 rounded-[var(--border-radius)] border-2 px-2 py-1.5 cursor-pointer transition-all ${
                      selectedAudio === af.name ? 'border-primary bg-primary/5' : 'border-border bg-muted hover:border-muted-foreground'
                    }`}
                    onClick={() => setSelectedAudio(selectedAudio === af.name ? '' : af.name)}
                  >
                    <Volume2 className={`h-3 w-3 flex-shrink-0 ${selectedAudio === af.name ? 'text-primary' : 'text-muted-foreground'}`} />
                    <span className="text-xs font-bold text-foreground truncate flex-1">{af.name}</span>
                    <button
                      className="opacity-0 group-hover:opacity-100 hover:opacity-100 transition-opacity"
                      onClick={(e) => { e.stopPropagation(); handleDeleteAudio(af.name); }}
                    >
                      <Trash2 className="h-3 w-3 text-red-500" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Render */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Film className="h-4 w-4" /> Render
            </h2>

            {/* Summary */}
            <div className="text-xs text-muted-foreground space-y-1 mb-3">
              <p>Block 1: {b1Images.size} images, {b1Duration}s, {b1ShuffleSpeed}s/frame{b1Caption.trim() ? ' + caption' : ''}</p>
              <p>Block 2: {b2Source || 'none'} ({b2SourceType}), {b2Duration}s</p>
              <p>Audio: {selectedAudio || 'none'}</p>
              <p className="font-bold text-foreground">Total: {totalDuration.toFixed(1)}s</p>
            </div>

            <Button onClick={handleRender} disabled={!canRender} className="w-full">
              <Play className="mr-2 h-4 w-4" />
              {rendering ? 'Rendering...' : 'Render Slideshow'}
            </Button>

            {/* Progress */}
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

          {/* Completed Renders */}
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
                        <a
                          href={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(render.name)}`)}
                          download
                          className="inline-flex"
                        >
                          <Button variant="outline" size="sm" className="h-7 px-2 text-xs">Download</Button>
                        </a>
                        <Button
                          variant="outline" size="sm"
                          className="h-7 px-2 text-xs text-red-600 hover:bg-red-50"
                          onClick={() => handleDeleteRender(render.name)}
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                    <video
                      src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(render.name)}`)}
                      controls
                      className="w-full rounded-[var(--border-radius)] border-2 border-border"
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
