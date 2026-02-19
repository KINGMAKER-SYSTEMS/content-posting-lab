import { create } from 'zustand';
import { type Job, type Project } from '../types/api';

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

type JobsState = Record<string, TrackedJob>;

function readStoredProject(): Project | null {
  if (typeof window === 'undefined') {
    return null;
  }

  const stored = localStorage.getItem('activeProject');
  if (!stored) {
    return null;
  }

  try {
    const parsed = JSON.parse(stored) as Project;
    if (!parsed?.name || !parsed?.path) {
      localStorage.removeItem('activeProject');
      return null;
    }
    return parsed;
  } catch {
    localStorage.removeItem('activeProject');
    return null;
  }
}

interface WorkflowState {
  activeProject: Project | null;
  jobs: JobsState;
  notifications: Notification[];
  recentlyGeneratedVideos: string[];
  recentlyScrapedCaptions: string[];
  videoRunningCount: number;
  captionJobActive: boolean;
  burnReadyCount: number;
  burnSelection: BurnSelectionDraft;
  setActiveProject: (project: Project | null) => void;
  addNotification: (type: 'success' | 'error' | 'info', message: string) => void;
  removeNotification: (id: string) => void;
  updateJob: (jobId: string, data: Partial<TrackedJob>) => void;
  addGeneratedVideo: (jobId: string) => void;
  addScrapedCaption: (username: string) => void;
  setVideoRunningCount: (count: number) => void;
  setCaptionJobActive: (active: boolean) => void;
  setBurnReadyCount: (count: number) => void;
  incrementBurnReadyCount: (delta: number) => void;
  primeBurnSelection: (selection: Partial<BurnSelectionDraft>) => void;
  clearBurnSelection: () => void;
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  activeProject: readStoredProject(),
  jobs: {},
  notifications: [],
  recentlyGeneratedVideos: [],
  recentlyScrapedCaptions: [],
  videoRunningCount: 0,
  captionJobActive: false,
  burnReadyCount: 0,
  burnSelection: {
    videoPaths: [],
    captionSource: null,
  },

  setActiveProject: (project) => {
    set({ activeProject: project });
    if (typeof window === 'undefined') {
      return;
    }

    if (project) {
      localStorage.setItem('activeProject', JSON.stringify(project));
    } else {
      localStorage.removeItem('activeProject');
    }
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
}));
