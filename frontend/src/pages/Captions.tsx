import { type FormEventHandler, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { EmptyState } from '../components';
import { useWebSocket, type WebSocketStatus } from '../hooks/useWebSocket';
import { useWorkflowStore } from '../stores/workflowStore';
import type { CaptionResult, CaptionWSMessage } from '../types/api';

function getStatusBadge(status: WebSocketStatus) {
  if (status === 'connected') {
    return { label: 'CONNECTED', className: 'text-green-400 bg-green-400/10', dot: 'bg-green-400' };
  }
  if (status === 'reconnecting') {
    return { label: 'RECONNECTING', className: 'text-yellow-300 bg-yellow-500/10', dot: 'bg-yellow-300' };
  }
  if (status === 'connecting') {
    return { label: 'CONNECTING', className: 'text-sky-300 bg-sky-500/10', dot: 'bg-sky-300' };
  }
  if (status === 'error') {
    return { label: 'DISCONNECTED', className: 'text-red-300 bg-red-500/10', dot: 'bg-red-300' };
  }
  return { label: 'IDLE', className: 'text-gray-400 bg-white/5', dot: 'bg-gray-500' };
}

export function CaptionsPage() {
  const navigate = useNavigate();
  const {
    activeProject,
    addScrapedCaption,
    addNotification,
    setCaptionJobActive,
    incrementBurnReadyCount,
    primeBurnSelection,
  } = useWorkflowStore();

  const [profileUrl, setProfileUrl] = useState('');
  const [maxVideos, setMaxVideos] = useState(10);
  const [jobId, setJobId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [results, setResults] = useState<CaptionResult[]>([]);
  const [isScraping, setIsScraping] = useState(false);
  const [csvUrl, setCsvUrl] = useState<string | null>(null);
  const [lastUsername, setLastUsername] = useState<string | null>(null);

  const logsEndRef = useRef<HTMLDivElement>(null);

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = isScraping && jobId ? `${wsProtocol}//${window.location.host}/api/captions/ws/${jobId}` : null;

  const handleMessage = (data: CaptionWSMessage) => {
    switch (data.event) {
      case 'status':
        setLogs((prev) => [...prev, `[status] ${data.text}`]);
        return;
      case 'urls_collected':
        setLogs((prev) => [...prev, `[info] Collected ${data.count} URLs`]);
        return;
      case 'downloading':
        setLogs((prev) => [...prev, `[download] ${data.index + 1}/${data.total} (${data.video_id})`]);
        return;
      case 'frame_ready':
        setLogs((prev) => [...prev, `[frame] Ready for ${data.video_id}`]);
        return;
      case 'ocr_started':
        setLogs((prev) => [...prev, `[ocr] Reading ${data.video_id}`]);
        return;
      case 'ocr_done': {
        setLogs((prev) => [...prev, `[ocr] Done for ${data.video_id}`]);
        setResults((prev) => {
          const next = [...prev];
          const result: CaptionResult = {
            index: data.index,
            video_id: data.video_id,
            video_url: '',
            caption: data.caption,
            error: data.error,
          };
          next[data.index] = result;
          return next;
        });
        return;
      }
      case 'all_complete': {
        setIsScraping(false);
        setCaptionJobActive(false);
        setLastUsername(data.username);
        setLogs((prev) => [...prev, `[done] Completed ${data.results.length} results`]);
        setResults(data.results || []);
        if (data.csv && activeProject) {
          setCsvUrl(
            `/api/captions/export/${encodeURIComponent(data.username)}?project=${encodeURIComponent(activeProject.name)}`,
          );
          addScrapedCaption(data.username);
          incrementBurnReadyCount(data.results.length > 0 ? 1 : 0);
          addNotification('success', `${data.results.length} captions scraped from @${data.username}`);
          window.dispatchEvent(new Event('burn:refresh-request'));
        }
        return;
      }
      case 'error':
        setLogs((prev) => [...prev, `[error] ${data.error}`]);
        setIsScraping(false);
        setCaptionJobActive(false);
        addNotification('error', data.error);
        return;
      default:
        return;
    }
  };

  const { status, error, sendMessage, reconnect } = useWebSocket(wsUrl, {
    onOpen: () => {
      if (!activeProject) {
        return;
      }

      sendMessage({
        action: 'start',
        profile_url: profileUrl,
        max_videos: maxVideos,
        sort: 'latest',
        project: activeProject.name,
      });
    },
    onMessage: (event) => {
      try {
        const payload = JSON.parse(event.data) as CaptionWSMessage;
        handleMessage(payload);
      } catch {
        addNotification('error', 'Failed to parse WebSocket payload from caption server.');
      }
    },
    onError: () => {
      addNotification('error', 'Caption WebSocket connection failed.');
    },
  });

  const statusBadge = getStatusBadge(status);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  useEffect(() => {
    setCaptionJobActive(isScraping);
    return () => {
      setCaptionJobActive(false);
    };
  }, [isScraping, setCaptionJobActive]);

  const handleStart: FormEventHandler<HTMLFormElement> = (event) => {
    event.preventDefault();
    if (!profileUrl || !activeProject) {
      return;
    }

    const newJobId = Math.random().toString(36).slice(2, 10);
    setJobId(newJobId);
    setLogs([]);
    setResults([]);
    setCsvUrl(null);
    setLastUsername(null);
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

  if (!activeProject) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState
          icon="📁"
          title="No Project Selected"
          description="Please select or create a project to start scraping captions."
        />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden lg:flex-row">
      <div className="w-full flex-shrink-0 overflow-y-auto border-r border-white/10 bg-black/20 p-6 backdrop-blur-sm lg:w-[400px]">
        <h2 className="mb-6 text-xl font-bold text-white">Scrape Captions</h2>

        <form onSubmit={handleStart} className="space-y-6">
          <div>
            <label className="label">TikTok Profile URL</label>
            <input
              type="url"
              value={profileUrl}
              onChange={(event) => setProfileUrl(event.target.value)}
              placeholder="https://www.tiktok.com/@username"
              className="input"
              required
              disabled={isScraping}
            />
          </div>

          <div>
            <label className="label">Max Videos</label>
            <input
              type="number"
              min="1"
              max="50"
              value={maxVideos}
              onChange={(event) => setMaxVideos(parseInt(event.target.value, 10))}
              className="input"
              disabled={isScraping}
            />
          </div>

          <button
            type="submit"
            disabled={isScraping || !profileUrl}
            className={`btn btn-primary w-full ${isScraping || !profileUrl ? 'cursor-not-allowed opacity-50' : ''}`}
          >
            {isScraping ? 'Scraping...' : 'Start Scraping'}
          </button>

          {(status === 'error' || error) && isScraping ? (
            <button type="button" className="btn btn-secondary w-full" onClick={reconnect}>
              Retry Connection
            </button>
          ) : null}
        </form>

        <div className="mt-8">
          <div className="mb-2 flex items-center justify-between">
            <label className="text-xs font-semibold uppercase tracking-wider text-gray-400">Live Logs</label>
            <span className={`flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-medium ${statusBadge.className}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${statusBadge.dot}`} />
              {statusBadge.label}
            </span>
          </div>
          <div className="h-64 overflow-y-auto rounded-lg border border-white/10 bg-black/50 p-3 font-mono text-xs text-gray-300">
            {logs.length === 0 ? (
              <span className="italic text-gray-600">Waiting to start...</span>
            ) : (
              logs.map((log, index) => (
                <div key={`${log}-${index}`} className="mb-1 break-all border-b border-white/5 pb-1 last:border-0">
                  {log}
                </div>
              ))
            )}
            <div ref={logsEndRef} />
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-8">
        <div className="mx-auto max-w-6xl">
          <div className="mb-8 flex items-center justify-between">
            <h2 className="text-2xl font-bold text-white">Scraped Captions</h2>
            <div className="flex gap-2">
              {results.length > 0 ? (
                <button onClick={jumpToBurn} className="btn btn-secondary flex items-center gap-2">
                  <span>Use in Burn</span>
                  <span className="text-lg">→</span>
                </button>
              ) : null}
              {csvUrl ? (
                <a
                  href={csvUrl}
                  download
                  className="btn btn-secondary flex items-center gap-2 border-green-500/20 text-green-400 hover:bg-green-500/10 hover:text-green-300"
                >
                  <span>Download CSV</span>
                </a>
              ) : null}
            </div>
          </div>

          {results.length === 0 ? (
            <EmptyState
              icon="📝"
              title="No Captions Yet"
              description="Enter a TikTok profile URL to start scraping captions."
            />
          ) : (
            <div className="grid grid-cols-1 gap-4">
              {results.map((result, index) => (
                <div key={`${result.video_id}-${index}`} className="card flex gap-4">
                  <div className="flex-1">
                    <div className="mb-2 flex items-center gap-2">
                      <span className="rounded bg-white/10 px-2 py-1 font-mono text-xs text-gray-300">#{index + 1}</span>
                      <span className="font-mono text-xs text-gray-500">{result.video_id}</span>
                    </div>
                    <p className="whitespace-pre-wrap text-sm text-gray-200">
                      {result.caption || <span className="italic text-gray-600">No caption text found</span>}
                    </p>
                    {result.error ? <p className="mt-2 text-xs text-red-400">Error: {result.error}</p> : null}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
