import type React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorBayPage } from "./EditorBay";

vi.mock("@mui/material", async () => {
  const React = await import("react");
  const component = (tag: keyof React.JSX.IntrinsicElements = "div") => {
    const MockComponent = ({
      children,
      ...props
    }: {
      children?: React.ReactNode;
    }) => React.createElement(tag, props, children);
    MockComponent.displayName = `Mock${tag}`;
    return MockComponent;
  };
  const Button = ({
    children,
    onClick,
    disabled,
  }: {
    children?: React.ReactNode;
    onClick?: () => void;
    disabled?: boolean;
  }) => (
    <button type="button" onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
  return {
    Box: component("div"),
    AppBar: component("div"),
    Toolbar: component("div"),
    Typography: component("span"),
    IconButton: Button,
    Slider: ({
      value,
      onChange,
    }: {
      value?: number;
      onChange?: (_event: unknown, value: number) => void;
    }) => (
      <input
        type="range"
        value={value ?? 0}
        onChange={(event) =>
          onChange?.(event, Number(event.currentTarget.value))
        }
      />
    ),
    Drawer: component("aside"),
    List: component("ul"),
    ListItem: component("li"),
    ListItemText: ({ primary }: { primary?: React.ReactNode }) => (
      <span>{primary}</span>
    ),
    TextField: ({
      value,
      onChange,
      placeholder,
    }: {
      value?: string;
      onChange?: (event: React.ChangeEvent<HTMLTextAreaElement>) => void;
      placeholder?: string;
    }) => (
      <textarea
        value={value ?? ""}
        onChange={onChange}
        placeholder={placeholder}
      />
    ),
    Button,
    Chip: ({ label }: { label?: React.ReactNode }) => <span>{label}</span>,
    Divider: component("hr"),
    Tooltip: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    ToggleButton: Button,
    ToggleButtonGroup: component("div"),
    CircularProgress: () => <span>Loading</span>,
    Paper: component("div"),
  };
});

vi.mock("@mui/icons-material/PlayArrow", () => ({
  default: () => <span>play</span>,
}));
vi.mock("@mui/icons-material/Pause", () => ({
  default: () => <span>pause</span>,
}));
vi.mock("@mui/icons-material/Gesture", () => ({
  default: () => <span>draw</span>,
}));
vi.mock("@mui/icons-material/RadioButtonUnchecked", () => ({
  default: () => <span>circle</span>,
}));
vi.mock("@mui/icons-material/NorthEast", () => ({
  default: () => <span>arrow</span>,
}));
vi.mock("@mui/icons-material/DeleteOutlined", () => ({
  default: () => <span>delete</span>,
}));
vi.mock("@mui/icons-material/AddComment", () => ({
  default: () => <span>comment</span>,
}));
vi.mock("@mui/icons-material/Undo", () => ({
  default: () => <span>undo</span>,
}));
vi.mock("react-konva", () => ({
  Stage: ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  ),
  Layer: ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  ),
  Line: () => null,
  Circle: () => null,
  Arrow: () => null,
}));
vi.mock("wavesurfer.js", () => ({
  default: {
    create: () => ({
      setMuted: vi.fn(),
      on: vi.fn(),
      setTime: vi.fn(),
      destroy: vi.fn(),
    }),
  },
}));

const baseProject = {
  schema: "editor-timeline/v1",
  projectId: "ep_real",
  title: "Real Episode",
  fps: 30,
  width: 1920,
  height: 1080,
  revision: 3,
  assets: {
    card: {
      id: "card",
      type: "image",
      src: "/agenticnews-assets/card.png",
      metadata: {},
    },
    vo: {
      id: "vo",
      type: "audio",
      src: "/agenticnews-assets/vo.wav",
      metadata: {},
    },
  },
  tracks: {
    graphics_1: {
      id: "graphics_1",
      kind: "graphics",
      name: "Graphics",
      index: 20,
      locked: false,
    },
    audio_1: {
      id: "audio_1",
      kind: "audio",
      name: "Voice",
      index: 40,
      locked: false,
    },
  },
  clips: {
    card_clip: {
      id: "card_clip",
      assetId: "card",
      trackId: "graphics_1",
      kind: "artifact",
      start: 1,
      duration: 2,
      sourceStart: 0,
      enabled: true,
      muted: false,
      volume: 1,
      transform: { x: 0.2, y: 0.3, scale: 1, opacity: 1 },
      effects: [],
      keyframes: [],
      metadata: {},
    },
    vo_clip: {
      id: "vo_clip",
      assetId: "vo",
      trackId: "audio_1",
      kind: "voiceover",
      start: 0,
      duration: 5,
      sourceStart: 0,
      enabled: true,
      muted: false,
      volume: 0.8,
      transform: { x: 0.5, y: 0.5, scale: 1, opacity: 1 },
      effects: [],
      keyframes: [],
      metadata: {},
    },
  },
  markers: {},
  notes: {},
  effects: {},
  keyframes: {},
  renderCache: {
    frames: {
      "0.00": {
        frame:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_0.00.png",
        at: 0,
        revision: 3,
      },
    },
  },
  commandLog: [],
  createdAt: 1,
  updatedAt: 1,
};

const videoProject = {
  ...baseProject,
  renderCache: {
    video: {
      backend: "ffmpeg",
      video:
        "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
      start: 0,
      duration: 5,
      revision: 3,
    },
  },
};

const healthyAssetHealth = {
  ok: true,
  renderable: true,
  projectId: "ep_real",
  revision: 3,
  checkedFiles: 2,
  missingFiles: [],
  uniqueMissingFiles: [],
  badSources: [],
  missingClipAssets: [],
  missingClipTracks: [],
  materializationPlan: [],
  blockedMaterializations: [],
  copyCandidates: [],
  derivativeMaterializations: [],
  wouldMutateOnLoad: false,
  wouldMutateOnLoadReasons: [],
};

const blockedAssetHealth = {
  ...healthyAssetHealth,
  ok: false,
  renderable: false,
  missingFiles: [
    {
      assetId: "vo",
      type: "audio",
      src: "/agenticnews-assets/vo.wav",
      file: "/tmp/assets/vo.wav",
      clipIds: ["vo_clip"],
      enabledClipIds: ["vo_clip"],
    },
  ],
  uniqueMissingFiles: ["/tmp/assets/vo.wav"],
  blockedMaterializations: [
    {
      type: "audio",
      status: "blocked",
      provenance: "would_derive_from_flattened_episode",
    },
  ],
  wouldMutateOnLoad: true,
  wouldMutateOnLoadReasons: ["source recovery metadata"],
};

const windowVideoProject = {
  ...baseProject,
  renderCache: {
    windows: {
      "1.50_2.00": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_1.50_2.00.mp4",
        start: 1.5,
        duration: 2,
        revision: 3,
      },
    },
  },
};

const floatingStartWindowVideoProject = {
  ...baseProject,
  clips: {
    ...baseProject.clips,
    card_clip: {
      ...baseProject.clips.card_clip,
      start: 201.39,
      duration: 10,
    },
  },
  renderCache: {
    windows: {
      "201.39_2.00": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_201.39_2.00.mp4",
        start: 201.39000000000001,
        duration: 2.005,
        revision: 3,
      },
    },
  },
};

const fullAndWindowVideoProject = {
  ...baseProject,
  renderCache: {
    video: {
      backend: "openshot",
      video:
        "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
      start: 0,
      duration: 5,
      revision: 3,
    },
    windows: {
      "1.50_2.00": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_1.50_2.00.mp4",
        start: 1.5,
        duration: 2,
        revision: 3,
      },
    },
  },
};

const overlappingWindowVideoProject = {
  ...baseProject,
  renderCache: {
    video: {
      backend: "openshot",
      video:
        "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
      start: 0,
      duration: 5,
      revision: 3,
    },
    windows: {
      "0.00_0.28": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_0.00_0.25.mp4",
        start: 0,
        duration: 0.277,
        revision: 3,
      },
      "0.00_2.00": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_0.00_2.00.mp4",
        start: 0,
        duration: 2,
        revision: 3,
      },
    },
  },
};

const staleWindowFreshFullVideoProject = {
  ...baseProject,
  revision: 10,
  renderCache: {
    video: {
      backend: "openshot",
      video:
        "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
      start: 0,
      duration: 5,
      revision: 10,
    },
    windows: {
      "1.50_2.00": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_1.50_2.00.mp4",
        start: 1.5,
        duration: 2,
        revision: 9,
      },
    },
  },
};

const unversionedCacheProject = {
  ...baseProject,
  revision: 10,
  renderCache: {
    video: {
      backend: "openshot",
      video:
        "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
      start: 0,
      duration: 5,
    },
    windows: {
      "1.50_2.00": {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_1.50_2.00.mp4",
        start: 1.5,
        duration: 2,
      },
    },
    frames: {
      "0.00": {
        backend: "openshot",
        frame:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real_0.00.png",
        at: 0,
      },
    },
  },
};

const farTimelineProject = {
  ...baseProject,
  renderCache: {},
  clips: {
    ...baseProject.clips,
    far_clip: {
      ...baseProject.clips.card_clip,
      id: "far_clip",
      start: 828,
      duration: 3,
      metadata: {},
    },
  },
};

function withMovedClip() {
  return {
    ...baseProject,
    revision: 4,
    clips: {
      ...baseProject.clips,
      card_clip: {
        ...baseProject.clips.card_clip,
        start: 1.5,
      },
    },
  };
}

function withUndoneClipMove() {
  return {
    ...baseProject,
    revision: 4,
    clips: {
      ...baseProject.clips,
      card_clip: {
        ...baseProject.clips.card_clip,
        start: 0.5,
      },
    },
  };
}

function withNote() {
  return {
    ...baseProject,
    revision: 4,
    notes: {
      note_1: {
        id: "note_1",
        target: { clipId: "card_clip", time: 1, frame: 1 },
        text: "Fix the card timing",
        suggestedCommand: {
          op: "clip.move",
          payload: { clipId: "card_clip", start: 1.5 },
        },
        metadata: {},
      },
    },
  };
}

function withRenderedWindow(
  project = baseProject,
  start = 1,
  duration = 2,
) {
  const windowKey = `${start.toFixed(2)}_${duration.toFixed(2)}`;
  const video =
    `/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/` +
    `ep_real_${start.toFixed(2)}_${duration.toFixed(2)}.mp4`;
  const renderCache = project.renderCache as
    | {
        windows?: Record<
          string,
          {
            backend?: string;
            video?: string;
            start?: number;
            duration?: number;
            revision?: number;
          }
        >;
      }
    | undefined;
  return {
    ...project,
    renderCache: {
      ...(renderCache || {}),
      windows: {
        ...(renderCache?.windows || {}),
        [windowKey]: {
          backend: "openshot",
          video,
          start,
          duration,
          revision: project.revision,
        },
      },
    },
  };
}

function withRenderedFullTimeline() {
  return {
    ...baseProject,
    renderCache: {
      video: {
        backend: "openshot",
        video:
          "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
        start: 0,
        duration: 5,
        revision: baseProject.revision,
      },
    },
  };
}

function withHiddenClipNoRenderCache() {
  const { renderCache: _renderCache, ...projectWithoutRenderCache } = baseProject;
  return {
    ...projectWithoutRenderCache,
    revision: 4,
    clips: {
      ...baseProject.clips,
      card_clip: {
        ...baseProject.clips.card_clip,
        enabled: false,
      },
    },
  };
}

describe("EditorBayPage v2 timeline", () => {
  let projectFixture: unknown = baseProject;
  let assetHealthFixture: unknown = healthyAssetHealth;
  let frameRenderFixture: unknown = {
    backend: "ffmpeg",
    frame: "/tmp/frame.png",
    at: 1,
    revision: 3,
  };

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (
        url.endsWith("/api/agenticnews/editor-timelines/ep_real") &&
        !init?.method
      ) {
        return jsonResponse(projectFixture);
      }
      if (
        url.endsWith("/api/agenticnews/editor-timelines/ep_real/asset-health") &&
        !init?.method
      ) {
        return jsonResponse(assetHealthFixture);
      }
      if (url.endsWith("/api/agenticnews/editor-timelines/ep_real/commands")) {
        const body = JSON.parse(String(init?.body || "{}"));
        if (body.op === "clip.move") return jsonResponse(withMovedClip());
        if (body.op === "clip.hide") return jsonResponse(withHiddenClipNoRenderCache());
        if (body.op === "note.add") return jsonResponse(withNote());
        return jsonResponse({
          ...baseProject,
          revision: baseProject.revision + 1,
        });
      }
      if (
        url.endsWith(
          "/api/agenticnews/editor-timelines/ep_real/commands/revert-last",
        )
      ) {
        return jsonResponse(withUndoneClipMove());
      }
      if (url.endsWith("/api/agenticnews/editor-render/ep_real/frame")) {
        return jsonResponse(frameRenderFixture);
      }
      if (url.endsWith("/api/agenticnews/editor-render/ep_real/render")) {
        const body = JSON.parse(String(init?.body || "{}"));
        if (typeof body.duration === "number") {
          const start = Number(body.start || 0);
          const duration = Number(body.duration || 0);
          const nextProject = withRenderedWindow(
            projectFixture as typeof baseProject,
            start,
            duration,
          );
          const renderCache = nextProject.renderCache as {
            windows: Record<string, { video?: string }>;
          };
          projectFixture = nextProject;
          return jsonResponse({
            backend: "openshot",
            video: renderCache.windows[
              `${start.toFixed(2)}_${duration.toFixed(2)}`
            ].video,
            start,
            duration,
            revision: nextProject.revision,
          });
        }
        projectFixture = withRenderedFullTimeline();
        return jsonResponse({
          backend: "openshot",
          video:
            "/Volumes/T9/agenticbuildernews/agenticnews_assets/editor_renders/ep_real.mp4",
          start: 0,
          duration: 5,
          revision: baseProject.revision,
        });
      }
      return jsonResponse({}, 404);
    },
  );

  beforeEach(() => {
    window.history.pushState({}, "", "/editor/ep_real");
    projectFixture = baseProject;
    assetHealthFixture = healthyAssetHealth;
    frameRenderFixture = {
      backend: "ffmpeg",
      frame: "/tmp/frame.png",
      at: 1,
      revision: 3,
    };
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("loads the commanded timeline graph and renders tracks and clips", async () => {
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    expect(screen.getByText("rev 3")).toBeTruthy();
    expect(screen.getByText("Graphics")).toBeTruthy();
    expect(screen.getByText("Voice")).toBeTruthy();
    expect(
      screen.getByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Timeline .* Voice .* Voice/i }),
    ).toBeTruthy();
    expect(screen.queryByText("card_clip")).toBeNull();
    const preview = await screen.findByAltText("Rendered preview frame");
    expect(preview.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real_0.00.png?rev=3",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/agenticnews/editor-timelines/ep_real",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/agenticnews/editor-timelines/ep_real/asset-health",
    );
  });

  it("surfaces blocked source health and prevents fresh renders from missing real assets", async () => {
    assetHealthFixture = blockedAssetHealth;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    expect(await screen.findByText("Source graph blocked")).toBeTruthy();
    expect(screen.getByText(/1 missing files/i)).toBeTruthy();
    expect(screen.getByText(/1 used by enabled clips/i)).toBeTruthy();
    expect(screen.getByText(/1 blocked recoveries/i)).toBeTruthy();
    expect(screen.getByText(/Cached previews may not represent/i)).toBeTruthy();
    expect(screen.getByText(/Load would mutate/i)).toBeTruthy();

    expect(
      (screen.getByRole("button", {
        name: /render frame/i,
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", {
        name: /render window/i,
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", {
        name: /render full/i,
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("keeps a real editor-rendered video synchronized with the timeline playhead", async () => {
    projectFixture = videoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real.mp4?rev=3",
    );

    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    fireEvent.change(playhead, { target: { value: "2.5" } });
    expect(video.currentTime).toBe(2.5);

    video.currentTime = 3.25;
    fireEvent.timeUpdate(video);
    expect(playhead.value).toBe("3.25");
  });

  it("uses cached OpenShot window renders with timeline-relative seeking", async () => {
    projectFixture = windowVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    fireEvent.change(playhead, { target: { value: "2.5" } });

    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real_1.50_2.00.mp4?rev=3",
    );
    expect(video.currentTime).toBe(1);

    video.currentTime = 1.25;
    fireEvent.timeUpdate(video);
    expect(playhead.value).toBe("2.75");
  });

  it("matches cached windows when floating point start time drifts by an epsilon", async () => {
    projectFixture = floatingStartWindowVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    fireEvent.change(playhead, { target: { value: "201.39" } });

    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real_201.39_2.00.mp4?rev=3",
    );
    expect(video.currentTime).toBe(0);
  });

  it("uses an active window render ahead of the full cached video", async () => {
    projectFixture = fullAndWindowVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    fireEvent.change(playhead, { target: { value: "2.5" } });

    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real_1.50_2.00.mp4?rev=3",
    );
    expect(video.currentTime).toBe(1);
  });

  it("falls back to the full render at absolute timeline time when seeking outside a cached window", async () => {
    projectFixture = fullAndWindowVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    const video = document.querySelector("video") as HTMLVideoElement;

    fireEvent.change(playhead, { target: { value: "2.5" } });
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real_1.50_2.00.mp4?rev=3",
    );
    expect(video.currentTime).toBe(1);

    fireEvent.change(playhead, { target: { value: "4.5" } });

    await waitFor(() => {
      expect(video.getAttribute("src")).toBe(
        "/agenticnews-assets/editor_renders/ep_real.mp4?rev=3",
      );
      expect(video.currentTime).toBe(4.5);
      expect(playhead.value).toBe("4.5");
    });
  });

  it("uses the full render at the exact end of a cached window", async () => {
    projectFixture = fullAndWindowVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    const video = document.querySelector("video") as HTMLVideoElement;

    fireEvent.change(playhead, { target: { value: "3.5" } });

    await waitFor(() => {
      expect(video.getAttribute("src")).toBe(
        "/agenticnews-assets/editor_renders/ep_real.mp4?rev=3",
      );
      expect(video.currentTime).toBe(3.5);
    });
  });

  it("prefers the broadest active window cache when overlapping windows exist", async () => {
    projectFixture = overlappingWindowVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();

    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real_0.00_2.00.mp4?rev=3",
    );
  });

  it("keeps the playhead visible when selecting a far timeline clip", async () => {
    const user = userEvent.setup();
    projectFixture = farTimelineProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const timelineScroll = screen.getByTestId("timeline-scroll");
    Object.defineProperty(timelineScroll, "clientWidth", {
      configurable: true,
      value: 900,
    });
    Object.defineProperty(timelineScroll, "scrollWidth", {
      configurable: true,
      value: 66720,
    });
    timelineScroll.scrollLeft = 0;

    const timelineInner = timelineScroll.firstElementChild as HTMLElement;
    expect(timelineInner.style.width).toBe("66720px");

    const expectPlayheadVisibleAtFarClip = () => {
      const playheadX = 160 + 828 * 80;
      expect(timelineScroll.scrollLeft).toBeGreaterThan(65000);
      expect(timelineScroll.scrollLeft).toBeLessThan(playheadX);
      expect(playheadX).toBeLessThan(
        timelineScroll.scrollLeft + timelineScroll.clientWidth,
      );
    };

    await user.click(screen.getByRole("button", { name: /starts 13:48\.0/i }));

    await waitFor(() => {
      expectPlayheadVisibleAtFarClip();
    });

    timelineScroll.scrollLeft = 0;
    await user.click(screen.getByRole("button", { name: /render window/i }));

    await waitFor(() => {
      const video = document.querySelector("video") as HTMLVideoElement;
      expect(video.getAttribute("src")).toBe(
        "/agenticnews-assets/editor_renders/ep_real_828.00_3.00.mp4?rev=3",
      );
      expectPlayheadVisibleAtFarClip();
    });
  });

  it("renders the selected clip as an OpenShot window cache", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(
      await screen.findByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    );
    await user.click(screen.getByRole("button", { name: /render window/i }));

    await waitFor(() => {
      const video = document.querySelector("video") as HTMLVideoElement;
      expect(video?.getAttribute("src")).toBe(
        "/agenticnews-assets/editor_renders/ep_real_1.00_2.00.mp4?rev=3",
      );
    });

    const renderCall = fetchMock.mock.calls.find(([url, init]) => {
      if (!String(url).endsWith("/api/agenticnews/editor-render/ep_real/render")) {
        return false;
      }
      const body = JSON.parse(String((init as RequestInit).body));
      return typeof body.duration === "number";
    });
    expect(renderCall).toBeTruthy();
    expect(JSON.parse(String((renderCall![1] as RequestInit).body))).toMatchObject({
      start: 1,
      duration: 2,
    });
  });

  it("clears one-off preview frames after output-changing clip commands", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(
      await screen.findByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    );
    await user.click(screen.getByRole("button", { name: /render frame/i }));

    await waitFor(() => {
      expect(screen.getByAltText("Rendered preview frame").getAttribute("src")).toBe(
        "/tmp/frame.png?rev=3",
      );
    });

    await user.click(screen.getByRole("button", { name: /hide clip/i }));

    await waitFor(() => {
      expect(screen.getByText("rev 4")).toBeTruthy();
      expect(screen.queryByAltText("Rendered preview frame")).toBeNull();
      expect(screen.getByText(/No editor render at this playhead/i)).toBeTruthy();
    });
  });

  it("ignores cache-skipped frame render responses from stale timeline revisions", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(
      await screen.findByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    );
    await user.click(screen.getByRole("button", { name: /render frame/i }));

    await waitFor(() => {
      expect(screen.getByAltText("Rendered preview frame").getAttribute("src")).toBe(
        "/tmp/frame.png?rev=3",
      );
    });

    projectFixture = withMovedClip();
    frameRenderFixture = {
      backend: "ffmpeg",
      frame: "/tmp/stale-frame.png",
      at: 1,
      revision: 3,
      currentRevision: 4,
      cacheSkipped: true,
    };
    await user.click(screen.getByRole("button", { name: /render frame/i }));

    await waitFor(() => {
      expect(screen.getByText("rev 4")).toBeTruthy();
      expect(screen.queryByAltText("Rendered preview frame")).toBeNull();
    });
  });

  it("does not advance the playhead with synthetic playback when no rendered video is active", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    expect(playhead.value).toBe("0");

    await user.click(screen.getByRole("button", { name: /^play$/i }));
    await new Promise((resolve) => window.setTimeout(resolve, 250));

    expect(playhead.value).toBe("0");
    expect(screen.getByText(/Render a video window before playback/i)).toBeTruthy();
  });

  it("renders the full timeline cache on explicit request", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /render full/i }));

    await waitFor(() => {
      const video = document.querySelector("video") as HTMLVideoElement;
      expect(video?.getAttribute("src")).toBe(
        "/agenticnews-assets/editor_renders/ep_real.mp4?rev=3",
      );
    });

    const renderCall = fetchMock.mock.calls.find(([url, init]) => {
      if (!String(url).endsWith("/api/agenticnews/editor-render/ep_real/render")) {
        return false;
      }
      return !("duration" in JSON.parse(String((init as RequestInit).body)));
    });
    expect(renderCall).toBeTruthy();
    expect(JSON.parse(String((renderCall![1] as RequestInit).body))).toEqual({});
  });

  it("ignores stale window render cache when a current full render exists", async () => {
    projectFixture = staleWindowFreshFullVideoProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    fireEvent.change(playhead, { target: { value: "2.5" } });

    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(
      "/agenticnews-assets/editor_renders/ep_real.mp4?rev=10",
    );
    expect(video.currentTime).toBe(2.5);
  });

  it("ignores unversioned render cache from unsanitized timeline responses", async () => {
    projectFixture = unversionedCacheProject;
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    const playhead = screen.getByLabelText(
      /timeline playhead/i,
    ) as HTMLInputElement;
    fireEvent.change(playhead, { target: { value: "2.5" } });

    expect(document.querySelector("video")).toBeNull();
    expect(screen.queryByAltText("Rendered preview frame")).toBeNull();
  });

  it("dispatches clip move commands instead of privately mutating the timeline", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(
      await screen.findByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    );
    await user.click(screen.getByRole("button", { name: /nudge clip later/i }));

    await waitFor(() => expect(screen.getByText("rev 4")).toBeTruthy());

    const commandCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith(
          "/api/agenticnews/editor-timelines/ep_real/commands",
        ) && JSON.parse(String((init as RequestInit).body)).op === "clip.move",
    );
    expect(commandCall).toBeTruthy();
    const command = JSON.parse(String((commandCall![1] as RequestInit).body));
    expect(command).toMatchObject({
      op: "clip.move",
      actor: "human",
      expectedRevision: 3,
      payload: { clipId: "card_clip", start: 1.5 },
    });
  });

  it("sends undo through the revision-checked revert endpoint", async () => {
    const user = userEvent.setup();
    projectFixture = {
      ...baseProject,
      commandLog: [
        {
          id: "cmd_move",
          op: "clip.move",
          revision: 3,
        },
      ],
    };
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /undo/i }));

    await waitFor(() => expect(screen.getByText("rev 4")).toBeTruthy());

    const undoCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith(
        "/api/agenticnews/editor-timelines/ep_real/commands/revert-last",
      ),
    );
    expect(undoCall).toBeTruthy();
    expect(JSON.parse(String((undoCall![1] as RequestInit).body))).toMatchObject({
      actor: "human",
      expectedRevision: 3,
    });
  });

  it("disables undo when every historical command is already reverted", async () => {
    projectFixture = {
      ...baseProject,
      commandLog: [
        {
          id: "cmd_move",
          op: "clip.move",
          revision: 3,
        },
        {
          id: "cmd_revert",
          op: "clip.update",
          revision: 4,
          revertsCommandId: "cmd_move",
        },
      ],
    };
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: /undo/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("applies transform and audio volume as one revision-safe clip update", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    expect(await screen.findByText("Real Episode")).toBeTruthy();
    await user.clear(screen.getByLabelText(/volume/i));
    await user.type(screen.getByLabelText(/volume/i), "0.35");
    await user.click(screen.getByRole("button", { name: /apply properties/i }));

    const commandCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith(
        "/api/agenticnews/editor-timelines/ep_real/commands",
      ),
    );
    const updateCalls = commandCalls.filter(
      ([, init]) =>
        JSON.parse(String((init as RequestInit).body)).op === "clip.update",
    );
    expect(updateCalls).toHaveLength(1);
    const command = JSON.parse(String((updateCalls[0][1] as RequestInit).body));
    expect(command).toMatchObject({
      op: "clip.update",
      actor: "human",
      expectedRevision: 3,
      payload: {
        clipId: "vo_clip",
        patch: {
          volume: 0.35,
          transform: { x: 0.5, y: 0.5, scale: 1, opacity: 1 },
        },
      },
    });
    expect(
      commandCalls.some(
        ([, init]) =>
          JSON.parse(String((init as RequestInit).body)).op === "clip.volume",
      ),
    ).toBe(false);
  });

  it("refuses to split when the playhead is outside the selected clip", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(
      await screen.findByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    );
    fireEvent.change(screen.getByLabelText(/timeline playhead/i), {
      target: { value: "4.5" },
    });
    await user.click(
      screen.getByRole("button", { name: /split at playhead/i }),
    );

    expect(
      await screen.findByText(
        "Move the playhead inside the selected clip before splitting.",
      ),
    ).toBeTruthy();
    expect(
      fetchMock.mock.calls.some(([url, init]) => {
        if (
          !String(url).endsWith(
            "/api/agenticnews/editor-timelines/ep_real/commands",
          )
        ) {
          return false;
        }
        return (
          JSON.parse(String((init as RequestInit).body)).op === "clip.split"
        );
      }),
    ).toBe(false);
  });

  it("attaches notes to an exact clip and timeline time through the command API", async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(
      await screen.findByRole("button", {
        name: /Timeline .* Visual Layer .* Graphics/i,
      }),
    );
    await user.type(screen.getByLabelText(/note text/i), "Fix the card timing");
    await user.click(screen.getByRole("button", { name: /add clip note/i }));

    await waitFor(() =>
      expect(screen.getByText("Fix the card timing")).toBeTruthy(),
    );
    const noteCard = screen.getByText("Fix the card timing").closest("button");
    expect(noteCard?.textContent).toContain("Timeline · Visual Layer");
    expect(noteCard?.textContent).not.toContain("card_clip");

    const commandCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith(
          "/api/agenticnews/editor-timelines/ep_real/commands",
        ) && JSON.parse(String((init as RequestInit).body)).op === "note.add",
    );
    expect(commandCall).toBeTruthy();
    const command = JSON.parse(String((commandCall![1] as RequestInit).body));
    expect(command).toMatchObject({
      op: "note.add",
      actor: "human",
      expectedRevision: 3,
      payload: {
        target: { clipId: "card_clip", time: 1, frame: 1 },
        text: "Fix the card timing",
      },
    });
    expect(command.payload.suggestedCommand).toMatchObject({
      op: "clip.move",
      payload: { clipId: "card_clip" },
    });
  });
});

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}
