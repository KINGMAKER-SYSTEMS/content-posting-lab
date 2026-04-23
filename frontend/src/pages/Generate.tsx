import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiUrl, staticUrl } from '../lib/api';
import { useWorkflowStore } from '../stores/workflowStore';
import type { ColorCorrection, Provider, Job, VideoEntry, ProviderSchemas, SchemaField } from '../types/api';
import { EmptyState, LazyVideo, ProgressBar } from '../components';
import {
  XIcon,
  PlayIcon,
  FolderOpenIcon,
  UploadSimpleIcon,
  ArrowRightIcon,
} from '@phosphor-icons/react';
import {
  DEFAULT_COLOR_CORRECTION,
  applyCSSFilterPreview,
  getColorCorrectionOrNull,
} from '../lib/colorCorrection';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';

const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  generating: 'Starting',
  polling: 'Generating',
  downloading: 'Saving',
  done: 'Done',
  error: 'Error',
  failed: 'Error',
  processing: 'Processing',
};

function statusLabel(s: string): string {
  return STATUS_LABELS[s] || s;
}

function statusVariant(s: string): 'warning' | 'info' | 'success' | 'error' | 'secondary' {
  switch (s) {
    case 'queued':
      return 'warning';
    case 'generating':
    case 'polling':
    case 'downloading':
    case 'processing':
      return 'info';
    case 'done':
      return 'success';
    case 'error':
    case 'failed':
      return 'error';
    default:
      return 'secondary';
  }
}

function isTerminal(v: VideoEntry): boolean {
  return v.status === 'done' || v.status === 'failed' || v.status === 'error';
}

const CROP_MULTIPLIERS: Record<string, number> = {
  none: 1,
  dual: 2,
  triptych: 3,
  both: 5,
};

function cropMultiplier(mode?: string | null): number {
  if (!mode) return 1;
  return CROP_MULTIPLIERS[mode] ?? 1;
}

function expectedOutputCount(job: Pick<Job, 'count' | 'crop_mode'>): number {
  return job.count * cropMultiplier(job.crop_mode);
}

function deliveredFileCount(videos: VideoEntry[]): number {
  return videos.reduce((n, v) => {
    if (v.status !== 'done') return n;
    if (v.crops && v.crops.length > 0) return n + v.crops.length;
    if (v.file) return n + 1;
    return n;
  }, 0);
}

interface PromptEntry {
  prompt: string;
  provider: string;
  count: number;
  duration: number;
  aspect_ratio: string;
  resolution: string;
  has_media: boolean;
  job_id: string;
  timestamp: string;
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

function formatJobTimestamp(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
}

function getDateKey(iso?: string): string {
  if (!iso) return 'Unknown';
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

const PROVIDER_SHORT: Record<string, string> = {
  'hailuo': 'Hailuo',
  'wan-t2v': 'Wan',
  'wan-i2v': 'Wan',
  'wan-i2v-fast': 'Wan',
  'grok': 'Grok',
  'pruna-pvideo': 'Pruna',
  'pruna-pvideo-vertical': 'Pruna 9:16',
};

/** Trigger a browser download from a Blob with the given filename. */
function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const CC_SLIDERS: ReadonlyArray<{ key: keyof ColorCorrection; label: string; min: number; max: number }> = [
  { key: 'brightness', label: 'Brightness', min: -100, max: 100 },
  { key: 'contrast', label: 'Contrast', min: -100, max: 100 },
  { key: 'saturation', label: 'Saturation', min: -100, max: 100 },
  { key: 'sharpness', label: 'Sharpness', min: 0, max: 100 },
  { key: 'shadow', label: 'Shadow', min: -100, max: 100 },
  { key: 'temperature', label: 'Temperature', min: -100, max: 100 },
  { key: 'tint', label: 'Tint', min: -100, max: 100 },
  { key: 'fade', label: 'Fade', min: 0, max: 100 },
];

/** Convert a base64 data URI to a File object. */
function dataUriToFile(dataUri: string, filename: string): File {
  const [header, b64] = dataUri.split(',');
  const mime = header.match(/:(.*?);/)?.[1] ?? 'image/png';
  const bytes = atob(b64);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new File([arr], filename, { type: mime });
}

const activePolls = new Set<string>();
const POLL_TIMEOUT_MS = 15 * 60 * 1000;

interface PollStatus {
  startedAt: number;
  lastTickAt: number;
  reconnecting: boolean;
}

const pollStatus = new Map<string, PollStatus>();
const pollListeners = new Set<() => void>();

function notifyPollListeners() {
  for (const listener of pollListeners) listener();
}

/** Subscribe to poll-status changes. Returns unsubscribe fn. */
export function subscribeToPollStatus(listener: () => void): () => void {
  pollListeners.add(listener);
  return () => { pollListeners.delete(listener); };
}

export function getPollStatus(jobId: string): PollStatus | undefined {
  return pollStatus.get(jobId);
}

function startPolling(
  jobId: string,
  onUpdate: (job: Job) => void,
  onComplete: (job: Job) => void,
) {
  if (activePolls.has(jobId)) return;
  activePolls.add(jobId);
  const startedAt = Date.now();
  pollStatus.set(jobId, { startedAt, lastTickAt: startedAt, reconnecting: false });
  notifyPollListeners();
  let consecutiveErrors = 0;

  const tick = async () => {
    if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
      activePolls.delete(jobId);
      pollStatus.delete(jobId);
      notifyPollListeners();
      onComplete({
        id: jobId,
        prompt: '',
        provider: '',
        count: 0,
        videos: [{ index: 0, status: 'error', error: 'Polling timed out after 15 minutes' }],
      });
      return;
    }

    try {
      const res = await fetch(apiUrl(`/api/video/jobs/${jobId}`));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const job: Job = await res.json();
      consecutiveErrors = 0;
      const prev = pollStatus.get(jobId);
      if (prev) {
        pollStatus.set(jobId, { ...prev, lastTickAt: Date.now(), reconnecting: false });
        notifyPollListeners();
      }
      onUpdate(job);

      const allDone = job.videos.every(isTerminal);
      if (!allDone) {
        setTimeout(tick, 2000);
      } else {
        activePolls.delete(jobId);
        pollStatus.delete(jobId);
        notifyPollListeners();
        onComplete(job);
      }
    } catch {
      consecutiveErrors++;
      const prev = pollStatus.get(jobId);
      if (prev) {
        pollStatus.set(jobId, { ...prev, reconnecting: true });
        notifyPollListeners();
      }
      if (consecutiveErrors >= 10) {
        activePolls.delete(jobId);
        pollStatus.delete(jobId);
        notifyPollListeners();
        onComplete({
          id: jobId,
          prompt: '',
          provider: '',
          count: 0,
          videos: [{ index: 0, status: 'error', error: 'Lost connection to server' }],
        });
        return;
      }
      setTimeout(tick, 3000);
    }
  };
  tick();
}

/** Provider p95 typical durations (approximate, based on observed latency). */
const PROVIDER_P95_MS: Record<string, number> = {
  sora: 4 * 60 * 1000,
  luma: 3 * 60 * 1000,
  'fal-wan': 2 * 60 * 1000,
  'fal-kling': 2 * 60 * 1000,
  'fal-ovi': 2 * 60 * 1000,
  'rep-minimax': 3 * 60 * 1000,
  'rep-wan': 3 * 60 * 1000,
  'rep-kling': 3 * 60 * 1000,
  grok: 2 * 60 * 1000,
};

function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m > 0 ? `${m}m ${s.toString().padStart(2, '0')}s` : `${s}s`;
}

export function GeneratePage() {
  const navigate = useNavigate();
  const {
    activeProjectName,
    generateJobs,
    addGenerateJob,
    setGenerateJob,
    clearGenerateJobs,
    addGeneratedVideo,
    addNotification,
    setVideoRunningCount,
    incrementBurnReadyCount,
    primeBurnSelection,
    generatePrefill,
    clearGeneratePrefill,
    archivedJobIds,
    archiveJob,
    archiveJobs,
    unarchiveJob,
  } = useWorkflowStore();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollTick, setPollTick] = useState(0);

  useEffect(() => {
    const rerender = () => setPollTick((t) => t + 1);
    const unsubscribe = subscribeToPollStatus(rerender);
    const interval = window.setInterval(rerender, 1000);
    return () => { unsubscribe(); window.clearInterval(interval); };
  }, []);

  const [prompt, setPrompt] = useState('');
  const [selectedProvider, setSelectedProvider] = useState('');
  const [count, setCount] = useState(1);
  const [duration, setDuration] = useState(10);
  const [aspectRatio, setAspectRatio] = useState('9:16');
  const [resolution, setResolution] = useState('720p');
  const [mediaFile, setMediaFile] = useState<File | null>(null);
  const [lastImageFile, setLastImageFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // Selection mode state
  const [selectMode, setSelectMode] = useState(false);
  const [selectedVideos, setSelectedVideos] = useState<Set<string>>(new Set());

  // Archive-view + per-job expand state. Done jobs default to collapsed so
  // the feed stays clean; users can expand individual jobs on demand.
  const [showArchived, setShowArchived] = useState(false);
  const [expandedJobIds, setExpandedJobIds] = useState<Set<string>>(new Set());
  const toggleJobExpanded = useCallback((jobId: string) => {
    setExpandedJobIds((prev) => {
      const next = new Set(prev);
      if (next.has(jobId)) next.delete(jobId);
      else next.add(jobId);
      return next;
    });
  }, []);

  // Global color-correction draft — applied to EVERY done video in the feed
  // (live CSS preview) and to every file when the user clicks "Download all".
  const [batchCc, setBatchCc] = useState<ColorCorrection>(DEFAULT_COLOR_CORRECTION);
  const [batchCcBusy, setBatchCcBusy] = useState(false);

  const [providerSchemas, setProviderSchemas] = useState<ProviderSchemas>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [extraParams, setExtraParams] = useState<Record<string, unknown>>({});

  const [promptHistory, setPromptHistory] = useState<PromptEntry[]>([]);
  const [historyOpen, setHistoryOpen] = useState(true);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const lastImageInputRef = useRef<HTMLInputElement>(null);
  const applyingPrefillRef = useRef(false);

  const onUpdateRef = useRef<(job: Job) => void>(() => {});
  const onCompleteRef = useRef<(job: Job) => void>(() => {});

  onUpdateRef.current = (job: Job) => {
    setGenerateJob(job);
  };

  onCompleteRef.current = (job: Job) => {
    const successCount = job.videos.filter((v) => v.status === 'done').length;
    if (successCount > 0) {
      addGeneratedVideo(job.id);
      incrementBurnReadyCount(successCount);
      addNotification('success', `Generated ${successCount} videos for "${job.prompt.substring(0, 20)}..."`);
      window.dispatchEvent(new Event('burn:refresh-request'));
    } else {
      addNotification('error', `Failed to generate videos for "${job.prompt.substring(0, 20)}..."`);
    }
  };

  const stableOnUpdate = useCallback((job: Job) => onUpdateRef.current(job), []);
  const stableOnComplete = useCallback((job: Job) => onCompleteRef.current(job), []);

  useEffect(() => {
    for (const job of generateJobs) {
      const allDone = job.videos.every(isTerminal);
      if (!allDone) {
        startPolling(job.id, stableOnUpdate, stableOnComplete);
      }
    }

    const runningCount = generateJobs.filter((job) => job.videos.some((v) => !isTerminal(v))).length;
    setVideoRunningCount(runningCount);
  }, [generateJobs, stableOnUpdate, stableOnComplete, setVideoRunningCount]);

  useEffect(() => {
    if (!activeProjectName) {
      setProviders([]);
      setSelectedProvider('');
      return;
    }

    let isCancelled = false;

    fetch(apiUrl('/api/video/providers'))
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text();
          throw new Error(text || `Failed to load providers (${res.status})`);
        }
        return res.json();
      })
      .then((data: Provider[]) => {
        if (isCancelled) return;
        setProviders(data);
        setSelectedProvider((current) => {
          if (current && data.some((p) => p.id === current)) return current;
          return data.length > 0 ? data[0].id : '';
        });
      })
      .catch((err: unknown) => {
        if (isCancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load providers';
        setError(message);
        addNotification('error', message);
      });

    fetch(apiUrl('/api/video/provider-schemas'))
      .then((r) => (r.ok ? r.json() : {}))
      .then((data: ProviderSchemas) => {
        if (!isCancelled) setProviderSchemas(data);
      })
      .catch(() => {});

    return () => {
      isCancelled = true;
    };
  }, [activeProjectName, addNotification]);

  const fetchHistory = useCallback(() => {
    if (!activeProjectName) return;
    fetch(apiUrl(`/api/video/prompts?project=${encodeURIComponent(activeProjectName)}`))
      .then((r) => (r.ok ? r.json() : []))
      .then((data: PromptEntry[]) => setPromptHistory(data))
      .catch(() => {});
  }, [activeProjectName]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  // Recover jobs from backend on mount / project change
  useEffect(() => {
    if (!activeProjectName) return;
    let cancelled = false;

    // Clear old project's jobs and stop all active polls
    clearGenerateJobs();
    activePolls.clear();

    const recover = async () => {
      try {
        const res = await fetch(apiUrl(`/api/video/jobs?project=${encodeURIComponent(activeProjectName)}`));
        if (!res.ok || cancelled) return;
        const serverJobs: Job[] = await res.json();

        for (const job of serverJobs) {
          setGenerateJob(job);
          const allDone = job.videos.every(isTerminal);
          if (!allDone) {
            startPolling(job.id, stableOnUpdate, stableOnComplete);
          }
        }
      } catch {
        // Recovery is best-effort
      }
    };
    recover();

    return () => { cancelled = true; };
  }, [activeProjectName, setGenerateJob, clearGenerateJobs, stableOnUpdate, stableOnComplete]);

  useEffect(() => {
    // Skip cleanup when prefill is being applied — the prefill sets provider
    // and then immediately sets lastImageFile, so the cleanup would clobber it.
    if (applyingPrefillRef.current) return;
    // Seed extraParams with schema defaults so submit sends the correct values
    // even if the user never touches the controls (e.g. Hailuo duration=6).
    const schema = providerSchemas[selectedProvider];
    const defaults: Record<string, unknown> = {};
    if (schema) {
      for (const [key, field] of Object.entries(schema)) {
        if (key === '_advanced' || key === 'image_required') continue;
        const f = field as SchemaField;
        if (f.default !== undefined) defaults[key] = f.default;
      }
    }
    setExtraParams(defaults);
    setAdvancedOpen(false);
    setLastImageFile(null);
    if (lastImageInputRef.current) lastImageInputRef.current.value = '';
  }, [selectedProvider, providerSchemas]);

  // Consume generatePrefill from Recreate → Generate workflow
  useEffect(() => {
    if (!generatePrefill) return;
    applyingPrefillRef.current = true;

    setPrompt(generatePrefill.prompt);
    setAspectRatio(generatePrefill.aspectRatio);
    setCount(1);

    // Set provider (triggers the selectedProvider effect above, which is guarded)
    if (providers.some((p) => p.id === generatePrefill.provider)) {
      setSelectedProvider(generatePrefill.provider);
    }

    // Convert first frame data URI → File for the media upload
    const firstFile = dataUriToFile(generatePrefill.firstFrameDataUri, 'first_frame.png');
    setMediaFile(firstFile);
    if (fileInputRef.current) {
      const dt = new DataTransfer();
      dt.items.add(firstFile);
      fileInputRef.current.files = dt.files;
    }

    // Convert last frame data URI → File if available
    if (generatePrefill.lastFrameDataUri) {
      const lastFile = dataUriToFile(generatePrefill.lastFrameDataUri, 'last_frame.png');
      setLastImageFile(lastFile);
      if (lastImageInputRef.current) {
        const dt = new DataTransfer();
        dt.items.add(lastFile);
        lastImageInputRef.current.files = dt.files;
      }
    }

    clearGeneratePrefill();

    // Release the guard after a tick so the selectedProvider effect runs but is skipped
    requestAnimationFrame(() => {
      applyingPrefillRef.current = false;
    });
  }, [generatePrefill, providers, clearGeneratePrefill]);

  const applyPromptEntry = (entry: PromptEntry) => {
    setPrompt(entry.prompt);
    if (providers.some((p) => p.id === entry.provider)) {
      setSelectedProvider(entry.provider);
    }
    setCount(entry.count);
    setDuration(entry.duration);
    setAspectRatio(entry.aspect_ratio);
    setResolution(entry.resolution);
  };

  const clearHistory = async () => {
    if (!activeProjectName) return;
    await fetch(apiUrl(`/api/video/prompts?project=${encodeURIComponent(activeProjectName)}`), { method: 'DELETE' });
    setPromptHistory([]);
  };

  const handleSubmit: React.FormEventHandler<HTMLFormElement> = async (e) => {
    e.preventDefault();
    if (!activeProjectName) return;

    setLoading(true);
    setError(null);

    // Schema-driven extraParams override top-level defaults for shared keys
    // (e.g. Hailuo schema sets duration=6 and resolution=768p, which must
    // take precedence over the generic form defaults of duration=10 / 720p)
    const effectiveDuration = extraParams.duration !== undefined ? Number(extraParams.duration) : duration;
    const effectiveResolution = extraParams.resolution !== undefined ? String(extraParams.resolution) : resolution;
    const effectiveAspectRatio = extraParams.aspect_ratio !== undefined ? String(extraParams.aspect_ratio) : aspectRatio;

    const formData = new FormData();
    formData.append('prompt', prompt);
    formData.append('provider', selectedProvider);
    formData.append('count', count.toString());
    formData.append('duration', effectiveDuration.toString());
    formData.append('aspect_ratio', effectiveAspectRatio);
    formData.append('resolution', effectiveResolution);
    formData.append('project', activeProjectName);
    if (mediaFile) {
      formData.append('media', mediaFile);
    }
    if (lastImageFile) {
      formData.append('last_image', lastImageFile);
    }

    // Append model-specific params from schema controls (skip keys already sent above)
    const TOP_LEVEL_KEYS = new Set(['duration', 'resolution', 'aspect_ratio']);
    for (const [key, val] of Object.entries(extraParams)) {
      if (val !== undefined && val !== null && val !== '' && !TOP_LEVEL_KEYS.has(key)) {
        // "none" crop_mode means no cropping — don't send it
        if (key === 'crop_mode' && val === 'none') continue;
        formData.append(key, String(val));
      }
    }

    try {
      const res = await fetch(apiUrl('/api/video/generate'), {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(text);
      }

      const data = await res.json();
      const newJob: Job = {
        id: data.job_id,
        prompt,
        provider: selectedProvider,
        count,
        project: activeProjectName,
        videos: Array.from({ length: count }, (_, index) => ({ index, status: 'queued' as const })),
      };
      addGenerateJob(newJob);
      fetchHistory();

      setPrompt('');
      setMediaFile(null);
      setLastImageFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      if (lastImageInputRef.current) lastImageInputRef.current.value = '';
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to submit job';
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setMediaFile(e.target.files[0]);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length) {
      const dt = new DataTransfer();
      dt.items.add(e.dataTransfer.files[0]);
      if (fileInputRef.current) {
        fileInputRef.current.files = dt.files;
      }
      setMediaFile(e.dataTransfer.files[0]);
    }
  };

  const clearFile = (e: React.MouseEvent) => {
    e.stopPropagation();
    setMediaFile(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const schema = providerSchemas[selectedProvider];

  const handleParamChange = (key: string, value: unknown) => {
    setExtraParams((prev) => ({ ...prev, [key]: value }));
  };

  // Helper: group providers by group field
  const groupedProviders = providers.reduce<Record<string, Provider[]>>((acc, p) => {
    const group = p.group || 'Other';
    (acc[group] ??= []).push(p);
    return acc;
  }, {});

  const toggleVideoSelection = useCallback((filePath: string) => {
    setSelectedVideos((prev) => {
      const next = new Set(prev);
      if (next.has(filePath)) next.delete(filePath); else next.add(filePath);
      return next;
    });
  }, []);

  const selectAllFromJob = useCallback((job: Job) => {
    const paths = job.videos
      .filter((v) => v.status === 'done' && typeof v.file === 'string')
      .map((v) => v.file as string);
    setSelectedVideos((prev) => {
      const next = new Set(prev);
      const allSelected = paths.every((p) => next.has(p));
      if (allSelected) { for (const p of paths) next.delete(p); }
      else { for (const p of paths) next.add(p); }
      return next;
    });
  }, []);

  const selectAllVideos = useCallback(() => {
    const allPaths = generateJobs.flatMap((j) =>
      j.videos.filter((v) => v.status === 'done' && typeof v.file === 'string').map((v) => v.file as string)
    );
    setSelectedVideos((prev) => {
      if (prev.size === allPaths.length && allPaths.every((p) => prev.has(p))) return new Set();
      return new Set(allPaths);
    });
  }, [generateJobs]);

  const sendSelectedToBurn = useCallback(() => {
    const paths = Array.from(selectedVideos);
    if (paths.length === 0) { addNotification('info', 'No videos selected.'); return; }
    primeBurnSelection({ videoPaths: paths });
    setSelectMode(false);
    setSelectedVideos(new Set());
    navigate('/burn');
  }, [selectedVideos, primeBurnSelection, navigate, addNotification]);

  const sendSelectionToBurn = (job: Job) => {
    const videoPaths = job.videos.reduce<string[]>((paths, video) => {
      if (video.status === 'done' && typeof video.file === 'string') {
        paths.push(video.file);
      }
      return paths;
    }, []);

    if (videoPaths.length === 0) {
      addNotification('info', 'No completed videos available to preselect in Burn yet.');
      return;
    }

    primeBurnSelection({ videoPaths });
    navigate('/burn');
  };

  // Live CSS preview for the batch color-correct sliders, shown on selected
  // cards so the user sees their dialed values before committing to an encode.
  const batchCcPreview = useMemo(
    () => (getColorCorrectionOrNull(batchCc) ? applyCSSFilterPreview(batchCc) : ''),
    [batchCc],
  );

  // Split jobs into live vs archived. Archived stays hidden behind a toggle
  // so the feed only shows active work by default.
  const archivedSet = useMemo(() => new Set(archivedJobIds), [archivedJobIds]);
  const liveJobs = useMemo(
    () => generateJobs.filter((j) => !archivedSet.has(j.id)),
    [generateJobs, archivedSet],
  );
  const archivedJobs = useMemo(
    () => generateJobs.filter((j) => archivedSet.has(j.id)),
    [generateJobs, archivedSet],
  );

  // Every done video file in the live feed (including per-crop outputs).
  // Archived jobs are excluded — Download All targets visible work only.
  const allDoneVideoPaths = useMemo(() => {
    const out: string[] = [];
    for (const job of liveJobs) {
      for (const v of job.videos) {
        if (v.status !== 'done') continue;
        if (v.crops && v.crops.length > 0) {
          for (const c of v.crops) if (c.file) out.push(c.file);
        } else if (v.file) {
          out.push(v.file);
        }
      }
    }
    return out;
  }, [liveJobs]);

  const runBatchColorCorrect = useCallback(async () => {
    if (allDoneVideoPaths.length === 0) {
      addNotification('info', 'No finished videos to download yet.');
      return;
    }
    const cc = getColorCorrectionOrNull(batchCc);
    // Server caps bulk requests at 50 items — chunk client-side so any feed
    // size works. Multiple zips land (one per batch) for large batches.
    const CHUNK = 50;
    const chunks: string[][] = [];
    for (let i = 0; i < allDoneVideoPaths.length; i += CHUNK) {
      chunks.push(allDoneVideoPaths.slice(i, i + CHUNK));
    }
    setBatchCcBusy(true);
    const today = new Date().toISOString().slice(0, 10);
    let succeeded = 0;
    try {
      for (let idx = 0; idx < chunks.length; idx++) {
        const paths = chunks[idx];
        const items = paths.map((p) => ({ path: p, color_correction: cc }));
        const res = await fetch(apiUrl('/api/video/color-correct/bulk'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project: activeProjectName, items }),
        });
        if (!res.ok) {
          const text = await res.text().catch(() => '');
          throw new Error(text || `HTTP ${res.status}`);
        }
        const blob = await res.blob();
        const suffix = chunks.length > 1 ? `_part${idx + 1}of${chunks.length}` : '';
        triggerBlobDownload(blob, `videos_${today}_${paths.length}${suffix}.zip`);
        succeeded += paths.length;
      }
      // Auto-archive fully-done, non-error jobs on successful download — keeps
      // the feed tidy once the user has their files.
      const downloadedSet = new Set(allDoneVideoPaths);
      const toArchive: string[] = [];
      for (const job of liveJobs) {
        const allTerminal = job.videos.every(isTerminal);
        const anyError = job.videos.some((v) => v.status === 'error' || v.status === 'failed');
        if (!allTerminal || anyError) continue;
        const jobPaths: string[] = [];
        for (const v of job.videos) {
          if (v.crops && v.crops.length > 0) {
            for (const c of v.crops) if (c.file) jobPaths.push(c.file);
          } else if (v.file) {
            jobPaths.push(v.file);
          }
        }
        if (jobPaths.length > 0 && jobPaths.every((p) => downloadedSet.has(p))) {
          toArchive.push(job.id);
        }
      }
      if (toArchive.length > 0) archiveJobs(toArchive);
      addNotification(
        'success',
        `Downloaded ${succeeded} ${cc ? 'color-corrected ' : ''}videos${chunks.length > 1 ? ` in ${chunks.length} zips` : ''}${toArchive.length > 0 ? ` · archived ${toArchive.length} jobs` : ''}`,
      );
    } catch (e) {
      if (succeeded > 0) {
        addNotification(
          'error',
          `Only ${succeeded}/${allDoneVideoPaths.length} downloaded before failing: ${e instanceof Error ? e.message : 'unknown'}`,
        );
      } else {
        addNotification('error', e instanceof Error ? `Download failed: ${e.message}` : 'Download failed');
      }
    } finally {
      setBatchCcBusy(false);
    }
  }, [activeProjectName, addNotification, allDoneVideoPaths, archiveJobs, batchCc, liveJobs]);

  // Renders a single job. Fully-done jobs collapse to a one-line row until
  // the user expands them — keeps the feed scannable once batches pile up.
  // `mode` only switches the header action (Archive in the live feed,
  // Restore in the archived section).
  const renderJobCard = (job: Job, mode: 'live' | 'archived') => {
    const doneCount = job.videos.filter((v) => v.status === 'done').length;
    const errorCount = job.videos.filter((v) => v.status === 'error' || v.status === 'failed').length;
    const total = job.count;
    const allFinished = doneCount + errorCount === total;
    const pct = Math.round(((doneCount + errorCount) / total) * 100);
    const mult = cropMultiplier(job.crop_mode);
    const expectedFiles = expectedOutputCount(job);
    const deliveredFiles = deliveredFileCount(job.videos);
    const isFullyDone = allFinished && errorCount === 0 && doneCount === total;
    const isExpanded = expandedJobIds.has(job.id);
    const showCollapsed = isFullyDone && !isExpanded && !selectMode;

    const archiveButton = mode === 'live' ? (
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); archiveJob(job.id); }}
        className="text-[11px] font-bold text-muted-foreground hover:text-foreground transition-colors"
        title="Hide this job from the active feed"
      >
        Archive
      </button>
    ) : (
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); unarchiveJob(job.id); }}
        className="text-[11px] font-bold text-muted-foreground hover:text-primary transition-colors"
        title="Return this job to the active feed"
      >
        Restore
      </button>
    );

    if (showCollapsed) {
      return (
        <div
          key={job.id}
          className="group flex items-center gap-3 rounded-[var(--border-radius)] border-2 border-border bg-card px-3 py-2 hover:shadow-[2px_2px_0_0_var(--border)] hover:translate-x-[1px] hover:translate-y-[1px] transition-all cursor-pointer"
          onClick={() => toggleJobExpanded(job.id)}
        >
          <span className="text-xs text-muted-foreground -rotate-90 inline-block">▼</span>
          <div className="flex-1 truncate text-sm text-muted-foreground italic" title={job.prompt}>
            "{job.prompt}" <span className="text-foreground font-bold not-italic">[{job.provider}]</span>
          </div>
          <Badge variant="success" className="text-[10px] shadow-none">
            {deliveredFiles} {deliveredFiles === 1 ? 'file' : 'files'}
          </Badge>
          {job.created_at && (
            <span className="text-[10px] text-muted-foreground">{formatJobTimestamp(job.created_at)}</span>
          )}
          <div className="opacity-0 group-hover:opacity-100 transition-opacity">
            {archiveButton}
          </div>
        </div>
      );
    }

    return (
      <Card key={job.id}>
        <CardContent className="pt-0">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm text-muted-foreground italic truncate max-w-[50%]" title={job.prompt}>
              "{job.prompt}" <span className="text-foreground font-bold">[{job.provider}]</span>
              {job.created_at && (
                <span className="text-[10px] text-muted-foreground not-italic ml-2">
                  {formatJobTimestamp(job.created_at)}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              {(() => {
                const status = getPollStatus(job.id);
                if (!status || allFinished) return null;
                const elapsedMs = Date.now() - status.startedAt;
                const p95 = PROVIDER_P95_MS[job.provider] ?? 2 * 60 * 1000;
                const overP95 = elapsedMs > p95;
                // Reference pollTick so this block re-renders every second
                void pollTick;
                return (
                  <div className="flex items-center gap-2 text-[11px]">
                    {status.reconnecting ? (
                      <Badge variant="warning" className="text-[10px] shadow-none">
                        Reconnecting…
                      </Badge>
                    ) : (
                      <span className="heartbeat-dot" aria-label="Live polling" />
                    )}
                    <span className={`font-mono tabular-nums ${overP95 ? 'text-amber-400' : 'text-muted-foreground'}`}>
                      {formatElapsed(elapsedMs)}
                    </span>
                    {overP95 && !status.reconnecting && (
                      <span className="text-[10px] text-muted-foreground italic">
                        Taking longer than usual…
                      </span>
                    )}
                  </div>
                );
              })()}
              {isFullyDone && (
                <button
                  type="button"
                  title="Collapse this job"
                  onClick={() => toggleJobExpanded(job.id)}
                  className="text-[11px] font-bold text-muted-foreground hover:text-primary transition-colors"
                >
                  Collapse
                </button>
              )}
              <button
                type="button"
                title="Copy generation command"
                onClick={async () => {
                  const params = [
                    `Prompt: ${job.prompt}`,
                    `Provider: ${job.provider}`,
                    `Count: ${job.count}`,
                    `Job ID: ${job.id}`,
                  ].join('\n');
                  try {
                    await navigator.clipboard.writeText(params);
                    addNotification('success', 'Command copied to clipboard');
                  } catch {
                    addNotification('error', 'Failed to copy to clipboard');
                  }
                }}
                className="text-[11px] font-bold text-muted-foreground hover:text-primary transition-colors"
              >
                Copy
              </button>
              {archiveButton}
              <Badge variant={allFinished && errorCount === 0 ? 'success' : 'info'}>
                {mult > 1
                  ? `${deliveredFiles}/${expectedFiles} files`
                  : `${doneCount}/${total} done`}
              </Badge>
            </div>
          </div>

          <ProgressBar
            value={pct}
            color={allFinished && errorCount === 0 ? 'success' : 'primary'}
            className="mb-3"
          />

          <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 mb-2">
            {job.videos.flatMap((video, idx) => {
              const multiCrop = (video.crops?.length ?? 0) > 1;
              const items = video.crops && video.crops.length > 0
                ? video.crops.map((crop, ci) => ({
                    key: `${job.id}-${idx}-crop${ci}`,
                    url: crop.url,
                    file: crop.file,
                    label: multiCrop ? `Crop ${ci + 1}` : undefined,
                    video,
                    idx,
                  }))
                : [{ key: `${job.id}-${idx}`, url: video.url, file: video.file, label: undefined, video, idx }];

              return items.map(({ key, url, file, label, video: v, idx: vIdx }) => (
                <div
                  key={key}
                  className={`group/card relative rounded-[var(--border-radius)] overflow-hidden border-2 bg-card transition-all ${
                    selectMode && file && selectedVideos.has(file)
                      ? 'border-primary shadow-[4px_4px_0_0_var(--primary)]'
                      : 'border-border shadow-[2px_2px_0_0_var(--border)] hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[1px_1px_0_0_var(--border)]'
                  }`}
                  onClick={selectMode && v.status === 'done' && file ? () => toggleVideoSelection(file) : undefined}
                >
                  <div className="relative bg-muted aspect-[9/16] flex items-center justify-center">
                    {selectMode && v.status === 'done' && file && (
                      <div className="absolute top-2 left-2 z-10">
                        <input
                          type="checkbox"
                          checked={selectedVideos.has(file)}
                          onChange={() => toggleVideoSelection(file)}
                          onClick={(e) => e.stopPropagation()}
                          className="h-5 w-5 accent-primary cursor-pointer"
                        />
                      </div>
                    )}
                    {v.status === 'done' && url ? (
                      <LazyVideo
                        src={staticUrl(url)}
                        className="w-full h-full object-cover"
                        style={batchCcPreview ? { filter: batchCcPreview } : undefined}
                      />
                    ) : v.status === 'error' || v.status === 'failed' ? (
                      <div className="flex flex-col items-center gap-2 text-muted-foreground text-sm">
                        <XIcon size={28} weight="bold" />
                        <span className="font-bold">Failed</span>
                      </div>
                    ) : (
                      <div className="flex flex-col items-center gap-2 text-muted-foreground text-sm">
                        {v.status === 'queued' ? (
                          <PlayIcon size={28} weight="duotone" className="opacity-30" />
                        ) : (
                          <div className="w-7 h-7 border-3 border-muted border-t-primary rounded-full animate-spin" />
                        )}
                        <span className="font-bold">{statusLabel(v.status)}</span>
                      </div>
                    )}
                    {label && v.status === 'done' && (
                      <div className="absolute top-1.5 left-1.5">
                        <Badge variant="secondary" className="text-[10px] shadow-none">{label}</Badge>
                      </div>
                    )}
                    {v.status === 'done' && file && !selectMode && (
                      <button
                        type="button"
                        onClick={async (e) => {
                          e.stopPropagation();
                          const project = job.project || 'quick-test';
                          try {
                            const r = await fetch(apiUrl(`/api/video/file?project=${encodeURIComponent(project)}&path=${encodeURIComponent(file!)}`), { method: 'DELETE' });
                            if (!r.ok) throw new Error('Failed');
                            const updatedVideos = job.videos.map((vv, vi) => {
                              if (vi !== vIdx) return vv;
                              if (vv.crops && vv.crops.length > 0) {
                                const remainingCrops = vv.crops.filter((c) => c.file !== file);
                                if (remainingCrops.length > 0) return { ...vv, crops: remainingCrops };
                              }
                              return { ...vv, status: 'failed' as const, error: 'Deleted', file: undefined, url: undefined, crops: undefined };
                            });
                            setGenerateJob({ ...job, videos: updatedVideos });
                            addNotification('success', 'Video deleted');
                          } catch {
                            addNotification('error', 'Failed to delete video');
                          }
                        }}
                        className="absolute top-1.5 right-1.5 z-10 rounded bg-background/90 px-1.5 py-0.5 text-[10px] font-bold text-muted-foreground opacity-0 group-hover/card:opacity-100 hover:text-destructive transition-opacity"
                        title="Delete this video"
                      >
                        Delete
                      </button>
                    )}
                  </div>

                  {v.status === 'done' && url ? (
                    <div className="px-2.5 py-1.5 flex items-center justify-end text-xs">
                      <a href={staticUrl(url)} download className="text-primary hover:underline font-bold text-xs">
                        Download
                      </a>
                    </div>
                  ) : (
                    <div className="px-2.5 py-1.5 flex items-center justify-between text-xs">
                      <Badge variant={statusVariant(v.status)} className="text-[10px] shadow-none">
                        {statusLabel(v.status)}
                      </Badge>
                    </div>
                  )}

                  {v.error && (
                    <div className="px-2.5 pb-1.5 text-[11px] text-destructive break-all">{v.error}</div>
                  )}
                </div>
              ));
            })}
          </div>

          {doneCount > 0 && (
            <div className="flex gap-2 justify-end mt-2 flex-wrap">
              {selectMode ? (
                <Button variant="outline" size="sm" onClick={() => selectAllFromJob(job)}>
                  {job.videos.filter((v) => v.status === 'done' && v.file).every((v) => selectedVideos.has(v.file!)) ? 'Deselect Job' : 'Select Job'}
                </Button>
              ) : (
                <Button variant="secondary" size="sm" onClick={() => sendSelectionToBurn(job)}>
                  Use in Burn <ArrowRightIcon size={14} weight="bold" className="inline ml-1" />
                </Button>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    );
  };

  if (!activeProjectName) {
    return (
      <div className="h-full flex items-center justify-center">
        <EmptyState icon={<FolderOpenIcon size={48} weight="duotone" />} title="No Project Selected" description="Please select or create a project to start generating videos." />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col lg:flex-row overflow-hidden">
      {/* Left sidebar — form */}
      <div className="w-full lg:w-[420px] border-r-2 border-border bg-card p-6 overflow-y-auto flex-shrink-0">
        <h2 className="text-xl font-heading text-foreground mb-1">Generate Video</h2>
        <p className="text-xs text-muted-foreground mb-6">Create AI videos from text prompts</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <Label htmlFor="gen-provider">Provider</Label>
            <Select value={selectedProvider} onValueChange={setSelectedProvider}>
              <SelectTrigger id="gen-provider" className="w-full mt-1">
                <SelectValue placeholder="Select provider..." />
              </SelectTrigger>
              <SelectContent>
                {providers.length === 0 ? (
                  <SelectItem value="__loading" disabled>Loading providers...</SelectItem>
                ) : (
                  Object.entries(groupedProviders).map(([group, items]) => (
                    <SelectGroup key={group}>
                      <SelectLabel className="text-xs font-bold text-muted-foreground uppercase tracking-wider">
                        {group}
                      </SelectLabel>
                      {items.map((p) => (
                        <SelectItem key={p.id} value={p.id}>
                          {p.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  ))
                )}
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label htmlFor="gen-prompt">Prompt</Label>
            <Textarea
              id="gen-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Describe the video you want to generate..."
              className="mt-1 min-h-24 resize-y"
              required
            />
          </div>

          <div>
            <Label>Upload Image or Video (optional)</Label>
            <div
              className={`relative mt-1 border-2 border-dashed rounded-[var(--border-radius)] p-4 text-center cursor-pointer transition-colors ${
                dragOver
                  ? 'border-primary bg-primary/5'
                  : mediaFile
                    ? 'border-primary bg-primary/10'
                    : 'border-border hover:border-muted-foreground hover:bg-muted'
              }`}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
            >
              {mediaFile && (
                <button
                  type="button"
                  onClick={clearFile}
                  className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full border-2 border-border bg-card hover:bg-muted text-foreground text-xs flex items-center justify-center"
                >
                  ×
                </button>
              )}
              <input type="file" ref={fileInputRef} onChange={handleFileChange} className="hidden" accept="image/*,video/*" />
              {mediaFile ? (
                <div className="text-sm text-primary font-bold truncate">{mediaFile.name}</div>
              ) : (
                <>
                  <div className="mb-0.5 text-muted-foreground"><UploadSimpleIcon size={20} weight="bold" /></div>
                  <div className="text-muted-foreground text-sm">
                    Drop file or <strong className="text-foreground">click to browse</strong>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Count — always shown */}
          <div>
            <Label htmlFor="gen-count">Concurrent</Label>
            <Input
              id="gen-count"
              type="number"
              min="1"
              max="20"
              value={count}
              onChange={(e) => setCount(parseInt(e.target.value) || 1)}
              className="mt-1"
            />
          </div>

          {/* Schema-driven provider controls */}
          {schema && (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                {Object.entries(schema)
                  .filter(([k]) => k !== '_advanced' && k !== 'image_required')
                  .map(([key, field]) => {
                    const f = field as SchemaField;
                    const val = extraParams[key];
                    switch (f.type) {
                      case 'select':
                        return (
                          <div key={key}>
                            <Label>{f.label}</Label>
                            <Select
                              value={String(val ?? f.default)}
                              onValueChange={(v) => handleParamChange(key, f.options?.every((o) => typeof o === 'number') ? Number(v) : v)}
                            >
                              <SelectTrigger className="w-full mt-1"><SelectValue /></SelectTrigger>
                              <SelectContent>
                                {f.options?.map((opt) => (
                                  <SelectItem key={String(opt)} value={String(opt)}>{String(opt)}</SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            {f.note && <p className="text-[11px] text-muted-foreground mt-0.5">{f.note}</p>}
                          </div>
                        );
                      case 'range':
                        return (
                          <div key={key}>
                            <Label>{f.label}: {String(val ?? f.default)}</Label>
                            <input
                              type="range"
                              min={f.min}
                              max={f.max}
                              step={f.step ?? 1}
                              value={Number(val ?? f.default)}
                              onChange={(e) => handleParamChange(key, Number(e.target.value))}
                              className="mt-1 w-full accent-primary"
                            />
                            {f.note && <p className="text-[11px] text-muted-foreground mt-0.5">{f.note}</p>}
                          </div>
                        );
                      case 'toggle':
                        return (
                          <div key={key} className="flex items-center justify-between col-span-2">
                            <Label>{f.label}</Label>
                            <button
                              type="button"
                              role="switch"
                              aria-checked={Boolean(val ?? f.default)}
                              onClick={() => handleParamChange(key, !(val ?? f.default))}
                              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                                Boolean(val ?? f.default) ? 'bg-primary' : 'bg-muted-foreground/30'
                              }`}
                            >
                              <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
                                Boolean(val ?? f.default) ? 'translate-x-[18px]' : 'translate-x-[2px]'
                              }`} />
                            </button>
                          </div>
                        );
                      default:
                        return null;
                    }
                  })}
              </div>

              {/* Image required notice for i2v models */}
              {schema.image_required && !mediaFile && (
                <Card className="border-amber-400 bg-amber-50">
                  <CardContent className="py-2 text-sm text-amber-800">
                    This model requires an image. Upload one above.
                  </CardContent>
                </Card>
              )}

              {/* Last frame image upload for wan-i2v-fast */}
              {schema.last_image_supported && (
                <div>
                  <Label>Last Frame Image (optional)</Label>
                  <div
                    className={`relative mt-1 border-2 border-dashed rounded-[var(--border-radius)] p-3 text-center cursor-pointer transition-colors ${
                      lastImageFile
                        ? 'border-primary bg-primary/10'
                        : 'border-border hover:border-muted-foreground hover:bg-muted'
                    }`}
                    onClick={() => lastImageInputRef.current?.click()}
                  >
                    {lastImageFile && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setLastImageFile(null);
                          if (lastImageInputRef.current) lastImageInputRef.current.value = '';
                        }}
                        className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full border-2 border-border bg-card hover:bg-muted text-foreground text-xs flex items-center justify-center"
                      >
                        ×
                      </button>
                    )}
                    <input
                      type="file"
                      ref={lastImageInputRef}
                      onChange={(e) => {
                        if (e.target.files?.[0]) setLastImageFile(e.target.files[0]);
                      }}
                      className="hidden"
                      accept="image/*"
                    />
                    {lastImageFile ? (
                      <div className="text-sm text-primary font-bold truncate">{lastImageFile.name}</div>
                    ) : (
                      <div className="text-muted-foreground text-xs">
                        Drop or <strong className="text-foreground">click</strong> to upload last frame
                      </div>
                    )}
                  </div>
                  <p className="text-[11px] text-muted-foreground mt-0.5">Guide where the animation ends</p>
                </div>
              )}

              {/* Advanced toggle */}
              {schema._advanced && (
                <>
                  <button
                    type="button"
                    onClick={() => setAdvancedOpen(!advancedOpen)}
                    className="text-xs font-bold text-muted-foreground hover:text-primary transition-colors flex items-center gap-1"
                  >
                    <span className={`inline-flex transition-transform ${advancedOpen ? 'rotate-90' : ''}`}><PlayIcon size={10} weight="fill" /></span>
                    Advanced Settings
                  </button>
                  {advancedOpen && (
                    <div className="space-y-3 pl-3 border-l-2 border-border">
                      {Object.entries(schema._advanced as Record<string, SchemaField>).map(([key, f]) => {
                        const val = extraParams[key];
                        switch (f.type) {
                          case 'range':
                            return (
                              <div key={key}>
                                <Label className="text-xs">{f.label}: {String(val ?? f.default)}</Label>
                                <input
                                  type="range"
                                  min={f.min}
                                  max={f.max}
                                  step={f.step ?? 1}
                                  value={Number(val ?? f.default)}
                                  onChange={(e) => handleParamChange(key, Number(e.target.value))}
                                  className="mt-1 w-full accent-primary"
                                />
                              </div>
                            );
                          case 'toggle':
                            return (
                              <div key={key} className="flex items-center justify-between">
                                <Label className="text-xs">{f.label}</Label>
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={Boolean(val ?? f.default)}
                                  onClick={() => handleParamChange(key, !(val ?? f.default))}
                                  className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                                    Boolean(val ?? f.default) ? 'bg-primary' : 'bg-muted-foreground/30'
                                  }`}
                                >
                                  <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
                                    Boolean(val ?? f.default) ? 'translate-x-[18px]' : 'translate-x-[2px]'
                                  }`} />
                                </button>
                              </div>
                            );
                          case 'text':
                            return (
                              <div key={key}>
                                <Label className="text-xs">{f.label}</Label>
                                <Input
                                  type="text"
                                  value={String(val ?? '')}
                                  onChange={(e) => handleParamChange(key, e.target.value || undefined)}
                                  placeholder={f.placeholder}
                                  className="mt-1 text-xs"
                                />
                              </div>
                            );
                          default:
                            return null;
                        }
                      })}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {error && (
            <Card className="border-destructive bg-red-50">
              <CardContent className="py-3 text-sm text-red-800">{error}</CardContent>
            </Card>
          )}

          {(() => {
            const cropModeRaw = extraParams.crop_mode;
            const cropMode = typeof cropModeRaw === 'string' ? cropModeRaw : undefined;
            const mult = cropMultiplier(cropMode);
            const expected = count * mult;
            if (mult <= 1) return null;
            return (
              <div className="flex items-center justify-between rounded-[var(--border-radius)] border-2 border-primary/30 bg-primary/5 px-3 py-2 text-xs">
                <span className="text-muted-foreground">
                  {count} × <span className="font-bold text-foreground">{mult}-crop</span>
                </span>
                <span className="font-bold text-foreground">
                  = {expected} output files
                </span>
              </div>
            );
          })()}

          <Button
            type="submit"
            disabled={loading || !selectedProvider}
            className="w-full"
          >
            {loading ? 'Submitting...' : (() => {
              const cropModeRaw = extraParams.crop_mode;
              const cropMode = typeof cropModeRaw === 'string' ? cropModeRaw : undefined;
              const expected = count * cropMultiplier(cropMode);
              if (expected === count) {
                return `Generate (${count} video${count === 1 ? '' : 's'})`;
              }
              return `Generate (${count} × ${cropMultiplier(cropMode)} = ${expected} files)`;
            })()}
          </Button>
        </form>

        {/* Color correction — applies to every done video in the feed */}
        <Card className="mt-6 border-primary/30">
          <CardContent className="py-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-xs font-bold uppercase tracking-wider text-foreground">Color Correction</div>
              <button
                type="button"
                onClick={() => setBatchCc(DEFAULT_COLOR_CORRECTION)}
                disabled={!getColorCorrectionOrNull(batchCc)}
                className="text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Reset
              </button>
            </div>
            <div className="space-y-1.5">
              {CC_SLIDERS.map((slider) => {
                const v = batchCc[slider.key];
                return (
                  <div key={slider.key} className="flex items-center gap-2">
                    <span className="min-w-[72px] text-[11px] text-muted-foreground">{slider.label}</span>
                    <input
                      type="range"
                      min={slider.min}
                      max={slider.max}
                      value={v}
                      onChange={(e) =>
                        setBatchCc((prev) => ({
                          ...prev,
                          [slider.key]: Number.parseInt(e.target.value, 10) || 0,
                        }))
                      }
                      className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-primary"
                    />
                    <span className="min-w-[28px] text-right text-[11px] font-bold tabular-nums text-foreground">{v}</span>
                  </div>
                );
              })}
            </div>
            <Button
              type="button"
              size="sm"
              onClick={runBatchColorCorrect}
              disabled={batchCcBusy || allDoneVideoPaths.length === 0}
              className="mt-3 w-full"
            >
              {batchCcBusy
                ? 'Preparing ZIP…'
                : allDoneVideoPaths.length === 0
                  ? 'No videos yet'
                  : `Download all (${allDoneVideoPaths.length})${getColorCorrectionOrNull(batchCc) ? ' with CC' : ''}`}
            </Button>
          </CardContent>
        </Card>

        {/* Prompt history */}
        <div className="mt-6 border-t-2 border-border pt-4">
          <button
            type="button"
            onClick={() => setHistoryOpen(!historyOpen)}
            className="flex items-center justify-between w-full text-left group"
          >
            <span className="text-sm font-bold text-foreground group-hover:text-primary transition-colors">
              Recent Prompts
              {promptHistory.length > 0 && (
                <span className="ml-1.5 text-[11px] text-muted-foreground font-base">({promptHistory.length})</span>
              )}
            </span>
            <span className={`text-muted-foreground text-xs transition-transform ${historyOpen ? 'rotate-0' : '-rotate-90'}`}>
              ▼
            </span>
          </button>

          {historyOpen && (
            <div className="mt-3">
              {promptHistory.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">No prompts yet. Generate a video to start building history.</p>
              ) : (
                <>
                  <ScrollArea className="max-h-[320px]">
                    <div className="space-y-1.5 pr-1">
                      {promptHistory.map((entry, i) => (
                        <button
                          key={`${entry.job_id}-${i}`}
                          type="button"
                          onClick={() => applyPromptEntry(entry)}
                          className="w-full text-left rounded-[var(--border-radius)] px-3 py-2 border-2 border-border bg-card hover:bg-muted hover:shadow-[2px_2px_0_0_var(--border)] transition-all group"
                        >
                          <div className="text-[13px] text-foreground group-hover:text-primary leading-snug line-clamp-2">
                            {entry.prompt}
                          </div>
                          <div className="flex items-center gap-2 mt-1 text-[11px] text-muted-foreground">
                            <span className="text-primary font-bold">{PROVIDER_SHORT[entry.provider] || entry.provider}</span>
                            <span>·</span>
                            <span>{entry.count}x</span>
                            <span>·</span>
                            <span>{entry.duration}s</span>
                            <span>·</span>
                            <span>{entry.aspect_ratio}</span>
                            <span className="ml-auto">{timeAgo(entry.timestamp)}</span>
                          </div>
                        </button>
                      ))}
                    </div>
                  </ScrollArea>
                  <button
                    type="button"
                    onClick={clearHistory}
                    className="mt-2 text-[11px] text-muted-foreground hover:text-destructive transition-colors font-bold"
                  >
                    Clear history
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Right panel — active jobs + completed */}
      <div className="flex-1 p-6 overflow-y-auto bg-background">
        <div className="max-w-6xl mx-auto">
          <div className="flex items-center justify-between mb-6">
            <div className="text-xl font-heading text-foreground">Active Jobs</div>
            <div className="flex items-center gap-2">
              {generateJobs.some((j) => j.videos.some((v) => v.status === 'done')) && (
                <Button
                  variant={selectMode ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => { setSelectMode((v) => !v); if (selectMode) setSelectedVideos(new Set()); }}
                >
                  {selectMode ? 'Cancel Select' : 'Select Videos'}
                </Button>
              )}
              {generateJobs.length > 0 && (
                <Badge variant="info">{generateJobs.filter((j) => j.videos.some((v) => !isTerminal(v))).length} running</Badge>
              )}
            </div>
          </div>

          {generateJobs.length === 0 ? (
            <EmptyState icon={<PlayIcon size={48} weight="duotone" />} title="No Videos Yet" description="Enter a prompt and hit Generate." />
          ) : (
            <div className="space-y-6">
              {liveJobs.length === 0 && archivedJobs.length > 0 && (
                <div className="rounded-[var(--border-radius)] border-2 border-dashed border-border bg-muted/30 px-4 py-6 text-center text-sm text-muted-foreground">
                  All caught up. {archivedJobs.length} archived {archivedJobs.length === 1 ? 'job' : 'jobs'} below.
                </div>
              )}
              {/* Group live jobs by date */}
              {(() => {
                const groups = new Map<string, Job[]>();
                for (const job of liveJobs) {
                  const key = getDateKey(job.created_at);
                  groups.set(key, [...(groups.get(key) || []), job]);
                }
                return Array.from(groups.entries()).map(([dateLabel, dateJobs]) => {
                  const dateFileCount = dateJobs.reduce((n, j) => n + deliveredFileCount(j.videos), 0);
                  return (
                    <div key={dateLabel}>
                      <div className="flex items-center gap-3 mb-3">
                        <div className="text-sm font-bold text-muted-foreground uppercase tracking-wider">{dateLabel}</div>
                        <div className="flex-1 h-px bg-border" />
                        <Badge variant="secondary" className="text-[10px]">
                          {dateFileCount} {dateFileCount === 1 ? 'file' : 'files'}
                        </Badge>
                      </div>
                      <div className="space-y-3">
                        {dateJobs.map((job) => renderJobCard(job, 'live'))}
                      </div>
                    </div>
                  );
                });
              })()}

              {archivedJobs.length > 0 && (
                <div className="pt-4 border-t border-border">
                  <button
                    type="button"
                    onClick={() => setShowArchived((v) => !v)}
                    className="flex items-center gap-2 text-sm font-bold text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <span className={`text-xs transition-transform ${showArchived ? 'rotate-0' : '-rotate-90'}`}>▼</span>
                    Archived ({archivedJobs.length})
                  </button>
                  {showArchived && (
                    <div className="mt-4 space-y-3">
                      {archivedJobs.map((job) => renderJobCard(job, 'archived'))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Floating selection bar — send-to-Burn only; CC lives in the sidebar */}
        {selectMode && (
          <div className="sticky bottom-0 left-0 right-0 z-30 flex items-center justify-between border-t-2 border-border bg-card px-6 py-3 shadow-[0_-2px_8px_rgba(0,0,0,0.1)]">
            <div className="flex items-center gap-3">
              <Badge variant="secondary">{selectedVideos.size} selected</Badge>
              <Button variant="ghost" size="sm" onClick={selectAllVideos}>
                {selectedVideos.size === generateJobs.reduce((n, j) => n + j.videos.filter((v) => v.status === 'done' && v.file).length, 0) ? 'Deselect All' : 'Select All'}
              </Button>
            </div>
            <Button size="sm" disabled={selectedVideos.size === 0} onClick={sendSelectedToBurn}>
              Send {selectedVideos.size} to Burn <ArrowRightIcon size={14} weight="bold" className="inline ml-1" />
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
