import { create } from 'zustand';
import { type Project } from '../types/api';

interface Notification {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
}

interface WorkflowState {
  activeProject: Project | null;
  jobs: Record<string, any>; // Placeholder for job state
  notifications: Notification[];
  
  setActiveProject: (project: Project | null) => void;
  addNotification: (type: 'success' | 'error' | 'info', message: string) => void;
  removeNotification: (id: string) => void;
  updateJob: (jobId: string, data: any) => void;
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  activeProject: null,
  jobs: {},
  notifications: [],

  setActiveProject: (project) => {
    set({ activeProject: project });
    if (project) {
      localStorage.setItem('activeProject', JSON.stringify(project));
    } else {
      localStorage.removeItem('activeProject');
    }
  },

  addNotification: (type, message) => {
    const id = Math.random().toString(36).substring(7);
    set((state) => ({
      notifications: [...state.notifications, { id, type, message }],
    }));
    
    // Auto-dismiss after 5 seconds
    setTimeout(() => {
      set((state) => ({
        notifications: state.notifications.filter((n) => n.id !== id),
      }));
    }, 5000);
  },

  removeNotification: (id) => {
    set((state) => ({
      notifications: state.notifications.filter((n) => n.id !== id),
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
}));
