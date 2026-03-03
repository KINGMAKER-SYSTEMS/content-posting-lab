import { type FormEventHandler, useEffect, useMemo, useState } from 'react';
import { apiUrl } from '../lib/api';
import { ConfirmModal, EmptyState, StatusChip } from '../components';
import { useWorkflowStore } from '../stores/workflowStore';
import {
  type CreateProjectRequest,
  type CreateProjectResponse,
  type DeleteProjectResponse,
  type Project,
  type ProjectListResponse,
} from '../types/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

async function getProjects(): Promise<Project[]> {
  const response = await fetch(apiUrl('/api/projects'));
  if (!response.ok) {
    throw new Error(`Failed to fetch projects (${response.status})`);
  }
  const payload = (await response.json()) as ProjectListResponse;
  return payload.projects;
}

async function createProject(data: CreateProjectRequest): Promise<Project> {
  const response = await fetch(apiUrl('/api/projects'), {
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
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(name)}`), {
    method: 'DELETE',
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Failed to delete project (${response.status})`);
  }
  return (await response.json()) as DeleteProjectResponse;
}

export function ProjectsPage() {
  const { activeProjectName, setActiveProjectName, addNotification } = useWorkflowStore();
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
        if (loaded.length > 0 && !activeProjectName) {
          setActiveProjectName(loaded[0].name);
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
  }, [activeProjectName, addNotification, setActiveProjectName]);

  const handleCreateProject: FormEventHandler<HTMLFormElement> = async (event) => {
    event.preventDefault();
    const name = newProjectName.trim();
    if (!name) {
      return;
    }

    try {
      const created = await createProject({ name });
      setProjects((prev) => [created, ...prev]);
      setActiveProjectName(created.name);
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
      if (activeProjectName === projectToDelete.name) {
        setActiveProjectName(null);
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
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="mb-1 text-3xl font-heading text-foreground">Projects</h1>
          <p className="text-sm text-muted-foreground">Manage your content generation campaigns</p>
        </div>
        <Button onClick={() => setIsCreateModalOpen(true)}>
          + New Project
        </Button>
      </div>

      {/* Stats row */}
      <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card>
          <CardContent className="pt-0">
            <div className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Videos Generated</div>
            <div className="text-3xl font-heading text-primary">{totals.videos}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-0">
            <div className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Captions Scraped</div>
            <div className="text-3xl font-heading text-accent">{totals.captions}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-0">
            <div className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Videos Burned</div>
            <div className="text-3xl font-heading text-foreground">{totals.burned}</div>
          </CardContent>
        </Card>
      </div>

      {/* Project grid */}
      {isLoading ? (
        <div className="flex justify-center py-12">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
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
            const isActive = activeProjectName === project.name;

            return (
              <Card
                key={project.name}
                className={`group transition-all ${
                  isActive
                    ? 'border-primary shadow-[4px_4px_0_0_var(--primary)]'
                    : 'hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[3px_3px_0_0_var(--border)]'
                }`}
              >
                <CardContent className="pt-0">
                  <div className="mb-4 flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <h3 className="mb-1 text-lg font-heading text-foreground group-hover:text-primary transition-colors">
                        {project.name}
                      </h3>
                      <div className="truncate text-xs text-muted-foreground">{project.path}</div>
                    </div>
                    {isActive ? <StatusChip status="active" /> : null}
                  </div>

                  <div className="mb-6 grid grid-cols-3 gap-2">
                    <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-2 text-center">
                      <div className="text-lg font-heading text-foreground">{project.video_count}</div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Videos</div>
                    </div>
                    <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-2 text-center">
                      <div className="text-lg font-heading text-foreground">{project.caption_count}</div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Captions</div>
                    </div>
                    <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-2 text-center">
                      <div className="text-lg font-heading text-foreground">{project.burned_count}</div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Burned</div>
                    </div>
                  </div>

                  <div className="flex items-center gap-3">
                    <Button
                      variant={isActive ? 'outline' : 'default'}
                      className="flex-1"
                      disabled={isActive}
                      onClick={() => setActiveProjectName(project.name)}
                    >
                      {isActive ? 'Selected' : 'Select Project'}
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => setProjectToDelete(project)}
                      className="text-muted-foreground hover:text-destructive hover:bg-red-50"
                    >
                      <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                        />
                      </svg>
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Create modal — using shadcn Dialog */}
      <Dialog open={isCreateModalOpen} onOpenChange={(open) => { if (!open) setIsCreateModalOpen(false); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create New Project</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleCreateProject}>
            <div className="mb-6">
              <Label htmlFor="projectName">Project Name</Label>
              <Input
                id="projectName"
                value={newProjectName}
                onChange={(event) => setNewProjectName(event.target.value)}
                placeholder="e.g., spring-release-campaign"
                className="mt-2"
                autoFocus
              />
              {newProjectName ? (
                <p className="mt-2 text-xs text-muted-foreground">
                  Sanitized: <span className="font-mono font-bold text-foreground">{sanitizeProjectName(newProjectName)}</span>
                </p>
              ) : null}
            </div>
            <DialogFooter>
              <Button variant="outline" type="button" onClick={() => setIsCreateModalOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!newProjectName.trim()}>
                Create Project
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

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
