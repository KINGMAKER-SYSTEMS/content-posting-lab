import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { TelegramStatus, TelegramPoster, TelegramSound, RosterPage } from '@/types/api';

export interface TelegramTabProps {
  status: TelegramStatus | null;
  posters: TelegramPoster[];
  sounds: TelegramSound[];
  rosterPages: RosterPage[];
  error: string | null;
  setError: (v: string | null) => void;

  // Staging group
  chatIdInput: string;
  onChatIdInputChange: (v: string) => void;
  onSetGroup: () => void;
  onSyncTopics: () => void;
  onForwardPending: (integrationId: string) => void;
  syncResult: { created: number; existing: number } | null;
  scanning: boolean;
  scanProgress: string;
  scanResult: { scanned_topics: number; total_found: number } | null;
  onScanInventory: () => void;
  discovering: boolean;
  discoverProgress: string;
  onDiscoverTopics: () => void;
  onClearScanData: () => void;
  onAuditDupes: () => void;
  onCleanDupes: () => void;

  // Posters
  newPosterName: string;
  onNewPosterNameChange: (v: string) => void;
  newPosterChatId: string;
  onNewPosterChatIdChange: (v: string) => void;
  onAddPoster: () => void;
  onRemovePoster: (posterId: string) => void;
  onSyncPosterTopics: (posterId: string) => void;
  onForwardSounds: (posterId: string) => void;
  onUnassignPage: (posterId: string, pageId: string, pageName: string) => void;
  posterSelectedPages: Record<string, Set<string>>;
  posterAssignOpen: Record<string, boolean>;
  assignLoading: string | null;
  onTogglePageSelection: (posterId: string, pageId: string) => void;
  onSelectAllPages: (posterId: string, pageIds: string[]) => void;
  onClearPageSelection: (posterId: string) => void;
  onAssignPages: (posterId: string) => void;
  onOpenAssign: (posterId: string) => void;
  onCloseAssign: (posterId: string) => void;

  // Send content
  sendPage: string;
  onSendPageChange: (v: string) => void;
  sendFilePath: string;
  onSendFilePathChange: (v: string) => void;
  sendCaption: string;
  onSendCaptionChange: (v: string) => void;
  sendResult: string | null;
  onSendToStaging: () => void;

  // Derived
  topicEntries: [string, any][];
  pagesWithTopics: RosterPage[];
}

export function TelegramTab({
  status,
  posters,
  sounds,
  rosterPages,
  error,
  setError,
  chatIdInput,
  onChatIdInputChange,
  onSetGroup,
  onSyncTopics,
  onForwardPending,
  syncResult,
  scanning,
  scanProgress,
  scanResult,
  onScanInventory,
  discovering,
  discoverProgress,
  onDiscoverTopics,
  onClearScanData,
  onAuditDupes,
  onCleanDupes,
  newPosterName,
  onNewPosterNameChange,
  newPosterChatId,
  onNewPosterChatIdChange,
  onAddPoster,
  onRemovePoster,
  onSyncPosterTopics,
  onForwardSounds,
  onUnassignPage,
  posterSelectedPages,
  posterAssignOpen,
  assignLoading,
  onTogglePageSelection,
  onSelectAllPages,
  onClearPageSelection,
  onAssignPages,
  onOpenAssign,
  onCloseAssign,
  sendPage,
  onSendPageChange,
  sendFilePath,
  onSendFilePathChange,
  sendCaption,
  onSendCaptionChange,
  sendResult,
  onSendToStaging,
  topicEntries,
  pagesWithTopics,
}: TelegramTabProps) {
  const stagingGroup = status?.staging_group ?? null;

  return (
    <div className="space-y-6">
      {/* Content Storage (Staging Group) */}
      <Card>
        <CardHeader>
          <CardTitle>Content Storage</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Input
              type="number"
              value={chatIdInput}
              onChange={(e) => onChatIdInputChange(e.target.value)}
              placeholder="Group ID"
              className="flex-1"
            />
            <Button onClick={onSetGroup} disabled={!chatIdInput.trim()}>
              Set Group
            </Button>
          </div>

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
                              {page?.name || topic.topic_name || integrationId}
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
                                onClick={() => onForwardPending(integrationId)}
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

              {/* Action buttons */}
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="outline" onClick={onSyncTopics}>
                  Set Up Folders
                </Button>
                <Button variant="outline" disabled={scanning} onClick={onScanInventory}>
                  {scanning ? 'Scanning...' : 'Scan Inventory'}
                </Button>
                <Button variant="outline" disabled={discovering} onClick={onDiscoverTopics}>
                  {discovering ? 'Discovering...' : 'Discover Topics'}
                </Button>
                <Button variant="destructive" size="sm" onClick={onClearScanData}>
                  Clear Scan Data
                </Button>
                {(scanProgress || discoverProgress) && (
                  <span className="text-xs text-muted-foreground">{scanProgress || discoverProgress}</span>
                )}
                <Button variant="ghost" size="sm" onClick={onAuditDupes}>
                  Audit Dupes
                </Button>
                <Button variant="ghost" size="sm" className="text-destructive" onClick={onCleanDupes}>
                  Clean Dupes
                </Button>
                {syncResult && (
                  <span className="text-sm text-muted-foreground">
                    Created {syncResult.created}, Existing {syncResult.existing}
                  </span>
                )}
                {scanResult && (
                  <span className="text-sm text-muted-foreground">
                    Scanned {scanResult.scanned_topics} topics, found {scanResult.total_found} items
                  </span>
                )}
              </div>

              {/* Dedup audit output */}
              {error && error.includes('duplicate') && (
                <Card className="border-amber-500 bg-amber-50">
                  <CardContent className="max-h-64 overflow-y-auto py-3">
                    <pre className="whitespace-pre-wrap text-xs text-amber-900 select-all">{error}</pre>
                    <Button size="xs" variant="ghost" className="mt-2" onClick={() => setError(null)}>Dismiss</Button>
                  </CardContent>
                </Card>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Posters */}
      <Card>
        <CardHeader>
          <CardTitle>Posters</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
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
                        <Button variant="outline" size="xs" onClick={() => onSyncPosterTopics(poster.poster_id)}>
                          Set Up Folders
                        </Button>
                        <Button
                          variant="outline"
                          size="xs"
                          onClick={() => onForwardSounds(poster.poster_id)}
                          disabled={sounds.filter((s) => s.active).length === 0}
                        >
                          Send Sounds
                        </Button>
                        <Button variant="destructive" size="xs" onClick={() => onRemovePoster(poster.poster_id)}>
                          Remove
                        </Button>
                      </div>
                    </div>

                    {/* Assigned pages */}
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
                              <button
                                type="button"
                                onClick={() => onUnassignPage(poster.poster_id, p.integration_id, p.name)}
                                className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full border border-border bg-destructive text-[10px] font-bold text-white opacity-0 transition-opacity group-hover:opacity-100"
                                title={`Remove ${p.name}`}
                              >
                                ×
                              </button>
                              {p.picture ? (
                                <img src={p.picture} alt={p.name} className="h-8 w-8 rounded-full border border-border object-cover" />
                              ) : (
                                <div className="flex h-8 w-8 items-center justify-center rounded-full border border-border bg-muted text-xs font-bold text-muted-foreground">
                                  {p.name.charAt(0).toUpperCase()}
                                </div>
                              )}
                              <span className="text-[11px] font-medium leading-tight text-foreground">{p.name}</span>
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
                            onClearPageSelection(poster.poster_id);
                            onOpenAssign(poster.poster_id);
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
                                onClick={() => onSelectAllPages(poster.poster_id, unassignedPages.map((p) => p.integration_id))}
                                className="text-xs font-medium text-primary hover:underline"
                              >
                                Select All
                              </button>
                              <span className="text-xs text-muted-foreground">|</span>
                              <button
                                type="button"
                                onClick={() => onClearPageSelection(poster.poster_id)}
                                className="text-xs font-medium text-muted-foreground hover:underline"
                              >
                                Clear
                              </button>
                            </div>
                          </div>

                          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
                            {unassignedPages.map((p) => {
                              const isSelected = selected.has(p.integration_id);
                              return (
                                <button
                                  key={p.integration_id}
                                  type="button"
                                  onClick={() => onTogglePageSelection(poster.poster_id, p.integration_id)}
                                  className={`relative flex flex-col items-center gap-1.5 rounded-[var(--border-radius)] border-2 p-2 text-center transition-all ${
                                    isSelected
                                      ? 'border-primary bg-primary/10 shadow-[2px_2px_0_0_var(--border)]'
                                      : 'border-border bg-card hover:border-muted-foreground hover:bg-muted/50'
                                  }`}
                                >
                                  <div
                                    className={`absolute right-1 top-1 flex h-4 w-4 items-center justify-center rounded-sm border transition-all ${
                                      isSelected ? 'border-primary bg-primary text-white' : 'border-border bg-card'
                                    }`}
                                  >
                                    {isSelected && (
                                      <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none">
                                        <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                                      </svg>
                                    )}
                                  </div>
                                  {p.picture ? (
                                    <img src={p.picture} alt={p.name} className="h-8 w-8 rounded-full border border-border object-cover" />
                                  ) : (
                                    <div className="flex h-8 w-8 items-center justify-center rounded-full border border-border bg-muted text-xs font-bold text-muted-foreground">
                                      {p.name.charAt(0).toUpperCase()}
                                    </div>
                                  )}
                                  <span className="text-[11px] font-medium leading-tight text-foreground">{p.name}</span>
                                </button>
                              );
                            })}
                          </div>

                          <div className="flex items-center gap-2">
                            <Button
                              onClick={() => onAssignPages(poster.poster_id)}
                              disabled={selected.size === 0 || assignLoading === poster.poster_id}
                            >
                              {assignLoading === poster.poster_id
                                ? 'Assigning & creating folders...'
                                : `Assign ${selected.size > 0 ? `${selected.size} Page${selected.size > 1 ? 's' : ''}` : 'Pages'}`}
                            </Button>
                            <Button
                              variant="outline"
                              onClick={() => {
                                onClearPageSelection(poster.poster_id);
                                onCloseAssign(poster.poster_id);
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

          <div className="border-t border-border pt-4">
            <p className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">Add Poster</p>
            <div className="flex items-center gap-2">
              <Input
                value={newPosterName}
                onChange={(e) => onNewPosterNameChange(e.target.value)}
                placeholder="Name"
                className="flex-1"
              />
              <Input
                type="number"
                value={newPosterChatId}
                onChange={(e) => onNewPosterChatIdChange(e.target.value)}
                placeholder="Group ID"
                className="w-40"
              />
              <Button
                onClick={onAddPoster}
                disabled={!newPosterName.trim() || !newPosterChatId.trim()}
              >
                Add Poster
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Send Content */}
      <Card>
        <CardHeader>
          <CardTitle>Send Content</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <label className="mb-1 block text-xs font-bold uppercase tracking-wider text-muted-foreground" htmlFor="send-page">
              Page
            </label>
            <select
              id="send-page"
              value={sendPage}
              onChange={(e) => onSendPageChange(e.target.value)}
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

          <div>
            <label className="mb-1 block text-xs font-bold uppercase tracking-wider text-muted-foreground" htmlFor="send-file">
              File Path
            </label>
            <Input
              id="send-file"
              value={sendFilePath}
              onChange={(e) => onSendFilePathChange(e.target.value)}
              placeholder="Path to video file"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-bold uppercase tracking-wider text-muted-foreground" htmlFor="send-caption">
              Caption (optional)
            </label>
            <textarea
              id="send-caption"
              value={sendCaption}
              onChange={(e) => onSendCaptionChange(e.target.value)}
              placeholder="Optional caption text..."
              rows={3}
              className="w-full rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          <div className="flex items-center gap-3">
            <Button
              onClick={onSendToStaging}
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
