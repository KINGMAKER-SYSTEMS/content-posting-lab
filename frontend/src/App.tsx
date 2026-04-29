import { useCallback, useEffect, useMemo, useState } from 'react';
import { BrowserRouter, Link, useLocation } from 'react-router-dom';
import { MoonIcon, SunIcon } from '@phosphor-icons/react';
import { HomePage } from './pages/Home';
import { CreatePage } from './pages/Create';
import { CaptionsStagePage } from './pages/CaptionsStage';
import { DistributionPage } from './pages/Distribution';
import { PipelinePage } from './pages/Pipeline';
import { PipelineWorkspacePage } from './pages/PipelineWorkspace';
import { ProjectSelector, ToastContainer } from './components';
import { BrandLogo } from './components/BrandLogo';
import { useTheme } from './components/ThemeProvider';
import { useWorkflowStore } from './stores/workflowStore';
import { type HealthResponse, type Project, type ProjectListResponse } from './types/api';
import { apiUrl } from './lib/api';
import { Badge } from '@/components/ui/badge';

interface HealthBannerItem {
  id: string;
  tone: 'warn' | 'info';
  message: string;
}

function AppShell() {
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();
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
  const [highlightedProject, setHighlightedProject] = useState<string | null>(null);

  // Track which top-level stages have been visited for lazy mounting
  const [visitedStages, setVisitedStages] = useState<Set<string>>(new Set(['/']));
  const [pipelineNewCount, setPipelineNewCount] = useState<number>(0);

  const visibleHealthItems = useMemo(
    () => healthItems.filter((item) => !dismissedHealth.includes(item.id)),
    [dismissedHealth, healthItems],
  );

  // ── Aggregate badges for pipeline tabs ──────────────────────────

  const createBadge = useMemo(() => {
    if (recreateJobActive) return 'LIVE';
    if (videoRunningCount > 0) return videoRunningCount;
    return undefined;
  }, [recreateJobActive, videoRunningCount]);

  const captionsBadge = useMemo(() => {
    if (captionJobActive) return 'LIVE';
    if (burnReadyCount > 0) return burnReadyCount;
    return undefined;
  }, [burnReadyCount, captionJobActive]);

  // ── Data fetching ───────────────────────────────────────────────

  const fetchProjects = useCallback(async () => {
    try {
      const response = await fetch(apiUrl('/api/projects'));
      if (!response.ok) throw new Error(`Failed to load projects (${response.status})`);
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
      if (!response.ok) throw new Error(`Health check failed (${response.status})`);
      const payload = (await response.json()) as HealthResponse;
      const items: HealthBannerItem[] = [];
      if (!payload.ffmpeg) items.push({ id: 'ffmpeg', tone: 'warn', message: 'ffmpeg not found - generation and burn jobs will fail.' });
      if (!payload.ytdlp) items.push({ id: 'ytdlp', tone: 'warn', message: 'yt-dlp not found - caption scraping will fail.' });
      const providerCount = Object.values(payload.providers || {}).filter(Boolean).length;
      if (providerCount === 0) items.push({ id: 'providers', tone: 'info', message: 'No video providers configured. Add API keys to .env to enable generation.' });
      setHealthItems(items);
      setDismissedHealth([]);
    } catch {
      setHealthItems([{ id: 'api-unreachable', tone: 'warn', message: 'Cannot connect to server on port 8000. Start API with `python app.py`.' }]);
      setDismissedHealth([]);
    }
  }, []);

  useEffect(() => {
    void fetchProjects();
    void fetchHealth();
  }, [fetchHealth, fetchProjects]);

  // Track visited stages for lazy mounting
  useEffect(() => {
    setVisitedStages((prev) => {
      let key: string;
      if (location.pathname.startsWith('/create')) key = '/create';
      else if (location.pathname.startsWith('/captions')) key = '/captions';
      else if (location.pathname.startsWith('/distribute')) key = '/distribute';
      else if (location.pathname.startsWith('/pipeline')) key = '/pipeline';
      else key = '/';
      if (prev.has(key)) return prev;
      return new Set([...prev, key]);
    });
  }, [location.pathname]);

  // Poll pipeline for "Pending Setup" badge count
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const resp = await fetch(apiUrl('/api/pipeline/stages'));
        if (!resp.ok) return;
        const data = await resp.json();
        if (cancelled) return;
        const stage = (data.stages || []).find((s: { status: string }) => s.status === 'New — Pending Setup');
        setPipelineNewCount(stage?.count ?? 0);
      } catch {
        // ignore
      }
    };
    void refresh();
    const t = window.setInterval(refresh, 60_000);
    return () => { cancelled = true; window.clearInterval(t); };
  }, []);

  useEffect(() => {
    const onProjectsChanged = () => void fetchProjects();
    window.addEventListener('projects:changed', onProjectsChanged);
    return () => window.removeEventListener('projects:changed', onProjectsChanged);
  }, [fetchProjects]);

  useEffect(() => {
    if (projects.length === 0) {
      if (activeProjectName) setActiveProjectName(null);
      return;
    }
    if (!activeProjectName) {
      setActiveProjectName(projects[0].name);
      return;
    }
    const stillExists = projects.some((p) => p.name === activeProjectName);
    if (!stillExists) setActiveProjectName(projects[0].name);
  }, [activeProjectName, projects, setActiveProjectName]);

  // ── Active tab detection ────────────────────────────────────────

  const activeStage = useMemo(() => {
    if (location.pathname.startsWith('/create')) return 'create';
    if (location.pathname.startsWith('/captions')) return 'captions';
    if (location.pathname.startsWith('/distribute')) return 'distribute';
    if (location.pathname.startsWith('/pipeline')) return 'pipeline';
    return 'home';
  }, [location.pathname]);

  return (
    <div className="min-h-screen bg-background text-foreground font-base">
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="border-b border-border bg-card">
        <div className="mx-auto max-w-7xl px-4 py-3">
          {/* Row 1: Logo + Project Selector */}
          <div className="flex items-center justify-between mb-2">
            <Link to="/" className="group flex items-center gap-3 text-foreground">
              <BrandLogo className="h-5 w-auto opacity-95 transition-opacity group-hover:opacity-100" />
              <span className="h-4 w-px bg-border" aria-hidden="true" />
              <span className="text-sm font-heading tracking-[0.08em] uppercase text-muted-foreground group-hover:text-foreground transition-colors">
                Content Posting Lab
              </span>
            </Link>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={toggleTheme}
                title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
                aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
                className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card text-muted-foreground transition-colors hover:text-foreground hover:border-primary/40"
              >
                {theme === 'dark' ? <SunIcon size={16} weight="bold" /> : <MoonIcon size={16} weight="bold" />}
              </button>
              <ProjectSelector
                className="w-full min-w-[260px] sm:w-[320px]"
                projects={projects}
                activeProjectName={activeProjectName}
                onSelect={(project) => setActiveProjectName(project.name)}
                onCreate={async (name) => {
                  const trimmed = name.trim();
                  if (!trimmed) return;
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
                    const payload = await response.json();
                    await fetchProjects();
                    setActiveProjectName(payload.project.name);
                    setHighlightedProject(payload.project.name);
                    window.setTimeout(() => setHighlightedProject((curr) => curr === payload.project.name ? null : curr), 2000);
                    addNotification('success', `Project "${payload.project.name}" created`);
                    window.dispatchEvent(new Event('projects:changed'));
                  } catch (error) {
                    const message = error instanceof Error ? error.message : 'Failed to create project';
                    addNotification('error', message);
                  }
                }}
                highlightedProjectName={highlightedProject}
              />
            </div>
          </div>

          {/* Row 2: Pipeline tabs */}
          <div className="flex gap-1 -mb-[2px]">
            {/* Home */}
            <Link
              to="/"
              className={`relative flex items-center gap-2 px-4 py-2.5 text-sm font-bold transition-colors ${
                activeStage === 'home'
                  ? 'text-foreground after:absolute after:bottom-0 after:left-3 after:right-3 after:h-[2px] after:rounded-full after:bg-[var(--brand-gradient)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              Home
            </Link>

            {/* Create */}
            <Link
              to="/create"
              className={`relative flex items-center gap-2 px-4 py-2.5 text-sm font-bold transition-colors ${
                activeStage === 'create'
                  ? 'text-foreground after:absolute after:bottom-0 after:left-3 after:right-3 after:h-[2px] after:rounded-full after:bg-[var(--brand-gradient)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              Create
              {createBadge !== undefined && (
                <Badge
                  variant={createBadge === 'LIVE' ? 'success' : activeStage === 'create' ? 'default' : 'secondary'}
                  className="text-[10px] px-1.5 py-0 shadow-none"
                >
                  {createBadge}
                </Badge>
              )}
            </Link>

            {/* Captions */}
            <Link
              to="/captions"
              className={`relative flex items-center gap-2 px-4 py-2.5 text-sm font-bold transition-colors ${
                activeStage === 'captions'
                  ? 'text-foreground after:absolute after:bottom-0 after:left-3 after:right-3 after:h-[2px] after:rounded-full after:bg-[var(--brand-gradient)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              Captions
              {captionsBadge !== undefined && (
                <Badge
                  variant={captionsBadge === 'LIVE' ? 'success' : activeStage === 'captions' ? 'default' : 'secondary'}
                  className="text-[10px] px-1.5 py-0 shadow-none"
                >
                  {captionsBadge}
                </Badge>
              )}
            </Link>

            {/* Distribute */}
            <Link
              to="/distribute"
              className={`relative flex items-center gap-2 px-4 py-2.5 text-sm font-bold transition-colors ${
                activeStage === 'distribute'
                  ? 'text-foreground after:absolute after:bottom-0 after:left-3 after:right-3 after:h-[2px] after:rounded-full after:bg-[var(--brand-gradient)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              Distribute
            </Link>

            {/* Pipeline */}
            <Link
              to="/pipeline"
              className={`relative flex items-center gap-2 px-4 py-2.5 text-sm font-bold transition-colors ${
                activeStage === 'pipeline'
                  ? 'text-foreground after:absolute after:bottom-0 after:left-3 after:right-3 after:h-[2px] after:rounded-full after:bg-[var(--brand-gradient)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              Pipeline
              {pipelineNewCount > 0 && (
                <Badge
                  variant={activeStage === 'pipeline' ? 'default' : 'warning'}
                  className="text-[10px] px-1.5 py-0 shadow-none"
                >
                  {pipelineNewCount}
                </Badge>
              )}
            </Link>
          </div>
        </div>
      </header>

      {/* ── Health warnings ────────────────────────────────────── */}
      {visibleHealthItems.length > 0 && (
        <div className="border-b border-border bg-card px-4 py-2">
          <div className="mx-auto flex max-w-7xl flex-col gap-2">
            {visibleHealthItems.map((item) => (
              <div
                key={item.id}
                className={`flex items-start justify-between gap-4 rounded-md border px-3 py-2 text-sm ${
                  item.tone === 'warn'
                    ? 'border-destructive/40 bg-destructive/10 text-destructive'
                    : 'border-border bg-muted text-muted-foreground'
                }`}
              >
                <span>{item.message}</span>
                <button
                  type="button"
                  className="rounded-md border border-border bg-card px-2 py-0.5 text-xs font-bold text-foreground transition-colors hover:border-primary/60 hover:text-primary"
                  onClick={() => setDismissedHealth((prev) => [...prev, item.id])}
                >
                  Dismiss
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <ToastContainer toasts={notifications} onDismiss={removeNotification} />

      {/* ── Page content — lazy-mounted stages ─────────────────── */}
      <main className="mx-auto max-w-7xl">
        {/* Home — always mounted (landing page) */}
        <div style={{ display: activeStage === 'home' ? 'block' : 'none' }}>
          <HomePage />
        </div>

        {/* Create stage */}
        {visitedStages.has('/create') && (
          <div style={{ display: activeStage === 'create' ? 'block' : 'none' }}>
            <CreatePage />
          </div>
        )}

        {/* Captions stage */}
        {visitedStages.has('/captions') && (
          <div style={{ display: activeStage === 'captions' ? 'block' : 'none' }}>
            <CaptionsStagePage />
          </div>
        )}

        {/* Distribute stage */}
        {visitedStages.has('/distribute') && (
          <div style={{ display: activeStage === 'distribute' ? 'block' : 'none' }}>
            <DistributionPage />
          </div>
        )}

        {/* Pipeline stage — Kanban view OR per-page workspace */}
        {visitedStages.has('/pipeline') && (() => {
          const path = location.pathname;
          const workspaceMatch = path.match(/^\/pipeline\/(.+)$/);
          if (activeStage === 'pipeline' && workspaceMatch) {
            return (
              <div>
                <PipelineWorkspacePage />
              </div>
            );
          }
          return (
            <div style={{ display: activeStage === 'pipeline' ? 'block' : 'none' }}>
              <PipelinePage />
            </div>
          );
        })()}
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
