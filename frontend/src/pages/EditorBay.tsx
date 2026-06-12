import { useCallback, useEffect, useMemo, useState, type PointerEvent, type ReactNode } from 'react';
import {
  Eye,
  EyeOff,
  Flag,
  MessageSquarePlus,
  Pause,
  Play,
  RefreshCw,
  Scissors,
  SkipForward,
  Volume2,
  VolumeX,
} from 'lucide-react';
import { apiUrl, staticUrl } from '../lib/api';

type AssetKind = 'image' | 'video' | 'audio' | 'title' | string;

interface EditorAsset {
  id: string;
  type: AssetKind;
  src: string;
  metadata?: Record<string, unknown>;
}

interface EditorTrack {
  id: string;
  kind: string;
  name: string;
  index: number;
  locked?: boolean;
}

interface EditorClip {
  id: string;
  assetId: string;
  trackId: string;
  kind: string;
  start: number;
  duration: number;
  sourceStart: number;
  enabled: boolean;
  muted: boolean;
  volume: number;
  transform: {
    x?: number;
    y?: number;
    scale?: number;
    opacity?: number;
  };
  effects?: unknown[];
  keyframes?: unknown[];
  metadata?: Record<string, unknown>;
}

interface EditorMarker {
  id: string;
  time: number;
  label: string;
  metadata?: Record<string, unknown>;
}

interface EditorNote {
  id: string;
  target: {
    clipId?: string;
    time?: number;
    frame?: number;
    trackId?: string;
  };
  text: string;
  suggestedCommand?: unknown;
  metadata?: Record<string, unknown>;
}

interface EditorProject {
  schema: string;
  projectId: string;
  title: string;
  fps: number;
  width: number;
  height: number;
  revision: number;
  assets: Record<string, EditorAsset>;
  tracks: Record<string, EditorTrack>;
  clips: Record<string, EditorClip>;
  markers: Record<string, EditorMarker>;
  notes: Record<string, EditorNote>;
  renderCache?: {
    video?: { video?: string; duration?: number; backend?: string };
    frames?: Record<string, { frame?: string; at?: number; backend?: string }>;
  };
}

interface CommandBody {
  op: string;
  actor: 'human';
  expectedRevision: number;
  payload: Record<string, unknown>;
}

interface DragState {
  clipId: string;
  originX: number;
  originStart: number;
  trackWidth: number;
}

const timeScale = 80;

const fmt = (seconds: number) => {
  const safe = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const mins = Math.floor(safe / 60);
  const secs = Math.floor(safe % 60);
  const tenths = Math.floor((safe % 1) * 10);
  return `${mins}:${secs.toString().padStart(2, '0')}.${tenths}`;
};

const roundTime = (value: number) => Math.round(Math.max(0, value) * 100) / 100;

const projectIdFromPath = () => {
  const match = window.location.pathname.match(/^\/editor\/(.+)$/);
  return match ? decodeURIComponent(match[1]) : '';
};

const objectValues = <T,>(record: Record<string, T> | undefined): T[] => Object.values(record || {});

export function EditorBayPage() {
  const projectId = useMemo(projectIdFromPath, []);
  const [project, setProject] = useState<EditorProject | null>(null);
  const [selectedClipId, setSelectedClipId] = useState<string>('');
  const [currentTime, setCurrentTime] = useState(0);
  const [noteText, setNoteText] = useState('');
  const [markerLabel, setMarkerLabel] = useState('Review marker');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busyOp, setBusyOp] = useState('');
  const [playing, setPlaying] = useState(false);
  const [previewFrame, setPreviewFrame] = useState('');
  const [drag, setDrag] = useState<DragState | null>(null);
  const [draftTiming, setDraftTiming] = useState({
    start: '0',
    duration: '1',
    sourceStart: '0',
    x: '0.5',
    y: '0.5',
    scale: '1',
    opacity: '1',
    volume: '1',
  });

  const tracks = useMemo(
    () => objectValues(project?.tracks).sort((a, b) => a.index - b.index || a.id.localeCompare(b.id)),
    [project],
  );
  const clips = useMemo(
    () => objectValues(project?.clips).sort((a, b) => a.start - b.start || a.id.localeCompare(b.id)),
    [project],
  );
  const markers = useMemo(
    () => objectValues(project?.markers).sort((a, b) => a.time - b.time || a.id.localeCompare(b.id)),
    [project],
  );
  const notes = useMemo(
    () => objectValues(project?.notes).sort((a, b) => (a.target.time || 0) - (b.target.time || 0) || a.id.localeCompare(b.id)),
    [project],
  );

  const selectedClip = selectedClipId ? project?.clips[selectedClipId] : undefined;
  const selectedAsset = selectedClip ? project?.assets[selectedClip.assetId] : undefined;
  const duration = useMemo(() => {
    const clipEnd = clips.reduce((max, clip) => Math.max(max, clip.start + clip.duration), 0);
    const markerEnd = markers.reduce((max, marker) => Math.max(max, marker.time), 0);
    return Math.max(6, Math.ceil(Math.max(clipEnd, markerEnd) + 1));
  }, [clips, markers]);
  const timelineWidth = Math.max(720, duration * timeScale);
  const visibleFrame = previewFrame || project?.renderCache?.frames?.[currentTime.toFixed(2)]?.frame || project?.renderCache?.frames?.[currentTime.toFixed(1)]?.frame || '';
  const videoPath = project?.renderCache?.video?.video || '';

  const loadProject = useCallback(async () => {
    if (!projectId) {
      setError('Missing editor project id.');
      setLoading(false);
      return;
    }
    setError('');
    setLoading(true);
    try {
      const response = await fetch(apiUrl(`/api/agenticnews/editor-timelines/${projectId}`));
      if (!response.ok) throw new Error(`timeline load failed (${response.status})`);
      const next = await response.json() as EditorProject;
      setProject(next);
      const firstClip = objectValues(next.clips).sort((a, b) => a.start - b.start)[0];
      if (firstClip) {
        setSelectedClipId((current) => current && next.clips[current] ? current : firstClip.id);
        setCurrentTime((current) => current || firstClip.start);
      }
    } catch (err) {
      setProject(null);
      setError(err instanceof Error ? err.message : 'timeline load failed');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadProject();
  }, [loadProject]);

  useEffect(() => {
    if (!selectedClip) return;
    setDraftTiming({
      start: String(roundTime(selectedClip.start)),
      duration: String(roundTime(selectedClip.duration)),
      sourceStart: String(roundTime(selectedClip.sourceStart || 0)),
      x: String(selectedClip.transform?.x ?? 0.5),
      y: String(selectedClip.transform?.y ?? 0.5),
      scale: String(selectedClip.transform?.scale ?? 1),
      opacity: String(selectedClip.transform?.opacity ?? 1),
      volume: String(selectedClip.volume ?? 1),
    });
  }, [selectedClip]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setCurrentTime((time) => {
        const next = roundTime(time + 0.1);
        if (next > duration) {
          setPlaying(false);
          return duration;
        }
        return next;
      });
    }, 100);
    return () => window.clearInterval(timer);
  }, [duration, playing]);

  const applyCommand = useCallback(async (op: string, payload: Record<string, unknown>) => {
    if (!project) return null;
    const command: CommandBody = {
      op,
      actor: 'human',
      expectedRevision: project.revision,
      payload,
    };
    setBusyOp(op);
    setError('');
    try {
      const response = await fetch(apiUrl(`/api/agenticnews/editor-timelines/${project.projectId}/commands`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(command),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `command failed (${response.status})`);
      }
      const next = await response.json() as EditorProject;
      setProject(next);
      return next;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'command failed');
      return null;
    } finally {
      setBusyOp('');
    }
  }, [project]);

  const selectClip = (clip: EditorClip) => {
    setSelectedClipId(clip.id);
    setCurrentTime(roundTime(clip.start));
  };

  const moveSelected = async (delta: number) => {
    if (!selectedClip) return;
    const nextStart = roundTime(selectedClip.start + delta);
    const updated = await applyCommand('clip.move', { clipId: selectedClip.id, start: nextStart });
    if (updated) setCurrentTime(nextStart);
  };

  const applyTiming = async () => {
    if (!selectedClip) return;
    const start = roundTime(Number(draftTiming.start));
    const durationValue = Math.max(0.1, roundTime(Number(draftTiming.duration)));
    const sourceStart = roundTime(Number(draftTiming.sourceStart));
    const updated = await applyCommand('clip.trim', {
      clipId: selectedClip.id,
      start,
      duration: durationValue,
      sourceStart,
    });
    if (updated) setCurrentTime(start);
  };

  const applyTransform = async () => {
    if (!selectedClip) return;
    await applyCommand('clip.transform', {
      clipId: selectedClip.id,
      transform: {
        x: Number(draftTiming.x),
        y: Number(draftTiming.y),
        scale: Number(draftTiming.scale),
        opacity: Number(draftTiming.opacity),
      },
    });
    if ((project?.tracks[selectedClip.trackId]?.kind || '').includes('audio') || selectedAsset?.type === 'audio') {
      await applyCommand('clip.volume', { clipId: selectedClip.id, volume: Number(draftTiming.volume) });
    }
  };

  const splitSelected = async () => {
    if (!selectedClip) return;
    const at = Math.min(selectedClip.start + selectedClip.duration - 0.1, Math.max(selectedClip.start + 0.1, currentTime));
    await applyCommand('clip.split', {
      clipId: selectedClip.id,
      at: roundTime(at),
      newClipId: `${selectedClip.id}_split_${Date.now().toString(36)}`,
    });
  };

  const toggleEnabled = async () => {
    if (!selectedClip) return;
    await applyCommand(selectedClip.enabled ? 'clip.hide' : 'clip.show', { clipId: selectedClip.id });
  };

  const toggleMuted = async () => {
    if (!selectedClip) return;
    await applyCommand(selectedClip.muted ? 'clip.unmute' : 'clip.mute', { clipId: selectedClip.id });
  };

  const addMarker = async () => {
    await applyCommand('marker.add', {
      time: roundTime(currentTime),
      label: markerLabel.trim() || 'Review marker',
      metadata: { source: 'editor-bay' },
    });
  };

  const addClipNote = async () => {
    if (!selectedClip || !noteText.trim()) return;
    const time = roundTime(currentTime);
    const nextStart = roundTime(selectedClip.start + 0.5);
    const updated = await applyCommand('note.add', {
      target: { clipId: selectedClip.id, time, frame: time },
      text: noteText.trim(),
      suggestedCommand: {
        op: 'clip.move',
        payload: { clipId: selectedClip.id, start: nextStart },
      },
      metadata: { source: 'editor-bay', assetId: selectedClip.assetId },
    });
    if (updated) setNoteText('');
  };

  const renderFrame = async () => {
    if (!project) return;
    setBusyOp('render.frame');
    setError('');
    try {
      const response = await fetch(apiUrl(`/api/agenticnews/editor-render/${project.projectId}/frame`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ at: roundTime(currentTime) }),
      });
      if (!response.ok) throw new Error(`frame render failed (${response.status})`);
      const result = await response.json() as { frame?: string };
      if (result.frame) setPreviewFrame(result.frame);
      await loadProject();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'frame render failed');
    } finally {
      setBusyOp('');
    }
  };

  const startDrag = (clip: EditorClip, event: PointerEvent<HTMLButtonElement>) => {
    const track = event.currentTarget.parentElement;
    if (!track) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDrag({
      clipId: clip.id,
      originX: event.clientX,
      originStart: clip.start,
      trackWidth: track.clientWidth || timelineWidth,
    });
    selectClip(clip);
  };

  const updateDrag = (event: PointerEvent<HTMLButtonElement>) => {
    if (!drag || drag.clipId !== selectedClipId) return;
    const deltaPx = event.clientX - drag.originX;
    const secondsPerPixel = duration / drag.trackWidth;
    setCurrentTime(roundTime(drag.originStart + deltaPx * secondsPerPixel));
  };

  const endDrag = async (event: PointerEvent<HTMLButtonElement>) => {
    if (!drag || drag.clipId !== selectedClipId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const deltaPx = event.clientX - drag.originX;
    const secondsPerPixel = duration / drag.trackWidth;
    const nextStart = roundTime(drag.originStart + deltaPx * secondsPerPixel);
    setDrag(null);
    if (Math.abs(nextStart - drag.originStart) >= 0.05) {
      await applyCommand('clip.move', { clipId: drag.clipId, start: nextStart });
    }
  };

  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center bg-[#080a0f] text-slate-200">
        <div className="text-sm font-semibold text-slate-400">Loading editor timeline</div>
      </div>
    );
  }

  if (error && !project) {
    return (
      <div className="grid min-h-screen place-items-center bg-[#080a0f] px-6 text-slate-100">
        <div className="max-w-md rounded-md border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
          {error}
        </div>
      </div>
    );
  }

  if (!project) return null;

  return (
    <div className="min-h-screen bg-[#080a0f] text-slate-100">
      <header className="border-b border-white/10 bg-[#11151d]">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-base font-semibold tracking-normal">{project.title || project.projectId}</h1>
              <span className="rounded border border-white/10 bg-white/5 px-2 py-0.5 text-xs font-semibold text-slate-300">
                rev {project.revision}
              </span>
              <span className="rounded border border-white/10 bg-white/5 px-2 py-0.5 text-xs text-slate-400">
                {clips.length} clips
              </span>
            </div>
            <div className="mt-1 text-xs text-slate-500">
              {project.projectId} · {project.width}x{project.height} · {project.fps} fps
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPlaying((value) => !value)}
              className="inline-flex h-9 items-center gap-2 rounded-md border border-white/10 bg-white/5 px-3 text-sm font-semibold text-slate-100 hover:border-cyan-400/50"
            >
              {playing ? <Pause size={16} /> : <Play size={16} />}
              {playing ? 'Pause' : 'Play'}
            </button>
            <button
              type="button"
              onClick={renderFrame}
              disabled={busyOp === 'render.frame'}
              className="inline-flex h-9 items-center gap-2 rounded-md bg-cyan-400 px-3 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-wait disabled:opacity-60"
            >
              <RefreshCw size={16} />
              Render frame
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div className="border-b border-red-500/20 bg-red-500/10 px-4 py-2 text-sm text-red-100">
          {error}
        </div>
      )}

      <main className="grid min-h-[calc(100vh-65px)] grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
        <section className="flex min-w-0 flex-col">
          <div className="grid min-h-[300px] grid-cols-1 gap-4 border-b border-white/10 p-4 xl:grid-cols-[minmax(320px,0.75fr)_minmax(320px,1fr)]">
            <div className="flex min-h-[260px] items-center justify-center overflow-hidden rounded-md border border-white/10 bg-black">
              {visibleFrame ? (
                <img
                  src={staticUrl(visibleFrame)}
                  alt="Rendered preview frame"
                  className="h-full max-h-[360px] w-full object-contain"
                />
              ) : videoPath ? (
                <video
                  src={staticUrl(videoPath)}
                  controls
                  className="h-full max-h-[360px] w-full object-contain"
                />
              ) : (
                <div className="text-center text-sm text-slate-500">
                  <div className="text-slate-300">{fmt(currentTime)}</div>
                  <div>No preview render cached</div>
                </div>
              )}
            </div>

            <div className="min-w-0 rounded-md border border-white/10 bg-[#0c1017] p-3">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <div className="text-sm font-semibold">Inspector</div>
                  <div className="text-xs text-slate-500">{selectedClip?.id || 'Select a clip'}</div>
                </div>
                {selectedClip && (
                  <span className="rounded bg-white/5 px-2 py-1 text-xs text-slate-400">
                    {selectedAsset?.type || selectedClip.kind}
                  </span>
                )}
              </div>

              {selectedClip ? (
                <div className="space-y-3">
                  <div className="grid grid-cols-3 gap-2">
                    <LabeledInput label="Start" value={draftTiming.start} onChange={(value) => setDraftTiming((draft) => ({ ...draft, start: value }))} />
                    <LabeledInput label="Duration" value={draftTiming.duration} onChange={(value) => setDraftTiming((draft) => ({ ...draft, duration: value }))} />
                    <LabeledInput label="Source" value={draftTiming.sourceStart} onChange={(value) => setDraftTiming((draft) => ({ ...draft, sourceStart: value }))} />
                  </div>
                  <div className="grid grid-cols-5 gap-2">
                    <LabeledInput label="X" value={draftTiming.x} onChange={(value) => setDraftTiming((draft) => ({ ...draft, x: value }))} />
                    <LabeledInput label="Y" value={draftTiming.y} onChange={(value) => setDraftTiming((draft) => ({ ...draft, y: value }))} />
                    <LabeledInput label="Scale" value={draftTiming.scale} onChange={(value) => setDraftTiming((draft) => ({ ...draft, scale: value }))} />
                    <LabeledInput label="Opacity" value={draftTiming.opacity} onChange={(value) => setDraftTiming((draft) => ({ ...draft, opacity: value }))} />
                    <LabeledInput label="Volume" value={draftTiming.volume} onChange={(value) => setDraftTiming((draft) => ({ ...draft, volume: value }))} />
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <ActionButton label="Apply timing" onClick={applyTiming} disabled={Boolean(busyOp)} />
                    <ActionButton label="Apply properties" onClick={applyTransform} disabled={Boolean(busyOp)} />
                    <ActionButton label="Nudge clip later" icon={<SkipForward size={15} />} onClick={() => moveSelected(0.5)} disabled={Boolean(busyOp)} />
                    <ActionButton label="Split at playhead" icon={<Scissors size={15} />} onClick={splitSelected} disabled={Boolean(busyOp)} />
                    <ActionButton
                      label={selectedClip.enabled ? 'Hide clip' : 'Show clip'}
                      icon={selectedClip.enabled ? <EyeOff size={15} /> : <Eye size={15} />}
                      onClick={toggleEnabled}
                      disabled={Boolean(busyOp)}
                    />
                    <ActionButton
                      label={selectedClip.muted ? 'Unmute clip' : 'Mute clip'}
                      icon={selectedClip.muted ? <Volume2 size={15} /> : <VolumeX size={15} />}
                      onClick={toggleMuted}
                      disabled={Boolean(busyOp)}
                    />
                  </div>
                </div>
              ) : (
                <div className="rounded border border-dashed border-white/10 p-4 text-sm text-slate-500">
                  Select a clip on the timeline.
                </div>
              )}
            </div>
          </div>

          <div className="border-b border-white/10 bg-[#0b0f16] px-4 py-3">
            <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
              <span>{fmt(currentTime)}</span>
              <span>{fmt(duration)}</span>
            </div>
            <input
              aria-label="Timeline playhead"
              type="range"
              min={0}
              max={duration}
              step={0.1}
              value={Math.min(currentTime, duration)}
              onChange={(event) => setCurrentTime(roundTime(Number(event.currentTarget.value)))}
              className="w-full accent-cyan-400"
            />
          </div>

          <div className="min-h-0 flex-1 overflow-auto bg-[#090c12] p-4">
            <div className="relative" style={{ width: timelineWidth + 160 }}>
              <TimeRuler duration={duration} />
              {markers.map((marker) => (
                <button
                  type="button"
                  key={marker.id}
                  onClick={() => setCurrentTime(roundTime(marker.time))}
                  className="absolute top-7 z-20 h-[calc(100%-28px)] w-px bg-amber-300/70"
                  style={{ left: 160 + marker.time * timeScale }}
                  title={`${marker.label} @ ${fmt(marker.time)}`}
                >
                  <span className="absolute -left-2 -top-4 rounded bg-amber-300 px-1 text-[10px] font-bold text-slate-950">
                    {marker.label}
                  </span>
                </button>
              ))}
              <div
                className="pointer-events-none absolute top-7 z-30 h-[calc(100%-28px)] w-px bg-white"
                style={{ left: 160 + currentTime * timeScale }}
              />
              <div className="space-y-2 pt-7">
                {tracks.map((track) => {
                  const trackClips = clips.filter((clip) => clip.trackId === track.id);
                  return (
                    <div key={track.id} className="grid h-16 grid-cols-[150px_minmax(720px,1fr)] gap-2">
                      <div className="flex flex-col justify-center rounded-md border border-white/10 bg-[#11151d] px-3">
                        <div className="truncate text-sm font-semibold">{track.name}</div>
                        <div className="text-xs text-slate-500">{track.kind}</div>
                      </div>
                      <div className="relative overflow-hidden rounded-md border border-white/10 bg-[#0d121a]">
                        <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(255,255,255,0.05)_1px,transparent_1px)] bg-[length:80px_100%]" />
                        {track.kind === 'audio' && (
                          <div className="absolute inset-x-0 top-1/2 h-4 -translate-y-1/2 opacity-60">
                            <div className="h-full bg-[repeating-linear-gradient(90deg,rgba(125,211,252,0.25)_0_3px,transparent_3px_8px)]" />
                          </div>
                        )}
                        {trackClips.map((clip) => {
                          const asset = project.assets[clip.assetId];
                          const selected = clip.id === selectedClipId;
                          const ghostStart = drag?.clipId === clip.id ? currentTime : clip.start;
                          return (
                            <button
                              type="button"
                              key={clip.id}
                              aria-label={`${clip.id} ${track.name}`}
                              onClick={() => selectClip(clip)}
                              onPointerDown={(event) => startDrag(clip, event)}
                              onPointerMove={updateDrag}
                              onPointerUp={endDrag}
                              className={`absolute top-2 h-12 rounded-md border px-2 text-left text-xs shadow-sm transition ${
                                selected
                                  ? 'border-cyan-300 bg-cyan-300 text-slate-950'
                                  : clip.enabled
                                    ? 'border-cyan-400/30 bg-cyan-400/20 text-cyan-50 hover:bg-cyan-400/30'
                                    : 'border-slate-500/30 bg-slate-700/20 text-slate-500'
                              }`}
                              style={{
                                left: ghostStart * timeScale,
                                width: Math.max(36, clip.duration * timeScale),
                              }}
                            >
                              <span className="block truncate font-semibold">{clip.id}</span>
                              <span className="block truncate opacity-75">{asset?.type || clip.kind} · {fmt(clip.start)}</span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </section>

        <aside className="border-t border-white/10 bg-[#11151d] lg:border-l lg:border-t-0">
          <div className="space-y-4 p-4">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Notes</h2>
                <span className="text-xs text-slate-500">{notes.length}</span>
              </div>
              <label className="block text-xs font-semibold text-slate-400" htmlFor="editor-note-text">
                Note text
              </label>
              <textarea
                id="editor-note-text"
                value={noteText}
                onChange={(event) => setNoteText(event.currentTarget.value)}
                className="mt-1 min-h-24 w-full resize-none rounded-md border border-white/10 bg-[#090c12] px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
                placeholder="Add an exact clip/time note"
              />
              <button
                type="button"
                onClick={addClipNote}
                disabled={!selectedClip || !noteText.trim() || Boolean(busyOp)}
                className="mt-2 inline-flex h-9 w-full items-center justify-center gap-2 rounded-md bg-cyan-400 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <MessageSquarePlus size={16} />
                Add clip note
              </button>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-400" htmlFor="editor-marker-label">
                Marker label
              </label>
              <input
                id="editor-marker-label"
                value={markerLabel}
                onChange={(event) => setMarkerLabel(event.currentTarget.value)}
                className="mt-1 h-9 w-full rounded-md border border-white/10 bg-[#090c12] px-3 text-sm text-slate-100 outline-none focus:border-cyan-400"
              />
              <button
                type="button"
                onClick={addMarker}
                disabled={Boolean(busyOp)}
                className="mt-2 inline-flex h-9 w-full items-center justify-center gap-2 rounded-md border border-white/10 bg-white/5 text-sm font-semibold text-slate-100 hover:border-amber-300/60 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Flag size={16} />
                Add marker
              </button>
            </div>
          </div>

          <div className="border-t border-white/10">
            {notes.length === 0 ? (
              <div className="p-4 text-sm text-slate-500">No notes on this timeline.</div>
            ) : (
              <div className="divide-y divide-white/10">
                {notes.map((note) => (
                  <button
                    type="button"
                    key={note.id}
                    onClick={() => {
                      if (note.target.clipId) setSelectedClipId(note.target.clipId);
                      setCurrentTime(roundTime(note.target.time || 0));
                    }}
                    className="block w-full p-4 text-left hover:bg-white/5"
                  >
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-cyan-300">{fmt(note.target.time || 0)}</span>
                      <span className="truncate text-xs text-slate-500">{note.target.clipId || note.target.trackId || 'frame'}</span>
                    </div>
                    <div className="text-sm text-slate-200">{note.text}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}

function TimeRuler({ duration }: { duration: number }) {
  const ticks = [];
  for (let time = 0; time <= duration; time += 1) ticks.push(time);
  return (
    <div className="absolute left-[160px] right-0 top-0 h-7">
      {ticks.map((time) => (
        <div
          key={time}
          className="absolute top-0 h-7 border-l border-white/10 pl-1 text-[10px] text-slate-500"
          style={{ left: time * timeScale }}
        >
          {fmt(time)}
        </div>
      ))}
    </div>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-semibold text-slate-500">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="h-8 w-full rounded-md border border-white/10 bg-[#090c12] px-2 text-xs text-slate-100 outline-none focus:border-cyan-400"
      />
    </label>
  );
}

function ActionButton({
  label,
  icon,
  onClick,
  disabled,
}: {
  label: string;
  icon?: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-white/10 bg-white/5 px-3 text-xs font-semibold text-slate-100 hover:border-cyan-400/50 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {icon}
      {label}
    </button>
  );
}
