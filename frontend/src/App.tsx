import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BrowserRouter, Link, useLocation, useNavigate } from 'react-router-dom';
import { CaptionsPage } from './pages/Captions';
import { BurnPage } from './pages/Burn';
import { GeneratePage } from './pages/Generate';
import { ProjectsPage } from './pages/Projects';
import { RecreatePage } from './pages/Recreate';
import { ClipperPage } from './pages/Clipper';
import { SlideshowPage } from './pages/Slideshow';
import { DistributionPage } from './pages/Distribution';
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

const TOOL_PATHS = ['/generate', '/clipper', '/recreate', '/captions', '/burn', '/slideshow'];

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

  const toolTabs = useMemo<NavTab[]>(
    () => [
      { path: '/generate', label: 'Generate', badge: videoRunningCount > 0 ? videoRunningCount : undefined },
      { path: '/clipper', label: 'Clipper' },
      { path: '/recreate', label: 'Recreate', badge: recreateJobActive ? 'LIVE' : undefined },
      { path: '/captions', label: 'Captions', badge: captionJobActive ? 'LIVE' : undefined },
      { path: '/burn', label: 'Burn', badge: burnReadyCount > 0 ? burnReadyCount : undefined },
      { path: '/slideshow', label: 'Slideshow' },
    ],
    [burnReadyCount, captionJobActive, recreateJobActive, videoRunningCount],
  );

  const [toolsOpen, setToolsOpen] = useState(false);
  const toolsRef = useRef<HTMLDivElement>(null);
  const isToolActive = TOOL_PATHS.includes(location.pathname);

  // Compute aggregate badge for the Tools menu
  const toolsBadgeCount = (videoRunningCount > 0 ? 1 : 0)
    + (recreateJobActive ? 1 : 0)
    + (captionJobActive ? 1 : 0)
    + (burnReadyCount > 0 ? 1 : 0);
  const hasToolLive = recreateJobActive || captionJobActive;

  // Close dropdown on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (toolsRef.current && !toolsRef.current.contains(e.target as Node)) {
        setToolsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

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

  const [showNewProjectInput, setShowNewProjectInput] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [visitedTabs, setVisitedTabs] = useState<Set<string>>(new Set(['/']));

  const handleQuickCreate = useCallback(() => {
    if (!showNewProjectInput) {
      setShowNewProjectInput(true);
      return;
    }
    const name = newProjectName.trim();
    if (name) {
      void createProject(name);
      setNewProjectName('');
      setShowNewProjectInput(false);
    }
  }, [createProject, newProjectName, showNewProjectInput]);

  useEffect(() => {
    void fetchProjects();
    void fetchHealth();
  }, [fetchHealth, fetchProjects]);

  // Track which tabs have been visited for lazy mounting
  useEffect(() => {
    setVisitedTabs((prev) => {
      // Normalize distribution sub-paths to their root for lazy mounting
      const key = location.pathname.startsWith('/distribution') ? '/distribution' : location.pathname;
      if (prev.has(key)) return prev;
      return new Set([...prev, key]);
    });
  }, [location.pathname]);

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
            {showNewProjectInput ? (
              <form
                className="flex items-center gap-1"
                onSubmit={(e) => { e.preventDefault(); handleQuickCreate(); }}
              >
                <input
                  autoFocus
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  placeholder="Project name..."
                  className="h-8 w-36 rounded border bg-background px-2 text-sm"
                  onBlur={() => { if (!newProjectName.trim()) setShowNewProjectInput(false); }}
                  onKeyDown={(e) => { if (e.key === 'Escape') { setShowNewProjectInput(false); setNewProjectName(''); } }}
                />
                <Button type="submit" size="sm" disabled={!newProjectName.trim()}>Create</Button>
              </form>
            ) : (
              <Button variant="outline" onClick={handleQuickCreate}>
                + New Project
              </Button>
            )}
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
          {/* Projects tab */}
          <Link
            to="/"
            className={`relative flex items-center gap-2 px-4 py-3 text-sm font-bold transition-colors ${
              location.pathname === '/'
                ? 'text-primary border-b-[3px] border-primary -mb-[2px]'
                : 'text-muted-foreground hover:text-foreground hover:bg-muted'
            }`}
          >
            Projects
            {projects.length > 0 ? (
              <Badge
                variant={location.pathname === '/' ? 'default' : 'secondary'}
                className="text-[10px] px-1.5 py-0 shadow-none"
              >
                {projects.length}
              </Badge>
            ) : null}
          </Link>

          {/* Tools dropdown */}
          <div ref={toolsRef} className="relative">
            <button
              type="button"
              onClick={() => setToolsOpen((prev) => !prev)}
              className={`relative flex items-center gap-2 px-4 py-3 text-sm font-bold transition-colors ${
                isToolActive
                  ? 'text-primary border-b-[3px] border-primary -mb-[2px]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              Tools
              <span className={`text-[10px] transition-transform ${toolsOpen ? 'rotate-180' : ''}`}>&#9660;</span>
              {toolsBadgeCount > 0 ? (
                <Badge
                  variant={hasToolLive ? 'success' : 'info'}
                  className="text-[10px] px-1.5 py-0 shadow-none"
                >
                  {hasToolLive ? 'LIVE' : toolsBadgeCount}
                </Badge>
              ) : null}
            </button>

            {toolsOpen && (
              <div className="absolute left-0 top-full mt-[2px] z-50 min-w-[200px] rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[4px_4px_0_0_var(--border)]">
                {toolTabs.map((tab) => {
                  const isActive = location.pathname === tab.path;
                  return (
                    <Link
                      key={tab.path}
                      to={tab.path}
                      onClick={() => setToolsOpen(false)}
                      className={`flex items-center justify-between gap-3 px-4 py-2.5 text-sm font-bold transition-colors ${
                        isActive
                          ? 'bg-primary/10 text-primary'
                          : 'text-foreground hover:bg-muted hover:text-primary'
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
            )}
          </div>

          {/* Distribution tab (merged Publish + Telegram) */}
          <Link
            to="/distribution"
            className={`relative flex items-center gap-2 px-4 py-3 text-sm font-bold transition-colors ${
              location.pathname.startsWith('/distribution')
                ? 'text-primary border-b-[3px] border-primary -mb-[2px]'
                : 'text-muted-foreground hover:text-foreground hover:bg-muted'
            }`}
          >
            Distribution
          </Link>
        </div>
      </nav>

      <ToastContainer toasts={notifications} onDismiss={removeNotification} />

      <main className="mx-auto max-w-7xl">
        {/* Lazy mount: pages only render once visited, then stay mounted (CSS toggle) */}
        <div style={{ display: location.pathname === '/' ? 'block' : 'none' }}>
          <ProjectsPage />
        </div>
        {visitedTabs.has('/generate') && (
          <div style={{ display: location.pathname === '/generate' ? 'block' : 'none' }}>
            <GeneratePage />
          </div>
        )}
        {visitedTabs.has('/captions') && (
          <div style={{ display: location.pathname === '/captions' ? 'block' : 'none' }}>
            <CaptionsPage />
          </div>
        )}
        {visitedTabs.has('/recreate') && (
          <div style={{ display: location.pathname === '/recreate' ? 'block' : 'none' }}>
            <RecreatePage />
          </div>
        )}
        {visitedTabs.has('/burn') && (
          <div style={{ display: location.pathname === '/burn' ? 'block' : 'none' }}>
            <BurnPage />
          </div>
        )}
        {visitedTabs.has('/clipper') && (
          <div style={{ display: location.pathname === '/clipper' ? 'block' : 'none' }}>
            <ClipperPage />
          </div>
        )}
        {visitedTabs.has('/slideshow') && (
          <div style={{ display: location.pathname === '/slideshow' ? 'block' : 'none' }}>
            <SlideshowPage />
          </div>
        )}
        {visitedTabs.has('/distribution') && (
          <div style={{ display: location.pathname.startsWith('/distribution') ? 'block' : 'none' }}>
            <DistributionPage />
          </div>
        )}
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
