import { useCallback, useEffect, useMemo, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { apiUrl, staticUrl } from '../lib/api';
import type {
  PostizIntegration,
  PostizStatusResponse,
  PostizUploadResponse,
  PublishableBatch,
  PublishableVideo,
  PublishableVideosResponse,
} from '../types/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

// ── Types ──────────────────────────────────────────────────────────────────

interface StagedVideo {
  video: PublishableVideo;
  batchId: string;
  integrationId: string | null;
  status: 'pending' | 'uploading' | 'uploaded' | 'posting' | 'posted' | 'error';
  postizPath?: string;
  error?: string;
}

// ── Component ──────────────────────────────────────────────────────────────

export function PublishPage() {
  const { activeProjectName, addNotification } = useWorkflowStore();

  // Postiz connection state
  const [postizStatus, setPostizStatus] = useState<PostizStatusResponse | null>(null);
  const [integrations, setIntegrations] = useState<PostizIntegration[]>([]);
  const [loadingIntegrations, setLoadingIntegrations] = useState(false);

  // Video state
  const [batches, setBatches] = useState<PublishableBatch[]>([]);
  const [loadingVideos, setLoadingVideos] = useState(false);

  // Staging state
  const [staged, setStaged] = useState<StagedVideo[]>([]);
  const [publishing, setPublishing] = useState(false);

  // ── Fetch Postiz status ────────────────────────────────────────────────

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/postiz/status'));
      const ct = resp.headers.get('content-type') ?? '';
      if (!ct.includes('application/json')) throw new Error('Not JSON');
      const data = (await resp.json()) as PostizStatusResponse;
      setPostizStatus(data);
    } catch {
      setPostizStatus({ configured: false, reachable: false });
    }
  }, []);

  // ── Fetch integrations ────────────────────────────────────────────────

  const fetchIntegrations = useCallback(async () => {
    setLoadingIntegrations(true);
    try {
      const resp = await fetch(apiUrl('/api/postiz/integrations'));
      if (!resp.ok) throw new Error(`Failed (${resp.status})`);
      const ct = resp.headers.get('content-type') ?? '';
      if (!ct.includes('application/json')) throw new Error('Server returned non-JSON response');
      const data = await resp.json();
      // Postiz returns an array directly or { integrations: [...] }
      const list = Array.isArray(data) ? data : (data.integrations ?? data);
      setIntegrations(Array.isArray(list) ? list : []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load integrations';
      addNotification('error', msg);
      setIntegrations([]);
    } finally {
      setLoadingIntegrations(false);
    }
  }, [addNotification]);

  // ── Fetch publishable videos ──────────────────────────────────────────

  const fetchVideos = useCallback(async () => {
    if (!activeProjectName) return;
    setLoadingVideos(true);
    try {
      const resp = await fetch(apiUrl(`/api/postiz/videos?project=${encodeURIComponent(activeProjectName)}`));
      if (!resp.ok) throw new Error(`Failed (${resp.status})`);
      const ct = resp.headers.get('content-type') ?? '';
      if (!ct.includes('application/json')) throw new Error('Server returned non-JSON response');
      const data = (await resp.json()) as PublishableVideosResponse;
      setBatches(data.batches);
    } catch {
      setBatches([]);
    } finally {
      setLoadingVideos(false);
    }
  }, [activeProjectName]);

  // ── Init ──────────────────────────────────────────────────────────────

  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  useEffect(() => {
    if (postizStatus?.configured && postizStatus?.reachable) {
      void fetchIntegrations();
    }
  }, [postizStatus, fetchIntegrations]);

  useEffect(() => {
    void fetchVideos();
  }, [fetchVideos]);

  // ── Stage / unstage videos ────────────────────────────────────────────

  const isStaged = useCallback(
    (batchId: string, videoName: string) =>
      staged.some((s) => s.batchId === batchId && s.video.name === videoName),
    [staged],
  );

  const toggleVideo = useCallback(
    (batch: PublishableBatch, video: PublishableVideo) => {
      setStaged((prev) => {
        const exists = prev.find((s) => s.batchId === batch.batch_id && s.video.name === video.name);
        if (exists) {
          return prev.filter((s) => s !== exists);
        }
        return [
          ...prev,
          {
            video,
            batchId: batch.batch_id,
            integrationId: integrations.length > 0 ? integrations[0].id : null,
            status: 'pending' as const,
          },
        ];
      });
    },
    [integrations],
  );

  const stageAllFromBatch = useCallback(
    (batch: PublishableBatch) => {
      setStaged((prev) => {
        const newItems: StagedVideo[] = [];
        for (const video of batch.videos) {
          const alreadyStaged = prev.some((s) => s.batchId === batch.batch_id && s.video.name === video.name);
          if (!alreadyStaged) {
            newItems.push({
              video,
              batchId: batch.batch_id,
              integrationId: integrations.length > 0 ? integrations[0].id : null,
              status: 'pending',
            });
          }
        }
        return [...prev, ...newItems];
      });
    },
    [integrations],
  );

  const unstageAll = useCallback(() => {
    setStaged([]);
  }, []);

  const assignIntegration = useCallback((index: number, integrationId: string) => {
    setStaged((prev) => prev.map((s, i) => (i === index ? { ...s, integrationId } : s)));
  }, []);

  const assignAllIntegration = useCallback((integrationId: string) => {
    setStaged((prev) => prev.map((s) => ({ ...s, integrationId })));
  }, []);

  const removeStagedItem = useCallback((index: number) => {
    setStaged((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // ── Publish flow ─────────────────────────────────────────────────────

  const canPublish = useMemo(
    () => staged.length > 0 && staged.every((s) => s.integrationId) && !publishing,
    [staged, publishing],
  );

  const publish = useCallback(async () => {
    if (!canPublish) return;
    setPublishing(true);

    // Group by integration
    const byIntegration = new Map<string, number[]>();
    for (let i = 0; i < staged.length; i++) {
      const s = staged[i];
      if (!s.integrationId) continue;
      const group = byIntegration.get(s.integrationId) ?? [];
      group.push(i);
      byIntegration.set(s.integrationId, group);
    }

    // Phase 1: Upload all videos
    for (let i = 0; i < staged.length; i++) {
      const s = staged[i];
      if (s.status === 'uploaded' || s.status === 'posted') continue;

      setStaged((prev) => prev.map((item, idx) => (idx === i ? { ...item, status: 'uploading' } : item)));

      try {
        const resp = await fetch(apiUrl('/api/postiz/upload'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: s.video.path }),
        });

        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `Upload failed (${resp.status})`);
        }

        const data = (await resp.json()) as PostizUploadResponse;
        setStaged((prev) =>
          prev.map((item, idx) => (idx === i ? { ...item, status: 'uploaded', postizPath: data.path } : item)),
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Upload failed';
        setStaged((prev) => prev.map((item, idx) => (idx === i ? { ...item, status: 'error', error: msg } : item)));
      }
    }

    // Phase 2: Create posts grouped by integration
    for (const [integrationId, indices] of byIntegration) {
      const videos = indices
        .filter((i) => staged[i]?.postizPath)
        .map((i) => {
          const item = staged[i];
          return { tag: item.video.name, postiz_path: item.postizPath! };
        });

      if (videos.length === 0) continue;

      // Mark as posting
      for (const i of indices) {
        setStaged((prev) =>
          prev.map((item, idx) =>
            idx === i && item.postizPath ? { ...item, status: 'posting' } : item,
          ),
        );
      }

      try {
        const resp = await fetch(apiUrl('/api/postiz/posts'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            integration_id: integrationId,
            videos,
          }),
        });

        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `Post creation failed (${resp.status})`);
        }

        // Mark as posted
        for (const i of indices) {
          setStaged((prev) =>
            prev.map((item, idx) => (idx === i && item.postizPath ? { ...item, status: 'posted' } : item)),
          );
        }

        const acct = integrations.find((ig) => ig.id === integrationId);
        addNotification('success', `${videos.length} video(s) posted to ${acct?.name ?? 'account'}`);
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Post creation failed';
        for (const i of indices) {
          setStaged((prev) =>
            prev.map((item, idx) => (idx === i ? { ...item, status: 'error', error: msg } : item)),
          );
        }
        addNotification('error', msg);
      }
    }

    setPublishing(false);
  }, [canPublish, staged, integrations, addNotification]);

  // ── Counts ────────────────────────────────────────────────────────────

  const totalVideos = useMemo(() => batches.reduce((sum, b) => sum + b.videos.length, 0), [batches]);
  const postedCount = useMemo(() => staged.filter((s) => s.status === 'posted').length, [staged]);
  const errorCount = useMemo(() => staged.filter((s) => s.status === 'error').length, [staged]);

  // ── Render ────────────────────────────────────────────────────────────

  if (!activeProjectName) {
    return (
      <div className="p-8 text-center text-muted-foreground">
        <p className="text-lg font-bold">Select a project to publish videos</p>
      </div>
    );
  }

  return (
    <div className="p-4 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-heading font-bold">Publish</h1>
          <p className="text-sm text-muted-foreground">
            Stage burned videos and post them to your connected accounts via Postiz
          </p>
        </div>
        <div className="flex items-center gap-2">
          {postizStatus ? (
            <Badge variant={postizStatus.configured && postizStatus.reachable ? 'success' : 'destructive'}>
              {postizStatus.configured
                ? postizStatus.reachable
                  ? 'Postiz Connected'
                  : 'Postiz Unreachable'
                : 'Postiz Not Configured'}
            </Badge>
          ) : (
            <Badge variant="secondary">Checking...</Badge>
          )}
          <Button variant="outline" size="sm" onClick={() => { void fetchStatus(); void fetchVideos(); void fetchIntegrations(); }}>
            Refresh
          </Button>
        </div>
      </div>

      {/* Not configured warning */}
      {postizStatus && !postizStatus.configured && (
        <div className="rounded-[var(--border-radius)] border-2 border-border bg-amber-100 text-amber-900 px-4 py-3 text-sm shadow-[2px_2px_0_0_var(--border)]">
          <strong>POSTIZ_API_KEY</strong> is not set in your <code>.env</code> file. Add it to enable publishing.
        </div>
      )}

      {postizStatus?.configured && postizStatus?.reachable && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left: Available Videos */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-bold">
                Burned Videos
                {totalVideos > 0 && (
                  <Badge variant="secondary" className="ml-2">{totalVideos}</Badge>
                )}
              </h2>
              <Button variant="outline" size="sm" onClick={fetchVideos} disabled={loadingVideos}>
                {loadingVideos ? 'Loading...' : 'Reload'}
              </Button>
            </div>

            {batches.length === 0 && !loadingVideos && (
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-8 text-center text-muted-foreground">
                No burned videos found. Burn some videos first in the Burn tab.
              </div>
            )}

            {batches.map((batch) => (
              <div
                key={batch.batch_id}
                className="rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[2px_2px_0_0_var(--border)]"
              >
                <div className="flex items-center justify-between border-b-2 border-border px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold font-mono">{batch.batch_id}</span>
                    <Badge variant="secondary">{batch.videos.length} videos</Badge>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => stageAllFromBatch(batch)}
                  >
                    Stage All
                  </Button>
                </div>
                <div className="divide-y divide-border">
                  {batch.videos.map((video) => {
                    const selected = isStaged(batch.batch_id, video.name);
                    return (
                      <div
                        key={video.name}
                        className={`flex items-center gap-3 px-3 py-2 cursor-pointer transition-colors ${
                          selected ? 'bg-primary/10' : 'hover:bg-muted'
                        }`}
                        onClick={() => toggleVideo(batch, video)}
                      >
                        <input
                          type="checkbox"
                          checked={selected}
                          readOnly
                          className="h-4 w-4 rounded border-2 border-border accent-primary"
                        />
                        <div className="flex-1 min-w-0">
                          <span className="text-sm font-mono truncate block">{video.name}</span>
                          <span className="text-xs text-muted-foreground">
                            {(video.size / 1024 / 1024).toFixed(1)} MB
                          </span>
                        </div>
                        <video
                          src={staticUrl(`/${video.path}`)}
                          className="h-12 w-20 rounded border border-border object-cover bg-black"
                          muted
                          preload="metadata"
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>

          {/* Right: Staging Area */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-bold">
                Staging Area
                {staged.length > 0 && (
                  <Badge variant="default" className="ml-2">{staged.length}</Badge>
                )}
              </h2>
              <div className="flex items-center gap-2">
                {staged.length > 0 && (
                  <Button variant="outline" size="sm" onClick={unstageAll}>
                    Clear All
                  </Button>
                )}
              </div>
            </div>

            {/* Connected Accounts selector */}
            {integrations.length > 0 && staged.length > 0 && (
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-3 py-2">
                <label className="text-xs font-bold text-muted-foreground block mb-1">
                  Assign all to account:
                </label>
                <select
                  className="w-full rounded-[var(--border-radius)] border-2 border-border bg-card px-2 py-1 text-sm font-bold"
                  onChange={(e) => assignAllIntegration(e.target.value)}
                  defaultValue=""
                >
                  <option value="" disabled>
                    Choose account...
                  </option>
                  {integrations.map((ig) => (
                    <option key={ig.id} value={ig.id}>
                      {ig.name} ({ig.provider})
                    </option>
                  ))}
                </select>
              </div>
            )}

            {staged.length === 0 && (
              <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-8 text-center text-muted-foreground">
                Select videos from the left to stage them for publishing.
              </div>
            )}

            {/* Staged items */}
            <div className="space-y-2">
              {staged.map((item, index) => (
                <div
                  key={`${item.batchId}-${item.video.name}`}
                  className={`rounded-[var(--border-radius)] border-2 px-3 py-2 shadow-[2px_2px_0_0_var(--border)] ${
                    item.status === 'posted'
                      ? 'border-green-400 bg-green-50'
                      : item.status === 'error'
                        ? 'border-red-400 bg-red-50'
                        : item.status === 'uploading' || item.status === 'posting'
                          ? 'border-amber-400 bg-amber-50'
                          : 'border-border bg-card'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <div className="flex-1 min-w-0">
                      <span className="text-sm font-mono truncate block">{item.video.name}</span>
                      <span className="text-xs text-muted-foreground">{item.batchId}</span>
                    </div>
                    <StatusBadge status={item.status} />
                    {item.status === 'pending' && (
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-foreground text-lg leading-none"
                        onClick={() => removeStagedItem(index)}
                      >
                        &times;
                      </button>
                    )}
                  </div>

                  {/* Per-item integration selector */}
                  {integrations.length > 0 && item.status === 'pending' && (
                    <select
                      className="mt-2 w-full rounded-[var(--border-radius)] border-2 border-border bg-card px-2 py-1 text-xs"
                      value={item.integrationId ?? ''}
                      onChange={(e) => assignIntegration(index, e.target.value)}
                    >
                      <option value="" disabled>
                        Assign account...
                      </option>
                      {integrations.map((ig) => (
                        <option key={ig.id} value={ig.id}>
                          {ig.name} ({ig.provider})
                        </option>
                      ))}
                    </select>
                  )}

                  {item.error && (
                    <p className="mt-1 text-xs text-red-700 truncate">{item.error}</p>
                  )}
                </div>
              ))}
            </div>

            {/* Publish button */}
            {staged.length > 0 && (
              <div className="flex items-center gap-3 pt-2">
                <Button
                  onClick={publish}
                  disabled={!canPublish}
                  className="flex-1"
                >
                  {publishing
                    ? 'Publishing...'
                    : `Publish ${staged.filter((s) => s.status === 'pending').length} Video(s)`}
                </Button>
                {postedCount > 0 && (
                  <Badge variant="success">{postedCount} posted</Badge>
                )}
                {errorCount > 0 && (
                  <Badge variant="destructive">{errorCount} failed</Badge>
                )}
              </div>
            )}

            {/* Connected Accounts List */}
            <div className="pt-4">
              <h3 className="text-sm font-bold mb-2">
                Connected Accounts
                {loadingIntegrations && <span className="text-muted-foreground font-normal ml-2">Loading...</span>}
              </h3>
              {integrations.length === 0 && !loadingIntegrations && (
                <p className="text-sm text-muted-foreground">
                  No connected accounts found. Connect accounts in your Postiz dashboard.
                </p>
              )}
              <div className="space-y-1">
                {integrations.map((ig) => (
                  <div
                    key={ig.id}
                    className="flex items-center gap-2 rounded-[var(--border-radius)] border-2 border-border bg-card px-3 py-2 text-sm"
                  >
                    {ig.picture && (
                      <img src={ig.picture} alt="" className="h-6 w-6 rounded-full border border-border" />
                    )}
                    <span className="font-bold">{ig.name}</span>
                    <Badge variant="secondary" className="text-[10px]">
                      {ig.provider}
                    </Badge>
                    {ig.disabled && (
                      <Badge variant="destructive" className="text-[10px]">disabled</Badge>
                    )}
                    <span className="text-xs text-muted-foreground ml-auto font-mono">{ig.id.slice(0, 12)}...</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Helper Components ──────────────────────────────────────────────────────

function StatusBadge({ status }: { status: StagedVideo['status'] }) {
  const variants: Record<StagedVideo['status'], { variant: 'default' | 'secondary' | 'success' | 'destructive'; label: string }> = {
    pending: { variant: 'secondary', label: 'Pending' },
    uploading: { variant: 'default', label: 'Uploading...' },
    uploaded: { variant: 'default', label: 'Uploaded' },
    posting: { variant: 'default', label: 'Posting...' },
    posted: { variant: 'success', label: 'Posted' },
    error: { variant: 'destructive', label: 'Error' },
  };

  const { variant, label } = variants[status];
  return (
    <Badge variant={variant} className="text-[10px] px-1.5 py-0 shadow-none">
      {label}
    </Badge>
  );
}
