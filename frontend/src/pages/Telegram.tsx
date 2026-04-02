import { useCallback, useEffect, useState } from 'react';
import { apiUrl } from '@/lib/api';
import { useWorkflowStore } from '@/stores/workflowStore';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type {
  TelegramStatus,
  TelegramPoster,
  TelegramSound,
  TelegramBatchResult,
  RosterPage,
} from '@/types/api';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

async function fetchOk(url: string, init?: RequestInit): Promise<void> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
}

const US_TIMEZONES = [
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
] as const;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function TelegramPage() {
  const { activeProjectName, addNotification } = useWorkflowStore();

  // Global status fetched on mount and after mutations
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [posters, setPosters] = useState<TelegramPoster[]>([]);
  const [sounds, setSounds] = useState<TelegramSound[]>([]);
  const [rosterPages, setRosterPages] = useState<RosterPage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Section-local form state
  const [tokenInput, setTokenInput] = useState('');
  const [chatIdInput, setChatIdInput] = useState('');
  const [syncResult, setSyncResult] = useState<{ created: number; existing: number } | null>(null);

  const [newPosterName, setNewPosterName] = useState('');
  const [newPosterChatId, setNewPosterChatId] = useState('');
  const [posterSelectedPages, setPosterSelectedPages] = useState<Record<string, Set<string>>>({});
  const [posterAssignOpen, setPosterAssignOpen] = useState<Record<string, boolean>>({});
  const [assignLoading, setAssignLoading] = useState<string | null>(null);

  const [newSoundUrl, setNewSoundUrl] = useState('');
  const [newSoundLabel, setNewSoundLabel] = useState('');

  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [forwardTime, setForwardTime] = useState('09:00');
  const [timezone, setTimezone] = useState<string>('America/New_York');
  const [batchResult, setBatchResult] = useState<TelegramBatchResult | null>(null);
  const [batchRunning, setBatchRunning] = useState(false);

  const [sendPage, setSendPage] = useState('');
  const [sendFilePath, setSendFilePath] = useState('');
  const [sendCaption, setSendCaption] = useState('');
  const [sendResult, setSendResult] = useState<string | null>(null);

  // -----------------------------------------------------------------------
  // Data fetching
  // -----------------------------------------------------------------------

  const refresh = useCallback(async () => {
    try {
      const [statusData, postersData, soundsData, rosterData] = await Promise.all([
        fetchJson<TelegramStatus>(apiUrl('/api/telegram/status')),
        fetchJson<TelegramPoster[]>(apiUrl('/api/telegram/posters')).catch(() => [] as TelegramPoster[]),
        fetchJson<TelegramSound[]>(apiUrl('/api/telegram/sounds')).catch(() => [] as TelegramSound[]),
        fetchJson<{ pages: RosterPage[] }>(apiUrl('/api/roster/')).catch(() => ({ pages: [] as RosterPage[] })),
      ]);
      setStatus(statusData);
      setPosters(postersData);
      setSounds(soundsData);
      setRosterPages(rosterData.pages);

      // Sync schedule form state from server
      setScheduleEnabled(statusData.schedule.enabled);
      setForwardTime(statusData.schedule.forward_time || '09:00');
      setTimezone(statusData.schedule.timezone || 'America/New_York');

      setError(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load Telegram status';
      setError(msg);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  // -----------------------------------------------------------------------
  // Bot Configuration handlers
  // -----------------------------------------------------------------------

  const handleSaveToken = useCallback(async () => {
    if (!tokenInput.trim()) return;
    try {
      await fetchOk(apiUrl('/api/telegram/bot-token'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: tokenInput.trim() }),
      });
      setTokenInput('');
      addNotification('success', 'Bot token saved');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to save token');
    }
  }, [tokenInput, addNotification, refresh]);

  const handleClearToken = useCallback(async () => {
    try {
      await fetchOk(apiUrl('/api/telegram/bot-token'), { method: 'DELETE' });
      addNotification('success', 'Bot token cleared');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to clear token');
    }
  }, [addNotification, refresh]);

  // -----------------------------------------------------------------------
  // Staging Group handlers
  // -----------------------------------------------------------------------

  const handleSetGroup = useCallback(async () => {
    const id = parseInt(chatIdInput, 10);
    if (isNaN(id)) return;
    try {
      await fetchOk(apiUrl('/api/telegram/staging-group'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: id }),
      });
      setChatIdInput('');
      addNotification('success', 'Storage group connected');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to connect storage group');
    }
  }, [chatIdInput, addNotification, refresh]);

  const handleSyncTopics = useCallback(async () => {
    try {
      const result = await fetchJson<{ created: number; existing: number }>(
        apiUrl('/api/telegram/staging-group/sync-topics'),
        { method: 'POST' },
      );
      setSyncResult(result);
      addNotification('success', `Page folders: ${result.created} created, ${result.existing} existing`);
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to set up page folders');
    }
  }, [addNotification, refresh]);

  const handleForwardPending = useCallback(async (integrationId: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/forward/${encodeURIComponent(integrationId)}`), { method: 'POST' });
      addNotification('success', 'Forwarded pending content');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to forward');
    }
  }, [addNotification, refresh]);

  // -----------------------------------------------------------------------
  // Poster handlers
  // -----------------------------------------------------------------------

  const handleAddPoster = useCallback(async () => {
    const name = newPosterName.trim();
    const chatId = parseInt(newPosterChatId, 10);
    if (!name || isNaN(chatId)) return;
    try {
      await fetchOk(apiUrl('/api/telegram/posters'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, chat_id: chatId }),
      });
      setNewPosterName('');
      setNewPosterChatId('');
      addNotification('success', `Poster "${name}" added`);
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to add poster');
    }
  }, [newPosterName, newPosterChatId, addNotification, refresh]);

  const handleRemovePoster = useCallback(async (posterId: string) => {
    if (!confirm(`Remove poster "${posterId}"? This cannot be undone.`)) return;
    try {
      await fetchOk(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}`), { method: 'DELETE' });
      addNotification('success', 'Poster removed');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to remove poster');
    }
  }, [addNotification, refresh]);

  const handleSyncPosterTopics = useCallback(async (posterId: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}/sync-topics`), { method: 'POST' });
      addNotification('success', 'Page folders created');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to set up page folders');
    }
  }, [addNotification, refresh]);

  const handleUnassignPage = useCallback(async (posterId: string, pageId: string, pageName: string) => {
    try {
      await fetchOk(
        apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}/pages/${encodeURIComponent(pageId)}`),
        { method: 'DELETE' },
      );
      addNotification('success', `${pageName} removed`);
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to remove page');
    }
  }, [addNotification, refresh]);

  const togglePageSelection = useCallback((posterId: string, pageId: string) => {
    setPosterSelectedPages((prev) => {
      const current = new Set(prev[posterId] ?? []);
      if (current.has(pageId)) {
        current.delete(pageId);
      } else {
        current.add(pageId);
      }
      return { ...prev, [posterId]: current };
    });
  }, []);

  const selectAllPages = useCallback((posterId: string, pageIds: string[]) => {
    setPosterSelectedPages((prev) => ({ ...prev, [posterId]: new Set(pageIds) }));
  }, []);

  const clearPageSelection = useCallback((posterId: string) => {
    setPosterSelectedPages((prev) => ({ ...prev, [posterId]: new Set() }));
  }, []);

  const handleAssignPages = useCallback(async (posterId: string) => {
    const selected = posterSelectedPages[posterId];
    if (!selected || selected.size === 0) return;
    const pageIds = Array.from(selected);
    setAssignLoading(posterId);
    try {
      // Build name map so backend can name Telegram topics properly
      const pageNames: Record<string, string> = {};
      for (const pid of pageIds) {
        const page = rosterPages.find((p) => p.integration_id === pid);
        if (page?.name) pageNames[pid] = page.name;
      }
      const res = await fetch(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}/pages`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page_ids: pageIds, page_names: pageNames }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data?.detail || JSON.stringify(data) || `Request failed (${res.status})`);
      }
      setPosterSelectedPages((prev) => ({ ...prev, [posterId]: new Set() }));
      setPosterAssignOpen((prev) => ({ ...prev, [posterId]: false }));
      addNotification('success', `${pageIds.length} page${pageIds.length > 1 ? 's' : ''} assigned — click "Set Up Folders" to create topics`);
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to assign pages');
    } finally {
      setAssignLoading(null);
    }
  }, [posterSelectedPages, addNotification, refresh]);

  // -----------------------------------------------------------------------
  // Sound handlers
  // -----------------------------------------------------------------------

  const handleAddSound = useCallback(async () => {
    const url = newSoundUrl.trim();
    const label = newSoundLabel.trim();
    if (!url || !label) return;
    try {
      await fetchOk(apiUrl('/api/telegram/sounds'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, label }),
      });
      setNewSoundUrl('');
      setNewSoundLabel('');
      addNotification('success', 'Sound added');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to add sound');
    }
  }, [newSoundUrl, newSoundLabel, addNotification, refresh]);

  const handleToggleSound = useCallback(async (soundId: string, active: boolean) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/sounds/${encodeURIComponent(soundId)}`), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active }),
      });
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to toggle sound');
    }
  }, [addNotification, refresh]);

  const handleDeleteSound = useCallback(async (soundId: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/sounds/${encodeURIComponent(soundId)}`), { method: 'DELETE' });
      addNotification('success', 'Sound removed');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to delete sound');
    }
  }, [addNotification, refresh]);

  // -----------------------------------------------------------------------
  // Schedule handlers
  // -----------------------------------------------------------------------

  const handleSaveSchedule = useCallback(async () => {
    try {
      await fetchOk(apiUrl('/api/telegram/schedule'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: scheduleEnabled,
          forward_time: forwardTime,
          timezone,
        }),
      });
      addNotification('success', 'Schedule updated');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to update schedule');
    }
  }, [scheduleEnabled, forwardTime, timezone, addNotification, refresh]);

  const handleRunBatch = useCallback(async () => {
    setBatchRunning(true);
    setBatchResult(null);
    try {
      const result = await fetchJson<TelegramBatchResult>(
        apiUrl('/api/telegram/batch/run'),
        { method: 'POST' },
      );
      setBatchResult(result);
      addNotification('success', `Batch complete: ${result.videos_forwarded} videos forwarded`);
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Batch run failed');
    } finally {
      setBatchRunning(false);
    }
  }, [addNotification, refresh]);

  // -----------------------------------------------------------------------
  // Send Content handlers
  // -----------------------------------------------------------------------

  const handleSendToStaging = useCallback(async () => {
    if (!sendPage || !sendFilePath.trim()) return;
    try {
      await fetchOk(apiUrl('/api/telegram/send'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          integration_id: sendPage,
          file_path: sendFilePath.trim(),
          caption: sendCaption.trim() || undefined,
          project: activeProjectName || undefined,
        }),
      });
      setSendResult('Sent successfully');
      setSendFilePath('');
      setSendCaption('');
      addNotification('success', 'Content sent to storage');
      await refresh();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to send');
    }
  }, [sendPage, sendFilePath, sendCaption, activeProjectName, addNotification, refresh]);

  // -----------------------------------------------------------------------
  // Derived data
  // -----------------------------------------------------------------------

  const stagingGroup = status?.staging_group ?? null;
  const topics = stagingGroup?.topics ?? {};
  const topicEntries = Object.entries(topics);
  const schedule = status?.schedule ?? null;

  // Pages that have staging topics (for the Send section dropdown)
  const pagesWithTopics = rosterPages.filter((p) => topics[p.integration_id]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
      </div>
    );
  }

  if (error && !status) {
    return (
      <div className="mx-auto max-w-4xl p-8">
        <Card>
          <CardContent>
            <p className="text-sm text-destructive">{error}</p>
            <Button variant="outline" className="mt-4" onClick={() => { setLoading(true); refresh().finally(() => setLoading(false)); }}>
              Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-8">
      {/* Page Header */}
      <div>
        <h1 className="mb-1 text-3xl font-heading text-foreground">Telegram Distribution</h1>
        <p className="text-sm text-muted-foreground">
          Stage content, assign it to pages, and distribute to your posters on a daily schedule.
        </p>
      </div>

      {/* Section 1: Bot Configuration */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-3">
            Bot Configuration
            {status?.bot_configured ? (
              status.bot_running ? (
                <Badge variant="success">Connected</Badge>
              ) : (
                <Badge variant="error">Error</Badge>
              )
            ) : (
              <Badge variant="secondary">Not Configured</Badge>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {status?.bot_username && (
            <p className="mb-3 text-sm text-muted-foreground">
              Bot: <span className="font-mono font-bold text-foreground">@{status.bot_username}</span>
            </p>
          )}
          <div className="flex items-center gap-2">
            <Input
              type="password"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder="Paste bot token here"
              className="flex-1"
            />
            <Button onClick={handleSaveToken} disabled={!tokenInput.trim()}>
              Save Token
            </Button>
            {status?.bot_configured && (
              <Button variant="destructive" onClick={handleClearToken}>
                Clear Token
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Section 2: Staging Group */}
      <Card>
        <CardHeader>
          <CardTitle>Content Storage</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Set group input */}
          <div className="flex items-center gap-2">
            <Input
              type="number"
              value={chatIdInput}
              onChange={(e) => setChatIdInput(e.target.value)}
              placeholder="Group ID"
              className="flex-1"
            />
            <Button onClick={handleSetGroup} disabled={!chatIdInput.trim()}>
              Set Group
            </Button>
          </div>

          {/* Group info */}
          {stagingGroup?.chat_id && (
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-foreground">
                  {stagingGroup.name ?? `Chat ${stagingGroup.chat_id}`}
                </span>
                <Badge variant="success">Connected</Badge>
                <Badge variant="success">Bot Active</Badge>
              </div>

              {/* Inventory Table */}
              {topicEntries.length > 0 && (
                <div className="overflow-hidden rounded-[var(--border-radius)] border-2 border-border">
                  <table className="w-full divide-y divide-border">
                    <thead className="bg-muted">
                      <tr>
                        <th className="px-3 py-2 text-left text-xs font-bold uppercase tracking-wider text-muted-foreground">Page</th>
                        <th className="px-3 py-2 text-left text-xs font-bold uppercase tracking-wider text-muted-foreground">Folder Status</th>
                        <th className="px-3 py-2 text-right text-xs font-bold uppercase tracking-wider text-muted-foreground">Staged</th>
                        <th className="px-3 py-2 text-right text-xs font-bold uppercase tracking-wider text-muted-foreground">Pending</th>
                        <th className="px-3 py-2 text-right text-xs font-bold uppercase tracking-wider text-muted-foreground">Forwarded</th>
                        <th className="px-3 py-2 text-right text-xs font-bold uppercase tracking-wider text-muted-foreground">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {topicEntries.map(([integrationId, topic]) => {
                        const page = rosterPages.find((p) => p.integration_id === integrationId);
                        return (
                          <tr key={integrationId}>
                            <td className="px-3 py-2 text-sm font-medium text-foreground">
                              {page?.name ?? integrationId}
                            </td>
                            <td className="px-3 py-2 text-sm">
                              {topic.topic_id ? (
                                <Badge variant="success">Created</Badge>
                              ) : (
                                <Badge variant="secondary">Missing</Badge>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right text-sm tabular-nums text-foreground">
                              {topic.inventory_total ?? 0}
                            </td>
                            <td className="px-3 py-2 text-right text-sm tabular-nums text-foreground">
                              {topic.inventory_pending ?? 0}
                            </td>
                            <td className="px-3 py-2 text-right text-sm tabular-nums text-foreground">
                              {topic.inventory_forwarded ?? 0}
                            </td>
                            <td className="px-3 py-2 text-right">
                              <Button
                                size="xs"
                                variant="outline"
                                onClick={() => handleForwardPending(integrationId)}
                                disabled={(topic.inventory_pending ?? 0) === 0}
                              >
                                Forward Pending
                              </Button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Set Up Folders */}
              <div className="flex items-center gap-3">
                <Button variant="outline" onClick={handleSyncTopics}>
                  Set Up Folders
                </Button>
                {syncResult && (
                  <span className="text-sm text-muted-foreground">
                    Created {syncResult.created}, Existing {syncResult.existing}
                  </span>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Section 3: Posters */}
      <Card>
        <CardHeader>
          <CardTitle>Posters</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Poster grid */}
          {posters.length > 0 && (
            <div className="space-y-4">
              {posters.map((poster) => {
                const assignedPages = rosterPages.filter((p) => poster.page_ids.includes(p.integration_id));
                const unassignedPages = rosterPages.filter(
                  (p) => !posters.some((pr) => pr.page_ids.includes(p.integration_id)),
                );
                const isAssignOpen = posterAssignOpen[poster.poster_id] ?? false;
                const selected = posterSelectedPages[poster.poster_id] ?? new Set<string>();

                return (
                  <div
                    key={poster.poster_id}
                    className="rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[4px_4px_0_0_var(--border)]"
                  >
                    {/* Poster header */}
                    <div className="flex items-start justify-between border-b border-border p-4">
                      <div>
                        <p className="text-lg font-heading text-foreground">{poster.name}</p>
                        <p className="text-xs text-muted-foreground">
                          {assignedPages.length} page{assignedPages.length !== 1 ? 's' : ''} assigned
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="xs"
                          onClick={() => handleSyncPosterTopics(poster.poster_id)}
                        >
                          Set Up Folders
                        </Button>
                        <Button
                          variant="destructive"
                          size="xs"
                          onClick={() => handleRemovePoster(poster.poster_id)}
                        >
                          Remove
                        </Button>
                      </div>
                    </div>

                    {/* Assigned pages as cards */}
                    {assignedPages.length > 0 && (
                      <div className="border-b border-border p-4">
                        <p className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">
                          Assigned Pages
                        </p>
                        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
                          {assignedPages.map((p) => (
                            <div
                              key={p.integration_id}
                              className="group relative flex flex-col items-center gap-1.5 rounded-[var(--border-radius)] border-2 border-primary/30 bg-primary/5 p-2 text-center"
                            >
                              {/* Remove button — top right, visible on hover */}
                              <button
                                type="button"
                                onClick={() => handleUnassignPage(poster.poster_id, p.integration_id, p.name)}
                                className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full border border-border bg-destructive text-[10px] font-bold text-white opacity-0 transition-opacity group-hover:opacity-100"
                                title={`Remove ${p.name}`}
                              >
                                ×
                              </button>
                              {p.picture ? (
                                <img
                                  src={p.picture}
                                  alt={p.name}
                                  className="h-8 w-8 rounded-full border border-border object-cover"
                                />
                              ) : (
                                <div className="flex h-8 w-8 items-center justify-center rounded-full border border-border bg-muted text-xs font-bold text-muted-foreground">
                                  {p.name.charAt(0).toUpperCase()}
                                </div>
                              )}
                              <span className="text-[11px] font-medium leading-tight text-foreground">
                                {p.name}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Assign pages panel */}
                    <div className="p-4">
                      {!isAssignOpen ? (
                        <Button
                          variant="outline"
                          onClick={() => {
                            clearPageSelection(poster.poster_id);
                            setPosterAssignOpen((prev) => ({ ...prev, [poster.poster_id]: true }));
                          }}
                          disabled={unassignedPages.length === 0}
                        >
                          {unassignedPages.length > 0
                            ? `+ Assign Pages (${unassignedPages.length} available)`
                            : 'All pages assigned'}
                        </Button>
                      ) : (
                        <div className="space-y-3">
                          <div className="flex items-center justify-between">
                            <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
                              Select pages to assign
                            </p>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                onClick={() => selectAllPages(poster.poster_id, unassignedPages.map((p) => p.integration_id))}
                                className="text-xs font-medium text-primary hover:underline"
                              >
                                Select All
                              </button>
                              <span className="text-xs text-muted-foreground">|</span>
                              <button
                                type="button"
                                onClick={() => clearPageSelection(poster.poster_id)}
                                className="text-xs font-medium text-muted-foreground hover:underline"
                              >
                                Clear
                              </button>
                            </div>
                          </div>

                          {/* Page selection grid */}
                          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
                            {unassignedPages.map((p) => {
                              const isSelected = selected.has(p.integration_id);
                              return (
                                <button
                                  key={p.integration_id}
                                  type="button"
                                  onClick={() => togglePageSelection(poster.poster_id, p.integration_id)}
                                  className={`relative flex flex-col items-center gap-1.5 rounded-[var(--border-radius)] border-2 p-2 text-center transition-all ${
                                    isSelected
                                      ? 'border-primary bg-primary/10 shadow-[2px_2px_0_0_var(--border)]'
                                      : 'border-border bg-card hover:border-muted-foreground hover:bg-muted/50'
                                  }`}
                                >
                                  {/* Checkmark */}
                                  <div
                                    className={`absolute right-1 top-1 flex h-4 w-4 items-center justify-center rounded-sm border transition-all ${
                                      isSelected
                                        ? 'border-primary bg-primary text-white'
                                        : 'border-border bg-card'
                                    }`}
                                  >
                                    {isSelected && (
                                      <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none">
                                        <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                                      </svg>
                                    )}
                                  </div>

                                  {p.picture ? (
                                    <img
                                      src={p.picture}
                                      alt={p.name}
                                      className="h-8 w-8 rounded-full border border-border object-cover"
                                    />
                                  ) : (
                                    <div className="flex h-8 w-8 items-center justify-center rounded-full border border-border bg-muted text-xs font-bold text-muted-foreground">
                                      {p.name.charAt(0).toUpperCase()}
                                    </div>
                                  )}
                                  <span className="text-[11px] font-medium leading-tight text-foreground">
                                    {p.name}
                                  </span>
                                </button>
                              );
                            })}
                          </div>

                          {/* Action buttons */}
                          <div className="flex items-center gap-2">
                            <Button
                              onClick={() => handleAssignPages(poster.poster_id)}
                              disabled={selected.size === 0 || assignLoading === poster.poster_id}
                            >
                              {assignLoading === poster.poster_id
                                ? 'Assigning & creating folders...'
                                : `Assign ${selected.size > 0 ? `${selected.size} Page${selected.size > 1 ? 's' : ''}` : 'Pages'}`
                              }
                            </Button>
                            <Button
                              variant="outline"
                              onClick={() => {
                                clearPageSelection(poster.poster_id);
                                setPosterAssignOpen((prev) => ({ ...prev, [poster.poster_id]: false }));
                              }}
                            >
                              Cancel
                            </Button>
                            {selected.size > 0 && (
                              <span className="text-xs text-muted-foreground">
                                {selected.size} selected — folders will be created automatically
                              </span>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {posters.length === 0 && (
            <p className="text-sm text-muted-foreground">No posters configured yet.</p>
          )}

          {/* Add poster form */}
          <div className="border-t border-border pt-4">
            <p className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">Add Poster</p>
            <div className="flex items-center gap-2">
              <Input
                value={newPosterName}
                onChange={(e) => setNewPosterName(e.target.value)}
                placeholder="Name"
                className="flex-1"
              />
              <Input
                type="number"
                value={newPosterChatId}
                onChange={(e) => setNewPosterChatId(e.target.value)}
                placeholder="Group ID"
                className="w-40"
              />
              <Button
                onClick={handleAddPoster}
                disabled={!newPosterName.trim() || !newPosterChatId.trim()}
              >
                Add Poster
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Section 4: Sounds */}
      <Card>
        <CardHeader>
          <CardTitle>Active Sounds</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {sounds.length > 0 ? (
            <div className="space-y-2">
              {sounds.map((sound) => (
                <div
                  key={sound.id}
                  className="flex items-center gap-3 rounded-[var(--border-radius)] border-2 border-border p-3"
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-foreground">{sound.label}</p>
                    <a
                      href={sound.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="block truncate text-xs text-primary underline-offset-2 hover:underline"
                    >
                      {sound.url}
                    </a>
                  </div>

                  {/* Active toggle */}
                  <button
                    type="button"
                    role="switch"
                    aria-checked={sound.active}
                    onClick={() => handleToggleSound(sound.id, !sound.active)}
                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-border transition-colors ${
                      sound.active ? 'bg-primary' : 'bg-muted'
                    }`}
                  >
                    <span
                      className={`pointer-events-none inline-block h-4 w-4 translate-y-[1px] rounded-full bg-white shadow-sm ring-0 transition-transform ${
                        sound.active ? 'translate-x-5' : 'translate-x-0.5'
                      }`}
                    />
                  </button>

                  <Button
                    variant="destructive"
                    size="xs"
                    onClick={() => handleDeleteSound(sound.id)}
                  >
                    Delete
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No sounds configured.</p>
          )}

          {/* Add sound form */}
          <div className="border-t border-border pt-4">
            <p className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">Add Sound</p>
            <div className="flex items-center gap-2">
              <Input
                value={newSoundUrl}
                onChange={(e) => setNewSoundUrl(e.target.value)}
                placeholder="Sound URL"
                className="flex-1"
              />
              <Input
                value={newSoundLabel}
                onChange={(e) => setNewSoundLabel(e.target.value)}
                placeholder="Label"
                className="w-40"
              />
              <Button
                onClick={handleAddSound}
                disabled={!newSoundUrl.trim() || !newSoundLabel.trim()}
              >
                Add
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Section 5: Schedule & Distribution */}
      <Card>
        <CardHeader>
          <CardTitle>Schedule</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-4">
            {/* Enabled toggle */}
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium text-foreground" htmlFor="schedule-toggle">
                Enabled
              </label>
              <button
                id="schedule-toggle"
                type="button"
                role="switch"
                aria-checked={scheduleEnabled}
                onClick={() => setScheduleEnabled((prev) => !prev)}
                className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-border transition-colors ${
                  scheduleEnabled ? 'bg-primary' : 'bg-muted'
                }`}
              >
                <span
                  className={`pointer-events-none inline-block h-4 w-4 translate-y-[1px] rounded-full bg-white shadow-sm ring-0 transition-transform ${
                    scheduleEnabled ? 'translate-x-5' : 'translate-x-0.5'
                  }`}
                />
              </button>
            </div>

            {/* Forward time */}
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium text-foreground" htmlFor="forward-time">
                Forward at
              </label>
              <Input
                id="forward-time"
                type="time"
                value={forwardTime}
                onChange={(e) => setForwardTime(e.target.value)}
                className="w-32"
              />
            </div>

            {/* Timezone */}
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium text-foreground" htmlFor="timezone-select">
                Timezone
              </label>
              <select
                id="timezone-select"
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                className="h-9 rounded-md border border-border bg-card px-3 text-sm text-foreground"
              >
                {US_TIMEZONES.map((tz) => (
                  <option key={tz} value={tz}>
                    {tz.replace('America/', '').replace('_', ' ')}
                  </option>
                ))}
              </select>
            </div>

            <Button variant="outline" onClick={handleSaveSchedule}>
              Save Schedule
            </Button>
          </div>

          {/* Last run */}
          <p className="text-xs text-muted-foreground">
            Last run:{' '}
            {schedule?.last_run
              ? new Date(schedule.last_run).toLocaleString()
              : 'Never'}
          </p>

          {/* Run batch */}
          <div className="flex items-center gap-3 border-t border-border pt-4">
            <Button onClick={handleRunBatch} disabled={batchRunning}>
              {batchRunning ? 'Running...' : 'Run Batch Now'}
            </Button>
            {batchResult && (
              <span className="text-sm text-muted-foreground">
                Notified {batchResult.posters_notified} posters, forwarded {batchResult.videos_forwarded} videos, sent {batchResult.sounds_sent} sounds
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Section 6: Send Content */}
      <Card>
        <CardHeader>
          <CardTitle>Send Content</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Page selector */}
          <div>
            <label className="mb-1 block text-xs font-bold uppercase tracking-wider text-muted-foreground" htmlFor="send-page">
              Page
            </label>
            <select
              id="send-page"
              value={sendPage}
              onChange={(e) => setSendPage(e.target.value)}
              className="h-9 w-full rounded-md border border-border bg-card px-3 text-sm text-foreground"
            >
              <option value="">Select a page...</option>
              {pagesWithTopics.map((p) => (
                <option key={p.integration_id} value={p.integration_id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          {/* File path */}
          <div>
            <label className="mb-1 block text-xs font-bold uppercase tracking-wider text-muted-foreground" htmlFor="send-file">
              File Path
            </label>
            <Input
              id="send-file"
              value={sendFilePath}
              onChange={(e) => setSendFilePath(e.target.value)}
              placeholder="Path to video file"
            />
          </div>

          {/* Caption */}
          <div>
            <label className="mb-1 block text-xs font-bold uppercase tracking-wider text-muted-foreground" htmlFor="send-caption">
              Caption (optional)
            </label>
            <textarea
              id="send-caption"
              value={sendCaption}
              onChange={(e) => setSendCaption(e.target.value)}
              placeholder="Optional caption text..."
              rows={3}
              className="w-full rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {/* Send button */}
          <div className="flex items-center gap-3">
            <Button
              onClick={handleSendToStaging}
              disabled={!sendPage || !sendFilePath.trim()}
            >
              Send Content
            </Button>
            {sendResult && (
              <Badge variant="success">{sendResult}</Badge>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
