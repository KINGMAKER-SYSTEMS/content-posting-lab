import { useWorkflowStore } from '../stores/workflowStore';

export function useProject() {
  const { activeProjectName, setActiveProjectName } = useWorkflowStore();

  return {
    activeProjectName,
    setActiveProjectName,
  };
}
