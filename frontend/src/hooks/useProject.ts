import { useEffect } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { type Project } from '../types/api';

export function useProject() {
  const { activeProject, setActiveProject } = useWorkflowStore();

  useEffect(() => {
    // Load from localStorage on mount if not already in store
    if (!activeProject) {
      const stored = localStorage.getItem('activeProject');
      if (stored) {
        try {
          const project = JSON.parse(stored) as Project;
          setActiveProject(project);
        } catch (e) {
          console.error('Failed to parse stored project', e);
          localStorage.removeItem('activeProject');
        }
      }
    }
  }, [activeProject, setActiveProject]);

  return {
    activeProject,
    setActiveProject,
  };
}
