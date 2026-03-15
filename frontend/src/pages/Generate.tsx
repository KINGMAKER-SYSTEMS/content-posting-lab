import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiUrl, staticUrl } from '../lib/api';
import { useWorkflowStore } from '../stores/workflowStore';
import type { Provider, Job, VideoEntry, ProviderSchemas, SchemaField } from '../types/api';
import { EmptyState, LazyVideo, ProgressBar } from '../components';
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

const PROVIDER_SHORT: Record<string, string> = {
  'hailuo': 'Hailuo',
  'wan-t2v': 'Wan',
  'wan-i2v': 'Wan',
  'wan-i2v-fast': 'Wan',
  'grok': 'Grok',
};

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

function startPolling(
  jobId: string,
  onUpdate: (job: Job) => void,
  onComplete: (job: Job) => void,
) {
  if (activePolls.has(jobId)) return;
  activePolls.add(jobId);
  const startedAt = Date.now();
  let consecutiveErrors = 0;

  const tick = async () => {
    if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
      activePolls.delete(jobId);
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
      onUpdate(job);

      const allDone = job.videos.every(isTerminal);
      if (!allDone) {
        setTimeout(tick, 2000);
      } else {
        activePolls.delete(jobId);
        onComplete(job);
      }
    } catch {
      consecutiveErrors++;
      if (consecutiveErrors >= 10) {
        activePolls.delete(jobId);
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
  } = useWorkflowStore();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [prompt, setPrompt] = useState('');
  const [selectedProvider, setSelectedProvider] = useState('');
  const [count, setCount] = useState(1);
  const [duration, setDuration] = useState(10);
  const [aspectRatio, setAspectRatio] = useState('9:16');
  const [resolution, setResolution] = useState('720p');
  const [mediaFile, setMediaFile] = useState<File | null>(null);
  const [lastImageFile, setLastImageFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);

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

  if (!activeProjectName) {
    return (
      <div className="h-full flex items-center justify-center">
        <EmptyState icon="📁" title="No Project Selected" description="Please select or create a project to start generating videos." />
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
                  <div className="text-lg mb-0.5">⇪</div>
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
                    <span className={`transition-transform ${advancedOpen ? 'rotate-90' : ''}`}>&#9654;</span>
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

          <Button
            type="submit"
            disabled={loading || !selectedProvider}
            className="w-full"
          >
            {loading ? 'Submitting...' : 'Generate'}
          </Button>
        </form>

        {/* Quick tip */}
        <Card className="mt-6 bg-primary/10 border-primary/30">
          <CardContent className="py-3">
            <div className="text-xs font-bold uppercase tracking-wider text-foreground mb-1">Quick Tip</div>
            <p className="text-xs text-muted-foreground">
              Use specific visual descriptions. "Cinematic close-up of rain hitting neon-lit pavement" works better than "rainy city".
            </p>
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
            {generateJobs.length > 0 && (
              <Badge variant="info">{generateJobs.filter((j) => j.videos.some((v) => !isTerminal(v))).length} running</Badge>
            )}
          </div>

          {generateJobs.length === 0 ? (
            <EmptyState icon="▶" title="No Videos Yet" description="Enter a prompt and hit Generate." />
          ) : (
            <div className="space-y-6">
              {generateJobs.map((job) => {
                const doneCount = job.videos.filter((v) => v.status === 'done').length;
                const errorCount = job.videos.filter((v) => v.status === 'error' || v.status === 'failed').length;
                const total = job.count;
                const allFinished = doneCount + errorCount === total;
                const pct = Math.round(((doneCount + errorCount) / total) * 100);

                return (
                  <Card key={job.id}>
                    <CardContent className="pt-0">
                      <div className="flex items-center justify-between mb-2">
                        <div className="text-sm text-muted-foreground italic truncate max-w-[60%]" title={job.prompt}>
                          "{job.prompt}" <span className="text-foreground font-bold">[{job.provider}]</span>
                        </div>
                        <Badge variant={allFinished && errorCount === 0 ? 'success' : 'info'}>
                          {doneCount}/{total} done
                        </Badge>
                      </div>

                      <ProgressBar
                        value={pct}
                        color={allFinished && errorCount === 0 ? 'success' : 'primary'}
                        className="mb-3"
                      />

                      <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 mb-2">
                        {job.videos.flatMap((video, idx) => {
                          // If crops exist, render each crop as a separate card
                          const items = video.crops && video.crops.length > 0
                            ? video.crops.map((crop, ci) => ({ key: `${job.id}-${idx}-crop${ci}`, url: crop.url, file: crop.file, label: `Crop ${ci + 1}`, video, idx }))
                            : [{ key: `${job.id}-${idx}`, url: video.url, file: video.file, label: undefined, video, idx }];

                          return items.map(({ key, url, file, label, video: v, idx: vIdx }) => (
                          <div
                            key={key}
                            className="rounded-[var(--border-radius)] overflow-hidden border-2 border-border bg-card shadow-[2px_2px_0_0_var(--border)] hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[1px_1px_0_0_var(--border)] transition-all"
                          >
                            <div className="relative bg-muted aspect-[9/16] flex items-center justify-center">
                              {v.status === 'done' && url ? (
                                <LazyVideo
                                  src={staticUrl(url)}
                                  className="w-full h-full object-cover"
                                />
                              ) : v.status === 'error' || v.status === 'failed' ? (
                                <div className="flex flex-col items-center gap-2 text-muted-foreground text-sm">
                                  <span className="text-2xl">✕</span>
                                  <span className="font-bold">Failed</span>
                                </div>
                              ) : (
                                <div className="flex flex-col items-center gap-2 text-muted-foreground text-sm">
                                  {v.status === 'queued' ? (
                                    <span className="text-2xl opacity-30">▶</span>
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
                            </div>

                            <div className="px-2.5 py-1.5 flex items-center justify-between text-xs">
                              <Badge variant={statusVariant(v.status)} className="text-[10px] shadow-none">
                                {statusLabel(v.status)}
                              </Badge>
                              <div className="flex items-center gap-2">
                                {v.status === 'done' && file && (
                                  <button
                                    type="button"
                                    onClick={async (e) => {
                                      e.stopPropagation();
                                      const project = job.project || 'quick-test';
                                      try {
                                        const r = await fetch(apiUrl(`/api/video/file?project=${encodeURIComponent(project)}&path=${encodeURIComponent(file!)}`), { method: 'DELETE' });
                                        if (!r.ok) throw new Error('Failed');
                                        setGenerateJob({
                                          ...job,
                                          videos: job.videos.map((vv, vi) => vi === vIdx ? { ...vv, status: 'failed' as const, error: 'Deleted', file: undefined, url: undefined, crops: undefined } : vv),
                                        });
                                        addNotification('success', 'Video deleted');
                                      } catch {
                                        addNotification('error', 'Failed to delete video');
                                      }
                                    }}
                                    className="text-muted-foreground hover:text-destructive transition-colors font-bold text-[11px]"
                                    title="Delete video"
                                  >
                                    Delete
                                  </button>
                                )}
                                {v.status === 'done' && url && (
                                  <a href={staticUrl(url)} download className="text-primary hover:underline font-bold text-xs">
                                    Download
                                  </a>
                                )}
                              </div>
                            </div>

                            {v.error && (
                              <div className="px-2.5 pb-1.5 text-[11px] text-destructive break-all">{v.error}</div>
                            )}
                          </div>
                          ));
                        })}
                      </div>

                      {doneCount > 0 && (
                        <div className="flex gap-2 justify-end mt-2">
                          <Button variant="secondary" size="sm" onClick={() => sendSelectionToBurn(job)}>
                            Use in Burn →
                          </Button>
                          {doneCount > 1 && (
                            <Button asChild size="sm" variant="outline">
                              <a href={apiUrl(`/api/video/jobs/${job.id}/download-all`)} download>
                                Download All ({doneCount})
                              </a>
                            </Button>
                          )}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
