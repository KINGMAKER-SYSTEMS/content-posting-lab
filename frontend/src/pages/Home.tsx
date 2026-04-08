import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiUrl } from '../lib/api';
import { useWorkflowStore } from '../stores/workflowStore';
import { ConfirmModal, EmptyState } from '../components';
import type {
  Project,
  ProjectListResponse,
  CreateProjectRequest,
  CreateProjectResponse,
  DeleteProjectResponse,
} from '../types/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';

// ── API helpers ─────────────────────────────────────────────────────

async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(apiUrl('/api/projects'));
  if (!res.ok) throw new Error(`Failed to fetch projects (${res.status})`);
  const payload = (await res.json()) as ProjectListResponse;
  return payload.projects;
}

async function createProjectApi(data: CreateProjectRequest): Promise<Project> {
  const res = await fetch(apiUrl('/api/projects'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed to create project (${res.status})`);
  }
  const payload = (await res.json()) as CreateProjectResponse;
  return payload.project;
}

async function deleteProjectApi(name: string): Promise<DeleteProjectResponse> {
  const res = await fetch(apiUrl(`/api/projects/${encodeURIComponent(name)}`), { method: 'DELETE' });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed to delete project (${res.status})`);
  }
  return (await res.json()) as DeleteProjectResponse;
}

// ── Component ───────────────────────────────────────────────────────

export function HomePage() {
  const navigate = useNavigate();
  const {
    activeProjectName,
    setActiveProjectName,
    addNotification,
    updateProjectStats,
    videoRunningCount,
    captionJobActive,
    recreateJobActive,
    burnReadyCount,
    generateJobs,
    uploadJobs,
  } = useWorkflowStore();

  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [showCreateInput, setShowCreateInput] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);
  const [showAllProjects, setShowAllProjects] = useState(false);

  // ── Load projects ───────────────────────────────────────────────

  const loadProjects = useCallback(async () => {
    setIsLoading(true);
    try {
      const loaded = await fetchProjects();
      setProjects(loaded);
      updateProjectStats(loaded);
      if (loaded.length > 0 && !activeProjectName) {
        setActiveProjectName(loaded[0].name);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load projects';
      addNotification('error', msg);
    } finally {
      setIsLoading(false);
    }
  }, [activeProjectName, addNotification, setActiveProjectName, updateProjectStats]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    const onChanged = () => void loadProjects();
    window.addEventListener('projects:changed', onChanged);
    return () => window.removeEventListener('projects:changed', onChanged);
  }, [loadProjects]);

  // ── Project CRUD ────────────────────────────────────────────────

  const handleCreate = useCallback(async () => {
    const name = newProjectName.trim();
    if (!name) return;
    try {
      const created = await createProjectApi({ name });
      setProjects((prev) => [created, ...prev]);
      setActiveProjectName(created.name);
      setShowCreateInput(false);
      setNewProjectName('');
      addNotification('success', `Project "${created.name}" created`);
      window.dispatchEvent(new Event('projects:changed'));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to create project';
      addNotification('error', msg);
    }
  }, [addNotification, newProjectName, setActiveProjectName]);

  const handleDelete = useCallback(async () => {
    if (!projectToDelete) return;
    try {
      await deleteProjectApi(projectToDelete.name);
      setProjects((prev) => prev.filter((p) => p.name !== projectToDelete.name));
      if (activeProjectName === projectToDelete.name) setActiveProjectName(null);
      addNotification('success', `Project "${projectToDelete.name}" deleted`);
      window.dispatchEvent(new Event('projects:changed'));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to delete project';
      addNotification('error', msg);
    } finally {
      setProjectToDelete(null);
    }
  }, [activeProjectName, addNotification, projectToDelete, setActiveProjectName]);

  // ── Derived data ────────────────────────────────────────────────

  const activeProject = useMemo(
    () => projects.find((p) => p.name === activeProjectName) ?? null,
    [activeProjectName, projects],
  );

  const totals = useMemo(
    () =>
      projects.reduce(
        (acc, p) => ({
          videos: acc.videos + p.video_count,
          captions: acc.captions + p.caption_count,
          burned: acc.burned + p.burned_count,
        }),
        { videos: 0, captions: 0, burned: 0 },
      ),
    [projects],
  );

  // Pipeline status items
  const pipelineItems = useMemo(() => {
    const items: { label: string; route: string; variant: 'success' | 'info' | 'warning' }[] = [];

    if (videoRunningCount > 0) {
      // Find active providers from generate jobs
      const activeProviders = generateJobs
        .filter((j) => j.videos.some((v) => !['done', 'failed', 'error'].includes(v.status)))
        .map((j) => j.provider)
        .filter((v, i, a) => a.indexOf(v) === i)
        .slice(0, 3);
      const providerText = activeProviders.length > 0 ? ` (${activeProviders.join(', ')})` : '';
      items.push({
        label: `${videoRunningCount} video${videoRunningCount > 1 ? 's' : ''} generating${providerText}`,
        route: '/create',
        variant: 'info',
      });
    }

    if (recreateJobActive) {
      items.push({ label: 'Recreate job running', route: '/create/recreate', variant: 'info' });
    }

    if (captionJobActive) {
      items.push({ label: 'Caption scrape active', route: '/captions', variant: 'info' });
    }

    if (burnReadyCount > 0) {
      items.push({
        label: `${burnReadyCount} video${burnReadyCount > 1 ? 's' : ''} ready to burn`,
        route: '/captions/burn',
        variant: 'warning',
      });
    }

    const uploading = uploadJobs.filter((j) => j.status === 'uploading' || j.status === 'queued');
    if (uploading.length > 0) {
      items.push({
        label: `Upload queue: ${uploading.length} pending`,
        route: '/distribute/uploads',
        variant: 'info',
      });
    }

    return items;
  }, [burnReadyCount, captionJobActive, generateJobs, recreateJobActive, uploadJobs, videoRunningCount]);

  // Recent activity from generate jobs
  const recentActivity = useMemo(() => {
    const items: { label: string; time: string; route: string }[] = [];

    // Completed generate jobs
    for (const job of generateJobs) {
      const doneCount = job.videos.filter((v) => v.status === 'done').length;
      if (doneCount > 0 && job.created_at) {
        items.push({
          label: `Generated ${doneCount} video${doneCount > 1 ? 's' : ''} via ${job.provider}`,
          time: job.created_at,
          route: '/create',
        });
      }
    }

    // Completed uploads
    for (const job of uploadJobs.filter((j) => j.status === 'completed')) {
      items.push({
        label: `Uploaded to ${job.account_name}`,
        time: job.completed_at ?? job.created_at,
        route: '/distribute/uploads',
      });
    }

    // Sort by time descending, take last 8
    return items
      .sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime())
      .slice(0, 8);
  }, [generateJobs, uploadJobs]);

  function timeAgo(iso: string): string {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  // ── Quick launch cards ──────────────────────────────────────────

  const quickLaunch = [
    {
      icon: '🎬',
      title: 'Generate Video',
      desc: 'AI-powered video creation',
      route: '/create',
      badge: videoRunningCount > 0 ? videoRunningCount : null,
    },
    {
      icon: '✂️',
      title: 'Clip Video',
      desc: 'Chop videos into short-form clips',
      route: '/create/clipper',
      badge: null,
    },
    {
      icon: '💬',
      title: 'Scrape Captions',
      desc: 'Extract captions from TikTok',
      route: '/captions',
      badge: captionJobActive ? 'LIVE' : null,
    },
    {
      icon: '🔥',
      title: 'Burn Captions',
      desc: 'Overlay captions onto videos',
      route: '/captions/burn',
      badge: burnReadyCount > 0 ? burnReadyCount : null,
    },
  ];

  // ── Render ──────────────────────────────────────────────────────

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
      </div>
    );
  }

  if (projects.length === 0) {
    return (
      <div className="mx-auto max-w-7xl p-8">
        <EmptyState
          icon="📁"
          title="No projects yet"
          description="Create your first project to start generating content."
          action={{
            label: 'Create Project',
            onClick: () => setShowCreateInput(true),
          }}
        />
        {showCreateInput && (
          <div className="mx-auto mt-6 flex max-w-sm items-center gap-2">
            <Input
              autoFocus
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              placeholder="Project name..."
              onKeyDown={(e) => {
                if (e.key === 'Enter') void handleCreate();
                if (e.key === 'Escape') { setShowCreateInput(false); setNewProjectName(''); }
              }}
            />
            <Button onClick={() => void handleCreate()} disabled={!newProjectName.trim()}>Create</Button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl p-6 space-y-6">

      {/* ── Project Summary ──────────────────────────────────────── */}
      <Card className="border-primary shadow-[4px_4px_0_0_var(--primary)]">
        <CardContent className="pt-0">
          <div className="flex items-center justify-between mb-4">
            <div>
              <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground mb-1">
                Active Project
              </div>
              <h2 className="text-2xl font-heading text-foreground">
                {activeProject?.name ?? 'None selected'}
              </h2>
            </div>
            <div className="flex items-center gap-2">
              {projects.length > 1 && (
                <Button variant="outline" size="sm" onClick={() => setShowAllProjects(!showAllProjects)}>
                  {showAllProjects ? 'Hide' : `All Projects (${projects.length})`}
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowCreateInput(!showCreateInput)}
              >
                + New
              </Button>
            </div>
          </div>

          {/* Inline create */}
          {showCreateInput && (
            <div className="mb-4 flex items-center gap-2">
              <Input
                autoFocus
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                placeholder="Project name..."
                className="max-w-xs"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handleCreate();
                  if (e.key === 'Escape') { setShowCreateInput(false); setNewProjectName(''); }
                }}
              />
              <Button size="sm" onClick={() => void handleCreate()} disabled={!newProjectName.trim()}>
                Create
              </Button>
              <Button variant="outline" size="sm" onClick={() => { setShowCreateInput(false); setNewProjectName(''); }}>
                Cancel
              </Button>
            </div>
          )}

          {/* Stats grid */}
          {activeProject && (
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 text-center">
                <div className="text-2xl font-heading text-foreground">{activeProject.video_count}</div>
                <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Videos</div>
              </div>
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 text-center">
                <div className="text-2xl font-heading text-foreground">{activeProject.caption_count}</div>
                <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Captions</div>
              </div>
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 text-center">
                <div className="text-2xl font-heading text-foreground">{activeProject.burned_count}</div>
                <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Burned</div>
              </div>
            </div>
          )}

          {/* Totals bar (all projects) */}
          {projects.length > 1 && (
            <div className="mt-3 flex items-center gap-4 text-xs text-muted-foreground">
              <span>All projects:</span>
              <span>{totals.videos} videos</span>
              <span>{totals.captions} captions</span>
              <span>{totals.burned} burned</span>
            </div>
          )}

          {/* Expandable project list */}
          {showAllProjects && (
            <div className="mt-4 space-y-2">
              {projects.map((p) => (
                <div
                  key={p.name}
                  className={`flex items-center justify-between rounded-[var(--border-radius)] border-2 border-border p-3 transition-colors ${
                    p.name === activeProjectName ? 'bg-primary/10 border-primary' : 'bg-card hover:bg-muted'
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-heading text-foreground">{p.name}</span>
                    <span className="text-xs text-muted-foreground">
                      {p.video_count}v / {p.caption_count}c / {p.burned_count}b
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {p.name !== activeProjectName && (
                      <Button variant="outline" size="sm" onClick={() => setActiveProjectName(p.name)}>
                        Select
                      </Button>
                    )}
                    {p.name === activeProjectName && (
                      <Badge variant="default" className="text-[10px] px-1.5 py-0 shadow-none">Active</Badge>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setProjectToDelete(p)}
                      className="text-muted-foreground hover:text-destructive hover:bg-red-50 px-2"
                    >
                      <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Pipeline Status ──────────────────────────────────────── */}
      <div>
        <div className="text-sm font-bold uppercase tracking-wider text-muted-foreground mb-3">
          Pipeline Status
        </div>
        {pipelineItems.length === 0 ? (
          <div className="rounded-[var(--border-radius)] border-2 border-dashed border-border bg-muted p-6 text-center">
            <div className="text-sm text-muted-foreground">
              Nothing running — start by creating content
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            {pipelineItems.map((item, i) => (
              <button
                key={i}
                type="button"
                onClick={() => navigate(item.route)}
                className="flex w-full items-center gap-3 rounded-[var(--border-radius)] border-2 border-border bg-card p-3 shadow-[2px_2px_0_0_var(--border)] hover:bg-muted transition-colors text-left"
              >
                <span className="h-2.5 w-2.5 rounded-full bg-green-500 animate-pulse flex-shrink-0" />
                <span className="text-sm font-base text-foreground flex-1">{item.label}</span>
                <Badge variant={item.variant === 'warning' ? 'warning' : 'info'} className="text-[10px] px-1.5 py-0 shadow-none">
                  {item.variant === 'info' ? 'ACTIVE' : 'READY'}
                </Badge>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* ── Quick Launch ─────────────────────────────────────────── */}
      <div>
        <div className="text-sm font-bold uppercase tracking-wider text-muted-foreground mb-3">
          Quick Launch
        </div>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {quickLaunch.map((card) => (
            <button
              key={card.route}
              type="button"
              onClick={() => navigate(card.route)}
              className="group rounded-[var(--border-radius)] border-2 border-border bg-card p-6 shadow-[var(--shadow)] cursor-pointer hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-[3px_3px_0_0_var(--border)] transition-all text-left"
            >
              <div className="text-3xl mb-3">{card.icon}</div>
              <div className="flex items-center gap-2">
                <div className="text-lg font-heading text-foreground group-hover:text-primary transition-colors">
                  {card.title}
                </div>
                {card.badge !== null && (
                  <Badge
                    variant={card.badge === 'LIVE' ? 'success' : 'info'}
                    className="text-[10px] px-1.5 py-0 shadow-none"
                  >
                    {card.badge}
                  </Badge>
                )}
              </div>
              <div className="text-xs text-muted-foreground mt-1">{card.desc}</div>
            </button>
          ))}
        </div>
      </div>

      {/* ── Recent Activity ──────────────────────────────────────── */}
      {recentActivity.length > 0 && (
        <div>
          <div className="text-sm font-bold uppercase tracking-wider text-muted-foreground mb-3">
            Recent Activity
          </div>
          <div className="space-y-2">
            {recentActivity.map((item, i) => (
              <button
                key={i}
                type="button"
                onClick={() => navigate(item.route)}
                className="flex w-full items-center gap-3 rounded-[var(--border-radius)] border-2 border-border bg-card p-3 shadow-[2px_2px_0_0_var(--border)] hover:bg-muted transition-colors text-left"
              >
                <span className="text-sm font-base text-foreground flex-1">{item.label}</span>
                <span className="text-xs text-muted-foreground whitespace-nowrap">{timeAgo(item.time)}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Delete Confirm ───────────────────────────────────────── */}
      <ConfirmModal
        isOpen={!!projectToDelete}
        title="Delete Project"
        message={`Are you sure you want to delete "${projectToDelete?.name}"? This action cannot be undone.`}
        confirmLabel="Delete"
        isDestructive
        onConfirm={handleDelete}
        onCancel={() => setProjectToDelete(null)}
      />
    </div>
  );
}
