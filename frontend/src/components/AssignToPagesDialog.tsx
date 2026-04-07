import { useCallback, useEffect, useState } from 'react';
import { fetchApi, postApi } from '@/lib/api';
import { useWorkflowStore } from '@/stores/workflowStore';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { RosterPageWithStaging, AssignBatchResponse } from '@/types/api';

interface AssignToPagesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  batchId: string;
  projectName: string;
  videoCount: number;
}

export function AssignToPagesDialog({
  open,
  onOpenChange,
  batchId,
  projectName,
  videoCount,
}: AssignToPagesDialogProps) {
  const { addNotification } = useWorkflowStore();

  const [pages, setPages] = useState<RosterPageWithStaging[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<AssignBatchResponse | null>(null);

  // Fetch project pages when dialog opens
  useEffect(() => {
    if (!open || !projectName) return;
    setResult(null);
    setLoading(true);
    fetchApi<{ pages: RosterPageWithStaging[] }>(`/api/roster/project/${encodeURIComponent(projectName)}`)
      .then((data) => {
        setPages(data.pages);
        // Auto-select all pages that have staging topics
        const eligible = data.pages
          .filter((p) => p.has_staging_topic)
          .map((p) => p.integration_id);
        setSelected(new Set(eligible));
      })
      .catch((err) => {
        addNotification('error', err instanceof Error ? err.message : 'Failed to load pages');
        setPages([]);
      })
      .finally(() => setLoading(false));
  }, [open, projectName, addNotification]);

  const togglePage = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectedCount = selected.size;

  // Compute round-robin preview
  const splitPreview = selectedCount > 0
    ? Array.from(selected).map((id, idx) => {
        const page = pages.find((p) => p.integration_id === id);
        const count = Math.floor(videoCount / selectedCount) + (idx < videoCount % selectedCount ? 1 : 0);
        return { name: page?.name ?? id, count };
      })
    : [];

  const handleSend = useCallback(async () => {
    if (selectedCount === 0) return;
    setSending(true);
    try {
      const res = await postApi<AssignBatchResponse>('/api/telegram/assign-batch', {
        batch_id: batchId,
        project: projectName,
        integration_ids: Array.from(selected),
      });
      setResult(res);
      const totalSent = res.assignments.reduce((sum, a) => sum + a.sent, 0);
      addNotification('success', `Assigned ${totalSent} videos across ${res.pages_used} pages`);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Assignment failed');
    } finally {
      setSending(false);
    }
  }, [batchId, projectName, selected, selectedCount, addNotification]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Assign to Pages</DialogTitle>
          <p className="text-sm text-muted-foreground">
            Split {videoCount} video{videoCount !== 1 ? 's' : ''} across pages for <span className="font-medium text-foreground">{projectName}</span>
          </p>
        </DialogHeader>

        {loading ? (
          <div className="flex justify-center py-8">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-muted border-t-primary" />
          </div>
        ) : result ? (
          /* ── Results view ── */
          <div className="space-y-3">
            <p className="text-sm font-medium text-foreground">
              Sent {result.assignments.reduce((s, a) => s + a.sent, 0)} / {result.total} videos
            </p>
            {result.assignments.map((a) => (
              <div
                key={a.integration_id}
                className="flex items-center justify-between rounded-md border border-border p-3"
              >
                <div>
                  <p className="text-sm font-medium text-foreground">{a.page_name}</p>
                  <p className="text-xs text-muted-foreground">{a.files.length} assigned, {a.sent} sent</p>
                </div>
                {a.errors.length > 0 ? (
                  <Badge variant="error">{a.errors.length} error{a.errors.length > 1 ? 's' : ''}</Badge>
                ) : (
                  <Badge variant="success">Done</Badge>
                )}
              </div>
            ))}
          </div>
        ) : pages.length === 0 ? (
          /* ── No pages assigned ── */
          <div className="py-8 text-center">
            <p className="text-sm text-muted-foreground">
              No pages assigned to this project.
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Assign pages from the Roster tab or Telegram settings.
            </p>
          </div>
        ) : (
          /* ── Page selection ── */
          <div className="space-y-4">
            {/* Page list */}
            <div className="max-h-64 space-y-2 overflow-y-auto">
              {pages.map((page) => {
                const eligible = page.has_staging_topic;
                const isSelected = selected.has(page.integration_id);
                return (
                  <button
                    key={page.integration_id}
                    type="button"
                    disabled={!eligible}
                    onClick={() => eligible && togglePage(page.integration_id)}
                    className={`flex w-full items-center gap-3 rounded-md border-2 p-3 text-left transition-all ${
                      !eligible
                        ? 'cursor-not-allowed border-border/50 bg-muted/30 opacity-50'
                        : isSelected
                          ? 'border-primary bg-primary/5'
                          : 'border-border hover:border-muted-foreground'
                    }`}
                  >
                    {/* Checkbox */}
                    <div
                      className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border ${
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

                    {/* Avatar */}
                    {page.picture ? (
                      <img src={page.picture} alt={page.name} className="h-7 w-7 rounded-full border border-border object-cover" />
                    ) : (
                      <div className="flex h-7 w-7 items-center justify-center rounded-full border border-border bg-muted text-xs font-bold text-muted-foreground">
                        {page.name.charAt(0).toUpperCase()}
                      </div>
                    )}

                    {/* Name + status */}
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-foreground">{page.name}</p>
                    </div>

                    {!eligible && (
                      <Badge variant="secondary" className="text-[10px]">No topic</Badge>
                    )}
                  </button>
                );
              })}
            </div>

            {/* Split preview */}
            {selectedCount > 0 && (
              <div className="rounded-md border border-border bg-muted/30 p-3">
                <p className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">Split Preview</p>
                <div className="space-y-1">
                  {splitPreview.map((item) => (
                    <div key={item.name} className="flex items-center justify-between text-sm">
                      <span className="text-foreground">{item.name}</span>
                      <span className="tabular-nums text-muted-foreground">{item.count} video{item.count !== 1 ? 's' : ''}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          {result ? (
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button
                onClick={handleSend}
                disabled={selectedCount === 0 || sending || loading}
              >
                {sending
                  ? 'Sending...'
                  : `Send to ${selectedCount} Page${selectedCount !== 1 ? 's' : ''}`}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
