import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
  type ReactNode,
} from "react";
import {
  AlertTriangle,
  Eye,
  EyeOff,
  Film,
  Flag,
  MessageSquarePlus,
  Pause,
  Play,
  RefreshCw,
  Scissors,
  SkipForward,
  Undo2,
  Volume2,
  VolumeX,
} from "lucide-react";
import { apiUrl, staticUrl } from "../lib/api";

type AssetKind = "image" | "video" | "audio" | "title" | string;

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
    video?: {
      video?: string;
      start?: number;
      duration?: number;
      backend?: string;
      revision?: number;
    };
    windows?: Record<
      string,
      {
        video?: string;
        start?: number;
        duration?: number;
        backend?: string;
        revision?: number;
      }
    >;
    frames?: Record<
      string,
      { frame?: string; at?: number; backend?: string; revision?: number }
    >;
  };
  commandLog?: unknown[];
}

interface CommandBody {
  op: string;
  actor: "human";
  expectedRevision: number;
  payload: Record<string, unknown>;
}

interface EditorAssetHealth {
  ok: boolean;
  renderable: boolean;
  projectId?: string;
  revision?: number;
  checkedFiles?: number;
  missingFiles?: {
    assetId?: string;
    type?: string;
    src?: string;
    file?: string;
    clipIds?: string[];
    enabledClipIds?: string[];
  }[];
  uniqueMissingFiles?: string[];
  badSources?: unknown[];
  missingClipAssets?: unknown[];
  missingClipTracks?: unknown[];
  blockedMaterializations?: unknown[];
  copyCandidates?: unknown[];
  derivativeMaterializations?: unknown[];
  wouldMutateOnLoad?: boolean;
  wouldMutateOnLoadReasons?: string[];
}

function hasRevertableCommand(commandLog?: unknown[]) {
  if (!Array.isArray(commandLog)) return false;
  const reverted = new Set(
    commandLog
      .map((entry) =>
        entry && typeof entry === "object"
          ? String((entry as { revertsCommandId?: unknown }).revertsCommandId || "")
          : "",
      )
      .filter(Boolean),
  );
  return commandLog.some((entry) => {
    if (!entry || typeof entry !== "object") return false;
    const command = entry as { id?: unknown; revertsCommandId?: unknown };
    const id = String(command.id || "");
    return Boolean(id) && !command.revertsCommandId && !reverted.has(id);
  });
}

interface DragState {
  clipId: string;
  originX: number;
  originStart: number;
  trackWidth: number;
}

const timeScale = 80;
const renderWindowStartTolerance = 0.02;

const outputChangingOps = new Set([
  "asset.import",
  "track.create",
  "clip.create",
  "clip.split",
  "clip.unsplit",
  "clip.move",
  "clip.trim",
  "clip.update",
  "clip.hide",
  "clip.show",
  "clip.mute",
  "clip.unmute",
  "clip.transform",
  "clip.opacity",
  "clip.volume",
]);

const commandChangesOutput = (op: string) => outputChangingOps.has(op);

const fmt = (seconds: number) => {
  const safe = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const mins = Math.floor(safe / 60);
  const secs = Math.floor(safe % 60);
  const tenths = Math.floor((safe % 1) * 10);
  return `${mins}:${secs.toString().padStart(2, "0")}.${tenths}`;
};

const roundTime = (value: number) => Math.round(Math.max(0, value) * 100) / 100;

const projectIdFromPath = () => {
  const match = window.location.pathname.match(/^\/editor\/(.+)$/);
  return match ? decodeURIComponent(match[1]) : "";
};

const objectValues = <T,>(record: Record<string, T> | undefined): T[] =>
  Object.values(record || {});

const recordValue = (value: unknown): Record<string, unknown> | undefined =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;

const segmentName = (clip: EditorClip, asset?: EditorAsset) => {
  const shot = recordValue(clip.metadata?.shot);
  const rawSegment = String(
    clip.metadata?.segmentId ||
      asset?.metadata?.segmentId ||
      shot?.segmentId ||
      "",
  );
  const match = rawSegment.match(/(?:^|_)s(\d+)$/);
  return match ? `Segment ${Number(match[1]) + 1}` : "Timeline";
};

const shotName = (clip: EditorClip) => {
  const shot = recordValue(clip.metadata?.shot);
  const rawShot = String(shot?.id || "");
  const match = rawShot.match(/^shot(\d+)$/);
  return match ? `Shot ${Number(match[1]) + 1}` : "";
};

const layerKindName = (clip: EditorClip, asset?: EditorAsset) => {
  const kind = String(clip.kind || asset?.type || "").toLowerCase();
  const assetType = String(asset?.type || "").toLowerCase();

  if (kind.includes("voice")) return "Voice";
  if (kind.includes("music")) return "Music";
  if (kind === "title" || kind === "lower_third" || assetType === "title")
    return "Title";
  if (assetType === "video") return "Video Layer";
  if (assetType === "audio") return "Audio";
  if (assetType === "image" || kind === "artifact") return "Visual Layer";
  return kind ? kind.replace(/_/g, " ") : "Clip";
};

const clipDisplayName = (clip: EditorClip, asset?: EditorAsset) =>
  [segmentName(clip, asset), shotName(clip), layerKindName(clip, asset)]
    .filter(Boolean)
    .join(" · ");

const noteTargetDisplayName = (note: EditorNote, project: EditorProject) => {
  if (note.target.clipId) {
    const clip = project.clips[note.target.clipId];
    return clip ? clipDisplayName(clip, project.assets[clip.assetId]) : "Clip";
  }

  if (note.target.trackId) {
    return project.tracks[note.target.trackId]?.name || "Track";
  }

  return "Frame";
};

const withCacheToken = (
  url: string,
  token?: number | string | null,
) => {
  if (!url || token === undefined || token === null || token === "") return url;
  if (/^(data:|blob:)/.test(url)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}rev=${encodeURIComponent(
    String(token),
  )}`;
};

const mediaUrl = (path: string, token?: number | string | null) => {
  if (!path) return "";
  if (/^(https?:|data:|blob:)/.test(path)) return withCacheToken(path, token);

  const assetMarker = "/agenticnews_assets/";
  const assetRootIndex = path.indexOf(assetMarker);
  if (assetRootIndex >= 0) {
    return withCacheToken(
      staticUrl(
        `/agenticnews-assets/${path.slice(assetRootIndex + assetMarker.length)}`,
      ),
      token,
    );
  }

  if (path.startsWith("agenticnews_assets/")) {
    return withCacheToken(
      staticUrl(
        `/agenticnews-assets/${path.slice("agenticnews_assets/".length)}`,
      ),
      token,
    );
  }

  return withCacheToken(staticUrl(path.startsWith("/") ? path : `/${path}`), token);
};

export function EditorBayPage() {
  const projectId = useMemo(projectIdFromPath, []);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const timelineScrollRef = useRef<HTMLDivElement | null>(null);
  const [project, setProject] = useState<EditorProject | null>(null);
  const [selectedClipId, setSelectedClipId] = useState<string>("");
  const [currentTime, setCurrentTime] = useState(0);
  const [noteText, setNoteText] = useState("");
  const [markerLabel, setMarkerLabel] = useState("Review marker");
  const [error, setError] = useState("");
  const [assetHealth, setAssetHealth] = useState<EditorAssetHealth | null>(
    null,
  );
  const [assetHealthError, setAssetHealthError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyOp, setBusyOp] = useState("");
  const [playing, setPlaying] = useState(false);
  const [previewFrame, setPreviewFrame] = useState<{
    frame: string;
    at: number;
    revision: number;
  } | null>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [draftTiming, setDraftTiming] = useState({
    start: "0",
    duration: "1",
    sourceStart: "0",
    x: "0.5",
    y: "0.5",
    scale: "1",
    opacity: "1",
    volume: "1",
  });

  const tracks = useMemo(
    () =>
      objectValues(project?.tracks).sort(
        (a, b) => a.index - b.index || a.id.localeCompare(b.id),
      ),
    [project],
  );
  const clips = useMemo(
    () =>
      objectValues(project?.clips).sort(
        (a, b) => a.start - b.start || a.id.localeCompare(b.id),
      ),
    [project],
  );
  const markers = useMemo(
    () =>
      objectValues(project?.markers).sort(
        (a, b) => a.time - b.time || a.id.localeCompare(b.id),
      ),
    [project],
  );
  const notes = useMemo(
    () =>
      objectValues(project?.notes).sort(
        (a, b) =>
          (a.target.time || 0) - (b.target.time || 0) ||
          a.id.localeCompare(b.id),
      ),
    [project],
  );

  const selectedClip = selectedClipId
    ? project?.clips[selectedClipId]
    : undefined;
  const selectedAsset = selectedClip
    ? project?.assets[selectedClip.assetId]
    : undefined;
  const selectedTrack = selectedClip
    ? project?.tracks[selectedClip.trackId]
    : undefined;
  const duration = useMemo(() => {
    const clipEnd = clips.reduce(
      (max, clip) => Math.max(max, clip.start + clip.duration),
      0,
    );
    const markerEnd = markers.reduce(
      (max, marker) => Math.max(max, marker.time),
      0,
    );
    return Math.max(6, Math.ceil(Math.max(clipEnd, markerEnd) + 1));
  }, [clips, markers]);
  const timelineWidth = Math.max(720, duration * timeScale);
  const cacheEntryIsCurrent = useCallback(
    (entry: { revision?: number } | undefined) =>
      Boolean(
        entry &&
          entry.revision !== undefined &&
          Number(entry.revision) === Number(project?.revision || 0),
      ),
    [project?.revision],
  );
  const cachedFrame = useMemo(() => {
    const frames = project?.renderCache?.frames || {};
    const exact = frames[currentTime.toFixed(2)];
    const rounded = frames[currentTime.toFixed(1)];
    if (exact && cacheEntryIsCurrent(exact)) return exact.frame || "";
    if (rounded && cacheEntryIsCurrent(rounded)) return rounded.frame || "";
    return "";
  }, [cacheEntryIsCurrent, currentTime, project?.renderCache?.frames]);
  const freshPreviewFrame =
    previewFrame &&
    Number(previewFrame.revision) === Number(project?.revision || 0) &&
    Math.abs(previewFrame.at - currentTime) < 0.05
      ? previewFrame.frame
      : "";
  const visibleFrame = freshPreviewFrame || cachedFrame;
  const visibleFrameUrl = mediaUrl(visibleFrame, visibleFrame ? project?.revision : undefined);
  const activeWindowRender = useMemo(() => {
    const windows = objectValues(project?.renderCache?.windows);
    return windows
      .filter((item) => {
        const start = Number(item.start || 0);
        const end = start + Number(item.duration || 0);
        return (
          cacheEntryIsCurrent(item) &&
          item.video &&
          currentTime + renderWindowStartTolerance >= start &&
          currentTime < end
        );
      })
      .sort((a, b) => Number(b.duration || 0) - Number(a.duration || 0))[0];
  }, [cacheEntryIsCurrent, currentTime, project?.renderCache?.windows]);
  const fullVideoRender = cacheEntryIsCurrent(project?.renderCache?.video)
    ? project?.renderCache?.video
    : undefined;
  const activeVideoRender = activeWindowRender?.video
    ? activeWindowRender
    : fullVideoRender?.video
      ? fullVideoRender
      : undefined;
  const activeVideoStart = activeWindowRender?.video
    ? Number(activeWindowRender.start || 0)
    : Number(fullVideoRender?.start || 0);
  const videoPath = activeVideoRender?.video || "";
  const videoUrl = mediaUrl(
    videoPath,
    activeVideoRender?.revision ?? project?.revision,
  );

  const seekPlayhead = useCallback(
    (value: number) => {
      const next = roundTime(Math.min(Math.max(0, value), duration));
      setCurrentTime(next);
      const video = videoRef.current;
      const videoTime = Math.max(0, next - activeVideoStart);
      if (video && Math.abs(video.currentTime - videoTime) > 0.08) {
        video.currentTime = videoTime;
      }
    },
    [activeVideoStart, duration],
  );

  useEffect(() => {
    if (!videoUrl) return;
    const video = videoRef.current;
    if (!video) return;
    const videoTime = Math.max(0, currentTime - activeVideoStart);
    if (Math.abs(video.currentTime - videoTime) > 0.08) {
      video.currentTime = videoTime;
    }
  }, [activeVideoStart, currentTime, videoUrl]);

  useEffect(() => {
    const viewport = timelineScrollRef.current;
    if (!viewport || viewport.clientWidth <= 0) return;
    const playheadX = 160 + currentTime * timeScale;
    const margin = Math.min(240, viewport.clientWidth * 0.25);
    const left = viewport.scrollLeft;
    const right = left + viewport.clientWidth;
    const maxScrollLeft =
      viewport.scrollWidth > viewport.clientWidth
        ? viewport.scrollWidth - viewport.clientWidth
        : Number.POSITIVE_INFINITY;
    const clampedScrollLeft = (value: number) =>
      Math.max(0, Math.min(value, maxScrollLeft));
    if (playheadX < left + margin) {
      viewport.scrollLeft = clampedScrollLeft(playheadX - margin);
    } else if (playheadX > right - margin) {
      viewport.scrollLeft = clampedScrollLeft(
        playheadX - viewport.clientWidth + margin,
      );
    }
  }, [currentTime, selectedClipId, timelineWidth, videoUrl]);

  const loadProject = useCallback(async () => {
    if (!projectId) {
      setError("Missing editor project id.");
      setLoading(false);
      return;
    }
    setError("");
    setLoading(true);
    try {
      const response = await fetch(
        apiUrl(`/api/agenticnews/editor-timelines/${projectId}`),
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(
          body.detail || `timeline load failed (${response.status})`,
        );
      }
      const next = (await response.json()) as EditorProject;
      setProject(next);
      setAssetHealth(null);
      setAssetHealthError("");
      try {
        const healthResponse = await fetch(
          apiUrl(
            `/api/agenticnews/editor-timelines/${projectId}/asset-health`,
          ),
        );
        if (healthResponse.ok) {
          setAssetHealth((await healthResponse.json()) as EditorAssetHealth);
        } else {
          setAssetHealthError(
            `asset health unavailable (${healthResponse.status})`,
          );
        }
      } catch (healthErr) {
        setAssetHealthError(
          healthErr instanceof Error
            ? healthErr.message
            : "asset health unavailable",
        );
      }
      const firstClip = objectValues(next.clips).sort(
        (a, b) => a.start - b.start,
      )[0];
      if (firstClip) {
        setSelectedClipId((current) =>
          current && next.clips[current] ? current : firstClip.id,
        );
        setCurrentTime((current) => current || roundTime(firstClip.start));
      }
    } catch (err) {
      setProject(null);
      setError(err instanceof Error ? err.message : "timeline load failed");
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
    if (playing && !videoUrl) setPlaying(false);
  }, [playing, videoUrl]);

  const togglePlayback = async () => {
    const video = videoRef.current;
    if (videoUrl && video) {
      if (video.paused) {
        video.currentTime = Math.max(0, currentTime - activeVideoStart);
        try {
          await video.play();
          setPlaying(true);
        } catch (err) {
          setError(
            err instanceof Error ? err.message : "video playback failed",
          );
        }
      } else {
        video.pause();
        setPlaying(false);
      }
      return;
    }
    setPlaying(false);
    setError("Render a video window before playback");
  };

  const applyCommand = useCallback(
    async (op: string, payload: Record<string, unknown>) => {
      if (!project) return null;
      const command: CommandBody = {
        op,
        actor: "human",
        expectedRevision: project.revision,
        payload,
      };
      setBusyOp(op);
      setError("");
      try {
        const response = await fetch(
          apiUrl(
            `/api/agenticnews/editor-timelines/${project.projectId}/commands`,
          ),
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(command),
          },
        );
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `command failed (${response.status})`);
        }
        const next = (await response.json()) as EditorProject;
        if (commandChangesOutput(op)) {
          setPreviewFrame(null);
          setPlaying(false);
        }
        setProject(next);
        return next;
      } catch (err) {
        setError(err instanceof Error ? err.message : "command failed");
        return null;
      } finally {
        setBusyOp("");
      }
    },
    [project],
  );

  const undoLastCommand = async () => {
    if (!project) return;
    setBusyOp("revert-last");
    setError("");
    try {
      const response = await fetch(
        apiUrl(
          `/api/agenticnews/editor-timelines/${project.projectId}/commands/revert-last`,
        ),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            actor: "human",
            expectedRevision: project.revision,
          }),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `undo failed (${response.status})`);
      }
      const next = (await response.json()) as EditorProject;
      const lastCommand = next.commandLog?.[
        Math.max(0, (next.commandLog?.length || 1) - 1)
      ] as { op?: string } | undefined;
      const inverseOp = String(lastCommand?.op || "");
      if (commandChangesOutput(inverseOp)) {
        setPreviewFrame(null);
        setPlaying(false);
      }
      setProject(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "undo failed");
    } finally {
      setBusyOp("");
    }
  };

  const selectClip = (clip: EditorClip) => {
    setSelectedClipId(clip.id);
    seekPlayhead(clip.start);
  };

  const moveSelected = async (delta: number) => {
    if (!selectedClip) return;
    const nextStart = roundTime(selectedClip.start + delta);
    const updated = await applyCommand("clip.move", {
      clipId: selectedClip.id,
      start: nextStart,
    });
    if (updated) seekPlayhead(nextStart);
  };

  const applyTiming = async () => {
    if (!selectedClip) return;
    const start = roundTime(Number(draftTiming.start));
    const durationValue = Math.max(
      0.1,
      roundTime(Number(draftTiming.duration)),
    );
    const sourceStart = roundTime(Number(draftTiming.sourceStart));
    const updated = await applyCommand("clip.trim", {
      clipId: selectedClip.id,
      start,
      duration: durationValue,
      sourceStart,
    });
    if (updated) seekPlayhead(start);
  };

  const applyTransform = async () => {
    if (!selectedClip) return;
    const patch: Record<string, unknown> = {
      transform: {
        x: Number(draftTiming.x),
        y: Number(draftTiming.y),
        scale: Number(draftTiming.scale),
        opacity: Number(draftTiming.opacity),
      },
    };
    if (
      (project?.tracks[selectedClip.trackId]?.kind || "").includes("audio") ||
      selectedAsset?.type === "audio"
    ) {
      patch.volume = Number(draftTiming.volume);
    }
    await applyCommand("clip.update", {
      clipId: selectedClip.id,
      patch,
    });
  };

  const splitSelected = async () => {
    if (!selectedClip) return;
    const at = roundTime(currentTime);
    const minSplit = selectedClip.start + 0.1;
    const maxSplit = selectedClip.start + selectedClip.duration - 0.1;
    if (at < minSplit || at > maxSplit) {
      setError("Move the playhead inside the selected clip before splitting.");
      return;
    }
    await applyCommand("clip.split", {
      clipId: selectedClip.id,
      at,
      newClipId: `${selectedClip.id}_split_${Date.now().toString(36)}`,
    });
  };

  const toggleEnabled = async () => {
    if (!selectedClip) return;
    await applyCommand(selectedClip.enabled ? "clip.hide" : "clip.show", {
      clipId: selectedClip.id,
    });
  };

  const toggleMuted = async () => {
    if (!selectedClip) return;
    await applyCommand(selectedClip.muted ? "clip.unmute" : "clip.mute", {
      clipId: selectedClip.id,
    });
  };

  const addMarker = async () => {
    await applyCommand("marker.add", {
      time: roundTime(currentTime),
      label: markerLabel.trim() || "Review marker",
      metadata: { source: "editor-bay" },
    });
  };

  const addClipNote = async () => {
    if (!selectedClip || !noteText.trim()) return;
    const time = roundTime(currentTime);
    const nextStart = roundTime(selectedClip.start + 0.5);
    const updated = await applyCommand("note.add", {
      target: { clipId: selectedClip.id, time, frame: time },
      text: noteText.trim(),
      suggestedCommand: {
        op: "clip.move",
        payload: { clipId: selectedClip.id, start: nextStart },
      },
      metadata: { source: "editor-bay", assetId: selectedClip.assetId },
    });
    if (updated) setNoteText("");
  };

  const renderFrame = async () => {
    if (!project) return;
    setBusyOp("render.frame");
    setError("");
    try {
      const response = await fetch(
        apiUrl(`/api/agenticnews/editor-render/${project.projectId}/frame`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ at: roundTime(currentTime) }),
        },
      );
      if (!response.ok)
        throw new Error(`frame render failed (${response.status})`);
      const result = (await response.json()) as {
        frame?: string;
        at?: number;
        revision?: number;
        currentRevision?: number;
        cacheSkipped?: boolean;
      };
      if (result.cacheSkipped) {
        setPreviewFrame(null);
        await loadProject();
        return;
      }
      if (result.frame)
        setPreviewFrame({
          frame: result.frame,
          at:
            typeof result.at === "number" ? result.at : roundTime(currentTime),
          revision:
            typeof result.revision === "number"
              ? result.revision
              : project.revision,
        });
      await loadProject();
    } catch (err) {
      setError(err instanceof Error ? err.message : "frame render failed");
    } finally {
      setBusyOp("");
    }
  };

  const renderVideoCache = async (mode: "window" | "full") => {
    if (!project) return;
    const isWindow = mode === "window";
    const windowStart = selectedClip
      ? roundTime(selectedClip.start)
      : roundTime(currentTime);
    const windowDuration = selectedClip
      ? roundTime(selectedClip.duration)
      : roundTime(Math.min(2, Math.max(0.1, duration - windowStart)));
    const busyKey = isWindow ? "render.window" : "render.full";
    setBusyOp(busyKey);
    setError("");
    try {
      const response = await fetch(
        apiUrl(`/api/agenticnews/editor-render/${project.projectId}/render`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            isWindow
              ? {
                  start: windowStart,
                  duration: windowDuration,
                }
              : {},
          ),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(
          body.detail || `${isWindow ? "window" : "full"} render failed (${response.status})`,
        );
      }
      if (isWindow) seekPlayhead(windowStart);
      await loadProject();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : `${isWindow ? "window" : "full"} render failed`,
      );
    } finally {
      setBusyOp("");
    }
  };

  const startDrag = (
    clip: EditorClip,
    event: PointerEvent<HTMLButtonElement>,
  ) => {
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
    seekPlayhead(drag.originStart + deltaPx * secondsPerPixel);
  };

  const endDrag = async (event: PointerEvent<HTMLButtonElement>) => {
    if (!drag || drag.clipId !== selectedClipId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const deltaPx = event.clientX - drag.originX;
    const secondsPerPixel = duration / drag.trackWidth;
    const nextStart = roundTime(drag.originStart + deltaPx * secondsPerPixel);
    setDrag(null);
    if (Math.abs(nextStart - drag.originStart) >= 0.05) {
      await applyCommand("clip.move", {
        clipId: drag.clipId,
        start: nextStart,
      });
      seekPlayhead(nextStart);
    }
  };

  const cancelDrag = (event: PointerEvent<HTMLButtonElement>) => {
    if (drag?.clipId === selectedClipId) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      setDrag(null);
      seekPlayhead(drag.originStart);
    }
  };

  if (loading) {
    return (
      <div className="editor-bay grid place-items-center text-slate-200">
        <div className="text-sm font-semibold text-slate-400">
          Loading editor timeline
        </div>
      </div>
    );
  }

  if (error && !project) {
    return (
      <div className="editor-bay grid place-items-center px-6 text-slate-100">
        <div className="max-w-md rounded-md border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
          {error}
        </div>
      </div>
    );
  }

  if (!project) return null;
  const canUndo = hasRevertableCommand(project.commandLog);
  const renderBlocked = Boolean(assetHealth && !assetHealth.renderable);
  const missingFileCount =
    assetHealth?.uniqueMissingFiles?.length ||
    assetHealth?.missingFiles?.length ||
    0;
  const enabledMissingFileCount =
    assetHealth?.missingFiles?.filter(
      (item) => (item.enabledClipIds || []).length > 0,
    ).length || 0;
  const blockedMaterializationCount =
    assetHealth?.blockedMaterializations?.length || 0;
  const badSourceCount = assetHealth?.badSources?.length || 0;
  const missingGraphCount =
    (assetHealth?.missingClipAssets?.length || 0) +
    (assetHealth?.missingClipTracks?.length || 0);
  const healthSummaryParts = [
    missingFileCount ? `${missingFileCount} missing files` : "",
    enabledMissingFileCount
      ? `${enabledMissingFileCount} used by enabled clips`
      : "",
    blockedMaterializationCount
      ? `${blockedMaterializationCount} blocked recoveries`
      : "",
    badSourceCount ? `${badSourceCount} bad sources` : "",
    missingGraphCount ? `${missingGraphCount} broken clip links` : "",
  ].filter(Boolean);

  return (
    <div className="editor-bay">
      <header className="editor-chrome-header">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="editor-title-metal text-base font-semibold">
                {project.title || project.projectId}
              </h1>
              <span className="editor-badge px-2 py-0.5 text-xs font-semibold">
                rev {project.revision}
              </span>
              <span className="editor-badge px-2 py-0.5 text-xs">
                {clips.length} clips
              </span>
            </div>
            <div className="mt-1 text-xs text-slate-500">
              {project.projectId} · {project.width}x{project.height} ·{" "}
              {project.fps} fps
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-start gap-2 sm:justify-end">
            <button
              type="button"
              onClick={undoLastCommand}
              disabled={!canUndo || busyOp === "revert-last"}
              className="editor-transport-button inline-flex h-9 items-center gap-2 px-3 text-sm font-semibold disabled:cursor-wait disabled:opacity-60"
            >
              <Undo2 size={16} />
              Undo
            </button>
            <button
              type="button"
              onClick={togglePlayback}
              className="editor-transport-button inline-flex h-9 items-center gap-2 px-3 text-sm font-semibold"
            >
              {playing ? <Pause size={16} /> : <Play size={16} />}
              {playing ? "Pause" : "Play"}
            </button>
            <button
              type="button"
              onClick={renderFrame}
              disabled={renderBlocked || busyOp === "render.frame"}
              className="editor-transport-button editor-primary-button inline-flex h-9 items-center gap-2 px-3 text-sm font-semibold disabled:cursor-wait disabled:opacity-60"
            >
              <RefreshCw size={16} />
              Render frame
            </button>
            <button
              type="button"
              onClick={() => void renderVideoCache("window")}
              disabled={renderBlocked || busyOp === "render.window"}
              className="editor-transport-button inline-flex h-9 items-center gap-2 px-3 text-sm font-semibold disabled:cursor-wait disabled:opacity-60"
            >
              <Film size={16} />
              Render window
            </button>
            <button
              type="button"
              onClick={() => void renderVideoCache("full")}
              disabled={renderBlocked || busyOp === "render.full"}
              className="editor-transport-button inline-flex h-9 items-center gap-2 px-3 text-sm font-semibold disabled:cursor-wait disabled:opacity-60"
            >
              <RefreshCw size={16} />
              Render full
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div className="border-b border-red-500/20 bg-red-500/10 px-4 py-2 text-sm text-red-100">
          {error}
        </div>
      )}

      {(renderBlocked || assetHealthError) && (
        <div
          className="border-b border-amber-500/25 bg-amber-500/10 px-4 py-3 text-sm text-amber-100"
          role="status"
        >
          <div className="flex flex-wrap items-start gap-3">
            <AlertTriangle
              size={18}
              className="mt-0.5 shrink-0 text-amber-300"
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1">
              <div className="font-semibold">
                {renderBlocked
                  ? "Source graph blocked"
                  : "Asset health unavailable"}
              </div>
              <div className="mt-1 text-amber-100/80">
                {renderBlocked
                  ? `${healthSummaryParts.join(" · ") || "Timeline sources are not renderable"}. Cached previews may not represent a fresh layered render.`
                  : assetHealthError}
              </div>
              {assetHealth?.wouldMutateOnLoadReasons?.length ? (
                <div className="mt-1 text-xs text-amber-100/70">
                  Load would mutate:{" "}
                  {assetHealth.wouldMutateOnLoadReasons.slice(0, 2).join(" · ")}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      )}

      <main className="grid min-h-[calc(100vh-65px)] grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
        <section className="flex min-w-0 flex-col">
          <div className="grid min-h-[300px] grid-cols-1 gap-4 border-b border-white/10 p-4 xl:grid-cols-[minmax(320px,0.75fr)_minmax(320px,1fr)]">
            <div
              className="editor-preview-shell flex min-h-[260px] items-center justify-center overflow-hidden"
              data-testid="editor-preview-shell"
              data-current-time={currentTime}
              data-active-video-src={videoUrl || ""}
              data-render-cache-window-count={
                objectValues(project?.renderCache?.windows).length
              }
              data-active-window-start={
                activeWindowRender?.video
                  ? String(activeWindowRender.start ?? "")
                  : ""
              }
            >
              {videoUrl ? (
                <video
                  ref={videoRef}
                  src={videoUrl}
                  controls
                  onTimeUpdate={(event) =>
                    setCurrentTime(
                      roundTime(
                        activeVideoStart + event.currentTarget.currentTime,
                      ),
                    )
                  }
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                  onEnded={() => setPlaying(false)}
                  className="h-full max-h-[360px] w-full object-contain"
                />
              ) : visibleFrameUrl ? (
                <img
                  src={visibleFrameUrl}
                  alt="Rendered preview frame"
                  className="h-full max-h-[360px] w-full object-contain"
                />
              ) : (
                <div className="text-center text-sm text-slate-500">
                  <div className="text-slate-300">{fmt(currentTime)}</div>
                  <div>No editor render at this playhead</div>
                </div>
              )}
            </div>

            <div className="editor-panel min-w-0 p-3">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <div className="text-sm font-semibold">Inspector</div>
                  <div className="text-xs text-slate-500">
                    {selectedClip
                      ? clipDisplayName(selectedClip, selectedAsset)
                      : "Select a clip"}
                  </div>
                  {selectedClip && (
                    <div className="text-[11px] text-slate-600">
                      {selectedTrack?.name || selectedClip.trackId} ·{" "}
                      {fmt(selectedClip.start)} to{" "}
                      {fmt(selectedClip.start + selectedClip.duration)}
                    </div>
                  )}
                </div>
                {selectedClip && (
                  <span className="editor-badge px-2 py-1 text-xs">
                    {selectedAsset?.type || selectedClip.kind}
                  </span>
                )}
              </div>

              {selectedClip ? (
                <div className="space-y-3">
                  <div className="grid grid-cols-3 gap-2">
                    <LabeledInput
                      label="Start"
                      value={draftTiming.start}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({ ...draft, start: value }))
                      }
                    />
                    <LabeledInput
                      label="Duration"
                      value={draftTiming.duration}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({
                          ...draft,
                          duration: value,
                        }))
                      }
                    />
                    <LabeledInput
                      label="Source"
                      value={draftTiming.sourceStart}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({
                          ...draft,
                          sourceStart: value,
                        }))
                      }
                    />
                  </div>
                  <div className="grid grid-cols-5 gap-2">
                    <LabeledInput
                      label="X"
                      value={draftTiming.x}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({ ...draft, x: value }))
                      }
                    />
                    <LabeledInput
                      label="Y"
                      value={draftTiming.y}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({ ...draft, y: value }))
                      }
                    />
                    <LabeledInput
                      label="Scale"
                      value={draftTiming.scale}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({ ...draft, scale: value }))
                      }
                    />
                    <LabeledInput
                      label="Opacity"
                      value={draftTiming.opacity}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({
                          ...draft,
                          opacity: value,
                        }))
                      }
                    />
                    <LabeledInput
                      label="Volume"
                      value={draftTiming.volume}
                      onChange={(value) =>
                        setDraftTiming((draft) => ({ ...draft, volume: value }))
                      }
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <ActionButton
                      label="Apply timing"
                      onClick={applyTiming}
                      disabled={Boolean(busyOp)}
                    />
                    <ActionButton
                      label="Apply properties"
                      onClick={applyTransform}
                      disabled={Boolean(busyOp)}
                    />
                    <ActionButton
                      label="Nudge clip later"
                      icon={<SkipForward size={15} />}
                      onClick={() => moveSelected(0.5)}
                      disabled={Boolean(busyOp)}
                    />
                    <ActionButton
                      label="Split at playhead"
                      icon={<Scissors size={15} />}
                      onClick={splitSelected}
                      disabled={Boolean(busyOp)}
                    />
                    <ActionButton
                      label={selectedClip.enabled ? "Hide clip" : "Show clip"}
                      icon={
                        selectedClip.enabled ? (
                          <EyeOff size={15} />
                        ) : (
                          <Eye size={15} />
                        )
                      }
                      onClick={toggleEnabled}
                      disabled={Boolean(busyOp)}
                    />
                    <ActionButton
                      label={selectedClip.muted ? "Unmute clip" : "Mute clip"}
                      icon={
                        selectedClip.muted ? (
                          <Volume2 size={15} />
                        ) : (
                          <VolumeX size={15} />
                        )
                      }
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

          <div className="border-b border-white/10 bg-[#0b0f16]/80 px-4 py-3">
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
              onChange={(event) =>
                seekPlayhead(Number(event.currentTarget.value))
              }
              className="editor-playhead-range w-full"
            />
          </div>

          <div
            ref={timelineScrollRef}
            data-testid="timeline-scroll"
            className="editor-timeline-shell min-h-0 flex-1 overflow-auto p-4"
          >
            <div className="relative" style={{ width: timelineWidth + 160 }}>
              <TimeRuler duration={duration} />
              {markers.map((marker) => (
                <button
                  type="button"
                  key={marker.id}
                  onClick={() => seekPlayhead(marker.time)}
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
                data-testid="timeline-playhead"
                className="pointer-events-none absolute top-7 z-30 h-[calc(100%-28px)] w-px bg-white"
                style={{ left: 160 + currentTime * timeScale }}
              />
              <div className="space-y-2 pt-7">
                {tracks.map((track) => {
                  const trackClips = clips.filter(
                    (clip) => clip.trackId === track.id,
                  );
                  return (
                    <div
                      key={track.id}
                      className="grid h-16 grid-cols-[150px_minmax(720px,1fr)] gap-2"
                    >
                      <div className="editor-track-label flex flex-col justify-center px-3">
                        <div className="truncate text-sm font-semibold">
                          {track.name}
                        </div>
                        <div className="text-xs text-slate-500">
                          {track.kind}
                        </div>
                      </div>
                      <div className="editor-track-lane relative overflow-hidden">
                        <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(255,255,255,0.05)_1px,transparent_1px)] bg-[length:80px_100%]" />
                        {track.kind === "audio" && (
                          <div className="absolute inset-x-0 top-1/2 h-4 -translate-y-1/2 opacity-60">
                            <div className="h-full bg-[repeating-linear-gradient(90deg,rgba(125,211,252,0.25)_0_3px,transparent_3px_8px)]" />
                          </div>
                        )}
                        {trackClips.map((clip) => {
                          const asset = project.assets[clip.assetId];
                          const selected = clip.id === selectedClipId;
                          const ghostStart =
                            drag?.clipId === clip.id ? currentTime : clip.start;
                          const label = clipDisplayName(clip, asset);
                          return (
                            <button
                              type="button"
                              key={clip.id}
                              data-testid="editor-clip"
                              data-clip-id={clip.id}
                              data-track-id={clip.trackId}
                              aria-label={`${label} · ${track.name} · starts ${fmt(clip.start)}`}
                              onClick={() => selectClip(clip)}
                              onPointerDown={(event) => startDrag(clip, event)}
                              onPointerMove={updateDrag}
                              onPointerUp={endDrag}
                              onPointerCancel={cancelDrag}
                              className={`editor-clip absolute top-2 h-12 border px-2 text-left text-xs transition ${
                                selected
                                  ? "editor-clip-active"
                                  : clip.enabled
                                    ? "editor-clip-enabled hover:brightness-110"
                                    : "editor-clip-disabled"
                              }`}
                              style={{
                                left: ghostStart * timeScale,
                                width: Math.max(36, clip.duration * timeScale),
                              }}
                            >
                              <span className="block truncate font-semibold">
                                {label}
                              </span>
                              <span className="block truncate opacity-75">
                                {asset?.type || clip.kind} · {fmt(clip.start)}
                              </span>
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

        <aside className="editor-panel border-t border-white/10 lg:border-l lg:border-t-0">
          <div className="space-y-4 p-4">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Notes</h2>
                <span className="text-xs text-slate-500">{notes.length}</span>
              </div>
              <label
                className="block text-xs font-semibold text-slate-400"
                htmlFor="editor-note-text"
              >
                Note text
              </label>
              <textarea
                id="editor-note-text"
                value={noteText}
                onChange={(event) => setNoteText(event.currentTarget.value)}
                className="editor-textarea mt-1 min-h-24 w-full resize-none px-3 py-2 text-sm outline-none"
                placeholder="Add an exact clip/time note"
              />
              <button
                type="button"
                onClick={addClipNote}
                disabled={!selectedClip || !noteText.trim() || Boolean(busyOp)}
                className="editor-transport-button editor-primary-button mt-2 inline-flex h-9 w-full items-center justify-center gap-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50"
              >
                <MessageSquarePlus size={16} />
                Add clip note
              </button>
            </div>

            <div>
              <label
                className="block text-xs font-semibold text-slate-400"
                htmlFor="editor-marker-label"
              >
                Marker label
              </label>
              <input
                id="editor-marker-label"
                value={markerLabel}
                onChange={(event) => setMarkerLabel(event.currentTarget.value)}
                className="editor-field mt-1 h-9 w-full px-3 text-sm outline-none"
              />
              <button
                type="button"
                onClick={addMarker}
                disabled={Boolean(busyOp)}
                className="editor-transport-button mt-2 inline-flex h-9 w-full items-center justify-center gap-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Flag size={16} />
                Add marker
              </button>
            </div>
          </div>

          <div className="border-t border-white/10">
            {notes.length === 0 ? (
              <div className="p-4 text-sm text-slate-500">
                No notes on this timeline.
              </div>
            ) : (
              <div className="divide-y divide-white/10">
                {notes.map((note) => (
                  <button
                    type="button"
                    key={note.id}
                    onClick={() => {
                      if (note.target.clipId)
                        setSelectedClipId(note.target.clipId);
                      seekPlayhead(note.target.time || 0);
                    }}
                    className="editor-note-item block w-full p-4 text-left"
                  >
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-cyan-300">
                        {fmt(note.target.time || 0)}
                      </span>
                      <span className="truncate text-xs text-slate-500">
                        {noteTargetDisplayName(note, project)}
                      </span>
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
      <span className="mb-1 block text-[11px] font-semibold text-slate-500">
        {label}
      </span>
      <input
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        className="editor-field h-8 w-full px-2 text-xs outline-none"
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
      className="editor-action-button inline-flex h-9 items-center justify-center gap-2 px-3 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50"
    >
      {icon}
      {label}
    </button>
  );
}
