import { create } from 'zustand';
import { type Job, type Project, type RosterPage } from '../types/api';

interface Notification {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
}

interface TrackedJob {
  kind: 'video' | 'caption';
  status: string;
  progress?: number;
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

  // Roster state
  rosterPages: RosterPage[];
  rosterLoading: boolean;

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
  primeGeneratePrefill: (prefill: GeneratePrefill) => void;
  clearGeneratePrefill: () => void;

  // Generate page actions
  addGenerateJob: (job: Job) => void;
  setGenerateJob: (job: Job) => void;
  removeGenerateJob: (jobId: string) => void;
  clearGenerateJobs: () => void;

  // Roster actions
  setRosterPages: (pages: RosterPage[]) => void;
  setRosterLoading: (loading: boolean) => void;
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
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
  rosterPages: [],
  rosterLoading: false,

  setActiveProjectName: (name) => {
    set({ activeProjectName: name });
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
    const id = Math.random().toString(36).substring(2, 10);
    set((state) => ({
      notifications: [...state.notifications, { id, type, message }],
    }));

    setTimeout(() => {
      set((state) => ({
        notifications: state.notifications.filter((notification) => notification.id !== id),
      }));
    }, 5000);
  },

  removeNotification: (id) => {
    set((state) => ({
      notifications: state.notifications.filter((notification) => notification.id !== id),
    }));
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
      return {
        recentlyGeneratedVideos: [jobId, ...state.recentlyGeneratedVideos],
      };
    });
  },

  addScrapedCaption: (username) => {
    set((state) => {
      if (state.recentlyScrapedCaptions.includes(username)) {
        return state;
      }
      return {
        recentlyScrapedCaptions: [username, ...state.recentlyScrapedCaptions],
      };
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

  primeGeneratePrefill: (prefill) => {
    set({ generatePrefill: prefill });
  },

  clearGeneratePrefill: () => {
    set({ generatePrefill: null });
  },

  // Generate page actions — jobs persist in store across tab switches
  addGenerateJob: (job) => {
    set((state) => ({
      generateJobs: [job, ...state.generateJobs],
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

  setRosterPages: (pages) => {
    set({ rosterPages: pages });
  },

  setRosterLoading: (loading) => {
    set({ rosterLoading: loading });
  },
}));
