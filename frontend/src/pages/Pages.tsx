import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiUrl } from '@/lib/api';
import { useWorkflowStore } from '@/stores/workflowStore';

// Pages — the page is the unit. What actually goes on a page: the library
// it draws from (its project), the clips that library holds, and its
// caption bank. The edits are the Lab's own ontology: reassign the
// library, or move a clip between libraries. No policy-speak — content.

interface PageSummary {
  pageId: string;
  handle: string;
  project: string | null;
  group: string | null;
  groupLabel: string | null;
  contentNiche: string | null;
  posterName: string | null;
  status: string | null;
  counts: { clips: number; captions: number; burned: number };
}

interface Clip {
  path: string;
  name: string;
  folder: string;
  created: number | null;
  url: string;
}

interface Caption {
  text: string;
  mood: string | null;
  videoId: string | null;
  batch: string;
  views: number | null;
}

interface PageContent {
  page: PageSummary;
  clips: Clip[];
  captions: Caption[];
  counts: { clips: number; captions: number; burned: number };
  note?: string;
}

interface ProjectInfo {
  name: string;
  video_count: number;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function PagesPage() {
  const { addNotification } = useWorkflowStore();
  const [pages, setPages] = useState<PageSummary[]>([]);
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [content, setContent] = useState<PageContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [groupFilter, setGroupFilter] = useState<string>('all');
  const [movingClip, setMovingClip] = useState<string | null>(null);

  const loadPages = useCallback(async () => {
    const data = await fetchJson<{ pages: PageSummary[] }>(apiUrl('/api/pages/'));
    setPages(data.pages);
    return data.pages;
  }, []);

  const loadContent = useCallback(async (pageId: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchJson<PageContent>(apiUrl(`/api/pages/${encodeURIComponent(pageId)}/content`));
      setContent(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load page content');
      setContent(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPages().catch((e) => setError(e instanceof Error ? e.message : 'Failed to load pages'));
    fetchJson<{ projects: ProjectInfo[] }>(apiUrl('/api/projects'))
      .then((data) => setProjects(data.projects ?? []))
      .catch(() => setProjects([]));
  }, [loadPages]);

  useEffect(() => {
    if (selectedId) loadContent(selectedId);
  }, [selectedId, loadContent]);

  const groups = useMemo(() => {
    const set = new Set<string>();
    for (const page of pages) if (page.group) set.add(page.group);
    return [...set].sort();
  }, [pages]);

  const filtered = useMemo(
    () => (groupFilter === 'all' ? pages : pages.filter((p) => p.group === groupFilter)),
    [pages, groupFilter],
  );

  const assignProject = useCallback(async (project: string | null) => {
    if (!selectedId) return;
    try {
      await fetchJson(apiUrl(`/api/pages/${encodeURIComponent(selectedId)}/project`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project }),
      });
      addNotification('success', project ? `Library switched to ${project}` : 'Library unassigned');
      await loadPages();
      await loadContent(selectedId);
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Assign failed');
    }
  }, [selectedId, addNotification, loadPages, loadContent]);

  const moveClip = useCallback(async (clip: Clip, toProject: string) => {
    if (!selectedId || !toProject) return;
    setMovingClip(clip.path);
    try {
      await fetchJson(apiUrl(`/api/pages/${encodeURIComponent(selectedId)}/clips/move`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: clip.path, toProject }),
      });
      addNotification('success', `Moved ${clip.name} to ${toProject}`);
      await loadPages();
      await loadContent(selectedId);
    } catch (e) {
      addNotification('error', e instanceof Error ? e.message : 'Move failed');
    } finally {
      setMovingClip(null);
    }
  }, [selectedId, addNotification, loadPages, loadContent]);

  const otherProjects = useMemo(
    () => projects.filter((p) => p.name !== content?.page.project),
    [projects, content],
  );

  return (
    <div className="flex h-[calc(100vh-120px)]">
      {/* ── Page rail ──────────────────────────────────────────── */}
      <aside className="w-72 shrink-0 overflow-y-auto border-r border-border p-3">
        <div className="mb-2 flex gap-1">
          <button
            type="button"
            onClick={() => setGroupFilter('all')}
            className={`rounded-md px-2 py-1 text-xs font-bold ${groupFilter === 'all' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}`}
          >
            All
          </button>
          {groups.map((group) => (
            <button
              key={group}
              type="button"
              onClick={() => setGroupFilter(group)}
              className={`rounded-md px-2 py-1 text-xs font-bold ${groupFilter === group ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}`}
            >
              {group}
            </button>
          ))}
        </div>
        {filtered.map((page) => (
          <button
            key={page.pageId}
            type="button"
            onClick={() => setSelectedId(page.pageId)}
            className={`mb-1 block w-full rounded-lg border p-2.5 text-left transition-colors ${
              selectedId === page.pageId
                ? 'border-primary bg-card'
                : 'border-border bg-card/50 hover:border-primary/40'
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-bold text-foreground">{page.handle}</span>
              {page.contentNiche && (
                <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {page.contentNiche}
                </span>
              )}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {page.project ?? 'no library'} · {page.counts.clips} clips
            </div>
          </button>
        ))}
      </aside>

      {/* ── Page content ───────────────────────────────────────── */}
      <main className="flex-1 overflow-y-auto p-4">
        {!selectedId && (
          <p className="mt-8 text-center text-sm text-muted-foreground">Pick a page to see and edit what goes on it.</p>
        )}
        {error && <p className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">{error}</p>}
        {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

        {content && !loading && (
          <>
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <h2 className="text-lg font-heading font-bold">{content.page.handle}</h2>
              {content.page.groupLabel && <span className="text-xs text-muted-foreground">{content.page.groupLabel}</span>}
              {content.page.posterName && <span className="text-xs text-muted-foreground">poster: {content.page.posterName}</span>}
              <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                Library
                <select
                  value={content.page.project ?? ''}
                  onChange={(e) => assignProject(e.target.value || null)}
                  className="rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground"
                >
                  <option value="">none (page goes dark)</option>
                  {[...new Set([content.page.project, ...projects.map((p) => p.name)].filter(Boolean))].map((name) => (
                    <option key={name} value={name as string}>{name}</option>
                  ))}
                </select>
              </label>
              <span className="text-xs text-muted-foreground">
                {content.counts.clips} clips · {content.counts.captions} caption batches · {content.counts.burned} burned
              </span>
            </div>

            {content.note === 'no_project_assigned' && (
              <p className="rounded-md border border-border bg-card p-3 text-sm text-muted-foreground">
                No library assigned — this page draws from nothing. Assign one above.
              </p>
            )}

            {/* ── Clips ── */}
            <h3 className="mb-2 text-sm font-bold uppercase tracking-wide text-muted-foreground">Clips</h3>
            <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              {content.clips.map((clip) => (
                <div key={clip.path} className="overflow-hidden rounded-lg border border-border bg-card">
                  <video src={apiUrl(clip.url)} controls preload="metadata" className="aspect-[9/16] w-full bg-black object-cover" />
                  <div className="p-2">
                    <p className="truncate text-xs text-foreground" title={clip.name}>{clip.name}</p>
                    <div className="mt-1 flex items-center gap-1">
                      <select
                        value=""
                        disabled={movingClip === clip.path || otherProjects.length === 0}
                        onChange={(e) => moveClip(clip, e.target.value)}
                        className="w-full rounded border border-border bg-card px-1 py-1 text-[11px] text-muted-foreground"
                      >
                        <option value="">{movingClip === clip.path ? 'Moving…' : 'Move to…'}</option>
                        {otherProjects.map((p) => (
                          <option key={p.name} value={p.name}>{p.name}</option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              ))}
              {content.clips.length === 0 && content.page.project && (
                <p className="col-span-full text-sm text-muted-foreground">This library is empty.</p>
              )}
            </div>

            {/* ── Captions ── */}
            <h3 className="mb-2 text-sm font-bold uppercase tracking-wide text-muted-foreground">Captions</h3>
            <div className="space-y-1.5 pb-8">
              {content.captions.map((caption, index) => (
                <div key={`${caption.batch}-${index}`} className="rounded-md border border-border bg-card px-3 py-2">
                  <p className="text-sm text-foreground">{caption.text}</p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    {caption.batch}{caption.views != null ? ` · ${caption.views.toLocaleString()} views` : ''}{caption.mood ? ` · ${caption.mood}` : ''}
                  </p>
                </div>
              ))}
              {content.captions.length === 0 && content.page.project && (
                <p className="text-sm text-muted-foreground">No caption bank for this library yet.</p>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
