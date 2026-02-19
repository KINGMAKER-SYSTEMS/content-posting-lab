import { useCallback, useEffect, useMemo, useState } from 'react';
import { BrowserRouter, Link, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { CaptionsPage } from './pages/Captions';
import { BurnPage } from './pages/Burn';
import { GeneratePage } from './pages/Generate';
import { ProjectsPage } from './pages/Projects';
import { ProjectSelector, ToastContainer } from './components';
import { useWorkflowStore } from './stores/workflowStore';
import { type CreateProjectResponse, type HealthResponse, type Project, type ProjectListResponse } from './types/api';

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
    activeProject,
    notifications,
    burnReadyCount,
    videoRunningCount,
    captionJobActive,
    removeNotification,
    setActiveProject,
    addNotification,
  } = useWorkflowStore();

  const [projects, setProjects] = useState<Project[]>([]);
  const [healthItems, setHealthItems] = useState<HealthBannerItem[]>([]);
  const [dismissedHealth, setDismissedHealth] = useState<string[]>([]);

  const tabs = useMemo<NavTab[]>(
    () => [
      { path: '/', label: 'Projects', badge: projects.length > 0 ? projects.length : undefined },
      { path: '/generate', label: 'Generate', badge: videoRunningCount > 0 ? videoRunningCount : undefined },
      { path: '/captions', label: 'Captions', badge: captionJobActive ? 'LIVE' : undefined },
      { path: '/burn', label: 'Burn', badge: burnReadyCount > 0 ? burnReadyCount : undefined },
    ],
    [burnReadyCount, captionJobActive, projects.length, videoRunningCount],
  );

  const visibleHealthItems = useMemo(
    () => healthItems.filter((item) => !dismissedHealth.includes(item.id)),
    [dismissedHealth, healthItems],
  );

  const fetchProjects = useCallback(async () => {
    try {
      const response = await fetch('/api/projects');
      if (!response.ok) {
        throw new Error(`Failed to load projects (${response.status})`);
      }

      const payload = (await response.json()) as ProjectListResponse;
      setProjects(payload.projects ?? []);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to load projects';
      addNotification('error', message);
      setProjects([]);
    }
  }, [addNotification]);

  const fetchHealth = useCallback(async () => {
    try {
      const response = await fetch('/api/health');
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
        const response = await fetch('/api/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: trimmed }),
        });

        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || `Failed to create project (${response.status})`);
        }

        const payload = (await response.json()) as CreateProjectResponse;
        setActiveProject(payload.project);
        addNotification('success', `Project "${payload.project.name}" created`);
        void fetchProjects();
        window.dispatchEvent(new Event('projects:changed'));
        navigate('/');
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to create project';
        addNotification('error', message);
      }
    },
    [addNotification, fetchProjects, navigate, setActiveProject],
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
      if (activeProject) {
        setActiveProject(null);
      }
      return;
    }

    if (!activeProject) {
      setActiveProject(projects[0]);
      return;
    }

    const latestActive = projects.find((project) => project.name === activeProject.name);
    if (!latestActive) {
      setActiveProject(projects[0]);
      return;
    }

    const isOutOfDate =
      latestActive.path !== activeProject.path ||
      latestActive.video_count !== activeProject.video_count ||
      latestActive.caption_count !== activeProject.caption_count ||
      latestActive.burned_count !== activeProject.burned_count;

    if (isOutOfDate) {
      setActiveProject(latestActive);
    }
  }, [activeProject, projects, setActiveProject]);

  return (
    <div className="min-h-screen bg-charcoal text-gray-300 font-sans selection:bg-purple-500/30">
      <header className="border-b border-white/10 bg-black/30 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <div className="h-2.5 w-2.5 rounded-full bg-purple-400 shadow-[0_0_14px_rgba(192,132,252,0.9)]" />
            <Link to="/" className="text-sm font-semibold tracking-wide text-white hover:text-purple-300">
              Content Posting Lab
            </Link>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <ProjectSelector
              className="w-full min-w-[260px] sm:w-[320px]"
              projects={projects}
              activeProject={activeProject}
              onSelect={setActiveProject}
              onCreate={createProject}
            />
            <button type="button" onClick={handleQuickCreate} className="btn btn-secondary text-sm">
              New Project
            </button>
          </div>
        </div>
      </header>

      {visibleHealthItems.length > 0 ? (
        <div className="border-b border-white/10 bg-black/40 px-4 py-2">
          <div className="mx-auto flex max-w-7xl flex-col gap-2">
            {visibleHealthItems.map((item) => (
              <div
                key={item.id}
                className={`flex items-start justify-between gap-4 rounded-lg border px-3 py-2 text-sm ${
                  item.tone === 'warn'
                    ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
                    : 'border-sky-500/30 bg-sky-500/10 text-sky-200'
                }`}
              >
                <span>{item.message}</span>
                <button
                  type="button"
                  className="rounded px-2 py-1 text-xs text-white/80 hover:bg-white/10"
                  onClick={() => setDismissedHealth((prev) => [...prev, item.id])}
                >
                  Dismiss
                </button>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <nav className="sticky top-0 z-40 border-b border-white/10 bg-black/20 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl gap-1 px-4">
          {tabs.map((tab) => {
            const isActive = location.pathname === tab.path;
            return (
              <Link
                key={tab.path}
                to={tab.path}
                className={`relative flex items-center gap-2 px-4 py-4 text-sm font-medium transition-all ${
                  isActive
                    ? 'text-purple-400 after:absolute after:bottom-0 after:left-0 after:right-0 after:h-0.5 after:bg-purple-500'
                    : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
                }`}
              >
                {tab.label}
                {tab.badge !== undefined ? (
                  <span
                    className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold ${
                      tab.badge === 'LIVE'
                        ? 'bg-emerald-500/20 text-emerald-200'
                        : isActive
                          ? 'bg-purple-500/20 text-purple-200'
                          : 'bg-white/10 text-gray-300'
                    }`}
                  >
                    {tab.badge}
                  </span>
                ) : null}
              </Link>
            );
          })}
        </div>
      </nav>

      <ToastContainer toasts={notifications} onDismiss={removeNotification} />

      <main className="mx-auto max-w-7xl">
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/generate" element={<GeneratePage />} />
          <Route path="/captions" element={<CaptionsPage />} />
          <Route path="/burn" element={<BurnPage />} />
        </Routes>
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
