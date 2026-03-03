import { useCallback, useEffect, useMemo, useState } from 'react';
import { BrowserRouter, Link, useLocation, useNavigate } from 'react-router-dom';
import { CaptionsPage } from './pages/Captions';
import { BurnPage } from './pages/Burn';
import { GeneratePage } from './pages/Generate';
import { ProjectsPage } from './pages/Projects';
import { RecreatePage } from './pages/Recreate';
import { ProjectSelector, ToastContainer } from './components';
import { useWorkflowStore } from './stores/workflowStore';
import { type CreateProjectResponse, type HealthResponse, type Project, type ProjectListResponse } from './types/api';
import { apiUrl } from './lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

interface NavTab {
  path: string;
  label: string;
  badge?: string | number;
}

interface HealthBannerItem {
  id: string;
  tone: 'warn' | 'info';
  message: string;
}

function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    activeProjectName,
    notifications,
    burnReadyCount,
    videoRunningCount,
    captionJobActive,
    recreateJobActive,
    removeNotification,
    setActiveProjectName,
    updateProjectStats,
    addNotification,
  } = useWorkflowStore();

  const [projects, setProjects] = useState<Project[]>([]);
  const [healthItems, setHealthItems] = useState<HealthBannerItem[]>([]);
  const [dismissedHealth, setDismissedHealth] = useState<string[]>([]);

  const tabs = useMemo<NavTab[]>(
    () => [
      { path: '/', label: 'Projects', badge: projects.length > 0 ? projects.length : undefined },
      { path: '/recreate', label: 'Recreate', badge: recreateJobActive ? 'LIVE' : undefined },
      { path: '/generate', label: 'Generate', badge: videoRunningCount > 0 ? videoRunningCount : undefined },
      { path: '/captions', label: 'Captions', badge: captionJobActive ? 'LIVE' : undefined },
      { path: '/burn', label: 'Burn', badge: burnReadyCount > 0 ? burnReadyCount : undefined },
    ],
    [burnReadyCount, captionJobActive, recreateJobActive, projects.length, videoRunningCount],
  );

  const visibleHealthItems = useMemo(
    () => healthItems.filter((item) => !dismissedHealth.includes(item.id)),
    [dismissedHealth, healthItems],
  );

  const fetchProjects = useCallback(async () => {
    try {
      const response = await fetch(apiUrl('/api/projects'));
      if (!response.ok) {
        throw new Error(`Failed to load projects (${response.status})`);
      }

      const payload = (await response.json()) as ProjectListResponse;
      const list = payload.projects ?? [];
      setProjects(list);
      updateProjectStats(list);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to load projects';
      addNotification('error', message);
      setProjects([]);
    }
  }, [addNotification, updateProjectStats]);

  const fetchHealth = useCallback(async () => {
    try {
      const response = await fetch(apiUrl('/api/health'));
      if (!response.ok) {
        throw new Error(`Health check failed (${response.status})`);
      }

      const payload = (await response.json()) as HealthResponse;
      const items: HealthBannerItem[] = [];

      if (!payload.ffmpeg) {
        items.push({ id: 'ffmpeg', tone: 'warn', message: 'ffmpeg not found - generation and burn jobs will fail.' });
      }
      if (!payload.ytdlp) {
        items.push({ id: 'ytdlp', tone: 'warn', message: 'yt-dlp not found - caption scraping will fail.' });
      }

      const providerCount = Object.values(payload.providers || {}).filter(Boolean).length;
      if (providerCount === 0) {
        items.push({
          id: 'providers',
          tone: 'info',
          message: 'No video providers configured. Add API keys to .env to enable generation.',
        });
      }

      setHealthItems(items);
      setDismissedHealth([]);
    } catch {
      setHealthItems([
        {
          id: 'api-unreachable',
          tone: 'warn',
          message: 'Cannot connect to server on port 8000. Start API with `python app.py`.',
        },
      ]);
      setDismissedHealth([]);
    }
  }, []);

  const createProject = useCallback(
    async (name: string) => {
      const trimmed = name.trim();
      if (!trimmed) {
        return;
      }

      try {
        const response = await fetch(apiUrl('/api/projects'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: trimmed }),
        });

        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || `Failed to create project (${response.status})`);
        }

        const payload = (await response.json()) as CreateProjectResponse;
        setActiveProjectName(payload.project.name);
        addNotification('success', `Project "${payload.project.name}" created`);
        void fetchProjects();
        window.dispatchEvent(new Event('projects:changed'));
        navigate('/');
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to create project';
        addNotification('error', message);
      }
    },
    [addNotification, fetchProjects, navigate, setActiveProjectName],
  );

  const handleQuickCreate = useCallback(() => {
    const name = window.prompt('Enter a new project name');
    if (name) {
      void createProject(name);
    }
  }, [createProject]);

  useEffect(() => {
    void fetchProjects();
    void fetchHealth();
  }, [fetchHealth, fetchProjects]);

  useEffect(() => {
    const onProjectsChanged = () => {
      void fetchProjects();
    };

    window.addEventListener('projects:changed', onProjectsChanged);
    return () => window.removeEventListener('projects:changed', onProjectsChanged);
  }, [fetchProjects]);

  useEffect(() => {
    if (projects.length === 0) {
      if (activeProjectName) {
        setActiveProjectName(null);
      }
      return;
    }

    if (!activeProjectName) {
      setActiveProjectName(projects[0].name);
      return;
    }

    const stillExists = projects.some((p) => p.name === activeProjectName);
    if (!stillExists) {
      setActiveProjectName(projects[0].name);
    }
  }, [activeProjectName, projects, setActiveProjectName]);

  return (
    <div className="min-h-screen bg-background text-foreground font-base">
      {/* Top header — thick bottom border, card bg */}
      <header className="border-b-2 border-border bg-card">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <div className="h-6 w-6 rounded-[var(--border-radius)] border-2 border-border bg-primary shadow-[2px_2px_0_0_var(--border)]" />
            <Link to="/" className="text-base font-heading text-foreground hover:text-primary transition-colors">
              Content Posting Lab
            </Link>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <ProjectSelector
              className="w-full min-w-[260px] sm:w-[320px]"
              projects={projects}
              activeProjectName={activeProjectName}
              onSelect={(project) => setActiveProjectName(project.name)}
              onCreate={createProject}
            />
            <Button variant="outline" onClick={handleQuickCreate}>
              + New Project
            </Button>
          </div>
        </div>
      </header>

      {/* Health warnings */}
      {visibleHealthItems.length > 0 ? (
        <div className="border-b-2 border-border bg-muted px-4 py-2">
          <div className="mx-auto flex max-w-7xl flex-col gap-2">
            {visibleHealthItems.map((item) => (
              <div
                key={item.id}
                className={`flex items-start justify-between gap-4 rounded-[var(--border-radius)] border-2 border-border px-3 py-2 text-sm font-base shadow-[2px_2px_0_0_var(--border)] ${
                  item.tone === 'warn'
                    ? 'bg-amber-100 text-amber-900'
                    : 'bg-secondary text-accent'
                }`}
              >
                <span>{item.message}</span>
                <button
                  type="button"
                  className="rounded-[var(--border-radius)] border-2 border-border bg-card px-2 py-0.5 text-xs font-bold text-foreground hover:bg-muted transition-colors"
                  onClick={() => setDismissedHealth((prev) => [...prev, item.id])}
                >
                  Dismiss
                </button>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* Tab navigation — sticky, thick border bottom */}
      <nav className="sticky top-0 z-40 border-b-2 border-border bg-card">
        <div className="mx-auto flex max-w-7xl gap-1 px-4">
          {tabs.map((tab) => {
            const isActive = location.pathname === tab.path;
            return (
              <Link
                key={tab.path}
                to={tab.path}
                className={`relative flex items-center gap-2 px-4 py-3 text-sm font-bold transition-colors ${
                  isActive
                    ? 'text-primary border-b-[3px] border-primary -mb-[2px]'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted'
                }`}
              >
                {tab.label}
                {tab.badge !== undefined ? (
                  <Badge
                    variant={tab.badge === 'LIVE' ? 'success' : isActive ? 'default' : 'secondary'}
                    className="text-[10px] px-1.5 py-0 shadow-none"
                  >
                    {tab.badge}
                  </Badge>
                ) : null}
              </Link>
            );
          })}
        </div>
      </nav>

      <ToastContainer toasts={notifications} onDismiss={removeNotification} />

      <main className="mx-auto max-w-7xl">
        <div style={{ display: location.pathname === '/' ? 'block' : 'none' }}>
          <ProjectsPage />
        </div>
        <div style={{ display: location.pathname === '/generate' ? 'block' : 'none' }}>
          <GeneratePage />
        </div>
        <div style={{ display: location.pathname === '/captions' ? 'block' : 'none' }}>
          <CaptionsPage />
        </div>
        <div style={{ display: location.pathname === '/recreate' ? 'block' : 'none' }}>
          <RecreatePage />
        </div>
        <div style={{ display: location.pathname === '/burn' ? 'block' : 'none' }}>
          <BurnPage />
        </div>
      </main>
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AppShell />
    </BrowserRouter>
  );
}

export default App;
