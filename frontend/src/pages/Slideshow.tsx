import { useState, useEffect, useRef, useCallback } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { apiUrl } from '../lib/api';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Upload, Trash2, Play, ChevronUp, ChevronDown, Plus, Image as ImageIcon, Film } from 'lucide-react';
import type { SlideshowImage, SlideshowRender, SlideshowRenderJob } from '../types/api';

interface Slide {
  id: string;
  image: string; // filename
  duration: number; // seconds
}

export function SlideshowPage() {
  const { activeProjectName, addNotification } = useWorkflowStore();
  const [images, setImages] = useState<SlideshowImage[]>([]);
  const [slides, setSlides] = useState<Slide[]>([]);
  const [renders, setRenders] = useState<SlideshowRender[]>([]);
  const [uploading, setUploading] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [job, setJob] = useState<SlideshowRenderJob | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Fetch images on project change
  const fetchImages = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/images?project=${encodeURIComponent(activeProjectName)}`));
      if (!res.ok) throw new Error(`Failed to fetch images (${res.status})`);
      const data = await res.json();
      setImages(data.images ?? []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to fetch images';
      addNotification('error', msg);
    }
  }, [activeProjectName, addNotification]);

  // Fetch renders on project change
  const fetchRenders = useCallback(async () => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(apiUrl(`/api/slideshow/renders?project=${encodeURIComponent(activeProjectName)}`));
      if (!res.ok) throw new Error(`Failed to fetch renders (${res.status})`);
      const data = await res.json();
      setRenders(data.renders ?? []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to fetch renders';
      addNotification('error', msg);
    }
  }, [activeProjectName, addNotification]);

  useEffect(() => {
    setSlides([]);
    setJob(null);
    setRendering(false);
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    void fetchImages();
    void fetchRenders();
  }, [activeProjectName, fetchImages, fetchRenders]);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, []);

  // Upload handler
  const handleUpload = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0 || !activeProjectName) return;
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('project', activeProjectName);
      for (let i = 0; i < files.length; i++) {
        formData.append('files', files[i]);
      }
      const res = await fetch(apiUrl('/api/slideshow/upload'), {
        method: 'POST',
        body: formData,
      });
      if (!res.ok) throw new Error(`Upload failed (${res.status})`);
      addNotification('success', `Uploaded ${files.length} image(s)`);
      void fetchImages();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Upload failed';
      addNotification('error', msg);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }, [activeProjectName, addNotification, fetchImages]);

  // Delete image handler
  const handleDeleteImage = useCallback(async (filename: string) => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(
        apiUrl(`/api/slideshow/images/${encodeURIComponent(filename)}?project=${encodeURIComponent(activeProjectName)}`),
        { method: 'DELETE' },
      );
      if (!res.ok) throw new Error(`Delete failed (${res.status})`);
      addNotification('success', `Deleted ${filename}`);
      // Remove from slides too
      setSlides((prev) => prev.filter((s) => s.image !== filename));
      void fetchImages();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Delete failed';
      addNotification('error', msg);
    }
  }, [activeProjectName, addNotification, fetchImages]);

  // Add image to slides
  const addSlide = useCallback((imageName: string) => {
    setSlides((prev) => [
      ...prev,
      { id: crypto.randomUUID(), image: imageName, duration: 3 },
    ]);
  }, []);

  // Remove slide
  const removeSlide = useCallback((id: string) => {
    setSlides((prev) => prev.filter((s) => s.id !== id));
  }, []);

  // Reorder slides
  const moveSlide = useCallback((id: string, direction: 'up' | 'down') => {
    setSlides((prev) => {
      const idx = prev.findIndex((s) => s.id === id);
      if (idx < 0) return prev;
      const newIdx = direction === 'up' ? idx - 1 : idx + 1;
      if (newIdx < 0 || newIdx >= prev.length) return prev;
      const copy = [...prev];
      [copy[idx], copy[newIdx]] = [copy[newIdx], copy[idx]];
      return copy;
    });
  }, []);

  // Update slide duration
  const updateDuration = useCallback((id: string, duration: number) => {
    setSlides((prev) =>
      prev.map((s) => (s.id === id ? { ...s, duration } : s)),
    );
  }, []);

  // Render handler with polling
  const handleRender = useCallback(async () => {
    if (!activeProjectName || slides.length === 0) return;
    setRendering(true);
    setJob(null);
    try {
      const res = await fetch(apiUrl('/api/slideshow/render'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: activeProjectName,
          slides: slides.map((s) => ({ image: s.image, duration: s.duration })),
        }),
      });
      if (!res.ok) throw new Error(`Render request failed (${res.status})`);
      const data = await res.json();
      const newJobId = data.job_id as string;

      // Start polling
      pollingRef.current = setInterval(async () => {
        try {
          const pollRes = await fetch(apiUrl(`/api/slideshow/job/${newJobId}`));
          if (!pollRes.ok) return;
          const pollData = (await pollRes.json()) as SlideshowRenderJob;
          setJob(pollData);

          if (pollData.status === 'complete' || pollData.status === 'error' || pollData.status === 'not_found') {
            if (pollingRef.current) {
              clearInterval(pollingRef.current);
              pollingRef.current = null;
            }
            setRendering(false);
            if (pollData.status === 'complete') {
              addNotification('success', 'Slideshow render complete!');
              void fetchRenders();
            } else if (pollData.status === 'error') {
              addNotification('error', pollData.message || 'Render failed');
            }
          }
        } catch {
          // Ignore polling errors
        }
      }, 1000);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Render failed';
      addNotification('error', msg);
      setRendering(false);
    }
  }, [activeProjectName, slides, addNotification, fetchRenders]);

  // Delete render handler
  const handleDeleteRender = useCallback(async (filename: string) => {
    if (!activeProjectName) return;
    try {
      const res = await fetch(
        apiUrl(`/api/slideshow/renders/${encodeURIComponent(filename)}?project=${encodeURIComponent(activeProjectName)}`),
        { method: 'DELETE' },
      );
      if (!res.ok) throw new Error(`Delete failed (${res.status})`);
      addNotification('success', `Deleted render ${filename}`);
      void fetchRenders();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Delete failed';
      addNotification('error', msg);
    }
  }, [activeProjectName, addNotification, fetchRenders]);

  const totalDuration = slides.reduce((sum, s) => sum + s.duration, 0);

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
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* ── Left Column: Upload + Image Gallery ── */}
        <div className="space-y-4">
          {/* Upload Area */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Upload className="h-4 w-4" />
              Upload Images
            </h2>
            <div className="flex items-center gap-3">
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                multiple
                onChange={(e) => handleUpload(e.target.files)}
                className="block w-full text-sm text-foreground file:mr-3 file:rounded-[var(--border-radius)] file:border-2 file:border-border file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-bold file:text-foreground file:cursor-pointer hover:file:bg-muted"
              />
              {uploading && (
                <Badge variant="info" className="text-xs whitespace-nowrap">Uploading...</Badge>
              )}
            </div>
          </div>

          {/* Image Gallery */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <ImageIcon className="h-4 w-4" />
              Image Gallery
              {images.length > 0 && (
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{images.length}</Badge>
              )}
            </h2>

            {images.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-6">
                No images uploaded yet. Upload images to get started.
              </p>
            ) : (
              <div className="grid grid-cols-3 sm:grid-cols-4 gap-3">
                {images.map((img) => (
                  <div
                    key={img.name}
                    className="group relative rounded-[var(--border-radius)] border-2 border-border bg-muted overflow-hidden shadow-[2px_2px_0_0_var(--border)] hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[1px_1px_0_0_var(--border)] transition-all"
                  >
                    <img
                      src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(img.name)}`)}
                      alt={img.name}
                      className="w-full aspect-square object-cover"
                    />
                    <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors flex items-center justify-center gap-1 opacity-0 group-hover:opacity-100">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-xs bg-card"
                        onClick={() => addSlide(img.name)}
                        title="Add to slideshow"
                      >
                        <Plus className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-xs bg-card text-red-600 hover:bg-red-50"
                        onClick={() => handleDeleteImage(img.name)}
                        title="Delete image"
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                    <div className="px-1.5 py-1 text-[10px] font-bold text-foreground truncate bg-card border-t-2 border-border">
                      {img.name}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* ── Right Column: Slide List + Render + Renders ── */}
        <div className="space-y-4">
          {/* Slide List */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-heading text-foreground flex items-center gap-2">
                <Play className="h-4 w-4" />
                Slide Timeline
                {slides.length > 0 && (
                  <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{slides.length}</Badge>
                )}
              </h2>
              {slides.length > 0 && (
                <span className="text-xs font-bold text-muted-foreground">
                  Total: {totalDuration.toFixed(1)}s
                </span>
              )}
            </div>

            {slides.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-6">
                Click the <Plus className="inline h-3 w-3" /> button on images in the gallery to add them to your slideshow.
              </p>
            ) : (
              <div className="space-y-2">
                {slides.map((slide, idx) => (
                  <div
                    key={slide.id}
                    className="flex items-center gap-3 rounded-[var(--border-radius)] border-2 border-border bg-muted px-3 py-2"
                  >
                    {/* Thumbnail */}
                    <img
                      src={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/slideshow-images/${encodeURIComponent(slide.image)}`)}
                      alt={slide.image}
                      className="h-10 w-10 rounded-[var(--border-radius)] border-2 border-border object-cover flex-shrink-0"
                    />

                    {/* Info */}
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-bold text-foreground truncate">{slide.image}</p>
                      <div className="flex items-center gap-2 mt-1">
                        <label className="text-[10px] font-bold text-muted-foreground">Duration:</label>
                        <input
                          type="number"
                          min={0.5}
                          max={30}
                          step={0.5}
                          value={slide.duration}
                          onChange={(e) => updateDuration(slide.id, parseFloat(e.target.value) || 3)}
                          className="w-16 rounded-[var(--border-radius)] border-2 border-border bg-card px-1.5 py-0.5 text-xs font-bold text-foreground"
                        />
                        <span className="text-[10px] text-muted-foreground">sec</span>
                      </div>
                    </div>

                    {/* Controls */}
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 w-6 p-0"
                        disabled={idx === 0}
                        onClick={() => moveSlide(slide.id, 'up')}
                        title="Move up"
                      >
                        <ChevronUp className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 w-6 p-0"
                        disabled={idx === slides.length - 1}
                        onClick={() => moveSlide(slide.id, 'down')}
                        title="Move down"
                      >
                        <ChevronDown className="h-3 w-3" />
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 w-6 p-0 text-red-600 hover:bg-red-50"
                        onClick={() => removeSlide(slide.id)}
                        title="Remove slide"
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Render Controls */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Film className="h-4 w-4" />
              Render
            </h2>

            <Button
              onClick={handleRender}
              disabled={slides.length === 0 || rendering}
              className="w-full"
            >
              <Play className="mr-2 h-4 w-4" />
              {rendering ? 'Rendering...' : `Render Slideshow (${slides.length} slides, ${totalDuration.toFixed(1)}s)`}
            </Button>

            {/* Progress bar */}
            {job && (job.status === 'pending' || job.status === 'running') && (
              <div className="mt-3 space-y-2">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-bold text-foreground">{job.message || 'Processing...'}</span>
                  <span className="font-bold text-muted-foreground">{Math.round(job.progress)}%</span>
                </div>
                <div className="h-3 w-full rounded-[var(--border-radius)] border-2 border-border bg-muted overflow-hidden">
                  <div
                    className="h-full bg-primary transition-all duration-300"
                    style={{ width: `${job.progress}%` }}
                  />
                </div>
              </div>
            )}

            {job && job.status === 'complete' && (
              <div className="mt-3 rounded-[var(--border-radius)] border-2 border-border bg-green-50 px-3 py-2">
                <p className="text-xs font-bold text-green-800">
                  Render complete! Video available in renders list below.
                </p>
              </div>
            )}

            {job && job.status === 'error' && (
              <div className="mt-3 rounded-[var(--border-radius)] border-2 border-border bg-red-50 px-3 py-2">
                <p className="text-xs font-bold text-red-800">
                  {job.message || 'Render failed.'}
                </p>
              </div>
            )}
          </div>

          {/* Renders List */}
          <div className="rounded-[var(--border-radius)] border-2 border-border bg-card p-4 shadow-[2px_2px_0_0_var(--border)]">
            <h2 className="text-sm font-heading text-foreground mb-3 flex items-center gap-2">
              <Film className="h-4 w-4" />
              Completed Renders
              {renders.length > 0 && (
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{renders.length}</Badge>
              )}
            </h2>

            {renders.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-4">
                No renders yet. Build a slideshow and hit render.
              </p>
            ) : (
              <div className="space-y-2">
                {renders.map((render) => (
                  <div
                    key={render.name}
                    className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-3"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-bold text-foreground truncate flex-1 mr-2">{render.name}</span>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <a
                          href={apiUrl(`/projects/${encodeURIComponent(activeProjectName)}/videos/slideshow/${encodeURIComponent(render.name)}`)}
                          download
                          className="inline-flex"
                        >
                          <Button variant="outline" size="sm" className="h-7 px-2 text-xs">
                            Download
                          </Button>
                        </a>
                        <Button
                          variant="outline"
                          size="sm"
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
