import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiUrl, staticUrl } from '../lib/api';
import { EmptyState } from '../components';
import { useWorkflowStore } from '../stores/workflowStore';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}:${sec.toFixed(1).padStart(4, '0')}` : `${sec.toFixed(1)}s`;
}

interface ClipInfo {
  index: number;
  name: string;
  start: number;
  duration: number;
  ok: boolean;
  url?: string;
}

interface PastJob {
  job_id: string;
  clip_count: number;
  clips: { name: string; url: string }[];
}

interface StagedFile {
  index: number;
  original_name: string;
  path: string;
  url: string;
  duration: number;
  width: number;
  height: number;
  trimStart: number;
  trimEnd: number;
}

// ── Trim Timeline (mini editor) ─────────────────────────────────────
function TrimTimeline({ file, onChange }: {
  file: StagedFile;
  onChange: (start: number, end: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const draggingRef = useRef<'start' | 'end' | 'playhead' | null>(null);
  const rafRef = useRef<number>(0);

  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(file.trimStart || 0.001);

  const pctStart = (file.trimStart / file.duration) * 100;
  const pctEnd = (file.trimEnd / file.duration) * 100;
  const pctPlayhead = (currentTime / file.duration) * 100;
  const trimDuration = file.trimEnd - file.trimStart;

  // Sync playhead position via rAF for smooth updates
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const tick = () => {
      setCurrentTime(video.currentTime);
      // Loop within trim region
      if (video.currentTime >= file.trimEnd - 0.05) {
        video.currentTime = file.trimStart || 0.001;
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    if (playing) {
      rafRef.current = requestAnimationFrame(tick);
    }
    return () => cancelAnimationFrame(rafRef.current);
  }, [playing, file.trimStart, file.trimEnd]);

  // When trim bounds change while paused, seek to the changed bound
  useEffect(() => {
    const video = videoRef.current;
    if (!video || playing) return;
    // Keep playhead inside trim region
    if (video.currentTime < file.trimStart || video.currentTime > file.trimEnd) {
      video.currentTime = file.trimStart || 0.001;
      setCurrentTime(video.currentTime);
    }
  }, [file.trimStart, file.trimEnd, playing]);

  // Set initial time once video data is loaded
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onLoaded = () => {
      video.currentTime = file.trimStart || 0.001;
      setCurrentTime(file.trimStart || 0.001);
    };
    // If already loaded (cached), seek immediately
    if (video.readyState >= 2) {
      onLoaded();
    } else {
      video.addEventListener('loadeddata', onLoaded, { once: true });
      return () => video.removeEventListener('loadeddata', onLoaded);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const togglePlay = () => {
    const video = videoRef.current;
    if (!video) return;
    if (playing) {
      video.pause();
      setPlaying(false);
    } else {
      // If at or past trim end, restart from trim start
      if (video.currentTime >= file.trimEnd - 0.1 || video.currentTime < file.trimStart) {
        video.currentTime = file.trimStart || 0.001;
      }
      void video.play().catch(() => {});
      setPlaying(true);
    }
  };

  const pctFromPointer = (e: React.PointerEvent): number => {
    if (!trackRef.current) return 0;
    const rect = trackRef.current.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  };

  const handlePointerDown = (handle: 'start' | 'end') => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    draggingRef.current = handle;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    // Pause during trim handle drag
    if (playing && videoRef.current) {
      videoRef.current.pause();
      setPlaying(false);
    }
  };

  const handleTrackPointerDown = (e: React.PointerEvent) => {
    // Click on the track itself (not on a handle) → scrub playhead
    if (draggingRef.current) return;
    const pct = pctFromPointer(e);
    const time = pct * file.duration;
    // Only scrub within trim region
    const clampedTime = Math.max(file.trimStart || 0.001, Math.min(file.trimEnd, time));
    if (videoRef.current) {
      videoRef.current.currentTime = clampedTime;
      setCurrentTime(clampedTime);
    }
    draggingRef.current = 'playhead';
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    // Pause while scrubbing
    if (playing && videoRef.current) {
      videoRef.current.pause();
      setPlaying(false);
    }
  };

  const handlePointerMove = (e: React.PointerEvent) => {
    if (!draggingRef.current || !trackRef.current) return;
    const pct = pctFromPointer(e);
    const time = pct * file.duration;

    if (draggingRef.current === 'start') {
      const newStart = Math.max(0, Math.min(time, file.trimEnd - 0.5));
      const rounded = Math.round(newStart * 10) / 10;
      onChange(rounded, file.trimEnd);
      if (videoRef.current) {
        videoRef.current.currentTime = rounded || 0.001;
        setCurrentTime(rounded || 0.001);
      }
    } else if (draggingRef.current === 'end') {
      const newEnd = Math.min(file.duration, Math.max(time, file.trimStart + 0.5));
      const rounded = Math.round(newEnd * 10) / 10;
      onChange(file.trimStart, rounded);
      if (videoRef.current) {
        videoRef.current.currentTime = rounded;
        setCurrentTime(rounded);
      }
    } else if (draggingRef.current === 'playhead') {
      const clampedTime = Math.max(file.trimStart || 0.001, Math.min(file.trimEnd, time));
      if (videoRef.current) {
        videoRef.current.currentTime = clampedTime;
        setCurrentTime(clampedTime);
      }
    }
  };

  const handlePointerUp = () => { draggingRef.current = null; };

  // Keyboard shortcuts when focused
  const handleKeyDown = (e: React.KeyboardEvent) => {
    const video = videoRef.current;
    if (!video) return;
    if (e.key === ' ' || e.key === 'k') {
      e.preventDefault();
      togglePlay();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      const newTime = Math.max(file.trimStart, video.currentTime - (e.shiftKey ? 5 : 1));
      video.currentTime = newTime;
      setCurrentTime(newTime);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      const newTime = Math.min(file.trimEnd, video.currentTime + (e.shiftKey ? 5 : 1));
      video.currentTime = newTime;
      setCurrentTime(newTime);
    } else if (e.key === 'i') {
      // Set in point at current playhead
      e.preventDefault();
      const rounded = Math.round(video.currentTime * 10) / 10;
      if (rounded < file.trimEnd - 0.5) onChange(rounded, file.trimEnd);
    } else if (e.key === 'o') {
      // Set out point at current playhead
      e.preventDefault();
      const rounded = Math.round(video.currentTime * 10) / 10;
      if (rounded > file.trimStart + 0.5) onChange(file.trimStart, rounded);
    }
  };

  return (
    <div className="space-y-2" tabIndex={0} onKeyDown={handleKeyDown} style={{ outline: 'none' }}>
      {/* Video player */}
      <div
        className="relative aspect-[9/16] bg-black rounded overflow-hidden cursor-pointer group"
        onClick={togglePlay}
      >
        <video
          ref={videoRef}
          src={`${staticUrl(file.url)}#t=0.001`}
          className="w-full h-full object-contain"
          playsInline muted preload="auto"
        />
        {/* Play/pause overlay */}
        {!playing && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/20">
            <div className="w-12 h-12 rounded-full bg-black/60 flex items-center justify-center text-white text-xl">
              &#9654;
            </div>
          </div>
        )}
        {/* Current time overlay */}
        <div className="absolute bottom-2 right-2 bg-black/70 text-white text-[11px] px-1.5 py-0.5 rounded font-mono">
          {fmtTime(currentTime)} / {fmtTime(file.duration)}
        </div>
      </div>

      {/* Controls row */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={togglePlay}
          className="w-7 h-7 flex items-center justify-center rounded bg-primary text-primary-foreground text-sm hover:opacity-80 transition-opacity flex-shrink-0"
        >
          {playing ? '\u23F8' : '\u25B6'}
        </button>

        {/* Timeline track */}
        <div
          ref={trackRef}
          className="relative flex-1 h-10 rounded bg-muted border-2 border-border select-none touch-none cursor-pointer"
          onPointerDown={handleTrackPointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={handlePointerUp}
        >
          {/* Dimmed regions (trimmed out) */}
          <div className="absolute inset-y-0 left-0 bg-black/30 rounded-l z-[1]" style={{ width: `${pctStart}%` }} />
          <div className="absolute inset-y-0 right-0 bg-black/30 rounded-r z-[1]" style={{ width: `${100 - pctEnd}%` }} />

          {/* Active region highlight */}
          <div className="absolute inset-y-0 border-y-2 border-primary/40" style={{ left: `${pctStart}%`, width: `${pctEnd - pctStart}%` }} />

          {/* Playhead — thin vertical line */}
          <div
            className="absolute top-0 bottom-0 w-0.5 bg-white z-[5] shadow-[0_0_3px_rgba(0,0,0,0.8)]"
            style={{ left: `${pctPlayhead}%`, transform: 'translateX(-50%)' }}
          >
            <div className="absolute -top-1 left-1/2 -translate-x-1/2 w-2.5 h-2.5 bg-white rounded-full shadow" />
          </div>

          {/* Start trim handle */}
          <div
            className="absolute top-0 bottom-0 w-4 cursor-ew-resize z-[6] flex items-center justify-center"
            style={{ left: `calc(${pctStart}% - 8px)` }}
            onPointerDown={handlePointerDown('start')}
          >
            <div className="w-1.5 h-6 rounded-sm bg-primary shadow-md border border-primary-foreground/30" />
          </div>

          {/* End trim handle */}
          <div
            className="absolute top-0 bottom-0 w-4 cursor-ew-resize z-[6] flex items-center justify-center"
            style={{ left: `calc(${pctEnd}% - 8px)` }}
            onPointerDown={handlePointerDown('end')}
          >
            <div className="w-1.5 h-6 rounded-sm bg-primary shadow-md border border-primary-foreground/30" />
          </div>
        </div>
      </div>

      {/* Time labels + keyboard hints */}
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>IN {fmtTime(file.trimStart)}</span>
        <span className="font-bold text-foreground">{fmtTime(trimDuration)} selected</span>
        <span>OUT {fmtTime(file.trimEnd)}</span>
      </div>
      <div className="text-[9px] text-muted-foreground text-center opacity-60">
        Space: play/pause &middot; I/O: set in/out &middot; Arrow keys: scrub &plusmn;1s (Shift: &plusmn;5s)
      </div>
    </div>
  );
}

// ── Main Component ──────────────────────────────────────────────────
export function ClipperPage() {
  const { activeProjectName, addNotification, primeBurnSelection } = useWorkflowStore();
  const navigate = useNavigate();

  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [videoUrl, setVideoUrl] = useState('');
  const [downloading, setDownloading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Staging state
  const [stagedFiles, setStagedFiles] = useState<StagedFile[]>([]);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);
  const [trimming, setTrimming] = useState(false);

  // Results
  const [resultClips, setResultClips] = useState<ClipInfo[]>([]);
  const [resultJobId, setResultJobId] = useState<string | null>(null);
  const [pastJobs, setPastJobs] = useState<PastJob[]>([]);

  const fetchPastJobs = useCallback(() => {
    if (!activeProjectName) return;
    fetch(apiUrl(`/api/clipper/jobs?project=${encodeURIComponent(activeProjectName)}`))
      .then((r) => (r.ok ? r.json() : { jobs: [] }))
      .then((data: { jobs: PastJob[] }) => setPastJobs(data.jobs || []))
      .catch(() => {});
  }, [activeProjectName]);

  useEffect(() => { fetchPastJobs(); }, [fetchPastJobs]);

  // ── Upload handler (single or multiple) ─────────────────────────
  const handleUpload = async (files: FileList) => {
    if (!activeProjectName || files.length === 0) return;
    setUploading(true);
    setStagedFiles([]);
    setBatchId(null);
    setResultClips([]);
    setResultJobId(null);
    setExpandedIndex(null);

    try {
      const formData = new FormData();
      for (let i = 0; i < files.length; i++) {
        formData.append('files', files[i]);
      }
      formData.append('project', activeProjectName);

      const resp = await fetch(apiUrl('/api/clipper/upload-batch'), {
        method: 'POST',
        body: formData,
      });
      if (!resp.ok) throw new Error(await resp.text() || `Upload failed (${resp.status})`);

      const data = await resp.json() as {
        batch_id: string;
        files: Array<{
          index: number; original_name: string; path: string; url: string;
          duration: number; width: number; height: number;
        }>;
      };

      setBatchId(data.batch_id);
      setStagedFiles(data.files.map((f) => ({
        ...f,
        trimStart: 0,
        trimEnd: f.duration,
      })));
      // Auto-expand if single file
      if (data.files.length === 1) setExpandedIndex(0);
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  // ── Download URL handler ─────────────────────────────────────────
  const handleDownloadUrl = async () => {
    if (!activeProjectName || !videoUrl.trim()) return;
    setDownloading(true);

    try {
      const resp = await fetch(apiUrl('/api/clipper/download-url'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: activeProjectName, video_url: videoUrl.trim() }),
      });
      if (!resp.ok) throw new Error(await resp.text() || `Download failed (${resp.status})`);

      const data = await resp.json() as {
        batch_id: string;
        files: Array<{
          index: number; original_name: string; path: string; url: string;
          duration: number; width: number; height: number;
        }>;
      };

      // If we already have staged files with a different batch, start fresh
      if (batchId && batchId !== data.batch_id) {
        setStagedFiles([]);
        setExpandedIndex(null);
      }
      setBatchId(data.batch_id);
      const newFiles = data.files.map((f) => ({ ...f, trimStart: 0, trimEnd: f.duration }));
      setStagedFiles((prev) => {
        const next = [...prev, ...newFiles];
        if (next.length === 1) setExpandedIndex(newFiles[0].index);
        return next;
      });
      setVideoUrl('');
      setResultClips([]);
      setResultJobId(null);
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'URL download failed');
    } finally {
      setDownloading(false);
    }
  };

  // ── Trim & process ─────────────────────────────────────────────
  const handleTrimAll = async () => {
    if (!activeProjectName || !batchId || stagedFiles.length === 0) return;
    setTrimming(true);
    setResultClips([]);

    try {
      const resp = await fetch(apiUrl('/api/clipper/trim-batch'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: activeProjectName,
          batch_id: batchId,
          trims: stagedFiles.map((f) => ({
            path: f.path,
            start: f.trimStart,
            end: f.trimEnd,
            original_name: f.original_name,
          })),
        }),
      });
      if (!resp.ok) throw new Error(await resp.text() || 'Trim failed');

      const data = await resp.json() as {
        job_id: string; clips: ClipInfo[]; ok_count: number; total: number;
      };

      setResultJobId(data.job_id);
      setResultClips(data.clips);
      setStagedFiles([]);
      setBatchId(null);
      setExpandedIndex(null);
      addNotification('success', `Trimmed ${data.ok_count}/${data.total} clips`);
      fetchPastJobs();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Trim failed');
    } finally {
      setTrimming(false);
    }
  };

  const updateTrim = useCallback((index: number, start: number, end: number) => {
    setStagedFiles((prev) => prev.map((f) =>
      f.index === index ? { ...f, trimStart: start, trimEnd: end } : f
    ));
  }, []);

  const removeStagedFile = useCallback((index: number) => {
    setStagedFiles((prev) => {
      const next = prev.filter((f) => f.index !== index);
      if (next.length === 0) setBatchId(null);
      return next;
    });
    if (expandedIndex === index) setExpandedIndex(null);
  }, [expandedIndex]);

  const viewPastJob = (job: PastJob) => {
    setResultClips(
      job.clips.map((c, i) => ({ index: i, name: c.name, start: 0, duration: 0, ok: true, url: c.url })),
    );
    setResultJobId(job.job_id);
    setStagedFiles([]);
    setBatchId(null);
    setExpandedIndex(null);
  };

  const deletePastJob = async (jobId: string) => {
    if (!activeProjectName) return;
    try {
      await fetch(apiUrl(`/api/clipper/jobs/${jobId}?project=${encodeURIComponent(activeProjectName)}`), { method: 'DELETE' });
      setPastJobs((prev) => prev.filter((j) => j.job_id !== jobId));
      if (resultJobId === jobId) { setResultClips([]); setResultJobId(null); }
    } catch { addNotification('error', 'Failed to delete job'); }
  };

  const downloadAll = (jobId: string) => {
    if (!activeProjectName) return;
    window.open(apiUrl(`/api/clipper/jobs/${jobId}/download-all?project=${encodeURIComponent(activeProjectName)}`));
  };

  const sendToBurn = (clips: ClipInfo[]) => {
    const videoPaths = clips
      .filter((c) => c.ok && c.url)
      .map((c) => {
        const match = c.url!.match(/\/clips\/(.+)$/);
        return match ? `clips/${match[1]}` : c.url!;
      });
    if (videoPaths.length === 0) {
      addNotification('info', 'No clips available.');
      return;
    }
    primeBurnSelection({ videoPaths });
    navigate('/burn');
  };

  const hasStaged = stagedFiles.length > 0;
  const hasResults = resultClips.length > 0;

  if (!activeProjectName) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState icon="&#128193;" title="No Project Selected" description="Please select or create a project to start clipping videos." />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden lg:flex-row">
      {/* ── Sidebar ── */}
      <div className="w-full flex-shrink-0 overflow-y-auto border-r-2 border-border bg-card p-6 lg:w-[380px]">
        <h2 className="mb-1 text-xl font-heading text-foreground">Clipper</h2>
        <p className="mb-6 text-xs text-muted-foreground">
          Upload clips, trim the start &amp; end, then process into 9:16 UGC clips
        </p>

        {/* Drop zone — always visible, accepts 1 or many */}
        <Label>Upload Videos</Label>
        <div
          className={`relative mt-1 border-2 border-dashed rounded-[var(--border-radius)] p-4 text-center cursor-pointer transition-colors ${
            dragOver
              ? 'border-primary bg-primary/5'
              : hasStaged
                ? 'border-primary bg-primary/10'
                : 'border-border hover:border-muted-foreground hover:bg-muted'
          }`}
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            if (e.dataTransfer.files.length) void handleUpload(e.dataTransfer.files);
          }}
        >
          <input
            type="file"
            ref={fileInputRef}
            onChange={(e) => { if (e.target.files?.length) void handleUpload(e.target.files); }}
            className="hidden"
            accept="video/*"
            multiple
            disabled={uploading || trimming}
          />
          {uploading ? (
            <div className="flex items-center justify-center gap-2 py-2">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted border-t-primary" />
              <span className="text-sm text-muted-foreground">Uploading & analyzing...</span>
            </div>
          ) : (
            <>
              <div className="text-lg mb-0.5">&#8679;</div>
              <div className="text-muted-foreground text-sm">
                Drop video{hasStaged ? 's to add more' : '(s)'} or <strong className="text-foreground">click to browse</strong>
              </div>
              <div className="text-[11px] text-muted-foreground mt-1">
                Single or multiple files &middot; MP4, MOV, MKV, WEBM
              </div>
            </>
          )}
        </div>

        {/* URL input */}
        <div className="mt-3">
          <Label>Or paste a video link</Label>
          <div className="mt-1 flex gap-2">
            <Input
              type="url"
              placeholder="https://www.tiktok.com/..."
              value={videoUrl}
              onChange={(e) => setVideoUrl(e.target.value)}
              disabled={downloading || trimming}
              onKeyDown={(e) => { if (e.key === 'Enter') void handleDownloadUrl(); }}
              className="flex-1 text-sm"
            />
            <Button
              size="sm"
              onClick={() => void handleDownloadUrl()}
              disabled={downloading || !videoUrl.trim() || trimming}
            >
              {downloading ? (
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted border-t-primary" />
              ) : 'Add'}
            </Button>
          </div>
          <p className="text-[10px] text-muted-foreground mt-1">TikTok, YouTube, Instagram, etc.</p>
        </div>

        {hasStaged && (
          <div className="mt-3 space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{stagedFiles.length} clip{stagedFiles.length !== 1 ? 's' : ''} staged</span>
              <button type="button" onClick={() => { setStagedFiles([]); setBatchId(null); setExpandedIndex(null); }}
                className="text-[11px] text-muted-foreground hover:text-destructive font-bold"
              >Clear all</button>
            </div>
            <Button onClick={() => void handleTrimAll()} disabled={trimming} className="w-full">
              {trimming ? 'Processing...' : `Trim & Process (${stagedFiles.length})`}
            </Button>
          </div>
        )}

        {/* Past jobs */}
        <div className="mt-6 border-t-2 border-border pt-4">
          <span className="text-sm font-bold text-foreground">
            Previous Jobs
            {pastJobs.length > 0 && (
              <span className="ml-1.5 text-[11px] text-muted-foreground font-normal">({pastJobs.length})</span>
            )}
          </span>
          <div className="mt-3 space-y-1.5">
            {pastJobs.length === 0 ? (
              <p className="text-xs text-muted-foreground italic">No previous jobs.</p>
            ) : (
              pastJobs.map((job) => (
                <div
                  key={job.job_id}
                  className="group flex items-center gap-2 rounded-[var(--border-radius)] border-2 border-border bg-card px-3 py-2 hover:bg-muted hover:shadow-[2px_2px_0_0_var(--border)] transition-all"
                >
                  <button type="button" onClick={() => viewPastJob(job)}
                    className="flex-1 text-left text-xs text-foreground group-hover:text-primary truncate"
                  >{job.job_id.slice(0, 8)}... ({job.clip_count} clips)</button>
                  <button type="button" onClick={() => downloadAll(job.job_id)}
                    className="text-[10px] text-muted-foreground hover:text-primary transition-colors font-bold opacity-0 group-hover:opacity-100"
                  >ZIP</button>
                  <button type="button" onClick={() => void deletePastJob(job.job_id)}
                    className="w-5 h-5 flex items-center justify-center rounded-full text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors text-xs opacity-0 group-hover:opacity-100"
                  >x</button>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* ── Right panel ── */}
      <div className="flex-1 overflow-y-auto p-6 bg-background">
        <div className="mx-auto max-w-5xl">

          {/* ── Staging: clip cards with expand-to-trim ── */}
          {hasStaged && (
            <>
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-xl font-heading text-foreground">
                  Stage &amp; Trim
                  <span className="ml-2 text-sm text-muted-foreground font-normal">{stagedFiles.length} clips</span>
                </h2>
                <Button size="sm" onClick={() => void handleTrimAll()} disabled={trimming}>
                  {trimming ? 'Processing...' : 'Trim & Process All'}
                </Button>
              </div>
              <p className="mb-4 text-xs text-muted-foreground">
                Click a clip to expand the trim tool. Drag handles to cut the start/end. Hover to preview.
              </p>

              <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
                {stagedFiles.map((file) => {
                  const isExpanded = expandedIndex === file.index;
                  const trimmed = file.trimStart > 0.05 || file.trimEnd < file.duration - 0.05;

                  return (
                    <div
                      key={file.index}
                      className={`rounded-[var(--border-radius)] overflow-hidden border-2 bg-card transition-all ${
                        isExpanded
                          ? 'border-primary shadow-[4px_4px_0_0_var(--primary)] col-span-1 row-span-1'
                          : trimmed
                            ? 'border-green-700 shadow-[2px_2px_0_0_var(--green-700,#15803d)] hover:translate-x-[1px] hover:translate-y-[1px]'
                            : 'border-border shadow-[2px_2px_0_0_var(--border)] hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[1px_1px_0_0_var(--border)]'
                      }`}
                    >
                      {/* Header */}
                      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
                        <button
                          type="button"
                          onClick={() => setExpandedIndex(isExpanded ? null : file.index)}
                          className="flex-1 text-left text-xs font-bold text-foreground truncate hover:text-primary transition-colors"
                          title={file.original_name}
                        >
                          {file.original_name}
                        </button>
                        <div className="flex items-center gap-1.5 ml-2">
                          {trimmed && (
                            <Badge variant="success" className="text-[9px] shadow-none px-1.5">trimmed</Badge>
                          )}
                          <span className="text-[10px] text-muted-foreground">{fmtTime(file.trimEnd - file.trimStart)}</span>
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); removeStagedFile(file.index); }}
                            className="w-4 h-4 flex items-center justify-center rounded-full text-muted-foreground hover:text-destructive text-[10px]"
                          >x</button>
                        </div>
                      </div>

                      {isExpanded ? (
                        /* Expanded: full trim UI */
                        <div className="p-3">
                          <TrimTimeline
                            file={file}
                            onChange={(start, end) => updateTrim(file.index, start, end)}
                          />
                          <div className="mt-2 text-[10px] text-muted-foreground text-center">
                            {file.width}x{file.height} &middot; {fmtTime(file.duration)} total
                          </div>
                        </div>
                      ) : (
                        /* Collapsed: thumbnail preview */
                        <button
                          type="button"
                          className="block w-full"
                          onClick={() => setExpandedIndex(file.index)}
                        >
                          <div className="relative aspect-[9/16] bg-muted">
                            <video
                              src={`${staticUrl(file.url)}#t=0.001`}
                              className="w-full h-full object-cover"
                              playsInline muted preload="metadata"
                              onLoadedData={(e) => { e.currentTarget.currentTime = 0.001; }}
                              onMouseEnter={(e) => { void e.currentTarget.play().catch(() => {}); }}
                              onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0.001; }}
                            />
                            {/* Trim indicator overlay */}
                            {trimmed && (
                              <div className="absolute bottom-0 inset-x-0 h-1.5 bg-muted/80">
                                <div
                                  className="absolute inset-y-0 bg-primary/70"
                                  style={{
                                    left: `${(file.trimStart / file.duration) * 100}%`,
                                    width: `${((file.trimEnd - file.trimStart) / file.duration) * 100}%`,
                                  }}
                                />
                              </div>
                            )}
                          </div>
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {/* ── Results ── */}
          {hasResults && (
            <>
              <div className={`flex items-center justify-between ${hasStaged ? 'mt-8' : ''} mb-6`}>
                <h2 className="text-xl font-heading text-foreground">
                  Processed Clips
                  <span className="ml-2 text-sm text-muted-foreground font-normal">
                    {resultClips.filter((c) => c.ok).length}/{resultClips.length}
                  </span>
                </h2>
                <div className="flex gap-2">
                  <Button variant="secondary" size="sm" onClick={() => sendToBurn(resultClips)}>
                    Use in Burn
                  </Button>
                  {resultJobId && (
                    <Button variant="outline" size="sm" onClick={() => downloadAll(resultJobId)}>
                      Download ZIP
                    </Button>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
                {resultClips.filter((c) => c.ok && c.url).map((clip) => (
                  <div
                    key={clip.index}
                    className="rounded-[var(--border-radius)] overflow-hidden border-2 border-border bg-card shadow-[2px_2px_0_0_var(--border)] hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[1px_1px_0_0_var(--border)] transition-all"
                  >
                    <div className="relative bg-muted aspect-[9/16]">
                      <video
                        src={`${staticUrl(clip.url!)}#t=0.001`}
                        className="w-full h-full object-cover"
                        playsInline muted loop preload="metadata"
                        onLoadedData={(e) => { e.currentTarget.currentTime = 0.001; }}
                        onMouseEnter={(e) => { void e.currentTarget.play().catch(() => {}); }}
                        onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0.001; }}
                      />
                    </div>
                    <div className="px-2.5 py-1.5 flex items-center justify-between text-xs">
                      <Badge variant="success" className="text-[10px] shadow-none">#{clip.index + 1}</Badge>
                      <div className="flex items-center gap-2">
                        {clip.duration > 0 && (
                          <span className="text-muted-foreground text-[11px]">{clip.duration.toFixed(1)}s</span>
                        )}
                        <a href={staticUrl(clip.url!)} download={clip.name}
                          className="text-primary hover:underline font-bold text-xs"
                        >Download</a>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {/* Empty state */}
          {!hasStaged && !hasResults && (
            <EmptyState
              icon="&#9986;"
              title="No Clips Yet"
              description="Drop video files (single or multiple) to upload, trim, and process them into 9:16 clips."
            />
          )}

          {/* Trimming progress */}
          {trimming && (
            <div className="mt-6 flex items-center gap-3 text-sm text-muted-foreground">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted border-t-primary" />
              Trimming & processing {stagedFiles.length} clips...
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
