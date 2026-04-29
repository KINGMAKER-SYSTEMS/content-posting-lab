import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  ArrowLeftIcon,
  CheckCircleIcon,
  CloudArrowUpIcon,
  EnvelopeSimpleIcon,
  FolderIcon,
  HashIcon,
  PaperPlaneTiltIcon,
  WarningIcon,
  ArrowsClockwiseIcon,
} from '@phosphor-icons/react';
import { apiUrl } from '../lib/api';
import { useWorkflowStore } from '../stores/workflowStore';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { PipelineWorkspace, WorkspaceR2Object } from '../types/api';

export function PipelineWorkspacePage() {
  const location = useLocation();
  const integrationId = useMemo(() => {
    const m = location.pathname.match(/^\/pipeline\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : '';
  }, [location.pathname]);
  const navigate = useNavigate();
  const { addNotification } = useWorkflowStore();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [workspace, setWorkspace] = useState<PipelineWorkspace | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{ name: string; pct: number } | null>(null);
  const [forwarding, setForwarding] = useState<string | null>(null);

  const fetchWorkspace = useCallback(async () => {
    if (!integrationId) return;
    setLoading(true);
    try {
      const resp = await fetch(apiUrl(`/api/pipeline/${encodeURIComponent(integrationId)}/workspace`));
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Request failed (${resp.status})`);
      }
      const data = (await resp.json()) as PipelineWorkspace;
      setWorkspace(data);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to load workspace');
    } finally {
      setLoading(false);
    }
  }, [integrationId, addNotification]);

  useEffect(() => {
    void fetchWorkspace();
  }, [fetchWorkspace]);

  const handleUpload = useCallback(async (file: File) => {
    if (!integrationId) return;
    setUploading(true);
    setUploadProgress({ name: file.name, pct: 0 });
    try {
      // Step 1: get presigned PUT URL
      const presignResp = await fetch(
        apiUrl(`/api/pipeline/${encodeURIComponent(integrationId)}/upload-presign`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            filename: file.name,
            content_type: file.type || 'video/mp4',
          }),
        },
      );
      if (!presignResp.ok) {
        const text = await presignResp.text();
        throw new Error(text || 'Failed to get upload URL');
      }
      const { url } = (await presignResp.json()) as { url: string; key: string };

      // Step 2: PUT directly to R2 with progress tracking
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', url);
        xhr.setRequestHeader('Content-Type', file.type || 'video/mp4');
        xhr.upload.addEventListener('progress', (e) => {
          if (e.lengthComputable) {
            setUploadProgress({ name: file.name, pct: Math.round((e.loaded / e.total) * 100) });
          }
        });
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve();
          else reject(new Error(`R2 PUT failed: ${xhr.status}`));
        };
        xhr.onerror = () => reject(new Error('R2 PUT network error'));
        xhr.send(file);
      });

      addNotification('success', `Uploaded ${file.name}`);
      await fetchWorkspace();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
      setUploadProgress(null);
    }
  }, [integrationId, addNotification, fetchWorkspace]);

  const handleForward = useCallback(async (obj: WorkspaceR2Object) => {
    if (!integrationId) return;
    setForwarding(obj.key);
    try {
      const resp = await fetch(
        apiUrl(`/api/pipeline/${encodeURIComponent(integrationId)}/forward-to-topic`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ r2_key: obj.key }),
        },
      );
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Forward failed (${resp.status})`);
      }
      addNotification('success', `Forwarded ${obj.filename} to telegram`);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Forward failed');
    } finally {
      setForwarding(null);
    }
  }, [integrationId, addNotification]);

  const onFilePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    files.forEach((f) => void handleUpload(f));
    e.target.value = ''; // reset so same file can be picked again
  };

  if (loading && !workspace) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Loading workspace...</div>
    );
  }

  if (!workspace) {
    return (
      <div className="p-6 space-y-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/pipeline')}>
          <ArrowLeftIcon size={14} weight="bold" className="mr-1" />
          Back to Pipeline
        </Button>
        <div className="text-sm text-destructive">Failed to load workspace.</div>
      </div>
    );
  }

  const { page, r2: r2State, telegram, cookie_status } = workspace;
  const progressPct = Math.min(100, Math.round((r2State.object_count / r2State.target) * 100));

  return (
    <div className="p-4 space-y-4">
      {/* ── Header ─────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => navigate('/pipeline')}>
            <ArrowLeftIcon size={14} weight="bold" className="mr-1" />
            Pipeline
          </Button>
          <h1 className="text-2xl font-heading font-bold">{page.name}</h1>
          {page.pipeline && (
            <Badge variant="secondary" className="text-[10px]">{page.pipeline}</Badge>
          )}
          {page.page_type && (
            <Badge variant="info" className="text-[10px]">{page.page_type}</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={() => void fetchWorkspace()} disabled={loading}>
            <ArrowsClockwiseIcon size={14} weight="bold" className={`mr-1 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Status bar (3 cards) ───────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {/* Email card */}
        <div className="rounded-md border border-border bg-card p-3 space-y-1">
          <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground flex items-center gap-1">
            <EnvelopeSimpleIcon size={11} weight="bold" />
            Email
          </div>
          {page.email_alias ? (
            <div className="text-xs font-mono truncate" title={page.email_alias}>
              {page.email_alias}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">— not minted —</div>
          )}
          {page.password && (
            <div className="text-[10px] text-muted-foreground">pw: <span className="font-mono">{page.password}</span></div>
          )}
        </div>

        {/* Telegram card */}
        <div className="rounded-md border border-border bg-card p-3 space-y-1">
          <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground flex items-center gap-1">
            <PaperPlaneTiltIcon size={11} weight="bold" />
            Telegram
          </div>
          {telegram.topic_present ? (
            <>
              <div className="text-xs flex items-center gap-1">
                <Badge variant="success" className="text-[9px]">Live</Badge>
                <span className="truncate">{telegram.topic_name}</span>
              </div>
              <div className="text-[10px] text-muted-foreground truncate">
                Poster: {telegram.poster_name}
              </div>
            </>
          ) : (
            <div className="text-xs text-muted-foreground">
              <Badge variant="warning" className="text-[9px] mr-1">No topic</Badge>
              {telegram.poster_name ? `Poster: ${telegram.poster_name}` : 'No poster assigned'}
            </div>
          )}
        </div>

        {/* R2 / Cookie card */}
        <div className="rounded-md border border-border bg-card p-3 space-y-1">
          <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground flex items-center gap-1">
            <FolderIcon size={11} weight="bold" />
            Storage / Cookies
          </div>
          {r2State.prefix ? (
            <div className="text-xs font-mono truncate" title={`${r2State.bucket}/${r2State.prefix}`}>
              {r2State.prefix}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">— no R2 prefix —</div>
          )}
          <div className="text-[10px] text-muted-foreground">
            Cookies: <Badge variant={cookie_status === 'valid' ? 'success' : 'warning'} className="text-[9px] ml-1">{cookie_status}</Badge>
          </div>
        </div>
      </div>

      {/* ── Progress + Upload zone ─────────────────────────── */}
      <div className="rounded-md border border-border bg-card p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-bold">
              Content progress: {r2State.object_count} / {r2State.target}
            </div>
            <div className="text-xs text-muted-foreground">
              Upload videos here. Each one lands in R2 → click "Send to Topic" to forward to {telegram.topic_name || 'the poster\'s telegram'}.
            </div>
          </div>
          <Button
            size="sm"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading || !r2State.configured || !r2State.prefix}
          >
            <CloudArrowUpIcon size={14} weight="bold" className="mr-1" />
            {uploading ? 'Uploading...' : 'Upload Video'}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*"
            multiple
            className="hidden"
            onChange={onFilePicked}
          />
        </div>

        {/* Progress bar */}
        <div className="h-2 bg-muted rounded overflow-hidden">
          <div
            className="h-full bg-[var(--brand-gradient)] transition-all"
            style={{ width: `${progressPct}%` }}
          />
        </div>

        {/* Per-upload progress */}
        {uploadProgress && (
          <div className="text-xs text-muted-foreground">
            <span className="font-mono">{uploadProgress.name}</span> — {uploadProgress.pct}%
          </div>
        )}

        {/* Configuration warnings */}
        {!r2State.configured && (
          <div className="text-xs text-destructive flex items-center gap-1">
            <WarningIcon size={12} weight="bold" />
            R2 not configured
          </div>
        )}
        {r2State.configured && !r2State.prefix && (
          <div className="text-xs text-warning flex items-center gap-1">
            <WarningIcon size={12} weight="bold" />
            No R2 prefix on this page — re-run setup
          </div>
        )}
        {!telegram.topic_present && (
          <div className="text-xs text-warning flex items-center gap-1">
            <WarningIcon size={12} weight="bold" />
            No telegram topic — re-run setup so videos can be forwarded
          </div>
        )}
      </div>

      {/* ── R2 file list ───────────────────────────────────── */}
      <div className="rounded-md border border-border bg-card overflow-hidden">
        <div className="px-3 py-2 border-b border-border">
          <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
            Videos in R2 ({r2State.object_count})
          </div>
        </div>
        {r2State.object_count === 0 ? (
          <div className="px-3 py-8 text-center text-xs text-muted-foreground">
            No videos uploaded yet. Click "Upload Video" above to add the first one.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="text-left px-3 py-2 font-bold">Filename</th>
                <th className="text-left px-3 py-2 font-bold w-20">Size</th>
                <th className="text-left px-3 py-2 font-bold w-32">Uploaded</th>
                <th className="text-right px-3 py-2 font-bold w-32">Action</th>
              </tr>
            </thead>
            <tbody>
              {r2State.objects.map((obj) => {
                const isForwarding = forwarding === obj.key;
                return (
                  <tr key={obj.key} className="border-t border-border hover:bg-muted/20">
                    <td className="px-3 py-2 font-mono text-xs truncate max-w-[400px]" title={obj.filename}>
                      {obj.filename}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {(obj.size / 1024 / 1024).toFixed(1)} MB
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {obj.last_modified ? new Date(obj.last_modified).toLocaleDateString() : '—'}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        size="xs"
                        variant="outline"
                        className="text-[11px] h-7"
                        onClick={() => void handleForward(obj)}
                        disabled={isForwarding || !telegram.topic_present}
                      >
                        {isForwarding ? (
                          <>...</>
                        ) : (
                          <>
                            <PaperPlaneTiltIcon size={11} weight="bold" className="mr-1" />
                            Send to Topic
                          </>
                        )}
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Footer info */}
      <div className="text-xs text-muted-foreground flex items-center gap-3 pt-2">
        {page.notion_page_id && (
          <a
            href={`https://www.notion.so/${page.notion_page_id.replace(/-/g, '')}`}
            target="_blank"
            rel="noreferrer"
            className="hover:text-foreground transition-colors flex items-center gap-1"
          >
            <HashIcon size={11} weight="bold" />
            Open in Notion
          </a>
        )}
        {telegram.poster_id && (
          <span className="flex items-center gap-1">
            <CheckCircleIcon size={11} weight="bold" className="text-success" />
            Forwarding to {telegram.poster_name}
          </span>
        )}
      </div>
    </div>
  );
}
