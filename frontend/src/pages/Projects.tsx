import { type FormEventHandler, useEffect, useMemo, useState } from 'react';
import { ConfirmModal, EmptyState, StatusChip } from '../components';
import { useWorkflowStore } from '../stores/workflowStore';
import {
  type CreateProjectRequest,
  type CreateProjectResponse,
  type DeleteProjectResponse,
  type Project,
  type ProjectListResponse,
} from '../types/api';

async function getProjects(): Promise<Project[]> {
  const response = await fetch('/api/projects');
  if (!response.ok) {
    throw new Error(`Failed to fetch projects (${response.status})`);
  }
  const payload = (await response.json()) as ProjectListResponse;
  return payload.projects;
}

async function createProject(data: CreateProjectRequest): Promise<Project> {
  const response = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Failed to create project (${response.status})`);
  }
  const payload = (await response.json()) as CreateProjectResponse;
  return payload.project;
}

async function removeProject(name: string): Promise<DeleteProjectResponse> {
  const response = await fetch(`/api/projects/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Failed to delete project (${response.status})`);
  }
  return (await response.json()) as DeleteProjectResponse;
}

export function ProjectsPage() {
  const { activeProject, setActiveProject, addNotification } = useWorkflowStore();
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);

  const totals = useMemo(
    () =>
      projects.reduce(
        (acc, project) => ({
          videos: acc.videos + project.video_count,
          captions: acc.captions + project.caption_count,
          burned: acc.burned + project.burned_count,
        }),
        { videos: 0, captions: 0, burned: 0 },
      ),
    [projects],
  );

  useEffect(() => {
    const loadProjects = async () => {
      setIsLoading(true);
      setError(null);
      try {
        const loaded = await getProjects();
        setProjects(loaded);
        if (loaded.length > 0 && !activeProject) {
          setActiveProject(loaded[0]);
        }
      } catch (loadError) {
        const message = loadError instanceof Error ? loadError.message : 'Failed to load projects';
        setError(message);
        addNotification('error', message);
      } finally {
        setIsLoading(false);
      }
    };

    void loadProjects();
  }, [activeProject, addNotification, setActiveProject]);

  const handleCreateProject: FormEventHandler<HTMLFormElement> = async (event) => {
    event.preventDefault();
    const name = newProjectName.trim();
    if (!name) {
      return;
    }

    try {
      const created = await createProject({ name });
      setProjects((prev) => [created, ...prev]);
      setActiveProject(created);
      setIsCreateModalOpen(false);
      setNewProjectName('');
      addNotification('success', `Project "${created.name}" created`);
      window.dispatchEvent(new Event('projects:changed'));
    } catch (createError) {
      const message = createError instanceof Error ? createError.message : 'Failed to create project';
      addNotification('error', message);
    }
  };

  const handleDeleteProject = async () => {
    if (!projectToDelete) {
      return;
    }

    try {
      await removeProject(projectToDelete.name);
      setProjects((prev) => prev.filter((project) => project.name !== projectToDelete.name));
      if (activeProject?.name === projectToDelete.name) {
        setActiveProject(null);
      }
      addNotification('success', `Project "${projectToDelete.name}" deleted`);
      window.dispatchEvent(new Event('projects:changed'));
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : 'Failed to delete project';
      addNotification('error', message);
    } finally {
      setProjectToDelete(null);
    }
  };

  const sanitizeProjectName = (name: string) => name.toLowerCase().replace(/[^a-z0-9\-_ ]/g, '').trim().replaceAll(' ', '-');

  return (
    <div className="mx-auto max-w-7xl p-8">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="mb-2 text-3xl font-bold text-white">Projects</h1>
          <p className="text-slate-400">Manage your content generation campaigns</p>
        </div>
        <button
          type="button"
          onClick={() => setIsCreateModalOpen(true)}
          className="btn btn-primary gap-2"
        >
          <span className="text-lg leading-none">+</span>
          <span>New Project</span>
        </button>
      </div>

      <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="card">
          <div className="mb-1 text-sm font-medium text-slate-400">Total Videos</div>
          <div className="text-2xl font-bold text-white">{totals.videos}</div>
        </div>
        <div className="card">
          <div className="mb-1 text-sm font-medium text-slate-400">Total Captions</div>
          <div className="text-2xl font-bold text-white">{totals.captions}</div>
        </div>
        <div className="card">
          <div className="mb-1 text-sm font-medium text-slate-400">Total Burned</div>
          <div className="text-2xl font-bold text-white">{totals.burned}</div>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <div className="h-8 w-8 animate-spin rounded-full border-b-2 border-purple-500" />
        </div>
      ) : error ? (
        <EmptyState
          icon="!"
          title="Unable to load projects"
          description={error}
          action={{
            label: 'Retry',
            onClick: () => window.location.reload(),
          }}
        />
      ) : projects.length === 0 ? (
        <EmptyState
          icon="📁"
          title="No projects yet"
          description="Create your first project to start generating content."
          action={{
            label: 'Create Project',
            onClick: () => setIsCreateModalOpen(true),
          }}
        />
      ) : (
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
          {projects.map((project) => {
            const isActive = activeProject?.name === project.name;

            return (
              <div
                key={project.name}
                className={`group rounded-xl border p-6 transition-all ${
                  isActive
                    ? 'border-purple-500 bg-purple-500/10 ring-1 ring-purple-500/20'
                    : 'border-white/10 bg-white/5 hover:border-white/25'
                }`}
              >
                <div className="mb-4 flex items-start justify-between gap-2">
                  <div>
                    <h3 className="mb-1 text-lg font-semibold text-white transition-colors group-hover:text-purple-300">
                      {project.name}
                    </h3>
                    <div className="truncate text-xs text-slate-500">{project.path}</div>
                  </div>
                  {isActive ? <StatusChip status="active" /> : null}
                </div>

                <div className="mb-6 grid grid-cols-3 gap-2">
                  <div className="rounded-lg bg-slate-900/50 p-2 text-center">
                    <div className="text-lg font-bold text-white">{project.video_count}</div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Videos</div>
                  </div>
                  <div className="rounded-lg bg-slate-900/50 p-2 text-center">
                    <div className="text-lg font-bold text-white">{project.caption_count}</div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Captions</div>
                  </div>
                  <div className="rounded-lg bg-slate-900/50 p-2 text-center">
                    <div className="text-lg font-bold text-white">{project.burned_count}</div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Burned</div>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => setActiveProject(project)}
                    disabled={isActive}
                    className={`flex-1 rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                      isActive
                        ? 'cursor-default bg-white/10 text-slate-400'
                        : 'bg-purple-600 text-white hover:bg-purple-700'
                    }`}
                  >
                    {isActive ? 'Selected' : 'Select Project'}
                  </button>
                  <button
                    type="button"
                    onClick={() => setProjectToDelete(project)}
                    className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-red-500/10 hover:text-red-400"
                    title="Delete Project"
                  >
                    <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                      />
                    </svg>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {isCreateModalOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
          <div className="w-full max-w-md rounded-xl border border-white/15 bg-charcoal p-6 shadow-xl">
            <h2 className="mb-4 text-xl font-bold text-white">Create New Project</h2>
            <form onSubmit={handleCreateProject}>
              <div className="mb-6">
                <label htmlFor="projectName" className="label">
                  Project Name
                </label>
                <input
                  id="projectName"
                  type="text"
                  value={newProjectName}
                  onChange={(event) => setNewProjectName(event.target.value)}
                  className="input"
                  placeholder="e.g., spring-release-campaign"
                  autoFocus
                />
                {newProjectName ? (
                  <p className="mt-2 text-xs text-slate-500">
                    Sanitized: <span className="font-mono text-slate-400">{sanitizeProjectName(newProjectName)}</span>
                  </p>
                ) : null}
              </div>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setIsCreateModalOpen(false)}
                  className="btn btn-secondary"
                >
                  Cancel
                </button>
                <button type="submit" disabled={!newProjectName.trim()} className="btn btn-primary">
                  Create Project
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}

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
}
