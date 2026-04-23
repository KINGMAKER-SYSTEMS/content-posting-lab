import { useMemo } from 'react';
import {
  CaretUpDownIcon,
  CaretUpIcon,
  CaretDownIcon,
  CheckIcon,
  XIcon,
} from '@phosphor-icons/react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type {
  RosterPage,
  EmailStatusResponse,
  EmailDestination,
  DriveStatusResponse,
  UploadQueueStats,
} from '@/types/api';

type SortKey = 'name' | 'provider' | 'project';
type SortDir = 'asc' | 'desc';

function comparePages(a: RosterPage, b: RosterPage, key: SortKey, dir: SortDir): number {
  const av = (key === 'project' ? a.project ?? '' : a[key]).toLowerCase();
  const bv = (key === 'project' ? b.project ?? '' : b[key]).toLowerCase();
  const cmp = av.localeCompare(bv);
  return dir === 'asc' ? cmp : -cmp;
}

const COOKIE_BADGE: Record<string, { variant: 'success' | 'warning' | 'error' | 'secondary'; label: string }> = {
  valid: { variant: 'success', label: 'Valid' },
  expired: { variant: 'warning', label: 'Expired' },
  missing: { variant: 'secondary', label: 'Missing' },
  corrupt: { variant: 'error', label: 'Corrupt' },
};

export interface RosterTabProps {
  rosterPages: RosterPage[];
  rosterLoading: boolean;
  syncing: boolean;
  postizConfigured: boolean;

  // Sort and filter
  sortKey: SortKey;
  sortDir: SortDir;
  onToggleSort: (key: SortKey) => void;
  filterProject: string;
  onFilterProjectChange: (v: string) => void;
  projectNames: string[];

  // Active project
  activeProjectName: string | null;

  // Email
  emailStatus: EmailStatusResponse | null;
  destinations: EmailDestination[];
  verifiedDestinations: EmailDestination[];
  showDestModal: boolean;
  onShowDestModal: (v: boolean) => void;
  creatingEmailFor: string | null;
  onCreateEmailAlias: (page: RosterPage) => void;
  onDeleteEmailAlias: (page: RosterPage) => void;
  newDestEmail: string;
  onNewDestEmailChange: (v: string) => void;
  addingDest: boolean;
  onAddDestination: () => void;
  onRefreshEmailStatus: () => void;

  // Cookies
  cookieStatuses: Record<string, string>;
  loggingIn: string | null;
  onTriggerLogin: (accountName: string) => void;

  // Drive
  driveStatus: DriveStatusResponse | null;
  driveInventory: Record<string, number>;

  // Inline edit
  editingCell: { id: string; field: 'drive_folder_url' } | null;
  editValue: string;
  savingId: string | null;
  onStartEdit: (page: RosterPage) => void;
  onCancelEdit: () => void;
  onCommitEdit: () => void;
  onEditValueChange: (v: string) => void;

  // Upload
  uploadStats: UploadQueueStats | null;
  queuedPerAccount: Record<string, number>;
  onOpenUploadForm: (page: RosterPage) => void;

  // Actions
  onAssignProject: (integrationId: string, project: string | null) => void;
  onRefreshRoster: () => void;
}

export function RosterTab({
  rosterPages,
  rosterLoading,
  syncing,
  postizConfigured,
  sortKey,
  sortDir,
  onToggleSort,
  filterProject,
  onFilterProjectChange,
  projectNames,
  activeProjectName,
  emailStatus,
  destinations,
  verifiedDestinations,
  showDestModal,
  onShowDestModal,
  creatingEmailFor,
  onCreateEmailAlias,
  onDeleteEmailAlias,
  newDestEmail,
  onNewDestEmailChange,
  addingDest,
  onAddDestination,
  onRefreshEmailStatus,
  cookieStatuses,
  loggingIn,
  onTriggerLogin,
  driveStatus,
  driveInventory,
  editingCell,
  editValue,
  savingId,
  onStartEdit,
  onCancelEdit,
  onCommitEdit,
  onEditValueChange,
  uploadStats,
  queuedPerAccount,
  onOpenUploadForm,
  onAssignProject,
  onRefreshRoster,
}: RosterTabProps) {
  const sortArrow = (key: SortKey) => {
    if (sortKey !== key) return <CaretUpDownIcon size={12} weight="bold" className="text-muted-foreground/40 ml-1 inline" />;
    return <span className="ml-1 inline-flex">{sortDir === 'asc' ? <CaretUpIcon size={12} weight="bold" /> : <CaretDownIcon size={12} weight="bold" />}</span>;
  };

  const displayPages = useMemo(() => {
    let pages = [...rosterPages];
    if (filterProject === 'unassigned') {
      pages = pages.filter((p) => !p.project);
    } else if (filterProject !== 'all') {
      pages = pages.filter((p) => p.project === filterProject);
    }
    pages.sort((a, b) => comparePages(a, b, sortKey, sortDir));
    return pages;
  }, [rosterPages, filterProject, sortKey, sortDir]);

  return (
    <div className="space-y-4">
      {/* Not configured warning */}
      {!postizConfigured && (
        <div className="rounded-[var(--border-radius)] border border-amber-500/30 bg-amber-500/10 text-amber-200 px-4 py-3 text-sm">
          <strong>POSTIZ_API_KEY</strong> is not set in your <code>.env</code> file. Add it to enable publishing.
        </div>
      )}

      {postizConfigured && (
        <div className="space-y-4">
          {/* Toolbar */}
          <div className="flex items-center justify-between rounded-[var(--border-radius)] border border-border bg-muted px-4 py-3">
            <div className="flex items-center gap-4 text-sm">
              <span>
                <span className="font-bold">{rosterPages.length}</span> page{rosterPages.length !== 1 ? 's' : ''}
              </span>
              <span className="text-muted-foreground">
                {rosterPages.filter((p) => p.project).length} assigned
              </span>
              {uploadStats && (uploadStats.queued > 0 || uploadStats.uploading > 0) && (
                <span className="text-muted-foreground">
                  {uploadStats.queued + uploadStats.uploading} in queue
                </span>
              )}
              {syncing && <Badge variant="info">Syncing...</Badge>}
            </div>
            <div className="flex items-center gap-2">
              {emailStatus?.configured && (
                <Button variant="outline" size="sm" onClick={() => onShowDestModal(true)}>
                  Destinations ({verifiedDestinations.length})
                </Button>
              )}
              <label className="text-xs font-bold text-muted-foreground">Filter:</label>
              <select
                className="rounded-[var(--border-radius)] border border-border bg-card px-2 py-1 text-xs font-bold"
                value={filterProject}
                onChange={(e) => onFilterProjectChange(e.target.value)}
              >
                <option value="all">All Pages</option>
                <option value="unassigned">Unassigned</option>
                {projectNames.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
              <Button variant="outline" size="sm" onClick={onRefreshRoster} disabled={rosterLoading}>
                {rosterLoading ? 'Loading...' : 'Refresh'}
              </Button>
            </div>
          </div>

          {/* Empty state */}
          {rosterPages.length === 0 && !rosterLoading && !syncing && (
            <div className="rounded-[var(--border-radius)] border border-border bg-muted px-4 py-8 text-center text-muted-foreground">
              No pages found. Make sure Postiz is reachable — pages sync automatically.
            </div>
          )}

          {/* Data table */}
          {displayPages.length > 0 && (
            <div className="rounded-[var(--border-radius)] border border-border bg-card shadow-[var(--shadow)] overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b-2 border-border bg-muted text-left">
                    <th className="px-3 py-2 w-10"></th>
                    <th className="px-3 py-2 font-bold cursor-pointer select-none hover:text-primary transition-colors" onClick={() => onToggleSort('name')}>
                      Name {sortArrow('name')}
                    </th>
                    <th className="px-3 py-2 font-bold cursor-pointer select-none hover:text-primary transition-colors" onClick={() => onToggleSort('provider')}>
                      Provider {sortArrow('provider')}
                    </th>
                    <th className="px-3 py-2 font-bold cursor-pointer select-none hover:text-primary transition-colors" onClick={() => onToggleSort('project')}>
                      Project {sortArrow('project')}
                    </th>
                    <th className="px-3 py-2 font-bold">Drive Folder</th>
                    <th className="px-3 py-2 font-bold">Email</th>
                    <th className="px-3 py-2 font-bold">Cookie</th>
                    <th className="px-3 py-2 font-bold">Queue</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {displayPages.map((page) => {
                    const isEditing = editingCell?.id === page.integration_id;
                    const isSaving = savingId === page.integration_id;
                    const isActive = page.project === activeProjectName;
                    const isCreatingEmail = creatingEmailFor === page.integration_id;
                    const cookieStatus = cookieStatuses[page.name] ?? 'missing';
                    const cookieBadge = COOKIE_BADGE[cookieStatus] ?? COOKIE_BADGE.missing;
                    const isLoggingIn = loggingIn === page.name;
                    const queueCount = queuedPerAccount[page.name] ?? 0;
                    const driveCount = driveInventory[page.integration_id] ?? 0;

                    return (
                      <tr
                        key={page.integration_id}
                        className={`transition-colors ${isActive ? 'bg-primary/5' : 'hover:bg-muted/50'}`}
                      >
                        <td className="px-3 py-2">
                          {page.picture ? (
                            <img src={page.picture} alt="" className="h-7 w-7 rounded-full border border-border" />
                          ) : (
                            <div className="h-7 w-7 rounded-full border border-border bg-muted" />
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <span className="font-bold block truncate max-w-[180px]">{page.name}</span>
                        </td>
                        <td className="px-3 py-2">
                          <Badge variant="secondary" className="text-[10px]">{page.provider}</Badge>
                        </td>
                        <td className="px-3 py-2">
                          <select
                            className="rounded-[var(--border-radius)] border border-border bg-card px-2 py-1 text-xs font-bold w-full min-w-[120px]"
                            value={page.project ?? ''}
                            onChange={(e) => onAssignProject(page.integration_id, e.target.value || null)}
                          >
                            <option value="">Unassigned</option>
                            {projectNames.map((name) => (
                              <option key={name} value={name}>{name}</option>
                            ))}
                          </select>
                        </td>
                        <td className="px-3 py-2 min-w-[200px]">
                          {isEditing ? (
                            <div className="flex items-center gap-1">
                              <Input
                                type="text"
                                className="text-xs h-7 flex-1"
                                placeholder="Paste Drive folder URL..."
                                value={editValue}
                                onChange={(e) => onEditValueChange(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') onCommitEdit();
                                  if (e.key === 'Escape') onCancelEdit();
                                }}
                                disabled={isSaving}
                                autoFocus
                              />
                              <Button size="xs" onClick={onCommitEdit} disabled={isSaving}>
                                {isSaving ? '...' : <CheckIcon size={12} weight="bold" />}
                              </Button>
                              <Button size="xs" variant="outline" onClick={onCancelEdit} disabled={isSaving}>
                                <XIcon size={12} weight="bold" />
                              </Button>
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5 cursor-pointer group" onClick={() => onStartEdit(page)}>
                              {page.drive_folder_id ? (
                                <>
                                  <Badge variant="success" className="text-[10px] shrink-0">Linked</Badge>
                                  <span className="text-[10px] text-muted-foreground font-mono truncate max-w-[120px]">
                                    {page.drive_folder_id}
                                  </span>
                                </>
                              ) : (
                                <span className="text-xs text-muted-foreground group-hover:text-primary transition-colors">
                                  Click to link...
                                </span>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 min-w-[180px]">
                          {page.email_alias ? (
                            <div className="flex items-center gap-1.5">
                              <span className="text-xs font-mono truncate max-w-[140px]">{page.email_alias}</span>
                              <button
                                type="button"
                                className="text-muted-foreground hover:text-destructive transition-colors shrink-0"
                                onClick={() => onDeleteEmailAlias(page)}
                                title="Remove alias"
                              >
                                <XIcon size={10} weight="bold" />
                              </button>
                            </div>
                          ) : emailStatus?.configured ? (
                            <Button
                              size="xs"
                              variant="outline"
                              className="text-[10px] h-6"
                              onClick={() => onCreateEmailAlias(page)}
                              disabled={isCreatingEmail}
                            >
                              {isCreatingEmail ? '...' : 'Create'}
                            </Button>
                          ) : (
                            <span className="text-xs text-muted-foreground/40">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-1.5">
                            <Badge variant={cookieBadge.variant} className="text-[10px]">{cookieBadge.label}</Badge>
                            {(cookieStatus === 'missing' || cookieStatus === 'expired') && (
                              <Button
                                size="xs"
                                variant="outline"
                                className="text-[10px] h-5 px-1.5"
                                onClick={() => onTriggerLogin(page.name)}
                                disabled={isLoggingIn}
                              >
                                {isLoggingIn ? '...' : 'Login'}
                              </Button>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-1.5">
                            {page.drive_folder_id && driveStatus?.configured ? (
                              <Badge
                                variant={driveCount > 0 ? 'info' : 'secondary'}
                                className="text-[10px]"
                                title={`${driveCount} video${driveCount !== 1 ? 's' : ''} in Drive`}
                              >
                                {driveCount}
                              </Badge>
                            ) : (
                              <span className="text-xs text-muted-foreground/40">—</span>
                            )}
                            {queueCount > 0 && (
                              <Badge variant="active" className="text-[10px]" title="Uploading to TikTok">{queueCount}</Badge>
                            )}
                            {cookieStatus === 'valid' && (
                              <Button
                                size="xs"
                                variant="outline"
                                className="text-[10px] h-5 px-1.5"
                                onClick={() => onOpenUploadForm(page)}
                              >
                                Upload
                              </Button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Destination Address Modal */}
      {showDestModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-md rounded-[var(--border-radius)] border border-border bg-card shadow-[0_24px_64px_rgba(0,0,0,0.65)] p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-heading font-bold">Destination Addresses</h2>
              <button type="button" className="text-muted-foreground hover:text-foreground" onClick={() => onShowDestModal(false)}>
                <XIcon size={18} weight="bold" />
              </button>
            </div>

            <p className="text-sm text-muted-foreground">
              Emails forwarded to these verified addresses. CF sends a verification link when you add one.
            </p>

            <div className="space-y-2">
              {destinations.length === 0 ? (
                <div className="text-sm text-muted-foreground text-center py-3">No destinations yet.</div>
              ) : (
                destinations.map((dest) => (
                  <div key={dest.id} className="flex items-center justify-between rounded-[var(--border-radius)] border border-border px-3 py-2">
                    <span className="text-sm font-mono">{dest.email}</span>
                    <Badge variant={dest.verified ? 'success' : 'warning'} className="text-[10px]">
                      {dest.verified ? 'Verified' : 'Pending'}
                    </Badge>
                  </div>
                ))
              )}
            </div>

            <div className="flex items-center gap-2">
              <Input
                type="email"
                placeholder="Add destination email..."
                className="text-sm h-9 flex-1"
                value={newDestEmail}
                onChange={(e) => onNewDestEmailChange(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') onAddDestination(); }}
                disabled={addingDest}
              />
              <Button size="sm" onClick={onAddDestination} disabled={addingDest || !newDestEmail.trim()}>
                {addingDest ? '...' : 'Add'}
              </Button>
            </div>

            <div className="flex justify-end">
              <Button variant="outline" size="sm" onClick={onRefreshEmailStatus}>Refresh</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
