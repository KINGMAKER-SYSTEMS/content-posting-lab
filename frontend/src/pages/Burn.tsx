import { useCallback, useEffect, useMemo, useState } from 'react';
import { EmptyState } from '../components';
import { useWorkflowStore } from '../stores/workflowStore';
import type { BurnResponse, CaptionSource, VideoFile } from '../types/api';

function buildBatchId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function wrapLines(context: CanvasRenderingContext2D, text: string, maxWidth: number) {
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    return [''];
  }

  const lines: string[] = [];
  let current = words[0];

  for (let index = 1; index < words.length; index += 1) {
    const next = `${current} ${words[index]}`;
    if (context.measureText(next).width <= maxWidth) {
      current = next;
    } else {
      lines.push(current);
      current = words[index];
    }
  }

  lines.push(current);
  return lines;
}

function buildOverlayPng(captionText: string, fontSize = 58, position: 'top' | 'center' | 'bottom' = 'bottom') {
  const width = 1080;
  const height = 1920;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d');

  if (!context) {
    return undefined;
  }

  context.clearRect(0, 0, width, height);
  context.font = `700 ${fontSize}px TikTokSans16pt-Bold, Montserrat, sans-serif`;
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillStyle = '#FFFFFF';
  context.strokeStyle = '#000000';
  context.lineJoin = 'round';
  context.lineWidth = 8;

  const lines = wrapLines(context, captionText, width - 120);
  const lineHeight = fontSize * 1.22;
  const totalHeight = lines.length * lineHeight;

  let startY = 0;
  if (position === 'top') {
    startY = height * 0.08;
  } else if (position === 'center') {
    startY = (height - totalHeight) / 2;
  } else {
    startY = height * 0.92 - totalHeight;
  }

  lines.forEach((line, index) => {
    const y = startY + index * lineHeight;
    context.strokeText(line, width / 2, y);
    context.fillText(line, width / 2, y);
  });

  return canvas.toDataURL('image/png');
}

export function BurnPage() {
  const {
    activeProject,
    addNotification,
    burnSelection,
    clearBurnSelection,
    setBurnReadyCount,
  } = useWorkflowStore();

  const [videos, setVideos] = useState<VideoFile[]>([]);
  const [captionSources, setCaptionSources] = useState<CaptionSource[]>([]);
  const [selectedVideo, setSelectedVideo] = useState('');
  const [selectedCaptionSource, setSelectedCaptionSource] = useState('');
  const [selectedCaptionIndex, setSelectedCaptionIndex] = useState(0);
  const [burning, setBurning] = useState(false);
  const [results, setResults] = useState<BurnResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastBatchId, setLastBatchId] = useState<string | null>(null);

  const selectedSource = useMemo(
    () => captionSources.find((source) => source.username === selectedCaptionSource),
    [captionSources, selectedCaptionSource],
  );

  const selectedCaption = selectedSource?.captions[selectedCaptionIndex];

  const loadData = useCallback(async () => {
    if (!activeProject) {
      return;
    }

    try {
      const projectQuery = encodeURIComponent(activeProject.name);
      const [videosResponse, captionsResponse] = await Promise.all([
        fetch(`/api/burn/videos?project=${projectQuery}`),
        fetch(`/api/burn/captions?project=${projectQuery}`),
      ]);

      if (!videosResponse.ok || !captionsResponse.ok) {
        throw new Error('Failed to load videos or captions for this project');
      }

      const videosPayload = (await videosResponse.json()) as { videos: VideoFile[] };
      const captionsPayload = (await captionsResponse.json()) as { sources: CaptionSource[] };

      const nextVideos = videosPayload.videos || [];
      const nextSources = captionsPayload.sources || [];

      setVideos(nextVideos);
      setCaptionSources(nextSources);

      const totalCaptions = nextSources.reduce((sum, source) => sum + source.count, 0);
      setBurnReadyCount(Math.min(nextVideos.length, totalCaptions));

      if (nextVideos.length > 0) {
        const preferredVideo = burnSelection.videoPaths.find((path) => nextVideos.some((video) => video.path === path));
        if (preferredVideo) {
          setSelectedVideo(preferredVideo);
        } else {
          setSelectedVideo((previous) => {
            if (previous && nextVideos.some((video) => video.path === previous)) {
              return previous;
            }
            return nextVideos[0].path;
          });
        }
      } else {
        setSelectedVideo('');
      }

      if (nextSources.length > 0) {
        if (burnSelection.captionSource && nextSources.some((source) => source.username === burnSelection.captionSource)) {
          setSelectedCaptionSource(burnSelection.captionSource);
        } else {
          setSelectedCaptionSource((previous) => {
            if (previous && nextSources.some((source) => source.username === previous)) {
              return previous;
            }
            return nextSources[0].username;
          });
        }
      } else {
        setSelectedCaptionSource('');
        setSelectedCaptionIndex(0);
      }

      if (burnSelection.videoPaths.length > 0 || burnSelection.captionSource) {
        clearBurnSelection();
      }
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : 'Failed to load burn data';
      setError(message);
      addNotification('error', message);
      setBurnReadyCount(0);
    }
  }, [activeProject, addNotification, burnSelection.captionSource, burnSelection.videoPaths, clearBurnSelection, setBurnReadyCount]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    const handleRefresh: EventListener = () => {
      void loadData();
    };

    window.addEventListener('burn:refresh-request', handleRefresh);
    return () => {
      window.removeEventListener('burn:refresh-request', handleRefresh);
      setBurnReadyCount(0);
    };
  }, [loadData, setBurnReadyCount]);

  useEffect(() => {
    if (!selectedSource) {
      setSelectedCaptionIndex(0);
      return;
    }
    if (selectedCaptionIndex >= selectedSource.captions.length) {
      setSelectedCaptionIndex(0);
    }
  }, [selectedCaptionIndex, selectedSource]);

  const handleBurn = async () => {
    if (!activeProject || !selectedVideo || !selectedCaption) {
      return;
    }

    setBurning(true);
    setError(null);

    const batchId = buildBatchId();
    const overlayPng = buildOverlayPng(selectedCaption.text, 58, 'bottom');

    try {
      const response = await fetch('/api/burn/overlay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: activeProject.name,
          batchId,
          index: results.length,
          videoPath: selectedVideo,
          overlayPng,
        }),
      });

      const payload = (await response.json()) as BurnResponse & { error?: string };
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || `Burn failed (${response.status})`);
      }

      setResults((previous) => [...previous, payload]);
      setLastBatchId(batchId);
      addNotification('success', 'Caption burned successfully');
      window.dispatchEvent(new Event('projects:changed'));
      window.dispatchEvent(new Event('burn:refresh-request'));
    } catch (burnError) {
      const message = burnError instanceof Error ? burnError.message : 'Burn failed';
      setError(message);
      addNotification('error', message);
    } finally {
      setBurning(false);
    }
  };

  if (!activeProject) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState
          icon="📁"
          title="No Project Selected"
          description="Please select or create a project to start burning captions."
        />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden lg:flex-row">
      <div className="w-full flex-shrink-0 overflow-y-auto border-r border-white/10 bg-black/20 p-6 backdrop-blur-sm lg:w-[400px]">
        <h2 className="mb-6 text-xl font-bold text-white">Burn Captions</h2>

        <div className="space-y-6">
          <div>
            <label className="label">Select Video</label>
            <select value={selectedVideo} onChange={(event) => setSelectedVideo(event.target.value)} className="input">
              <option value="" className="bg-charcoal">-- Select Video --</option>
              {videos.map((video) => (
                <option key={video.path} value={video.path} className="bg-charcoal">
                  {video.folder ? `${video.folder}/` : ''}
                  {video.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label">Select Caption Source</label>
            <select
              value={selectedCaptionSource}
              onChange={(event) => {
                setSelectedCaptionSource(event.target.value);
                setSelectedCaptionIndex(0);
              }}
              className="input"
            >
              <option value="" className="bg-charcoal">-- Select Source --</option>
              {captionSources.map((source) => (
                <option key={source.username} value={source.username} className="bg-charcoal">
                  @{source.username} ({source.count} captions)
                </option>
              ))}
            </select>
          </div>

          {selectedSource ? (
            <div>
              <label className="label">Select Caption</label>
              <div className="max-h-56 overflow-y-auto rounded-lg border border-white/10 bg-black/20">
                {selectedSource.captions.map((caption, index) => (
                  <button
                    key={`${caption.video_id}-${index}`}
                    type="button"
                    onClick={() => setSelectedCaptionIndex(index)}
                    className={`w-full border-b border-white/5 p-2 text-left text-sm transition-colors last:border-0 ${
                      selectedCaptionIndex === index
                        ? 'bg-purple-500/20 text-purple-200'
                        : 'text-gray-300 hover:bg-white/5'
                    }`}
                  >
                    <div className="mb-1 text-xs font-mono text-gray-500">{caption.video_id}</div>
                    <div className="line-clamp-2">{caption.text}</div>
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          <button
            type="button"
            onClick={handleBurn}
            disabled={burning || !selectedVideo || !selectedCaption}
            className={`btn btn-primary w-full ${burning || !selectedVideo || !selectedCaption ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            {burning ? 'Burning...' : 'Burn Caption'}
          </button>

          {error ? <div className="rounded-lg border border-red-500/20 bg-red-500/10 p-3 text-sm text-red-400">{error}</div> : null}

          {videos.length === 0 || captionSources.length === 0 ? (
            <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 p-3 text-xs text-amber-200">
              Burn requires at least one generated video and one caption source in this project.
            </div>
          ) : null}

          {lastBatchId ? (
            <a
              className="btn btn-secondary w-full justify-center text-green-300 hover:text-green-200"
              href={`/api/burn/zip/${encodeURIComponent(lastBatchId)}?project=${encodeURIComponent(activeProject.name)}`}
            >
              Download Latest Batch ZIP
            </a>
          ) : null}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-8">
        <div className="mx-auto max-w-4xl">
          <h2 className="mb-8 text-2xl font-bold text-white">Preview & Results</h2>

          <div className="relative mb-8 flex aspect-video items-center justify-center overflow-hidden rounded-xl border border-white/10 bg-black shadow-2xl">
            {selectedVideo ? (
              <div className="relative flex h-full w-full items-center justify-center bg-black/40">
                <div className="text-gray-500">[Video Preview: {selectedVideo.split('/').pop()}]</div>
                <div className="pointer-events-none absolute bottom-[10%] left-0 right-0 px-8 text-center">
                  <p
                    className="text-2xl font-bold text-white drop-shadow-md"
                    style={{ textShadow: '2px 2px 0 #000, -2px 2px 0 #000, 2px -2px 0 #000, -2px -2px 0 #000' }}
                  >
                    {selectedCaption?.text || ''}
                  </p>
                </div>
              </div>
            ) : (
              <div className="text-gray-600">Select a video to preview</div>
            )}
          </div>

          {results.length > 0 ? (
            <div>
              <h3 className="mb-4 text-lg font-bold text-white">Burn Results</h3>
              <div className="space-y-2">
                {results.map((result, index) => (
                  <div key={`${result.file || 'result'}-${index}`} className="flex items-center justify-between rounded-lg border border-white/10 bg-white/5 p-4">
                    <span className="text-gray-300">Job #{index + 1}</span>
                    {result.ok ? (
                      <span className="text-sm text-green-400">Success</span>
                    ) : (
                      <span className="text-sm text-red-400">{result.error || 'Failed'}</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
