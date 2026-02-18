import React, { useState, useEffect, useRef } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { useWebSocket } from '../hooks/useWebSocket';
import { EmptyState } from '../components';
import type { CaptionWSMessage, CaptionResult } from '../types/api';

export function CaptionsPage() {
  const { activeProject } = useWorkflowStore();
  const [profileUrl, setProfileUrl] = useState('');
  const [maxVideos, setMaxVideos] = useState(10);
  const [jobId, setJobId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [results, setResults] = useState<CaptionResult[]>([]);
  const [isScraping, setIsScraping] = useState(false);
  const [csvUrl, setCsvUrl] = useState<string | null>(null);
  
  const logsEndRef = useRef<HTMLDivElement>(null);

  // WebSocket URL - only set when we have a job ID
  const wsUrl = jobId ? `ws://${window.location.host}/api/captions/ws/${jobId}` : null;

  const { isConnected, sendMessage } = useWebSocket(wsUrl, {
    onOpen: () => {
      // Start the job once connected
      sendMessage({
        action: 'start',
        profile_url: profileUrl,
        max_videos: maxVideos,
        sort: 'latest'
      });
    },
    onMessage: (event) => {
      try {
        const data: CaptionWSMessage = JSON.parse(event.data);
        handleMessage(data);
      } catch (err) {
        console.error('Failed to parse WS message', err);
      }
    }
  });

  // Auto-scroll logs
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  const handleMessage = (data: CaptionWSMessage) => {
    switch (data.event) {
      case 'status':
        setLogs(prev => [...prev, `[STATUS] ${data.text}`]);
        break;
      case 'urls_collected':
        setLogs(prev => [...prev, `[INFO] Collected ${data.count} URLs`]);
        break;
      case 'downloading':
        setLogs(prev => [...prev, `[download] Video ${data.index + 1}/${data.total} (${data.video_id})`]);
        break;
      case 'frame_ready':
        setLogs(prev => [...prev, `[frame] Extracted frame for ${data.video_id}`]);
        break;
      case 'ocr_started':
        setLogs(prev => [...prev, `[ocr] Reading caption for ${data.video_id}...`]);
        break;
      case 'ocr_done':
        setLogs(prev => [...prev, `[ocr] Done: "${data.caption.substring(0, 30)}..."`]);
        setResults(prev => [...prev, {
          index: data.index,
          video_id: data.video_id,
          video_url: '', // We don't get this in ocr_done event usually, but it's in the result type
          caption: data.caption,
          error: data.error
        }]);
        break;
      case 'all_complete':
        setIsScraping(false);
        setLogs(prev => [...prev, `[DONE] All complete! CSV: ${data.csv}`]);
        if (data.csv) {
            // Construct full URL for download
            setCsvUrl(`/api/captions/export/${data.username}`);
        }
        break;
      case 'error':
        setLogs(prev => [...prev, `[ERROR] ${data.error}`]);
        setIsScraping(false);
        break;
    }
  };

  const handleStart = (e: React.FormEvent) => {
    e.preventDefault();
    if (!profileUrl) return;

    // Generate a random job ID
    const newJobId = Math.random().toString(36).substring(7);
    setJobId(newJobId);
    setLogs([]);
    setResults([]);
    setCsvUrl(null);
    setIsScraping(true);
  };

  if (!activeProject) {
    return (
      <div className="h-full flex items-center justify-center">
        <EmptyState
          icon="📁"
          title="No Project Selected"
          description="Please select or create a project to start scraping captions."
        />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col lg:flex-row overflow-hidden">
      {/* Sidebar - Controls */}
      <div className="w-full lg:w-[400px] bg-slate-900 border-r border-slate-800 p-6 overflow-y-auto flex-shrink-0">
        <h2 className="text-xl font-bold mb-6 text-white">Scrape Captions</h2>
        
        <form onSubmit={handleStart} className="space-y-6">
          <div>
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
              TikTok Profile URL
            </label>
            <input
              type="url"
              value={profileUrl}
              onChange={(e) => setProfileUrl(e.target.value)}
              placeholder="https://www.tiktok.com/@username"
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-purple-500"
              required
              disabled={isScraping}
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
              Max Videos
            </label>
            <input
              type="number"
              min="1"
              max="50"
              value={maxVideos}
              onChange={(e) => setMaxVideos(parseInt(e.target.value))}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-slate-200 focus:outline-none focus:border-purple-500"
              disabled={isScraping}
            />
          </div>

          <button
            type="submit"
            disabled={isScraping || !profileUrl}
            className={`w-full py-3 px-4 rounded-lg font-semibold text-white transition-all ${
              isScraping || !profileUrl
                ? 'bg-slate-700 cursor-not-allowed opacity-50'
                : 'bg-purple-600 hover:bg-purple-500 active:scale-[0.98]'
            }`}
          >
            {isScraping ? 'Scraping...' : 'Start Scraping'}
          </button>
        </form>

        {/* Logs Console */}
        <div className="mt-8">
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Live Logs
            </label>
            {isConnected && (
              <span className="flex items-center gap-1.5 text-[10px] text-green-400 font-medium bg-green-400/10 px-2 py-0.5 rounded-full">
                <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
                CONNECTED
              </span>
            )}
          </div>
          <div className="bg-black/50 border border-slate-800 rounded-lg p-3 h-64 overflow-y-auto font-mono text-xs text-slate-300">
            {logs.length === 0 ? (
              <span className="text-slate-600 italic">Waiting to start...</span>
            ) : (
              logs.map((log, i) => (
                <div key={i} className="mb-1 break-all border-b border-slate-800/50 pb-1 last:border-0">
                  {log}
                </div>
              ))
            )}
            <div ref={logsEndRef} />
          </div>
        </div>
      </div>

      {/* Main Panel - Results */}
      <div className="flex-1 bg-slate-950 p-8 overflow-y-auto">
        <div className="max-w-6xl mx-auto">
          <div className="flex items-center justify-between mb-8">
            <h2 className="text-2xl font-bold text-white">Scraped Captions</h2>
            {csvUrl && (
              <a
                href={csvUrl}
                download
                className="flex items-center gap-2 bg-green-600 hover:bg-green-500 text-white px-4 py-2 rounded-lg font-medium transition-colors"
              >
                <span>Download CSV</span>
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                </svg>
              </a>
            )}
          </div>

          {results.length === 0 ? (
            <EmptyState
              icon="📝"
              title="No Captions Yet"
              description="Enter a TikTok profile URL to start scraping captions."
            />
          ) : (
            <div className="grid grid-cols-1 gap-4">
              {results.map((result, idx) => (
                <div key={idx} className="bg-slate-900/50 border border-slate-800 rounded-xl p-4 flex gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-mono bg-slate-800 text-slate-400 px-2 py-1 rounded">
                        #{idx + 1}
                      </span>
                      <span className="text-xs text-slate-500 font-mono">
                        {result.video_id}
                      </span>
                    </div>
                    <p className="text-slate-200 text-sm whitespace-pre-wrap">
                      {result.caption || <span className="text-slate-600 italic">No caption text found</span>}
                    </p>
                    {result.error && (
                      <p className="text-red-400 text-xs mt-2">Error: {result.error}</p>
                    )}
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
