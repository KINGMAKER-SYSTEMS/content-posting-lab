import { useWorkflowStore } from '../stores/workflowStore';

export function useProject() {
  const { activeProject, setActiveProject } = useWorkflowStore();

  return {
    activeProject,
    setActiveProject,
  };
}
