import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FolderOpenIcon, ScissorsIcon, PlayIcon, UploadSimpleIcon } from '@phosphor-icons/react';
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

// ── Types ────────────────────────────────────────────────────────────

interface StagedFile {
  index: number;
  original_name: string;
  path: string;
  url: string;
  thumbUrl: string;
  duration: number;
  width: number;
  height: number;
  trimStart: number;
  trimEnd: number;
}

interface ClipInfo {
  index: number;
  name: string;
  source_name?: string;
  start: number;
  duration: number;
  ok: boolean;
  url?: string;
  thumbUrl?: string;
}

interface PastJob {
  job_id: string;
  label?: string | null;
  clip_count: number;
  clips: { name: string; url: string; thumb_url?: string | null }[];
}

type ClipperStage = 'ingest' | 'trim' | 'configure' | 'processing' | 'results';

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

  // rAF playhead sync
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const tick = () => {
      setCurrentTime(video.currentTime);
      if (video.currentTime >= file.trimEnd - 0.05) {
        video.currentTime = file.trimStart || 0.001;
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    if (playing) rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [playing, file.trimStart, file.trimEnd]);

  // Keep playhead in trim region when bounds change
  useEffect(() => {
    const video = videoRef.current;
    if (!video || playing) return;
    if (video.currentTime < file.trimStart || video.currentTime > file.trimEnd) {
      video.currentTime = file.trimStart || 0.001;
      setCurrentTime(video.currentTime);
    }
  }, [file.trimStart, file.trimEnd, playing]);

  // Initial seek once video data loads
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onLoaded = () => {
      video.currentTime = file.trimStart || 0.001;
      setCurrentTime(file.trimStart || 0.001);
    };
    if (video.readyState >= 2) onLoaded();
    else {
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
    if (playing && videoRef.current) { videoRef.current.pause(); setPlaying(false); }
  };

  const handleTrackPointerDown = (e: React.PointerEvent) => {
    if (draggingRef.current) return;
    const pct = pctFromPointer(e);
    const time = pct * file.duration;
    const clampedTime = Math.max(file.trimStart || 0.001, Math.min(file.trimEnd, time));
    if (videoRef.current) { videoRef.current.currentTime = clampedTime; setCurrentTime(clampedTime); }
    draggingRef.current = 'playhead';
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    if (playing && videoRef.current) { videoRef.current.pause(); setPlaying(false); }
  };

  const handlePointerMove = (e: React.PointerEvent) => {
    if (!draggingRef.current || !trackRef.current) return;
    const pct = pctFromPointer(e);
    const time = pct * file.duration;

    if (draggingRef.current === 'start') {
      const newStart = Math.max(0, Math.min(time, file.trimEnd - 0.5));
      const rounded = Math.round(newStart * 10) / 10;
      onChange(rounded, file.trimEnd);
      if (videoRef.current) { videoRef.current.currentTime = rounded || 0.001; setCurrentTime(rounded || 0.001); }
    } else if (draggingRef.current === 'end') {
      const newEnd = Math.min(file.duration, Math.max(time, file.trimStart + 0.5));
      const rounded = Math.round(newEnd * 10) / 10;
      onChange(file.trimStart, rounded);
      if (videoRef.current) { videoRef.current.currentTime = rounded; setCurrentTime(rounded); }
    } else if (draggingRef.current === 'playhead') {
      const clampedTime = Math.max(file.trimStart || 0.001, Math.min(file.trimEnd, time));
      if (videoRef.current) { videoRef.current.currentTime = clampedTime; setCurrentTime(clampedTime); }
    }
  };

  const handlePointerUp = () => { draggingRef.current = null; };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    const video = videoRef.current;
    if (!video) return;
    if (e.key === ' ' || e.key === 'k') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      const t = Math.max(file.trimStart, video.currentTime - (e.shiftKey ? 5 : 1));
      video.currentTime = t; setCurrentTime(t);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      const t = Math.min(file.trimEnd, video.currentTime + (e.shiftKey ? 5 : 1));
      video.currentTime = t; setCurrentTime(t);
    } else if (e.key === 'i') {
      e.preventDefault();
      const r = Math.round(video.currentTime * 10) / 10;
      if (r < file.trimEnd - 0.5) onChange(r, file.trimEnd);
    } else if (e.key === 'o') {
      e.preventDefault();
      const r = Math.round(video.currentTime * 10) / 10;
      if (r > file.trimStart + 0.5) onChange(file.trimStart, r);
    }
  };

  return (
    <div className="space-y-2" tabIndex={0} onKeyDown={handleKeyDown} style={{ outline: 'none' }}>
      {/* Video player */}
      <div className="relative aspect-[9/16] bg-black rounded overflow-hidden cursor-pointer" onClick={togglePlay}>
        <video
          ref={videoRef}
          src={`${staticUrl(file.url)}#t=0.001`}
          poster={staticUrl(file.thumbUrl)}
          className="w-full h-full object-contain"
          playsInline muted preload="auto"
        />
        {!playing && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/20">
            <div className="w-12 h-12 rounded-full bg-black/60 flex items-center justify-center text-white"><PlayIcon size={24} weight="fill" /></div>
          </div>
        )}
        <div className="absolute bottom-2 right-2 bg-black/70 text-white text-[11px] px-1.5 py-0.5 rounded font-mono">
          {fmtTime(currentTime)} / {fmtTime(file.duration)}
        </div>
      </div>

      {/* Controls row */}
      <div className="flex items-center gap-2">
        <button type="button" onClick={togglePlay}
          className="w-7 h-7 flex items-center justify-center rounded bg-primary text-primary-foreground text-sm hover:opacity-80 transition-opacity flex-shrink-0">
          {playing ? '\u23F8' : '\u25B6'}
        </button>
        <div ref={trackRef}
          className="relative flex-1 h-10 rounded bg-muted border border-border select-none touch-none cursor-pointer"
          onPointerDown={handleTrackPointerDown} onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp} onPointerLeave={handlePointerUp}>
          <div className="absolute inset-y-0 left-0 bg-black/30 rounded-l z-[1]" style={{ width: `${pctStart}%` }} />
          <div className="absolute inset-y-0 right-0 bg-black/30 rounded-r z-[1]" style={{ width: `${100 - pctEnd}%` }} />
          <div className="absolute inset-y-0 border-y-2 border-primary/40" style={{ left: `${pctStart}%`, width: `${pctEnd - pctStart}%` }} />
          <div className="absolute top-0 bottom-0 w-0.5 bg-white z-[5] shadow-[0_0_3px_rgba(0,0,0,0.8)]"
            style={{ left: `${pctPlayhead}%`, transform: 'translateX(-50%)' }}>
            <div className="absolute -top-1 left-1/2 -translate-x-1/2 w-2.5 h-2.5 bg-white rounded-full shadow" />
          </div>
          <div className="absolute top-0 bottom-0 w-4 cursor-ew-resize z-[6] flex items-center justify-center"
            style={{ left: `calc(${pctStart}% - 8px)` }} onPointerDown={handlePointerDown('start')}>
            <div className="w-1.5 h-6 rounded-sm bg-primary shadow-md border border-primary-foreground/30" />
          </div>
          <div className="absolute top-0 bottom-0 w-4 cursor-ew-resize z-[6] flex items-center justify-center"
            style={{ left: `calc(${pctEnd}% - 8px)` }} onPointerDown={handlePointerDown('end')}>
            <div className="w-1.5 h-6 rounded-sm bg-primary shadow-md border border-primary-foreground/30" />
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>IN {fmtTime(file.trimStart)}</span>
        <span className="font-bold text-foreground">{fmtTime(trimDuration)} selected</span>
        <span>OUT {fmtTime(file.trimEnd)}</span>
      </div>
      <div className="text-[9px] text-muted-foreground text-center opacity-60">
        Space: play/pause &middot; I/O: set in/out &middot; Arrow keys: &plusmn;1s (Shift: &plusmn;5s)
      </div>
    </div>
  );
}

// ── Configure Panel ─────────────────────────────────────────────────

function ConfigurePanel({ stagedFiles, clipLength, setClipLength, onProcess, onBack }: {
  stagedFiles: StagedFile[];
  clipLength: number;
  setClipLength: (v: number) => void;
  onProcess: () => void;
  onBack: () => void;
}) {
  const breakdown = stagedFiles.map((f) => {
    const trimmed = f.trimEnd - f.trimStart;
    const clips = Math.max(1, Math.floor(trimmed / clipLength));
    return { name: f.original_name, trimmed, clips };
  });
  const totalTime = breakdown.reduce((a, b) => a + b.trimmed, 0);
  const totalClips = breakdown.reduce((a, b) => a + b.clips, 0);

  return (
    <div className="max-w-xl mx-auto">
      <h2 className="text-xl font-heading text-foreground mb-4">Configure Output</h2>
      <p className="text-sm text-muted-foreground mb-6">
        Set how long each output clip should be. Your trimmed sources will be split into clips of this length.
      </p>

      {/* Clip length selector */}
      <div className="mb-6">
        <Label>Output clip length (seconds)</Label>
        <div className="mt-2 flex items-center gap-3">
          <input
            type="range" min={3} max={30} step={1} value={clipLength}
            onChange={(e) => setClipLength(Number(e.target.value))}
            className="flex-1 accent-[var(--primary)]"
          />
          <div className="w-16 text-center">
            <Input
              type="number" min={1} max={60} step={1} value={clipLength}
              onChange={(e) => setClipLength(Math.max(1, Math.min(60, Number(e.target.value))))}
              className="text-center text-sm font-bold"
            />
          </div>
        </div>
      </div>

      {/* Breakdown table */}
      <div className="border border-border rounded-[var(--border-radius)] overflow-hidden mb-6">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-muted border-b-2 border-border">
              <th className="text-left px-3 py-2 font-bold text-foreground">Source</th>
              <th className="text-right px-3 py-2 font-bold text-foreground">Trimmed</th>
              <th className="text-right px-3 py-2 font-bold text-foreground">Clips</th>
            </tr>
          </thead>
          <tbody>
            {breakdown.map((row, i) => (
              <tr key={i} className="border-b border-border last:border-0">
                <td className="px-3 py-2 text-foreground truncate max-w-[200px]">{row.name}</td>
                <td className="px-3 py-2 text-right text-muted-foreground">{fmtTime(row.trimmed)}</td>
                <td className="px-3 py-2 text-right font-bold text-foreground">{row.clips}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="bg-muted border-t-2 border-border">
              <td className="px-3 py-2 font-bold text-foreground">Total</td>
              <td className="px-3 py-2 text-right font-bold text-foreground">{fmtTime(totalTime)}</td>
              <td className="px-3 py-2 text-right font-bold text-primary text-lg">{totalClips}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      <p className="text-xs text-muted-foreground mb-4 text-center">
        {totalClips} clip{totalClips !== 1 ? 's' : ''} at {clipLength}s each from {fmtTime(totalTime)} of source video
      </p>

      <div className="flex gap-3">
        <Button variant="outline" onClick={onBack} className="flex-1">Back to Trim</Button>
        <Button onClick={onProcess} className="flex-1">
          Process {totalClips} Clips
        </Button>
      </div>
    </div>
  );
}

// ── Main Component ──────────────────────────────────────────────────

export function ClipperPage() {
  const { activeProjectName, addNotification, primeBurnSelection } = useWorkflowStore();
  const navigate = useNavigate();

  // Restore persisted staging state
  const storageKey = activeProjectName ? `clipper:staging:${activeProjectName}` : null;
  const restored = useRef(false);
  const initial = (() => {
    if (restored.current || !storageKey) return null;
    restored.current = true;
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) return JSON.parse(raw) as { stagedFiles: StagedFile[]; batchId: string | null; stage: ClipperStage; clipLength: number };
    } catch { /* ignore */ }
    return null;
  })();

  // Pipeline stage
  const [stage, setStage] = useState<ClipperStage>(initial?.stage && initial.stage !== 'processing' ? initial.stage : 'ingest');

  // Ingest
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [videoUrl, setVideoUrl] = useState('');
  const [downloading, setDownloading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Staging
  const [stagedFiles, setStagedFiles] = useState<StagedFile[]>(initial?.stagedFiles ?? []);
  const [batchId, setBatchId] = useState<string | null>(initial?.batchId ?? null);
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  // Configure
  const [clipLength, setClipLength] = useState(initial?.clipLength ?? 7);

  // Persist staging state to localStorage
  useEffect(() => {
    if (!storageKey) return;
    if (stagedFiles.length === 0 && stage === 'ingest') {
      localStorage.removeItem(storageKey);
      return;
    }
    // Only persist during ingest/trim/configure — not processing/results
    if (stage === 'processing' || stage === 'results') return;
    localStorage.setItem(storageKey, JSON.stringify({ stagedFiles, batchId, stage, clipLength }));
  }, [storageKey, stagedFiles, batchId, stage, clipLength]);

  // Processing / Results
  const [processing, setProcessing] = useState(false);
  const [processProgress, setProcessProgress] = useState<{ clip: number; total: number; source: string } | null>(null);
  const [resultClips, setResultClips] = useState<ClipInfo[]>([]);
  const [resultJobId, setResultJobId] = useState<string | null>(null);
  const [pastJobs, setPastJobs] = useState<PastJob[]>([]);
  const [renamingJobId, setRenamingJobId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  const fetchPastJobs = useCallback(() => {
    if (!activeProjectName) return;
    fetch(apiUrl(`/api/clipper/jobs?project=${encodeURIComponent(activeProjectName)}`))
      .then((r) => (r.ok ? r.json() : { jobs: [] }))
      .then((data: { jobs: PastJob[] }) => setPastJobs(data.jobs || []))
      .catch(() => {});
  }, [activeProjectName]);

  useEffect(() => { fetchPastJobs(); }, [fetchPastJobs]);

  const handleRenameJob = useCallback(async (jobId: string, label: string) => {
    if (!activeProjectName || !label.trim()) return;
    try {
      const r = await fetch(apiUrl(`/api/clipper/jobs/${encodeURIComponent(jobId)}/rename?project=${encodeURIComponent(activeProjectName)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label.trim() }),
      });
      if (!r.ok) throw new Error('Rename failed');
      setRenamingJobId(null);
      fetchPastJobs();
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Rename failed');
    }
  }, [activeProjectName, fetchPastJobs, addNotification]);

  // Derived
  const hasStaged = stagedFiles.length > 0;
  const hasResults = resultClips.length > 0;
  const isBusy = uploading || downloading || processing;

  // ── Upload handler ─────────────────────────────────────────────
  // Streams each file as raw bytes to /api/clipper/stage-streamed, one at a time,
  // sharing a batch_id so they land in the same staging dir. Avoids multipart
  // encoding which Railway's proxy drops on large (>~500MB) uploads.
  const handleUpload = async (files: FileList) => {
    if (!activeProjectName || files.length === 0) return;
    setUploading(true);
    setUploadProgress(0);

    let totalSize = 0;
    for (let i = 0; i < files.length; i++) totalSize += files[i].size;
    const totalGB = totalSize / (1024 * 1024 * 1024);
    if (totalGB > 10) {
      addNotification('error', `Total file size (${totalGB.toFixed(1)}GB) exceeds 10GB limit.`);
      setUploading(false);
      return;
    }

    let batch: string | null = null;
    const staged: StagedFile[] = [];
    let bytesDoneBefore = 0;
    const failures: string[] = [];

    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const params = new URLSearchParams({
          project: activeProjectName,
          filename: file.name,
          index: String(i),
          ...(batch ? { batch_id: batch } : {}),
        });
        const url = apiUrl(`/api/clipper/stage-streamed?${params.toString()}`);

        try {
          const resp = await new Promise<{
            batch_id: string;
            file: {
              index: number; original_name: string; path: string; url: string; thumb_url: string;
              duration: number; width: number; height: number;
            };
          }>((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.setRequestHeader('Content-Type', 'application/octet-stream');

            xhr.upload.onprogress = (e) => {
              if (e.lengthComputable) {
                const overall = bytesDoneBefore + e.loaded;
                setUploadProgress(Math.round((overall / totalSize) * 100));
              }
            };

            xhr.onload = () => {
              if (xhr.status >= 200 && xhr.status < 300) {
                try { resolve(JSON.parse(xhr.responseText)); }
                catch { reject(new Error('Invalid response from server')); }
              } else {
                let msg = xhr.responseText || `Upload failed (${xhr.status})`;
                try {
                  const parsed = JSON.parse(xhr.responseText);
                  if (parsed?.detail) msg = parsed.detail;
                } catch { /* keep raw */ }
                reject(new Error(msg));
              }
            };
            xhr.onerror = () => reject(new Error(`Upload failed — connection error (${file.name})`));
            xhr.ontimeout = () => reject(new Error(`Upload timed out (${file.name})`));
            xhr.timeout = 0;
            xhr.send(file);
          });

          batch = resp.batch_id;
          staged.push({
            ...resp.file,
            thumbUrl: resp.file.thumb_url,
            trimStart: 0,
            trimEnd: resp.file.duration,
          });
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          failures.push(`${file.name}: ${msg}`);
          addNotification('error', `${file.name}: ${msg}`);
        } finally {
          bytesDoneBefore += file.size;
          setUploadProgress(Math.round((bytesDoneBefore / totalSize) * 100));
        }
      }

      if (staged.length === 0) {
        if (failures.length === 0) addNotification('error', 'No files were staged');
        return;
      }

      setBatchId(batch!);
      setStagedFiles((prev) => [...prev, ...staged]);
      if (stage === 'ingest' || stage === 'trim') setStage('trim');
      setExpandedIndex(staged[0].index);
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
      setUploadProgress(0);
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
          index: number; original_name: string; path: string; url: string; thumb_url: string;
          duration: number; width: number; height: number;
        }>;
      };

      const badFiles = data.files.filter((f) => !f.duration || f.duration <= 0);
      if (badFiles.length) {
        addNotification('error', `Downloaded video has no readable duration — likely corrupt or not a video`);
        return;
      }

      setBatchId(data.batch_id);
      const newFiles: StagedFile[] = data.files.map((f) => ({
        ...f, thumbUrl: f.thumb_url, trimStart: 0, trimEnd: f.duration,
      }));
      setStagedFiles((prev) => {
        const next = [...prev, ...newFiles];
        return next;
      });
      if (newFiles.length > 0) setExpandedIndex(newFiles[0].index);
      setVideoUrl('');
      if (stage === 'ingest' || stage === 'trim') setStage('trim');
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'URL download failed');
    } finally {
      setDownloading(false);
    }
  };

  // ── Process all (background job + polling) ──────────────────────────
  const handleProcessAll = async () => {
    if (!activeProjectName || !batchId || stagedFiles.length === 0) return;
    setStage('processing');
    setProcessing(true);
    setResultClips([]);
    setProcessProgress(null);

    try {
      // Start the job — returns immediately with job_id
      const startResp = await fetch(apiUrl('/api/clipper/process-batch'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: activeProjectName,
          batch_id: batchId,
          clip_length: clipLength,
          sources: stagedFiles.map((f) => ({
            path: f.path,
            trim_start: f.trimStart,
            trim_end: f.trimEnd,
            original_name: f.original_name,
          })),
        }),
      });
      if (!startResp.ok) throw new Error(await startResp.text() || 'Processing failed');

      const { job_id, total_clips } = await startResp.json();
      setResultJobId(job_id);
      setProcessProgress({ clip: 0, total: total_clips, source: '' });

      // Poll for progress until complete or error
      let done = false;
      while (!done) {
        await new Promise((r) => setTimeout(r, 1500));

        const pollResp = await fetch(apiUrl(`/api/clipper/process-batch/${encodeURIComponent(job_id)}`));
        if (!pollResp.ok) throw new Error('Failed to poll job status');

        const job = await pollResp.json();

        // Update progress
        setProcessProgress({ clip: job.clip, total: job.total, source: job.source || '' });

        // Update clips as they complete
        if (job.clips && job.clips.length > 0) {
          setResultClips(
            job.clips
              .filter((c: { ok: boolean }) => c.ok)
              .map((c: { thumb_url?: string }) => ({ ...c, thumbUrl: c.thumb_url })),
          );
        }

        if (job.status === 'complete') {
          setResultClips(
            (job.clips || [])
              .filter((c: { ok: boolean }) => c.ok)
              .map((c: { thumb_url?: string }) => ({ ...c, thumbUrl: c.thumb_url })),
          );
          setStage('results');
          addNotification('success', `Processed ${job.ok_count}/${job.total} clips`);
          fetchPastJobs();
          done = true;
        } else if (job.status === 'error') {
          throw new Error(job.error || 'Processing failed');
        }
      }
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Processing failed');
      setStage('configure');
    } finally {
      setProcessing(false);
      setProcessProgress(null);
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
      if (next.length === 0) { setBatchId(null); setStage('ingest'); }
      return next;
    });
    if (expandedIndex === index) setExpandedIndex(null);
  }, [expandedIndex]);

  const startNewBatch = () => {
    setStagedFiles([]);
    setBatchId(null);
    setExpandedIndex(null);
    setResultClips([]);
    setResultJobId(null);
    setStage('ingest');
    if (storageKey) localStorage.removeItem(storageKey);
  };

  const viewPastJob = (job: PastJob) => {
    setResultClips(
      job.clips.map((c, i) => ({
        index: i, name: c.name, start: 0, duration: 0, ok: true,
        url: c.url, thumbUrl: c.thumb_url ?? undefined,
      })),
    );
    setResultJobId(job.job_id);
    setStagedFiles([]);
    setBatchId(null);
    setExpandedIndex(null);
    setStage('results');
  };

  const deletePastJob = async (jobId: string) => {
    if (!activeProjectName) return;
    try {
      await fetch(apiUrl(`/api/clipper/jobs/${jobId}?project=${encodeURIComponent(activeProjectName)}`), { method: 'DELETE' });
      setPastJobs((prev) => prev.filter((j) => j.job_id !== jobId));
      if (resultJobId === jobId) { setResultClips([]); setResultJobId(null); setStage('ingest'); }
    } catch { addNotification('error', 'Failed to delete job'); }
  };

  const downloadAll = async (jobId: string) => {
    if (!activeProjectName) return;
    try {
      const url = apiUrl(`/api/clipper/jobs/${jobId}/download-all?project=${encodeURIComponent(activeProjectName)}`);
      const resp = await fetch(url);
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Download failed (${resp.status})`);
      }
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = `clips_${jobId.slice(0, 8)}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(blobUrl);
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'ZIP download failed');
    }
  };

  const sendToBurn = (clips: ClipInfo[]) => {
    const videoPaths = clips
      .filter((c) => c.ok && c.url)
      .map((c) => { const m = c.url!.match(/\/clips\/(.+)$/); return m ? `clips/${m[1]}` : c.url!; });
    if (videoPaths.length === 0) { addNotification('info', 'No clips available.'); return; }
    primeBurnSelection({ videoPaths });
    navigate('/burn');
  };

  if (!activeProjectName) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState icon={<FolderOpenIcon size={48} weight="duotone" />} title="No Project Selected" description="Select or create a project to start clipping." />
      </div>
    );
  }

  // ── Stage steps indicator ─────────────────────────────────────────
  const stages: { key: ClipperStage; label: string }[] = [
    { key: 'ingest', label: '1. Upload' },
    { key: 'trim', label: '2. Trim' },
    { key: 'configure', label: '3. Configure' },
    { key: 'results', label: '4. Results' },
  ];

  return (
    <div className="flex h-full flex-col overflow-hidden lg:flex-row">
      {/* ── Sidebar ── */}
      <div className="w-full flex-shrink-0 overflow-y-auto border-r-2 border-border bg-card p-6 lg:w-[380px]">
        <h2 className="mb-1 text-xl font-heading text-foreground">Clipper</h2>
        <p className="mb-4 text-xs text-muted-foreground">
          Upload &rarr; Trim &rarr; Configure clip length &rarr; Process into 9:16 clips
        </p>

        {/* Step indicator */}
        <div className="flex items-center gap-1 mb-5">
          {stages.map((s) => (
            <div key={s.key} className={`flex-1 text-center text-[10px] py-1 rounded-sm font-bold transition-colors ${
              s.key === stage || (s.key === 'results' && stage === 'processing')
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground'
            }`}>{s.label}</div>
          ))}
        </div>

        {/* Drop zone — always visible during ingest/trim */}
        {(stage === 'ingest' || stage === 'trim') && (
          <>
            <Label>Upload Videos</Label>
            <div
              className={`relative mt-1 border border-dashed rounded-[var(--border-radius)] p-4 text-center cursor-pointer transition-colors ${
                dragOver ? 'border-primary bg-primary/5'
                  : hasStaged ? 'border-primary bg-primary/10'
                    : 'border-border hover:border-muted-foreground hover:bg-muted'
              }`}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); if (e.dataTransfer.files.length) void handleUpload(e.dataTransfer.files); }}
            >
              <input type="file" ref={fileInputRef}
                onChange={(e) => { if (e.target.files?.length) void handleUpload(e.target.files); }}
                className="hidden" accept="video/*" multiple disabled={isBusy}
              />
              {uploading ? (
                <div className="flex flex-col items-center gap-2 py-2">
                  <div className="flex items-center gap-2">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted border-t-primary" />
                    <span className="text-sm text-muted-foreground">
                      {uploadProgress > 0 && uploadProgress < 100
                        ? `Uploading... ${uploadProgress}%`
                        : uploadProgress >= 100
                          ? 'Analyzing...'
                          : 'Uploading & analyzing...'}
                    </span>
                  </div>
                  {uploadProgress > 0 && (
                    <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
                      <div
                        className="h-full bg-primary transition-all duration-300"
                        style={{ width: `${Math.min(uploadProgress, 100)}%` }}
                      />
                    </div>
                  )}
                </div>
              ) : (
                <>
                  <div className="mb-0.5 text-muted-foreground"><UploadSimpleIcon size={20} weight="bold" /></div>
                  <div className="text-muted-foreground text-sm">
                    Drop video{hasStaged ? 's to add more' : '(s)'} or <strong className="text-foreground">click to browse</strong>
                  </div>
                  <div className="text-[11px] text-muted-foreground mt-1">MP4, MOV, MKV, WEBM</div>
                </>
              )}
            </div>

            {/* URL input */}
            <div className="mt-3">
              <Label>Or paste a video link</Label>
              <div className="mt-1 flex gap-2">
                <Input type="url" placeholder="https://www.tiktok.com/..."
                  value={videoUrl} onChange={(e) => setVideoUrl(e.target.value)}
                  disabled={isBusy} onKeyDown={(e) => { if (e.key === 'Enter') void handleDownloadUrl(); }}
                  className="flex-1 text-sm"
                />
                <Button size="sm" onClick={() => void handleDownloadUrl()} disabled={isBusy || !videoUrl.trim()}>
                  {downloading ? <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted border-t-primary" /> : 'Add'}
                </Button>
              </div>
              <p className="text-[10px] text-muted-foreground mt-1">TikTok, YouTube, Instagram, etc.</p>
            </div>
          </>
        )}

        {/* Staged files summary + actions */}
        {hasStaged && (stage === 'trim' || stage === 'configure') && (
          <div className="mt-4 space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{stagedFiles.length} clip{stagedFiles.length !== 1 ? 's' : ''} staged</span>
              <button type="button" onClick={startNewBatch}
                className="text-[11px] text-muted-foreground hover:text-destructive font-bold">Clear all</button>
            </div>
            {stage === 'trim' && (
              <Button onClick={() => setStage('configure')} className="w-full">
                Continue to Configure &rarr;
              </Button>
            )}
          </div>
        )}

        {/* New batch button in results */}
        {stage === 'results' && (
          <div className="mt-4">
            <Button variant="outline" onClick={startNewBatch} className="w-full">New Batch</Button>
          </div>
        )}

        {/* Past jobs */}
        <div className="mt-6 border-t-2 border-border pt-4">
          <span className="text-sm font-bold text-foreground">
            Previous Jobs
            {pastJobs.length > 0 && <span className="ml-1.5 text-[11px] text-muted-foreground font-normal">({pastJobs.length})</span>}
          </span>
          <div className="mt-3 space-y-1.5">
            {pastJobs.length === 0 ? (
              <p className="text-xs text-muted-foreground italic">No previous jobs.</p>
            ) : pastJobs.map((job) => (
              <div key={job.job_id}
                className="group flex items-center gap-2 rounded-[var(--border-radius)] border border-border bg-card px-3 py-2 hover:bg-muted hover:border-primary/30 transition-all">
                {renamingJobId === job.job_id ? (
                  <Input
                    value={renameValue}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void handleRenameJob(job.job_id, renameValue); if (e.key === 'Escape') setRenamingJobId(null); }}
                    onBlur={() => { if (renameValue.trim()) void handleRenameJob(job.job_id, renameValue); else setRenamingJobId(null); }}
                    autoFocus
                    className="h-6 text-xs flex-1"
                  />
                ) : (
                  <button type="button" onClick={() => viewPastJob(job)}
                    className="flex-1 text-left text-xs text-foreground group-hover:text-primary truncate">
                    {job.label || `${job.job_id.slice(0, 8)}...`} ({job.clip_count} clips)
                  </button>
                )}
                <button type="button" onClick={() => { setRenamingJobId(job.job_id); setRenameValue(job.label || job.job_id.slice(0, 8)); }}
                  className="text-[10px] text-muted-foreground hover:text-primary transition-colors font-bold opacity-0 group-hover:opacity-100">Rename</button>
                <button type="button" onClick={() => downloadAll(job.job_id)}
                  className="text-[10px] text-muted-foreground hover:text-primary transition-colors font-bold opacity-0 group-hover:opacity-100">ZIP</button>
                <button type="button" onClick={() => void deletePastJob(job.job_id)}
                  className="w-5 h-5 flex items-center justify-center rounded-full text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors text-xs opacity-0 group-hover:opacity-100">x</button>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── Right panel ── */}
      <div className="flex-1 overflow-y-auto p-6 bg-background">
        <div className="mx-auto max-w-5xl">

          {/* ── STAGE: Trim ── */}
          {(stage === 'trim') && hasStaged && (
            <>
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-xl font-heading text-foreground">
                  Stage &amp; Trim
                  <span className="ml-2 text-sm text-muted-foreground font-normal">{stagedFiles.length} clips</span>
                </h2>
                <Button size="sm" onClick={() => setStage('configure')}>
                  Continue &rarr;
                </Button>
              </div>
              <p className="mb-4 text-xs text-muted-foreground">
                Click a clip to expand the trim tool. Set in/out points for each, then continue to configure output.
              </p>

              <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
                {stagedFiles.map((file) => {
                  const isExpanded = expandedIndex === file.index;
                  const trimmed = file.trimStart > 0.05 || file.trimEnd < file.duration - 0.05;

                  return (
                    <div key={file.index}
                      className={`rounded-[var(--border-radius)] overflow-hidden border bg-card transition-all ${
                        isExpanded ? 'border-primary ring-2 ring-primary/40 col-span-1'
                          : trimmed ? 'border-emerald-500/40 ring-1 ring-emerald-500/30 hover:border-emerald-500/60'
                            : 'border-border shadow-[var(--shadow)] hover:border-primary/30'
                      }`}>
                      {/* Header */}
                      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
                        <button type="button" onClick={() => setExpandedIndex(isExpanded ? null : file.index)}
                          className="flex-1 text-left text-xs font-bold text-foreground truncate hover:text-primary transition-colors"
                          title={file.original_name}>
                          {file.original_name}
                        </button>
                        <div className="flex items-center gap-1.5 ml-2">
                          {trimmed && <Badge variant="success" className="text-[9px] shadow-none px-1.5">trimmed</Badge>}
                          <span className="text-[10px] text-muted-foreground">{fmtTime(file.trimEnd - file.trimStart)}</span>
                          <button type="button" onClick={(e) => { e.stopPropagation(); removeStagedFile(file.index); }}
                            className="w-4 h-4 flex items-center justify-center rounded-full text-muted-foreground hover:text-destructive text-[10px]">x</button>
                        </div>
                      </div>

                      {isExpanded ? (
                        <div className="p-3">
                          <TrimTimeline file={file} onChange={(s, e) => updateTrim(file.index, s, e)} />
                          <div className="mt-2 text-[10px] text-muted-foreground text-center">
                            {file.width}x{file.height} &middot; {fmtTime(file.duration)} total
                          </div>
                        </div>
                      ) : (
                        /* Collapsed: thumbnail image (not video!) */
                        <button type="button" className="block w-full" onClick={() => setExpandedIndex(file.index)}>
                          <div className="relative aspect-[9/16] bg-muted">
                            <img
                              src={staticUrl(file.thumbUrl)}
                              alt={file.original_name}
                              className="w-full h-full object-cover"
                              loading="lazy"
                            />
                            {trimmed && (
                              <div className="absolute bottom-0 inset-x-0 h-1.5 bg-muted/80">
                                <div className="absolute inset-y-0 bg-primary/70"
                                  style={{
                                    left: `${(file.trimStart / file.duration) * 100}%`,
                                    width: `${((file.trimEnd - file.trimStart) / file.duration) * 100}%`,
                                  }} />
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

          {/* ── STAGE: Configure ── */}
          {stage === 'configure' && hasStaged && (
            <ConfigurePanel
              stagedFiles={stagedFiles}
              clipLength={clipLength}
              setClipLength={setClipLength}
              onProcess={handleProcessAll}
              onBack={() => setStage('trim')}
            />
          )}

          {/* ── STAGE: Processing ── */}
          {stage === 'processing' && (
            <div className="max-w-xl mx-auto py-10">
              <h2 className="text-xl font-heading text-foreground mb-6 text-center">Processing</h2>

              {processProgress && (
                <div className="space-y-4">
                  {/* Progress bar */}
                  <div className="w-full bg-muted rounded-full h-4 border border-border overflow-hidden">
                    <div
                      className="h-full bg-primary rounded-full transition-all duration-300"
                      style={{ width: `${processProgress.total > 0 ? (processProgress.clip / processProgress.total) * 100 : 0}%` }}
                    />
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-foreground font-bold">
                      Clip {processProgress.clip} / {processProgress.total}
                    </span>
                    <span className="text-muted-foreground">
                      {Math.round((processProgress.clip / Math.max(1, processProgress.total)) * 100)}%
                    </span>
                  </div>
                  {processProgress.source && (
                    <p className="text-xs text-muted-foreground text-center">
                      Encoding from: <strong className="text-foreground">{processProgress.source}</strong>
                    </p>
                  )}

                  {/* Live results appearing */}
                  {resultClips.length > 0 && (
                    <div className="mt-6">
                      <p className="text-xs text-muted-foreground mb-3">{resultClips.length} clip{resultClips.length !== 1 ? 's' : ''} ready:</p>
                      <div className="grid grid-cols-[repeat(auto-fill,minmax(80px,1fr))] gap-2">
                        {resultClips.slice(-12).map((clip) => (
                          <div key={clip.index} className="aspect-[9/16] rounded overflow-hidden border border-border bg-muted">
                            {clip.thumbUrl ? (
                              <img src={staticUrl(clip.thumbUrl)} alt={clip.name}
                                className="w-full h-full object-cover" loading="lazy" />
                            ) : (
                              <div className="w-full h-full flex items-center justify-center text-[10px] text-muted-foreground">
                                #{clip.index + 1}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {!processProgress && (
                <div className="flex flex-col items-center gap-4">
                  <span className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
                  <p className="text-sm text-muted-foreground">Starting...</p>
                </div>
              )}
            </div>
          )}

          {/* ── STAGE: Results ── */}
          {stage === 'results' && hasResults && (
            <>
              <div className="flex items-center justify-between mb-6">
                <h2 className="text-xl font-heading text-foreground">
                  Processed Clips
                  <span className="ml-2 text-sm text-muted-foreground font-normal">
                    {resultClips.filter((c) => c.ok).length}/{resultClips.length}
                  </span>
                </h2>
                <div className="flex gap-2">
                  <Button variant="secondary" size="sm" onClick={() => sendToBurn(resultClips)}>Use in Burn</Button>
                  {resultJobId && <Button variant="outline" size="sm" onClick={() => downloadAll(resultJobId)}>Download ZIP</Button>}
                </div>
              </div>
              <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
                {resultClips.filter((c) => c.ok && c.url).map((clip) => (
                  <div key={clip.index}
                    className="rounded-[var(--border-radius)] overflow-hidden border border-border bg-card shadow-[var(--shadow)] hover:border-primary/30 transition-all">
                    <div className="relative bg-muted aspect-[9/16]">
                      {clip.thumbUrl ? (
                        <img src={staticUrl(clip.thumbUrl)} alt={clip.name}
                          className="w-full h-full object-cover" loading="lazy" />
                      ) : (
                        <video src={`${staticUrl(clip.url!)}#t=0.001`}
                          className="w-full h-full object-cover"
                          playsInline muted preload="metadata"
                          onLoadedData={(e) => { e.currentTarget.currentTime = 0.001; }} />
                      )}
                    </div>
                    <div className="px-2.5 py-1.5 flex items-center justify-between text-xs">
                      <Badge variant="success" className="text-[10px] shadow-none">#{clip.index + 1}</Badge>
                      <div className="flex items-center gap-2">
                        {clip.duration > 0 && (
                          <span className="text-muted-foreground text-[11px]">{clip.duration.toFixed(1)}s</span>
                        )}
                        <a href={staticUrl(clip.url!)} download={clip.name}
                          className="text-primary hover:underline font-bold text-xs">Download</a>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {/* ── Empty state (ingest) ── */}
          {stage === 'ingest' && !hasStaged && !hasResults && (
            <EmptyState
              icon={<ScissorsIcon size={48} weight="duotone" />}
              title="No Clips Yet"
              description="Upload video files or paste links. Trim each one, set your desired clip length, then process them all into 9:16 short-form clips."
            />
          )}
        </div>
      </div>
    </div>
  );
}
