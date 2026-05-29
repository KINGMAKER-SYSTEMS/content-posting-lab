/**
 * Telegram Mini App — the lightweight client posters open from Telegram.
 *
 * Renders standalone (no admin shell). It authenticates with the Telegram
 * WebApp `initData` string, shows the poster's pages + rendered videos, and
 * lets them file a content request that the attached agent picks up.
 *
 * Local dev without Telegram: append ?dev=<poster_id> to the URL and run the
 * backend with MINIAPP_DEV_AUTH=1.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiUrl, staticUrl } from '../lib/api';
import { LazyVideo } from '../components/LazyVideo';

// Minimal shape of the Telegram WebApp SDK surface we touch.
interface TelegramWebApp {
  initData: string;
  ready: () => void;
  expand: () => void;
  initDataUnsafe?: { user?: { id?: number; username?: string } };
}
declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

interface PageSummary {
  integration_id: string;
  name: string | null;
  project: string | null;
  provider: string | null;
  status: string | null;
}
interface Me {
  poster_id: string;
  name: string | null;
  page_count: number;
  pages: PageSummary[];
}
interface VideoItem {
  page_id: string;
  page_name: string | null;
  project: string | null;
  path: string;
  name: string;
  url: string;
  created: number | null;
}
interface ContentRequest {
  id: string;
  text: string;
  page_name: string | null;
  status: string;
  created_at: string;
  agent_note: string | null;
}

/** Auth headers for every Mini App API call. */
function authHeaders(): Record<string, string> {
  const tg = window.Telegram?.WebApp;
  if (tg?.initData) return { 'X-Telegram-Init-Data': tg.initData };
  const dev = new URLSearchParams(window.location.search).get('dev');
  if (dev) return { 'X-Dev-Poster-Id': dev };
  return {};
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(apiUrl(path), { headers: authHeaders() });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.detail || b.error || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function MiniApp() {
  const [me, setMe] = useState<Me | null>(null);
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [requests, setRequests] = useState<ContentRequest[]>([]);
  const [activePage, setActivePage] = useState<string>('all');
  const [tab, setTab] = useState<'content' | 'requests'>('content');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Request form
  const [reqText, setReqText] = useState('');
  const [reqPage, setReqPage] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    if (tg) {
      tg.ready();
      tg.expand();
    }
  }, []);

  const loadRequests = useCallback(async () => {
    try {
      const r = await apiGet<{ requests: ContentRequest[] }>('/api/miniapp/requests');
      setRequests(r.requests);
    } catch {
      /* non-fatal */
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [meData, vidData] = await Promise.all([
          apiGet<Me>('/api/miniapp/me'),
          apiGet<{ videos: VideoItem[] }>('/api/miniapp/videos'),
        ]);
        setMe(meData);
        setVideos(vidData.videos);
        await loadRequests();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load');
      } finally {
        setLoading(false);
      }
    })();
  }, [loadRequests]);

  const shownVideos = useMemo(
    () => (activePage === 'all' ? videos : videos.filter((v) => v.page_id === activePage)),
    [videos, activePage],
  );

  const submitRequest = async () => {
    if (!reqText.trim()) return;
    setSubmitting(true);
    try {
      await apiPost('/api/miniapp/requests', {
        text: reqText.trim(),
        page_id: reqPage || null,
      });
      setReqText('');
      setReqPage('');
      await loadRequests();
      setTab('requests');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to send request');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-2 bg-background px-6 text-center">
        <p className="text-sm font-semibold text-destructive">{error}</p>
        <p className="text-xs text-muted-foreground">
          Open this from your Telegram client, or ask an admin to link your Telegram account.
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-border bg-card/95 px-4 py-3 backdrop-blur">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-bold">{me?.name ?? 'Poster'}</p>
            <p className="text-xs text-muted-foreground">{me?.page_count ?? 0} pages</p>
          </div>
          <div className="flex gap-1 rounded-lg bg-muted p-0.5">
            <button
              className={`rounded-md px-3 py-1 text-xs font-semibold ${tab === 'content' ? 'bg-card shadow' : 'text-muted-foreground'}`}
              onClick={() => setTab('content')}
            >
              Content
            </button>
            <button
              className={`rounded-md px-3 py-1 text-xs font-semibold ${tab === 'requests' ? 'bg-card shadow' : 'text-muted-foreground'}`}
              onClick={() => setTab('requests')}
            >
              Requests
            </button>
          </div>
        </div>
      </header>

      {tab === 'content' ? (
        <>
          {/* Page filter */}
          <div className="flex gap-2 overflow-x-auto px-4 py-3">
            <FilterChip label="All" active={activePage === 'all'} onClick={() => setActivePage('all')} />
            {me?.pages.map((p) => (
              <FilterChip
                key={p.integration_id}
                label={p.name ?? p.integration_id}
                active={activePage === p.integration_id}
                onClick={() => setActivePage(p.integration_id)}
              />
            ))}
          </div>

          {shownVideos.length === 0 ? (
            <p className="px-4 py-10 text-center text-sm text-muted-foreground">
              No rendered videos yet for these pages.
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-2 px-4 pb-24 sm:grid-cols-3">
              {shownVideos.map((v) => (
                <div key={`${v.page_id}:${v.path}`} className="overflow-hidden rounded-lg border border-border bg-card">
                  <div className="aspect-[9/16] bg-muted">
                    <LazyVideo src={staticUrl(v.url)} className="h-full w-full object-cover" />
                  </div>
                  <p className="truncate px-2 py-1 text-[10px] text-muted-foreground" title={v.page_name ?? ''}>
                    {v.page_name ?? v.project}
                  </p>
                </div>
              ))}
            </div>
          )}
        </>
      ) : (
        <div className="px-4 py-3 pb-24">
          {/* Request form */}
          <div className="rounded-lg border border-border bg-card p-3">
            <p className="mb-2 text-xs font-semibold">Request more content</p>
            <textarea
              className="w-full rounded-md border border-border bg-background p-2 text-sm"
              rows={3}
              placeholder="e.g. Need 5 more lyric videos for this week's drop"
              value={reqText}
              onChange={(e) => setReqText(e.target.value)}
            />
            <select
              className="mt-2 w-full rounded-md border border-border bg-background p-2 text-sm"
              value={reqPage}
              onChange={(e) => setReqPage(e.target.value)}
            >
              <option value="">All my pages</option>
              {me?.pages.map((p) => (
                <option key={p.integration_id} value={p.integration_id}>
                  {p.name ?? p.integration_id}
                </option>
              ))}
            </select>
            <button
              className="mt-2 w-full rounded-md bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-50"
              disabled={!reqText.trim() || submitting}
              onClick={submitRequest}
            >
              {submitting ? 'Sending…' : 'Send request'}
            </button>
          </div>

          {/* Request history */}
          <div className="mt-4 space-y-2">
            {requests.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">No requests yet.</p>
            ) : (
              requests.map((r) => (
                <div key={r.id} className="rounded-lg border border-border bg-card p-3">
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm">{r.text}</p>
                    <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[10px] font-semibold uppercase">
                      {r.status}
                    </span>
                  </div>
                  {r.page_name && (
                    <p className="mt-1 text-[10px] text-muted-foreground">{r.page_name}</p>
                  )}
                  {r.agent_note && (
                    <p className="mt-1 text-[10px] text-primary">↳ {r.agent_note}</p>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function FilterChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
        active ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground'
      }`}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
