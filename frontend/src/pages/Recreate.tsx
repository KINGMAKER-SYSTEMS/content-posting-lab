import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiUrl, wsUrl } from '../lib/api';
import { EmptyState } from '../components';
import { useWebSocket, type WebSocketStatus } from '../hooks/useWebSocket';
import { useWorkflowStore } from '../stores/workflowStore';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ScrollArea } from '@/components/ui/scroll-area';

function getStatusBadge(status: WebSocketStatus) {
  if (status === 'connected') return { label: 'CONNECTED', variant: 'success' as const };
  if (status === 'reconnecting') return { label: 'RECONNECTING', variant: 'warning' as const };
  if (status === 'connecting') return { label: 'CONNECTING', variant: 'info' as const };
  if (status === 'error') return { label: 'DISCONNECTED', variant: 'error' as const };
  return { label: 'IDLE', variant: 'secondary' as const };
}

function tsNow(): string {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}

interface FrameState {
  firstOriginal: string | null;
  lastOriginal: string | null;
  firstClean: string | null;
  lastClean: string | null;
  duration: number | null;
}

interface PastJob {
  job_id: string;
  first_clean: string | null;
  last_clean: string | null;
  first_original: string | null;
  last_original: string | null;
}

interface RecreateWSMessage {
  event: string;
  video_id?: string;
  duration?: number;
  first_frame?: string;
  last_frame?: string;
  first_clean?: string;
  last_clean?: string;
  first_original?: string;
  last_original?: string;
  error?: string;
  text?: string;
  message?: string;
}

const EMPTY_FRAMES: FrameState = {
  firstOriginal: null,
  lastOriginal: null,
  firstClean: null,
  lastClean: null,
  duration: null,
};

export function RecreatePage() {
  const { activeProjectName, addNotification, setRecreateJobActive, primeGeneratePrefill } = useWorkflowStore();
  const navigate = useNavigate();

  const [videoUrl, setVideoUrl] = useState('');
  const [running, setRunning] = useState(false);
  const [complete, setComplete] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [frames, setFrames] = useState<FrameState>(EMPTY_FRAMES);
  const [generatedPrompt, setGeneratedPrompt] = useState<string | null>(null);
  const [promptLoading, setPromptLoading] = useState(false);
  const [resultPaths, setResultPaths] = useState<{ firstClean: string | null; lastClean: string | null }>({
    firstClean: null,
    lastClean: null,
  });
  const [pastJobs, setPastJobs] = useState<PastJob[]>([]);

  const jobIdRef = useRef<string | null>(null);
  const pipelineCompleteRef = useRef(false);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const runningRef = useRef(running);
  runningRef.current = running;

  const addLog = useCallback((msg: string) => {
    setLogs((prev) => [...prev, `[${tsNow()}] ${msg}`]);
  }, []);

  const fetchPastJobs = useCallback(() => {
    if (!activeProjectName) return;
    fetch(apiUrl(`/api/recreate/jobs?project=${encodeURIComponent(activeProjectName)}`))
      .then((r) => (r.ok ? r.json() : { jobs: [] }))
      .then((data: { jobs: PastJob[] }) => setPastJobs(data.jobs || []))
      .catch(() => {});
  }, [activeProjectName]);

  useEffect(() => {
    fetchPastJobs();
  }, [fetchPastJobs]);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  // Build WebSocket URL only when running
  const wsEndpoint = running && jobIdRef.current
    ? wsUrl(`/api/recreate/ws/${jobIdRef.current}`)
    : null;

  const { status, sendMessage, clearStartPayload } = useWebSocket(wsEndpoint, {
    onOpen: () => {
      if (!activeProjectName) return;
      addLog('Connected - starting pipeline');
      sendMessage({
        action: 'start',
        video_url: videoUrl,
        project: activeProjectName,
      });
    },
    onMessage: (event) => {
      try {
        const data = JSON.parse(event.data) as RecreateWSMessage;
        handleMessage(data);
      } catch {
        addNotification('error', 'Failed to parse WebSocket message from recreate server.');
      }
    },
    onError: () => {
      addLog('Connection error');
      addNotification('error', 'Recreate WebSocket connection failed.');
    },
    shouldReconnect: () => !pipelineCompleteRef.current,
  });

  const handleMessage = useCallback((data: RecreateWSMessage) => {
    switch (data.event) {
      case 'downloading':
        addLog(`Downloading video${data.video_id ? ` (${data.video_id})` : ''}...`);
        break;
      case 'downloaded':
        addLog('Video downloaded successfully');
        if (data.duration != null) {
          setFrames((prev) => ({ ...prev, duration: data.duration ?? null }));
        }
        break;
      case 'extracting_frames':
        addLog('Extracting first and last frames...');
        break;
      case 'frames_ready':
        addLog('Frames extracted');
        setFrames((prev) => ({
          ...prev,
          firstOriginal: data.first_frame || data.first_original || null,
          lastOriginal: data.last_frame || data.last_original || null,
          duration: data.duration ?? prev.duration,
        }));
        break;
      case 'removing_text':
        addLog('Removing burned-in text from frames...');
        break;
      case 'text_removed':
        addLog('Text removal complete');
        setFrames((prev) => ({
          ...prev,
          firstClean: data.first_clean || null,
          lastClean: data.last_clean || null,
        }));
        setResultPaths({
          firstClean: data.first_clean || null,
          lastClean: data.last_clean || null,
        });
        break;
      case 'complete':
        addLog('Pipeline complete');
        pipelineCompleteRef.current = true;
        clearStartPayload();
        setRunning(false);
        setComplete(true);
        setRecreateJobActive(false);
        // Update frames from complete event if provided
        if (data.first_clean || data.last_clean) {
          setFrames((prev) => ({
            ...prev,
            firstClean: data.first_clean || prev.firstClean,
            lastClean: data.last_clean || prev.lastClean,
            firstOriginal: data.first_original || prev.firstOriginal,
            lastOriginal: data.last_original || prev.lastOriginal,
          }));
          setResultPaths({
            firstClean: data.first_clean || null,
            lastClean: data.last_clean || null,
          });
        }
        fetchPastJobs();
        break;
      case 'error':
        addLog(`Error: ${data.error || data.message || 'Unknown error'}`);
        pipelineCompleteRef.current = true;
        clearStartPayload();
        setRunning(false);
        setRecreateJobActive(false);
        addNotification('error', data.error || data.message || 'Recreate pipeline failed');
        break;
      default:
        if (data.text || data.message) {
          addLog(data.text || data.message || '');
        }
        break;
    }
  }, [addLog, addNotification, clearStartPayload, fetchPastJobs, setRecreateJobActive]);

  const handleStart = () => {
    if (!videoUrl.trim() || !activeProjectName) return;
    const newJobId = crypto.randomUUID();
    jobIdRef.current = newJobId;
    pipelineCompleteRef.current = false;
    setLogs([]);
    setFrames(EMPTY_FRAMES);
    setResultPaths({ firstClean: null, lastClean: null });
    setGeneratedPrompt(null);
    setComplete(false);
    setRunning(true);
    setRecreateJobActive(true);
  };

  const viewPastJob = (job: PastJob) => {
    setFrames({
      firstOriginal: job.first_original || null,
      lastOriginal: job.last_original || null,
      firstClean: job.first_clean || null,
      lastClean: job.last_clean || null,
      duration: null,
    });
    setResultPaths({
      firstClean: job.first_clean || null,
      lastClean: job.last_clean || null,
    });
    setGeneratedPrompt(null);
    setComplete(true);
    setLogs([]);
  };

  const deletePastJob = async (jobId: string) => {
    if (!activeProjectName) return;
    try {
      await fetch(apiUrl(`/api/recreate/jobs/${jobId}?project=${encodeURIComponent(activeProjectName)}`), {
        method: 'DELETE',
      });
      setPastJobs((prev) => prev.filter((j) => j.job_id !== jobId));
    } catch {
      addNotification('error', 'Failed to delete job');
    }
  };

  const statusBadge = getStatusBadge(status);
  const hasFrames = frames.firstOriginal || frames.lastOriginal || frames.firstClean || frames.lastClean;

  if (!activeProjectName) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState
          icon="&#128193;"
          title="No Project Selected"
          description="Please select or create a project to start recreating videos."
        />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden lg:flex-row">
      {/* Left sidebar — controls */}
      <div className="w-full flex-shrink-0 overflow-y-auto border-r-2 border-border bg-card p-6 lg:w-[380px]">
        <h2 className="mb-1 text-xl font-heading text-foreground">Recreate</h2>
        <p className="mb-6 text-xs text-muted-foreground">
          Download a TikTok video, extract frames, and remove burned-in text
        </p>

        <div className="space-y-4">
          <div>
            <Label htmlFor="recreate-url">TikTok Video URL</Label>
            <Input
              id="recreate-url"
              value={videoUrl}
              onChange={(e) => setVideoUrl(e.target.value)}
              placeholder="https://www.tiktok.com/@user/video/..."
              className="mt-1"
              disabled={running}
            />
          </div>

          <Button
            onClick={handleStart}
            disabled={!videoUrl.trim() || running}
            className="w-full"
          >
            {running ? 'Processing...' : 'Extract & Clean'}
          </Button>

          {/* Processing indicator */}
          {running && (
            <Card>
              <CardContent className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted border-t-primary" />
                <span className="truncate">Pipeline running...</span>
                <Badge variant={statusBadge.variant} className="ml-auto text-[10px] shadow-none">
                  {statusBadge.label}
                </Badge>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Activity log */}
        <div className="mt-6">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
              Activity Log
            </span>
            {!running && logs.length > 0 && (
              <button
                type="button"
                onClick={() => setLogs([])}
                className="text-[11px] text-muted-foreground hover:text-destructive transition-colors font-bold"
              >
                Clear
              </button>
            )}
          </div>

          <ScrollArea className="h-52 rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 font-mono text-xs text-muted-foreground">
            {logs.length === 0 ? (
              <span className="italic">Waiting to start...</span>
            ) : (
              logs.map((log, i) => (
                <div key={`${i}-${log.slice(0, 20)}`} className="mb-1 break-all border-b border-border pb-1 last:border-0">
                  {log}
                </div>
              ))
            )}
            <div ref={logsEndRef} />
          </ScrollArea>
        </div>

        {/* Previous jobs */}
        <div className="mt-6 border-t-2 border-border pt-4">
          <span className="text-sm font-bold text-foreground">
            Previous Jobs
            {pastJobs.length > 0 && (
              <span className="ml-1.5 text-[11px] text-muted-foreground font-normal">
                ({pastJobs.length})
              </span>
            )}
          </span>

          <div className="mt-3 space-y-1.5">
            {pastJobs.length === 0 ? (
              <p className="text-xs text-muted-foreground italic">
                No previous jobs. Run a recreate pipeline to start.
              </p>
            ) : (
              pastJobs.map((job) => (
                <div
                  key={job.job_id}
                  className="group flex items-center gap-2 rounded-[var(--border-radius)] border-2 border-border bg-card px-3 py-2 hover:bg-muted hover:shadow-[2px_2px_0_0_var(--border)] transition-all"
                >
                  <button
                    type="button"
                    onClick={() => viewPastJob(job)}
                    className="flex-1 text-left text-xs text-foreground group-hover:text-primary truncate"
                  >
                    {job.job_id.slice(0, 8)}...
                  </button>
                  <button
                    type="button"
                    onClick={() => deletePastJob(job.job_id)}
                    className="w-5 h-5 flex items-center justify-center rounded-full text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors text-xs opacity-0 group-hover:opacity-100"
                  >
                    x
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Right panel — frame results */}
      <div className="flex-1 overflow-y-auto p-6 bg-background">
        <div className="mx-auto max-w-5xl">
          <div className="mb-6 flex items-center justify-between">
            <h2 className="text-xl font-heading text-foreground">Frame Results</h2>
            {complete && hasFrames && (
              <Badge variant="success">Complete</Badge>
            )}
          </div>

          {!hasFrames ? (
            <EmptyState
              icon="&#127910;"
              title="No Frames Yet"
              description="Enter a TikTok video URL and click Extract & Clean to extract and process frames."
            />
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* First Frame column */}
              <div className="space-y-4">
                <h3 className="text-sm font-bold text-foreground uppercase tracking-wider">
                  First Frame
                </h3>

                {/* Original */}
                <Card>
                  <CardContent className="p-0">
                    <div className="px-3 py-2 border-b-2 border-border">
                      <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                        Original
                      </span>
                    </div>
                    <div className="aspect-[9/16] bg-muted flex items-center justify-center">
                      {frames.firstOriginal ? (
                        <img
                          src={frames.firstOriginal}
                          alt="First frame original"
                          className="w-full h-full object-contain"
                        />
                      ) : (
                        <span className="text-muted-foreground text-sm">Waiting...</span>
                      )}
                    </div>
                  </CardContent>
                </Card>

                {/* Cleaned */}
                <Card>
                  <CardContent className="p-0">
                    <div className="px-3 py-2 border-b-2 border-border flex items-center justify-between">
                      <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                        Cleaned
                      </span>
                      {resultPaths.firstClean && (
                        <a
                          href={resultPaths.firstClean}
                          download
                          className="text-[11px] text-primary hover:underline font-bold"
                        >
                          Download
                        </a>
                      )}
                    </div>
                    <div className="aspect-[9/16] bg-muted flex items-center justify-center">
                      {frames.firstClean ? (
                        <img
                          src={frames.firstClean}
                          alt="First frame cleaned"
                          className="w-full h-full object-contain"
                        />
                      ) : running ? (
                        <div className="flex flex-col items-center gap-2 text-muted-foreground">
                          <div className="w-7 h-7 border-3 border-muted border-t-primary rounded-full animate-spin" />
                          <span className="text-xs">Removing text...</span>
                        </div>
                      ) : (
                        <span className="text-muted-foreground text-sm">--</span>
                      )}
                    </div>
                  </CardContent>
                </Card>
              </div>

              {/* Last Frame column */}
              <div className="space-y-4">
                <h3 className="text-sm font-bold text-foreground uppercase tracking-wider">
                  Last Frame
                </h3>

                {/* Original */}
                <Card>
                  <CardContent className="p-0">
                    <div className="px-3 py-2 border-b-2 border-border">
                      <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                        Original
                      </span>
                    </div>
                    <div className="aspect-[9/16] bg-muted flex items-center justify-center">
                      {frames.lastOriginal ? (
                        <img
                          src={frames.lastOriginal}
                          alt="Last frame original"
                          className="w-full h-full object-contain"
                        />
                      ) : (
                        <span className="text-muted-foreground text-sm">Waiting...</span>
                      )}
                    </div>
                  </CardContent>
                </Card>

                {/* Cleaned */}
                <Card>
                  <CardContent className="p-0">
                    <div className="px-3 py-2 border-b-2 border-border flex items-center justify-between">
                      <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                        Cleaned
                      </span>
                      {resultPaths.lastClean && (
                        <a
                          href={resultPaths.lastClean}
                          download
                          className="text-[11px] text-primary hover:underline font-bold"
                        >
                          Download
                        </a>
                      )}
                    </div>
                    <div className="aspect-[9/16] bg-muted flex items-center justify-center">
                      {frames.lastClean ? (
                        <img
                          src={frames.lastClean}
                          alt="Last frame cleaned"
                          className="w-full h-full object-contain"
                        />
                      ) : running ? (
                        <div className="flex flex-col items-center gap-2 text-muted-foreground">
                          <div className="w-7 h-7 border-3 border-muted border-t-primary rounded-full animate-spin" />
                          <span className="text-xs">Removing text...</span>
                        </div>
                      ) : (
                        <span className="text-muted-foreground text-sm">--</span>
                      )}
                    </div>
                  </CardContent>
                </Card>
              </div>
            </div>
          )}

          {/* Duration info */}
          {frames.duration != null && (
            <Card className="mt-6">
              <CardContent className="py-3 flex items-center gap-2 text-sm text-muted-foreground">
                <span className="font-bold text-foreground">Duration:</span>
                <span>{frames.duration.toFixed(1)}s</span>
              </CardContent>
            </Card>
          )}

          {/* Generate Prompt + Send to Generate */}
          {frames.firstClean && frames.lastClean && (
            <Card className="mt-6">
              <CardContent className="p-0">
                <div className="px-4 py-3 border-b-2 border-border flex items-center justify-between">
                  <span className="text-sm font-bold text-foreground">Generate Video Prompt</span>
                  <Badge variant="info" className="text-[10px]">GPT-4.1 Vision</Badge>
                </div>
                <div className="p-4 space-y-3">
                  {generatedPrompt === null ? (
                    <Button
                      onClick={async () => {
                        setPromptLoading(true);
                        try {
                          const resp = await fetch(apiUrl('/api/recreate/generate-prompt'), {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                              first_frame: frames.firstClean,
                              last_frame: frames.lastClean,
                            }),
                          });
                          if (!resp.ok) {
                            const err = await resp.json().catch(() => ({ detail: 'Request failed' }));
                            throw new Error(err.detail || 'Failed to generate prompt');
                          }
                          const data = await resp.json() as { prompt: string };
                          setGeneratedPrompt(data.prompt);
                        } catch (e) {
                          addNotification('error', e instanceof Error ? e.message : 'Failed to generate prompt');
                        } finally {
                          setPromptLoading(false);
                        }
                      }}
                      disabled={promptLoading}
                      className="w-full"
                      variant="secondary"
                    >
                      {promptLoading ? (
                        <span className="flex items-center gap-2">
                          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted border-t-primary" />
                          Analyzing frames...
                        </span>
                      ) : (
                        'Generate Prompt from Frames'
                      )}
                    </Button>
                  ) : (
                    <>
                      <textarea
                        value={generatedPrompt}
                        onChange={(e) => setGeneratedPrompt(e.target.value)}
                        rows={4}
                        className="w-full rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 text-sm text-foreground resize-y focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-primary"
                        placeholder="Video generation prompt..."
                      />
                      <div className="flex gap-2">
                        <Button
                          variant="secondary"
                          className="flex-1"
                          onClick={async () => {
                            setGeneratedPrompt(null);
                            setPromptLoading(true);
                            try {
                              const resp = await fetch(apiUrl('/api/recreate/generate-prompt'), {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                  first_frame: frames.firstClean,
                                  last_frame: frames.lastClean,
                                }),
                              });
                              if (!resp.ok) throw new Error('Failed');
                              const data = await resp.json() as { prompt: string };
                              setGeneratedPrompt(data.prompt);
                            } catch {
                              addNotification('error', 'Failed to regenerate prompt');
                            } finally {
                              setPromptLoading(false);
                            }
                          }}
                          disabled={promptLoading}
                        >
                          {promptLoading ? 'Regenerating...' : 'Regenerate'}
                        </Button>
                        <Button
                          className="flex-1"
                          onClick={() => {
                            primeGeneratePrefill({
                              prompt: generatedPrompt,
                              firstFrameDataUri: frames.firstClean!,
                              lastFrameDataUri: frames.lastClean,
                              provider: 'wan-i2v-fast',
                              aspectRatio: '9:16',
                            });
                            navigate('/generate');
                          }}
                        >
                          Send to Generate →
                        </Button>
                      </div>
                    </>
                  )}
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
