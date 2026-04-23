import { create } from 'zustand';
import { type Job, type Project, type RosterPage, type UploadJob, type UploadQueueStats } from '../types/api';

interface Notification {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
  baseMessage: string;
  count: number;
  createdAt: number;
  timerId: ReturnType<typeof setTimeout> | null;
}

const DEDUP_WINDOW_MS = 5000;
const NOTIFICATION_TTL_MS = 5000;
const MAX_VISIBLE_NOTIFICATIONS = 5;

interface TrackedJob {
  kind: 'video' | 'caption';
  status: string;
  progress?: number;
  error?: string;
  provider?: Job['provider'];
  prompt?: Job['prompt'];
  username?: string;
}

interface BurnSelectionDraft {
  videoPaths: string[];
  captionSource: string | null;
}

export interface GeneratePrefill {
  prompt: string;
  firstFrameDataUri: string;
  lastFrameDataUri: string | null;
  provider: string;
  aspectRatio: string;
}

export interface ProjectStats {
  path: string;
  video_count: number;
  caption_count: number;
  burned_count: number;
}

type JobsState = Record<string, TrackedJob>;

function readStoredProjectName(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }

  const stored = localStorage.getItem('activeProjectName');
  if (stored) {
    return stored;
  }

  // Migration: read old format and convert
  const legacy = localStorage.getItem('activeProject');
  if (legacy) {
    try {
      const parsed = JSON.parse(legacy) as { name?: string };
      if (parsed?.name) {
        localStorage.setItem('activeProjectName', parsed.name);
        localStorage.removeItem('activeProject');
        return parsed.name;
      }
    } catch {
      // ignore
    }
    localStorage.removeItem('activeProject');
  }

  return null;
}

function readArchivedForProject(project: string | null): string[] {
  if (!project || typeof window === 'undefined') return [];
  try {
    const raw = localStorage.getItem(`archived:${project}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every((v) => typeof v === 'string')) {
      return parsed;
    }
  } catch {
    // ignore malformed
  }
  return [];
}

function writeArchivedForProject(project: string | null, ids: string[]): void {
  if (!project || typeof window === 'undefined') return;
  const key = `archived:${project}`;
  if (ids.length === 0) {
    localStorage.removeItem(key);
  } else {
    localStorage.setItem(key, JSON.stringify(ids));
  }
}

interface WorkflowState {
  activeProjectName: string | null;
  projectStats: Record<string, ProjectStats>;
  jobs: JobsState;
  notifications: Notification[];
  recentlyGeneratedVideos: string[];
  recentlyScrapedCaptions: string[];
  videoRunningCount: number;
  captionJobActive: boolean;
  recreateJobActive: boolean;
  burnReadyCount: number;
  burnSelection: BurnSelectionDraft;
  generatePrefill: GeneratePrefill | null;

  // Generate page state — persists across tab switches
  generateJobs: Job[];

  // Archived Generate job IDs for the active project — persisted to
  // localStorage under `archived:{project}` so hidden jobs stay hidden
  // across reloads. Re-loaded on project switch.
  archivedJobIds: string[];

  // Roster state
  rosterPages: RosterPage[];
  rosterLoading: boolean;

  // Upload queue state
  uploadJobs: UploadJob[];
  uploadStats: UploadQueueStats | null;

  setActiveProjectName: (name: string | null) => void;
  updateProjectStats: (projects: Project[]) => void;
  addNotification: (type: 'success' | 'error' | 'info', message: string) => void;
  removeNotification: (id: string) => void;
  updateJob: (jobId: string, data: Partial<TrackedJob>) => void;
  addGeneratedVideo: (jobId: string) => void;
  addScrapedCaption: (username: string) => void;
  setVideoRunningCount: (count: number) => void;
  setCaptionJobActive: (active: boolean) => void;
  setRecreateJobActive: (active: boolean) => void;
  setBurnReadyCount: (count: number) => void;
  incrementBurnReadyCount: (delta: number) => void;
  primeBurnSelection: (selection: Partial<BurnSelectionDraft>) => void;
  clearBurnSelection: () => void;
  consumeBurnSelection: () => BurnSelectionDraft;
  primeGeneratePrefill: (prefill: GeneratePrefill) => void;
  clearGeneratePrefill: () => void;

  // Generate page actions
  addGenerateJob: (job: Job) => void;
  setGenerateJob: (job: Job) => void;
  removeGenerateJob: (jobId: string) => void;
  clearGenerateJobs: () => void;

  // Archive actions — all persist to localStorage `archived:{project}`
  archiveJob: (jobId: string) => void;
  archiveJobs: (jobIds: string[]) => void;
  unarchiveJob: (jobId: string) => void;
  clearArchive: () => void;

  // Roster actions
  setRosterPages: (pages: RosterPage[]) => void;
  setRosterLoading: (loading: boolean) => void;

  // Upload actions
  setUploadJobs: (jobs: UploadJob[]) => void;
  setUploadStats: (stats: UploadQueueStats | null) => void;
  addUploadJob: (job: UploadJob) => void;
  updateUploadJob: (job: UploadJob) => void;
}

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  activeProjectName: readStoredProjectName(),
  projectStats: {},
  jobs: {},
  notifications: [],
  recentlyGeneratedVideos: [],
  recentlyScrapedCaptions: [],
  videoRunningCount: 0,
  captionJobActive: false,
  recreateJobActive: false,
  burnReadyCount: 0,
  burnSelection: {
    videoPaths: [],
    captionSource: null,
  },
  generatePrefill: null,
  generateJobs: [],
  archivedJobIds: readArchivedForProject(readStoredProjectName()),
  rosterPages: [],
  rosterLoading: false,
  uploadJobs: [],
  uploadStats: null,

  setActiveProjectName: (name) => {
    set({
      activeProjectName: name,
      archivedJobIds: readArchivedForProject(name),
    });
    if (typeof window === 'undefined') {
      return;
    }

    if (name) {
      localStorage.setItem('activeProjectName', name);
    } else {
      localStorage.removeItem('activeProjectName');
    }
  },

  updateProjectStats: (projects) => {
    const stats: Record<string, ProjectStats> = {};
    for (const p of projects) {
      stats[p.name] = {
        path: p.path,
        video_count: p.video_count,
        caption_count: p.caption_count,
        burned_count: p.burned_count,
      };
    }
    set({ projectStats: stats });
  },

  addNotification: (type, message) => {
    const now = Date.now();
    const current = useWorkflowStore.getState().notifications;
    const dup = current.find(
      (n) => n.type === type && n.baseMessage === message && (now - n.createdAt) < DEDUP_WINDOW_MS,
    );

    const dismiss = (id: string) => {
      set((state) => {
        const target = state.notifications.find((n) => n.id === id);
        if (target?.timerId) clearTimeout(target.timerId);
        return { notifications: state.notifications.filter((n) => n.id !== id) };
      });
    };

    if (dup) {
      if (dup.timerId) clearTimeout(dup.timerId);
      const newCount = dup.count + 1;
      const timerId = setTimeout(() => dismiss(dup.id), NOTIFICATION_TTL_MS);
      set((state) => ({
        notifications: state.notifications.map((n) =>
          n.id === dup.id
            ? { ...n, count: newCount, message: `${message} (×${newCount})`, createdAt: now, timerId }
            : n,
        ),
      }));
      return;
    }

    const id = Math.random().toString(36).substring(2, 10);
    const timerId = setTimeout(() => dismiss(id), NOTIFICATION_TTL_MS);
    set((state) => {
      const next = [...state.notifications, { id, type, message, baseMessage: message, count: 1, createdAt: now, timerId }];
      // Cap visible — drop oldest if we exceed the cap
      while (next.length > MAX_VISIBLE_NOTIFICATIONS) {
        const dropped = next.shift();
        if (dropped?.timerId) clearTimeout(dropped.timerId);
      }
      return { notifications: next };
    });
  },

  removeNotification: (id) => {
    set((state) => {
      const target = state.notifications.find((n) => n.id === id);
      if (target?.timerId) clearTimeout(target.timerId);
      return { notifications: state.notifications.filter((n) => n.id !== id) };
    });
  },

  updateJob: (jobId, data) => {
    set((state) => ({
      jobs: {
        ...state.jobs,
        [jobId]: { ...state.jobs[jobId], ...data },
      },
    }));
  },

  addGeneratedVideo: (jobId) => {
    set((state) => {
      if (state.recentlyGeneratedVideos.includes(jobId)) {
        return state;
      }
      // Cap at 50 entries to prevent unbounded growth
      const updated = [jobId, ...state.recentlyGeneratedVideos].slice(0, 50);
      return { recentlyGeneratedVideos: updated };
    });
  },

  addScrapedCaption: (username) => {
    set((state) => {
      if (state.recentlyScrapedCaptions.includes(username)) {
        return state;
      }
      // Cap at 50 entries
      const updated = [username, ...state.recentlyScrapedCaptions].slice(0, 50);
      return { recentlyScrapedCaptions: updated };
    });
  },

  setVideoRunningCount: (count) => {
    set({ videoRunningCount: Math.max(0, count) });
  },

  setCaptionJobActive: (active) => {
    set({ captionJobActive: active });
  },

  setRecreateJobActive: (active) => {
    set({ recreateJobActive: active });
  },

  setBurnReadyCount: (count) => {
    set({ burnReadyCount: Math.max(0, count) });
  },

  incrementBurnReadyCount: (delta) => {
    set((state) => ({ burnReadyCount: Math.max(0, state.burnReadyCount + delta) }));
  },

  primeBurnSelection: (selection) => {
    set((state) => ({
      burnSelection: {
        videoPaths: selection.videoPaths ?? state.burnSelection.videoPaths,
        captionSource: selection.captionSource ?? state.burnSelection.captionSource,
      },
    }));
  },

  clearBurnSelection: () => {
    set({
      burnSelection: {
        videoPaths: [],
        captionSource: null,
      },
    });
  },

  consumeBurnSelection: () => {
    let snapshot: BurnSelectionDraft = { videoPaths: [], captionSource: null };
    set((state) => {
      snapshot = {
        videoPaths: [...state.burnSelection.videoPaths],
        captionSource: state.burnSelection.captionSource,
      };
      return {
        burnSelection: { videoPaths: [], captionSource: null },
      };
    });
    return snapshot;
  },

  primeGeneratePrefill: (prefill) => {
    set({ generatePrefill: prefill });
  },

  clearGeneratePrefill: () => {
    set({ generatePrefill: null });
  },

  // Generate page actions — jobs persist in store across tab switches
  addGenerateJob: (job) => {
    set((state) => ({
      // Cap at 100 jobs to prevent unbounded growth
      generateJobs: [job, ...state.generateJobs].slice(0, 100),
    }));
  },

  setGenerateJob: (job) => {
    set((state) => {
      const exists = state.generateJobs.some((j) => j.id === job.id);
      if (exists) {
        return {
          generateJobs: state.generateJobs.map((j) => (j.id === job.id ? job : j)),
        };
      }
      return {
        generateJobs: [job, ...state.generateJobs],
      };
    });
  },

  removeGenerateJob: (jobId) => {
    set((state) => ({
      generateJobs: state.generateJobs.filter((j) => j.id !== jobId),
    }));
  },

  clearGenerateJobs: () => {
    set({ generateJobs: [] });
  },

  archiveJob: (jobId) => {
    set((state) => {
      if (state.archivedJobIds.includes(jobId)) return state;
      const next = [...state.archivedJobIds, jobId];
      writeArchivedForProject(state.activeProjectName, next);
      return { archivedJobIds: next };
    });
  },

  archiveJobs: (jobIds) => {
    set((state) => {
      const add = jobIds.filter((id) => !state.archivedJobIds.includes(id));
      if (add.length === 0) return state;
      const next = [...state.archivedJobIds, ...add];
      writeArchivedForProject(state.activeProjectName, next);
      return { archivedJobIds: next };
    });
  },

  unarchiveJob: (jobId) => {
    set((state) => {
      if (!state.archivedJobIds.includes(jobId)) return state;
      const next = state.archivedJobIds.filter((id) => id !== jobId);
      writeArchivedForProject(state.activeProjectName, next);
      return { archivedJobIds: next };
    });
  },

  clearArchive: () => {
    const project = get().activeProjectName;
    writeArchivedForProject(project, []);
    set({ archivedJobIds: [] });
  },

  setRosterPages: (pages) => {
    set({ rosterPages: pages });
  },

  setRosterLoading: (loading) => {
    set({ rosterLoading: loading });
  },

  setUploadJobs: (jobs) => {
    set({ uploadJobs: jobs });
  },

  setUploadStats: (stats) => {
    set({ uploadStats: stats });
  },

  addUploadJob: (job) => {
    set((state) => ({
      uploadJobs: [job, ...state.uploadJobs],
    }));
  },

  updateUploadJob: (job) => {
    set((state) => ({
      uploadJobs: state.uploadJobs.map((j) => (j.job_id === job.job_id ? job : j)),
    }));
  },
}));
