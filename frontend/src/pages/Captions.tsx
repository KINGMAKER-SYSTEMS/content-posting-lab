import { type FormEventHandler, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiUrl, wsUrl } from '../lib/api';
import { EmptyState, ProgressBar } from '../components';
import { useWebSocket, type WebSocketStatus } from '../hooks/useWebSocket';
import { useWorkflowStore } from '../stores/workflowStore';
import type { CaptionResult, CaptionWSMessage, MoodTag } from '../types/api';
import { MOOD_COLORS } from '../types/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Textarea } from '@/components/ui/textarea';

function getStatusBadge(status: WebSocketStatus) {
  if (status === 'connected') {
    return { label: 'CONNECTED', variant: 'success' as const };
  }
  if (status === 'reconnecting') {
    return { label: 'RECONNECTING', variant: 'warning' as const };
  }
  if (status === 'connecting') {
    return { label: 'CONNECTING', variant: 'info' as const };
  }
  if (status === 'error') {
    return { label: 'DISCONNECTED', variant: 'error' as const };
  }
  return { label: 'IDLE', variant: 'secondary' as const };
}

type ViewTab = 'grid' | 'table';

interface VideoDataRow {
  index: number;
  video_id: string;
  video_url: string;
  b64: string | null;
  caption: string;
  mood: MoodTag | null;
  error: string | null;
  frameStatus: 'pending' | 'ready' | 'error';
  ocrStatus: 'pending' | 'scanning' | 'done' | 'error';
}

function clampVideos(value: number) {
  if (!Number.isFinite(value)) {
    return 10;
  }
  return Math.min(50, Math.max(1, Math.round(value)));
}

export function CaptionsPage() {
  const navigate = useNavigate();
  const {
    activeProjectName,
    addScrapedCaption,
    addNotification,
    setCaptionJobActive,
    incrementBurnReadyCount,
    primeBurnSelection,
  } = useWorkflowStore();

  const [profileUrl, setProfileUrl] = useState('');
  const [maxVideos, setMaxVideos] = useState(10);
  const [sortBy, setSortBy] = useState<'latest' | 'popular'>('latest');
  const [activeTab, setActiveTab] = useState<ViewTab>('grid');
  const [jobId, setJobId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [results, setResults] = useState<CaptionResult[]>([]);
  const [isScraping, setIsScraping] = useState(false);
  const [hasStarted, setHasStarted] = useState(false);
  const [csvUrl, setCsvUrl] = useState<string | null>(null);
  const [lastUsername, setLastUsername] = useState<string | null>(null);
  const [statusText, setStatusText] = useState('Idle');
  const [showStatusSpinner, setShowStatusSpinner] = useState(false);
  const [totalVideos, setTotalVideos] = useState(0);
  const [framesLoaded, setFramesLoaded] = useState(0);
  const [ocrDone, setOcrDone] = useState(0);
  const [ocrPhaseStarted, setOcrPhaseStarted] = useState(false);
  const [isAllComplete, setIsAllComplete] = useState(false);
  const [videoData, setVideoData] = useState<Record<number, VideoDataRow>>({});
  const [loadedImages, setLoadedImages] = useState<Record<number, boolean>>({});
  const [copySuccess, setCopySuccess] = useState(false);
  const [scanPosition, setScanPosition] = useState(0);
  const [shimmerPosition, setShimmerPosition] = useState(-200);

  const logsEndRef = useRef<HTMLDivElement>(null);
  const frameProgressSetRef = useRef<Set<number>>(new Set());
  const ocrProgressSetRef = useRef<Set<number>>(new Set());
  const copyResetTimerRef = useRef<number | null>(null);

  const wsEndpoint = isScraping && jobId ? wsUrl(`/api/captions/ws/${jobId}`) : null;

  const rows = useMemo(
    () => Object.values(videoData).sort((a, b) => a.index - b.index),
    [videoData],
  );

  const hasAnyScanning = useMemo(
    () => rows.some((row) => row.ocrStatus === 'scanning'),
    [rows],
  );

  const hasPendingSkeleton = useMemo(
    () => rows.some((row) => row.frameStatus === 'pending' && !row.b64),
    [rows],
  );

  const progressPercent = useMemo(() => {
    if (totalVideos <= 0) return 0;
    if (isAllComplete) return 100;
    const phase1 = (framesLoaded / totalVideos) * 50;
    const phase2 = (ocrDone / totalVideos) * 50;
    return Math.min(100, Math.max(0, Math.round(phase1 + phase2)));
  }, [framesLoaded, isAllComplete, ocrDone, totalVideos]);

  const progressLabel = useMemo(() => {
    if (totalVideos <= 0) return '';
    if (isAllComplete) return `${totalVideos} videos processed`;
    if (ocrPhaseStarted || ocrDone > 0) return `${ocrDone} / ${totalVideos} captions`;
    return `${framesLoaded} / ${totalVideos} frames`;
  }, [framesLoaded, isAllComplete, ocrDone, ocrPhaseStarted, totalVideos]);

  const extractedCaptionCount = useMemo(
    () => rows.filter((row) => row.caption.trim().length > 0 && !row.error).length,
    [rows],
  );

  useEffect(() => {
    if (!hasAnyScanning) return;
    let raf = 0;
    let start = 0;
    const tick = (ts: number) => {
      if (start === 0) start = ts;
      const phase = ((ts - start) % 1500) / 1500;
      setScanPosition(phase * 100);
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(raf);
  }, [hasAnyScanning]);

  useEffect(() => {
    if (!hasPendingSkeleton) return;
    let raf = 0;
    let start = 0;
    const tick = (ts: number) => {
      if (start === 0) start = ts;
      const phase = ((ts - start) % 1500) / 1500;
      setShimmerPosition(200 - phase * 400);
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(raf);
  }, [hasPendingSkeleton]);

  useEffect(() => {
    return () => {
      if (copyResetTimerRef.current) window.clearTimeout(copyResetTimerRef.current);
    };
  }, []);

  const updateRow = (index: number, updater: (current: VideoDataRow) => VideoDataRow) => {
    setVideoData((prev) => {
      const existing = prev[index];
      if (!existing) return prev;
      return { ...prev, [index]: updater(existing) };
    });
  };

  const handleMessage = (data: CaptionWSMessage) => {
    switch (data.event) {
      case 'status':
        setLogs((prev) => [...prev, `[status] ${data.text}`]);
        setStatusText(data.text || 'Running...');
        return;
      case 'urls_collected': {
        const nextRows: Record<number, VideoDataRow> = {};
        for (let index = 0; index < data.count; index += 1) {
          nextRows[index] = {
            index, video_id: '', video_url: data.urls[index] || '', b64: null,
            caption: '', mood: null, error: null, frameStatus: 'pending', ocrStatus: 'pending',
          };
        }
        frameProgressSetRef.current = new Set();
        ocrProgressSetRef.current = new Set();
        setLogs((prev) => [...prev, `[info] Collected ${data.count} URLs`]);
        setStatusText(`Found ${data.count} videos - downloading frames...`);
        setTotalVideos(data.count);
        setFramesLoaded(0);
        setOcrDone(0);
        setOcrPhaseStarted(false);
        setVideoData(nextRows);
        setLoadedImages({});
        return;
      }
      case 'downloading':
        setLogs((prev) => [...prev, `[download] ${data.index + 1}/${data.total} (${data.video_id})`]);
        setStatusText(`Downloading ${data.index + 1}/${data.total}...`);
        return;
      case 'frame_ready': {
        if (!frameProgressSetRef.current.has(data.index)) {
          frameProgressSetRef.current.add(data.index);
          setFramesLoaded((prev) => prev + 1);
        }
        updateRow(data.index, (c) => ({ ...c, video_id: data.video_id, video_url: data.video_url || c.video_url, b64: data.b64, frameStatus: 'ready', error: null }));
        setLogs((prev) => [...prev, `[frame] Ready for ${data.video_id}`]);
        return;
      }
      case 'frame_error': {
        if (!frameProgressSetRef.current.has(data.index)) {
          frameProgressSetRef.current.add(data.index);
          setFramesLoaded((prev) => prev + 1);
        }
        updateRow(data.index, (c) => ({ ...c, video_id: data.video_id || c.video_id, frameStatus: 'error', ocrStatus: 'error', error: data.error }));
        setLogs((prev) => [...prev, `[frame:error] ${data.video_id} - ${data.error}`]);
        return;
      }
      case 'ocr_starting':
        setStatusText('Extracting captions...');
        setOcrPhaseStarted(true);
        return;
      case 'ocr_started':
        updateRow(data.index, (c) => (c.ocrStatus === 'done' || c.ocrStatus === 'error') ? c : { ...c, ocrStatus: 'scanning' });
        setLogs((prev) => [...prev, `[ocr] Reading ${data.video_id}`]);
        setStatusText(`OCR ${data.index + 1}/${data.total}...`);
        return;
      case 'ocr_done': {
        if (!ocrProgressSetRef.current.has(data.index)) {
          ocrProgressSetRef.current.add(data.index);
          setOcrDone((prev) => prev + 1);
        }
        updateRow(data.index, (c) => ({ ...c, video_id: data.video_id || c.video_id, caption: data.caption || '', mood: data.mood || null, error: data.error || null, ocrStatus: data.error ? 'error' : 'done' }));
        setLogs((prev) => [...prev, `[ocr] Done for ${data.video_id}`]);
        return;
      }
      case 'all_complete': {
        clearStartPayload();
        setIsScraping(false);
        setCaptionJobActive(false);
        setLastUsername(data.username);
        setIsAllComplete(true);
        setShowStatusSpinner(false);
        setStatusText('Complete');
        setLogs((prev) => [...prev, `[done] Completed ${data.results.length} results`]);
        setResults(data.results || []);
        if (activeProjectName && data.username) {
          setCsvUrl(apiUrl(`/api/captions/export/${encodeURIComponent(data.username)}?project=${encodeURIComponent(activeProjectName)}`));
          addScrapedCaption(data.username);
          incrementBurnReadyCount(data.results.length > 0 ? 1 : 0);
          addNotification('success', `${data.results.length} captions scraped from @${data.username}`);
          window.dispatchEvent(new Event('burn:refresh-request'));
        }
        return;
      }
      case 'error':
        clearStartPayload();
        setLogs((prev) => [...prev, `[error] ${data.error}`]);
        setIsScraping(false);
        setCaptionJobActive(false);
        setShowStatusSpinner(false);
        setStatusText(`Error: ${data.error}`);
        addNotification('error', data.error);
        return;
      default:
        return;
    }
  };

  const isScrapingRef = useRef(isScraping);
  isScrapingRef.current = isScraping;

  const { status, error, sendMessage, reconnect, clearStartPayload } = useWebSocket(wsEndpoint, {
    onOpen: () => {
      if (!activeProjectName) return;
      setStatusText('Scanning profile...');
      setShowStatusSpinner(true);
      sendMessage({
        action: 'start', profile_url: profileUrl,
        max_videos: maxVideos, sort: sortBy, project: activeProjectName,
      });
    },
    onMessage: (event) => {
      try {
        handleMessage(JSON.parse(event.data) as CaptionWSMessage);
      } catch {
        addNotification('error', 'Failed to parse WebSocket payload from caption server.');
      }
    },
    onError: () => {
      setShowStatusSpinner(false);
      setStatusText('Connection error');
      addNotification('error', 'Caption WebSocket connection failed.');
    },
    shouldReconnect: () => isScrapingRef.current,
  });

  const statusBadge = getStatusBadge(status);
  const shouldShowRetry = (status === 'error' || error) && isScraping;

  useEffect(() => { logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [logs]);

  useEffect(() => {
    setCaptionJobActive(isScraping);
    if (!isScraping && !isAllComplete) setShowStatusSpinner(false);
    return () => { setCaptionJobActive(false); };
  }, [isAllComplete, isScraping, setCaptionJobActive]);

  const handleStart: FormEventHandler<HTMLFormElement> = (event) => {
    event.preventDefault();
    if (!profileUrl || !activeProjectName) return;
    setJobId(Math.random().toString(36).slice(2, 10));
    setHasStarted(true);
    setActiveTab('grid');
    setLogs([]);
    setResults([]);
    setCsvUrl(null);
    setLastUsername(null);
    setStatusText('Connecting...');
    setShowStatusSpinner(true);
    setTotalVideos(0);
    setFramesLoaded(0);
    setOcrDone(0);
    setOcrPhaseStarted(false);
    setIsAllComplete(false);
    setVideoData({});
    setLoadedImages({});
    setCopySuccess(false);
    frameProgressSetRef.current = new Set();
    ocrProgressSetRef.current = new Set();
    setIsScraping(true);
  };

  const jumpToBurn = () => {
    if (!lastUsername) {
      addNotification('info', 'Run a scrape first to preselect a caption source in Burn.');
      return;
    }
    primeBurnSelection({ captionSource: lastUsername });
    navigate('/burn');
  };

  const updateCaptionValue = (index: number, value: string) => {
    updateRow(index, (c) => ({ ...c, caption: value }));
  };

  const copyAllCaptions = async () => {
    const text = rows.map((r) => r.caption.trim()).filter(Boolean).join('\n\n---\n\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopySuccess(true);
      if (copyResetTimerRef.current) window.clearTimeout(copyResetTimerRef.current);
      copyResetTimerRef.current = window.setTimeout(() => setCopySuccess(false), 1500);
    } catch {
      addNotification('error', 'Failed to copy captions to clipboard.');
    }
  };

  const exportCsv = () => {
    if (lastUsername && activeProjectName) {
      window.open(apiUrl(`/api/captions/export/${encodeURIComponent(lastUsername)}?project=${encodeURIComponent(activeProjectName)}`), '_blank');
      return;
    }
    const csvRows = ['video_id,video_url,caption,error'];
    for (const row of rows) {
      csvRows.push(`"${row.video_id.replaceAll('"', '""')}","${row.video_url.replaceAll('"', '""')}","${row.caption.replaceAll('"', '""')}","${(row.error || '').replaceAll('"', '""')}"`);
    }
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv;charset=utf-8' });
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = 'captions.csv';
    a.click();
    URL.revokeObjectURL(href);
  };

  if (!activeProjectName) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState icon="📁" title="No Project Selected" description="Please select or create a project to start scraping captions." />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden lg:flex-row">
      {/* Left sidebar */}
      <div className="w-full flex-shrink-0 overflow-y-auto border-r-2 border-border bg-card p-6 lg:w-[340px]">
        <h2 className="mb-1 text-xl font-heading text-foreground">Caption Extractor</h2>
        <p className="mb-6 text-xs text-muted-foreground">Extract burned-in captions from TikTok profiles</p>

        <form onSubmit={handleStart} className="space-y-4">
          <div>
            <Label htmlFor="captions-profile">TikTok Username</Label>
            <Input
              id="captions-profile"
              value={profileUrl}
              onChange={(e) => setProfileUrl(e.target.value)}
              placeholder="@username"
              className="mt-1"
              required
              disabled={isScraping}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="captions-max">Videos</Label>
              <Input
                id="captions-max"
                type="number"
                min="1"
                max="50"
                value={maxVideos}
                onChange={(e) => setMaxVideos(clampVideos(Number(e.target.value)))}
                className="mt-1"
                disabled={isScraping}
              />
            </div>
            <div>
              <Label htmlFor="captions-sort">Sort</Label>
              <Select value={sortBy} onValueChange={(v) => setSortBy(v as 'latest' | 'popular')} disabled={isScraping}>
                <SelectTrigger id="captions-sort" className="w-full mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="latest">Latest</SelectItem>
                  <SelectItem value="popular">Popular</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <Button type="submit" disabled={isScraping || !profileUrl} className="w-full">
            {isScraping ? 'Running...' : 'Extract Captions'}
          </Button>

          {shouldShowRetry ? (
            <Button variant="outline" className="w-full" onClick={reconnect}>
              Retry Connection
            </Button>
          ) : null}
        </form>

        {/* Status + logs */}
        <div className="mt-6">
          <Card className={`mb-3 ${hasStarted ? '' : 'opacity-70'}`}>
            <CardContent className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
              {showStatusSpinner ? (
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted border-t-primary" />
              ) : null}
              <span className="truncate">{statusText}</span>
            </CardContent>
          </Card>

          <div className="mb-2 flex items-center justify-between">
            <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Live Logs</span>
            <Badge variant={statusBadge.variant} className="text-[10px] shadow-none">
              {statusBadge.label}
            </Badge>
          </div>

          <ScrollArea className="h-64 rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 font-mono text-xs text-muted-foreground">
            {logs.length === 0 ? (
              <span className="italic">Waiting to start...</span>
            ) : (
              logs.map((log, i) => (
                <div key={`${log}-${i}`} className="mb-1 break-all border-b border-border pb-1 last:border-0">
                  {log}
                </div>
              ))
            )}
            <div ref={logsEndRef} />
          </ScrollArea>
        </div>
      </div>

      {/* Right content */}
      <div className="flex-1 overflow-y-auto p-6 bg-background">
        <div className="mx-auto max-w-6xl">
          <div className="mb-6 flex items-center justify-between">
            <h2 className="text-xl font-heading text-foreground">Scraped Captions</h2>
            <div className="flex gap-2">
              {results.length > 0 ? (
                <Button variant="secondary" onClick={jumpToBurn}>
                  Use in Burn →
                </Button>
              ) : null}
              {csvUrl ? (
                <Button asChild variant="outline">
                  <a href={csvUrl} download>Download CSV</a>
                </Button>
              ) : null}
            </div>
          </div>

          {!hasStarted ? (
            <EmptyState icon="🔎" title="No Captions Yet" description="Enter a TikTok username and hit Extract. Frames will populate here as videos are processed." />
          ) : (
            <div className="space-y-4">
              <ProgressBar
                value={progressPercent}
                label={progressLabel}
                color={isAllComplete ? 'success' : 'primary'}
                showValue
              />

              {/* Grid/Table tabs */}
              <div className="flex items-center border-b-2 border-border">
                <button
                  type="button"
                  onClick={() => setActiveTab('grid')}
                  className={`-mb-[2px] border-b-[3px] px-4 py-2 text-sm font-bold transition-colors ${
                    activeTab === 'grid' ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground'
                  }`}
                >
                  Grid
                  <Badge variant={activeTab === 'grid' ? 'default' : 'secondary'} className="ml-2 text-[10px] px-1.5 py-0 shadow-none">
                    {framesLoaded}
                  </Badge>
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab('table')}
                  className={`-mb-[2px] border-b-[3px] px-4 py-2 text-sm font-bold transition-colors ${
                    activeTab === 'table' ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground'
                  }`}
                >
                  Table
                  <Badge variant={activeTab === 'table' ? 'default' : 'secondary'} className="ml-2 text-[10px] px-1.5 py-0 shadow-none">
                    {ocrDone}
                  </Badge>
                </button>
              </div>

              {activeTab === 'grid' ? (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-3">
                  {rows.map((row) => {
                    const isError = row.frameStatus === 'error' || row.ocrStatus === 'error';
                    const isDone = row.ocrStatus === 'done' && !row.error;
                    const isScanning = row.ocrStatus === 'scanning';
                    const showSkeleton = row.frameStatus === 'pending' && !row.b64;

                    return (
                      <div
                        key={`grid-${row.index}`}
                        className={`relative aspect-[9/16] overflow-hidden rounded-[var(--border-radius)] border-2 transition-all ${
                          isError ? 'border-destructive' : isDone ? 'border-green-700' : isScanning ? 'border-primary shadow-[3px_3px_0_0_var(--primary)]' : 'border-border'
                        }`}
                      >
                        {showSkeleton ? (
                          <div
                            className="absolute inset-0 bg-muted"
                            style={{
                              backgroundImage: 'linear-gradient(110deg, transparent 30%, rgba(0,0,0,0.05) 50%, transparent 70%)',
                              backgroundSize: '200% 100%',
                              backgroundPosition: `${shimmerPosition}% 0`,
                            }}
                          />
                        ) : null}

                        {row.b64 ? (
                          <img
                            src={`data:image/jpeg;base64,${row.b64}`}
                            alt="Extracted frame"
                            onLoad={() => setLoadedImages((prev) => ({ ...prev, [row.index]: true }))}
                            className={`h-full w-full object-cover transition-opacity duration-500 ${loadedImages[row.index] ? 'opacity-100' : 'opacity-0'}`}
                          />
                        ) : null}

                        {isScanning ? (
                          <div className="pointer-events-none absolute inset-0">
                            <div
                              className="absolute left-0 right-0 h-[3px] bg-primary/70"
                              style={{ top: `${scanPosition}%` }}
                            />
                          </div>
                        ) : null}

                        {isDone ? (
                          <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent px-2 pb-2 pt-8 text-[11px] leading-snug text-white">
                            {row.mood ? (
                              <span className={`mb-1 inline-block rounded-sm border px-1.5 py-0.5 text-[9px] font-bold uppercase ${MOOD_COLORS[row.mood]}`}>
                                {row.mood}
                              </span>
                            ) : null}
                            <div>{row.caption.length > 80 ? `${row.caption.slice(0, 80)}...` : row.caption || 'No caption'}</div>
                          </div>
                        ) : null}

                        {(isDone || isError) ? (
                          <div className={`absolute right-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded-[var(--border-radius)] border-2 border-border text-[11px] font-bold ${isDone ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
                            {isDone ? '✓' : '!'}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="space-y-3">
                  {isAllComplete ? (
                    <Card>
                      <CardContent className="flex flex-wrap items-center justify-between gap-2 py-3">
                        <span className="text-sm text-muted-foreground">{extractedCaptionCount} captions extracted</span>
                        <div className="flex items-center gap-2">
                          <Button variant="outline" size="sm" onClick={copyAllCaptions}>
                            {copySuccess ? 'Copied!' : 'Copy All'}
                          </Button>
                          <Button size="sm" onClick={exportCsv}>
                            Export CSV
                          </Button>
                        </div>
                      </CardContent>
                    </Card>
                  ) : null}

                  <div className="overflow-x-auto rounded-[var(--border-radius)] border-2 border-border">
                    <table className="w-full min-w-[760px] border-collapse text-sm">
                      <thead>
                        <tr className="bg-muted">
                          <th className="sticky top-0 w-[60px] border-b-2 border-border bg-muted px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Frame</th>
                          <th className="sticky top-0 w-[200px] border-b-2 border-border bg-muted px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Video</th>
                          <th className="sticky top-0 border-b-2 border-border bg-muted px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Caption</th>
                          <th className="sticky top-0 w-[100px] border-b-2 border-border bg-muted px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Mood</th>
                          <th className="sticky top-0 w-[80px] border-b-2 border-border bg-muted px-3 py-2 text-left text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row) => {
                          const hasCaption = row.caption.trim().length > 0;
                          const isPending = row.ocrStatus === 'pending' || row.ocrStatus === 'scanning';
                          return (
                            <tr key={`table-${row.index}`} className="border-b border-border last:border-b-0 hover:bg-muted/50">
                              <td className="px-3 py-2 align-top">
                                {row.b64 ? (
                                  <img src={`data:image/jpeg;base64,${row.b64}`} alt="Frame" className="h-[88px] w-[50px] rounded-[var(--border-radius)] border-2 border-border object-cover" />
                                ) : (
                                  <div className="h-[88px] w-[50px] rounded-[var(--border-radius)] border-2 border-border bg-muted" />
                                )}
                              </td>
                              <td className="max-w-[200px] truncate px-3 py-2 align-top text-xs text-muted-foreground">
                                {row.video_url ? (
                                  <a href={row.video_url} target="_blank" rel="noopener noreferrer" className="hover:text-primary">{row.video_url}</a>
                                ) : '—'}
                              </td>
                              <td className="min-w-[260px] max-w-[420px] px-3 py-2 align-top">
                                {row.error ? (
                                  <span className="text-xs text-destructive">{row.error}</span>
                                ) : hasCaption ? (
                                  <Textarea
                                    value={row.caption}
                                    rows={Math.max(2, Math.ceil(row.caption.length / 50))}
                                    onChange={(e) => updateCaptionValue(row.index, e.target.value)}
                                    className="resize-y text-sm"
                                  />
                                ) : isPending ? (
                                  <span className="text-xs italic text-muted-foreground">Pending...</span>
                                ) : (
                                  <span className="text-xs italic text-muted-foreground">No caption detected</span>
                                )}
                              </td>
                              <td className="px-3 py-2 align-top">
                                {row.mood ? (
                                  <span className={`inline-block rounded-sm border px-2 py-0.5 text-[10px] font-bold uppercase ${MOOD_COLORS[row.mood]}`}>
                                    {row.mood}
                                  </span>
                                ) : hasCaption && !isPending ? (
                                  <span className="text-xs italic text-muted-foreground">—</span>
                                ) : null}
                              </td>
                              <td className="px-3 py-2 align-top">
                                {row.error ? (
                                  <Badge variant="error">Error</Badge>
                                ) : hasCaption ? (
                                  <Badge variant="success">Done</Badge>
                                ) : isPending ? (
                                  <Badge variant="secondary">—</Badge>
                                ) : (
                                  <Badge variant="secondary">Empty</Badge>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
