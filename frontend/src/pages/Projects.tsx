import React, { useEffect, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import type { Project, CreateProjectRequest } from '../types/api';
import { EmptyState } from '../components/EmptyState';
import { ConfirmModal } from '../components/ConfirmModal';
import { StatusChip } from '../components/StatusChip';

// Mock API client since we don't have a real one yet
const api = {
  getProjects: async (): Promise<Project[]> => {
    const res = await fetch('/api/projects');
    if (!res.ok) throw new Error('Failed to fetch projects');
    const data = await res.json();
    return data.projects;
  },
  createProject: async (data: CreateProjectRequest): Promise<Project> => {
    const res = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error('Failed to create project');
    return res.json();
  },
  deleteProject: async (name: string): Promise<void> => {
    const res = await fetch(`/api/projects/${name}`, {
      method: 'DELETE',
    });
    if (!res.ok) throw new Error('Failed to delete project');
  },
};

export const ProjectsPage: React.FC = () => {
  const { activeProject, setActiveProject, addNotification } = useWorkflowStore();
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);

  // Stats (mocked for now as they aren't in the Project type yet)
  const getProjectStats = () => ({
    videos: 0,
    captions: 0,
    burned: 0,
  });

  const fetchProjects = async () => {
    try {
      setIsLoading(true);
      const data = await api.getProjects();
      setProjects(data);
    } catch (error) {
      console.error('Error fetching projects:', error);
      // Fallback to mock data if API fails (for development)
      setProjects([
        {
          id: '1',
          name: 'Music Video Campaign',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
        {
          id: '2',
          name: 'Product Launch',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchProjects();
  }, []);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;

    try {
      const newProject = await api.createProject({ name: newProjectName });
      setProjects([...projects, newProject]);
      setActiveProject(newProject);
      setIsCreateModalOpen(false);
      setNewProjectName('');
      addNotification('success', `Project "${newProject.name}" created`);
    } catch (error) {
      console.error('Error creating project:', error);
      addNotification('error', 'Failed to create project');
    }
  };

  const handleDeleteProject = async () => {
    if (!projectToDelete) return;

    try {
      await api.deleteProject(projectToDelete.name);
      setProjects(projects.filter((p) => p.id !== projectToDelete.id));
      if (activeProject?.id === projectToDelete.id) {
        setActiveProject(null);
      }
      addNotification('success', `Project "${projectToDelete.name}" deleted`);
    } catch (error) {
      console.error('Error deleting project:', error);
      addNotification('error', 'Failed to delete project');
    } finally {
      setProjectToDelete(null);
    }
  };

  const sanitizeProjectName = (name: string) => {
    return name.replace(/[^a-zA-Z0-9-_ ]/g, '').trim();
  };

  return (
    <div className="p-8 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Projects</h1>
          <p className="text-slate-400">Manage your content generation campaigns</p>
        </div>
        <button
          onClick={() => setIsCreateModalOpen(true)}
          className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors flex items-center gap-2"
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          New Project
        </button>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-4">
          <div className="text-slate-400 text-sm font-medium mb-1">Total Videos</div>
          <div className="text-2xl font-bold text-white">0</div>
        </div>
        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-4">
          <div className="text-slate-400 text-sm font-medium mb-1">Total Captions</div>
          <div className="text-2xl font-bold text-white">0</div>
        </div>
        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-4">
          <div className="text-slate-400 text-sm font-medium mb-1">Total Burned</div>
          <div className="text-2xl font-bold text-white">0</div>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
        </div>
      ) : projects.length === 0 ? (
        <EmptyState
          title="No projects yet"
          message="Create your first project to start generating content."
          action={{
            label: 'Create Project',
            onClick: () => setIsCreateModalOpen(true),
          }}
          icon={
            <svg className="w-12 h-12 mx-auto text-slate-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
           {projects.map((project) => {
             const stats = getProjectStats();
             const isActive = activeProject?.id === project.id;

            return (
              <div
                key={project.id}
                className={`group relative bg-slate-800 border rounded-xl p-6 transition-all hover:shadow-lg ${
                  isActive ? 'border-blue-500 ring-1 ring-blue-500/20' : 'border-slate-700 hover:border-slate-600'
                }`}
              >
                <div className="flex justify-between items-start mb-4">
                  <div>
                    <h3 className="text-lg font-semibold text-white mb-1 group-hover:text-blue-400 transition-colors">
                      {project.name}
                    </h3>
                    <div className="text-xs text-slate-500">
                      Created {new Date(project.created_at).toLocaleDateString()}
                    </div>
                  </div>
                  {isActive && <StatusChip status="active" className="bg-blue-500/10 text-blue-400" />}
                </div>

                <div className="grid grid-cols-3 gap-2 mb-6">
                  <div className="text-center p-2 bg-slate-900/50 rounded-lg">
                    <div className="text-lg font-bold text-white">{stats.videos}</div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Videos</div>
                  </div>
                  <div className="text-center p-2 bg-slate-900/50 rounded-lg">
                    <div className="text-lg font-bold text-white">{stats.captions}</div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Captions</div>
                  </div>
                  <div className="text-center p-2 bg-slate-900/50 rounded-lg">
                    <div className="text-lg font-bold text-white">{stats.burned}</div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Burned</div>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <button
                    onClick={() => setActiveProject(project)}
                    disabled={isActive}
                    className={`flex-1 py-2 px-4 rounded-lg text-sm font-medium transition-colors ${
                      isActive
                        ? 'bg-slate-700 text-slate-400 cursor-default'
                        : 'bg-blue-600 hover:bg-blue-700 text-white'
                    }`}
                  >
                    {isActive ? 'Selected' : 'Select Project'}
                  </button>
                  <button
                    onClick={() => setProjectToDelete(project)}
                    className="p-2 text-slate-400 hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors"
                    title="Delete Project"
                  >
                    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Create Project Modal */}
      {isCreateModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
          <div className="bg-slate-800 border border-slate-700 rounded-xl w-full max-w-md p-6 shadow-xl">
            <h2 className="text-xl font-bold text-white mb-4">Create New Project</h2>
            <form onSubmit={handleCreateProject}>
              <div className="mb-6">
                <label htmlFor="projectName" className="block text-sm font-medium text-slate-300 mb-2">
                  Project Name
                </label>
                <input
                  type="text"
                  id="projectName"
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition-all"
                  placeholder="e.g., Summer Campaign 2024"
                  autoFocus
                />
                {newProjectName && (
                  <p className="mt-2 text-xs text-slate-500">
                    Preview: <span className="font-mono text-slate-400">{sanitizeProjectName(newProjectName)}</span>
                  </p>
                )}
              </div>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setIsCreateModalOpen(false)}
                  className="px-4 py-2 text-slate-300 hover:text-white hover:bg-slate-700 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!newProjectName.trim()}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg font-medium transition-colors"
                >
                  Create Project
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <ConfirmModal
        isOpen={!!projectToDelete}
        title="Delete Project"
        message={`Are you sure you want to delete "${projectToDelete?.name}"? This action cannot be undone.`}
        confirmLabel="Delete"
        isDestructive
        onConfirm={handleDeleteProject}
        onCancel={() => setProjectToDelete(null)}
      />
    </div>
  );
};
