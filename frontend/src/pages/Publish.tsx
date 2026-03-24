import { useCallback, useEffect, useMemo, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { apiUrl } from '../lib/api';
import type { RosterPage, PostizStatusResponse } from '../types/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

// ── Component ──────────────────────────────────────────────────────────────

export function PublishPage() {
  const {
    activeProjectName,
    projectStats,
    rosterPages,
    setRosterPages,
    rosterLoading,
    setRosterLoading,
    addNotification,
  } = useWorkflowStore();

  const [postizStatus, setPostizStatus] = useState<PostizStatusResponse | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [editingDrive, setEditingDrive] = useState<Record<string, string>>({});
  const [savingDrive, setSavingDrive] = useState<Record<string, boolean>>({});

  const projectNames = useMemo(() => Object.keys(projectStats).sort(), [projectStats]);

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

  // ── Fetch roster ────────────────────────────────────────────────────────

  const fetchRoster = useCallback(async () => {
    setRosterLoading(true);
    try {
      const resp = await fetch(apiUrl('/api/roster/'));
      if (!resp.ok) throw new Error(`Failed (${resp.status})`);
      const data = await resp.json();
      setRosterPages(data.pages ?? []);
    } catch {
      setRosterPages([]);
    } finally {
      setRosterLoading(false);
    }
  }, [setRosterPages, setRosterLoading]);

  // ── Sync from Postiz ───────────────────────────────────────────────────

  const syncFromPostiz = useCallback(async () => {
    setSyncing(true);
    try {
      const resp = await fetch(apiUrl('/api/roster/sync'), { method: 'POST' });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Sync failed (${resp.status})`);
      }
      const data = await resp.json();
      setRosterPages(data.pages ?? []);
      addNotification('success', `Synced: ${data.added} added, ${data.removed} removed from Postiz`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Sync failed';
      addNotification('error', msg);
    } finally {
      setSyncing(false);
    }
  }, [setRosterPages, addNotification]);

  // ── Assign page to project ─────────────────────────────────────────────

  const assignProject = useCallback(
    async (integrationId: string, project: string | null) => {
      try {
        const resp = await fetch(apiUrl(`/api/roster/${integrationId}`), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project }),
        });
        if (!resp.ok) throw new Error(`Failed (${resp.status})`);
        const data = await resp.json();
        setRosterPages(
          rosterPages.map((p) => (p.integration_id === integrationId ? data.page : p)),
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Assignment failed';
        addNotification('error', msg);
      }
    },
    [rosterPages, setRosterPages, addNotification],
  );

  // ── Save Drive folder URL ──────────────────────────────────────────────

  const saveDriveFolder = useCallback(
    async (integrationId: string) => {
      const url = editingDrive[integrationId] ?? '';
      setSavingDrive((prev) => ({ ...prev, [integrationId]: true }));
      try {
        const resp = await fetch(apiUrl(`/api/roster/${integrationId}`), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ drive_folder_url: url }),
        });
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `Failed (${resp.status})`);
        }
        const data = await resp.json();
        setRosterPages(
          rosterPages.map((p) => (p.integration_id === integrationId ? data.page : p)),
        );
        setEditingDrive((prev) => {
          const next = { ...prev };
          delete next[integrationId];
          return next;
        });
        addNotification('success', 'Drive folder linked');
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to save';
        addNotification('error', msg);
      } finally {
        setSavingDrive((prev) => ({ ...prev, [integrationId]: false }));
      }
    },
    [editingDrive, rosterPages, setRosterPages, addNotification],
  );

  // ── Init ──────────────────────────────────────────────────────────────

  useEffect(() => {
    void fetchStatus();
    void fetchRoster();
  }, [fetchStatus, fetchRoster]);

  // ── Group pages by project ─────────────────────────────────────────────

  const { assignedPages, unassignedPages } = useMemo(() => {
    const assigned: Record<string, RosterPage[]> = {};
    const unassigned: RosterPage[] = [];

    for (const page of rosterPages) {
      if (page.project) {
        if (!assigned[page.project]) assigned[page.project] = [];
        assigned[page.project].push(page);
      } else {
        unassigned.push(page);
      }
    }
    return { assignedPages: assigned, unassignedPages: unassigned };
  }, [rosterPages]);

  // ── Render ────────────────────────────────────────────────────────────

  return (
    <div className="p-4 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-heading font-bold">Publish</h1>
          <p className="text-sm text-muted-foreground">
            Manage pages, link Drive folders, and schedule content via Postiz
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
          <Button variant="outline" size="sm" onClick={fetchRoster} disabled={rosterLoading}>
            {rosterLoading ? 'Loading...' : 'Refresh'}
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
        <div className="space-y-6">
          {/* Sync bar */}
          <div className="flex items-center justify-between rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-3">
            <div className="text-sm">
              <span className="font-bold">{rosterPages.length}</span> page(s) in roster
              {rosterPages.filter((p) => p.project).length > 0 && (
                <span className="text-muted-foreground ml-2">
                  ({rosterPages.filter((p) => p.project).length} assigned)
                </span>
              )}
            </div>
            <Button onClick={syncFromPostiz} disabled={syncing} size="sm">
              {syncing ? 'Syncing...' : 'Sync from Postiz'}
            </Button>
          </div>

          {/* Roster: Pages grouped by project */}
          {rosterPages.length === 0 && !rosterLoading && (
            <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-8 text-center text-muted-foreground">
              No pages found. Click "Sync from Postiz" to import your connected accounts.
            </div>
          )}

          {/* Assigned pages — one section per project */}
          {Object.entries(assignedPages)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([projectName, pages]) => (
              <PageGroup
                key={projectName}
                title={projectName}
                pages={pages}
                projectNames={projectNames}
                onAssignProject={assignProject}
                editingDrive={editingDrive}
                setEditingDrive={setEditingDrive}
                savingDrive={savingDrive}
                onSaveDrive={saveDriveFolder}
                isActiveProject={projectName === activeProjectName}
              />
            ))}

          {/* Unassigned pages */}
          {unassignedPages.length > 0 && (
            <PageGroup
              title="Unassigned"
              pages={unassignedPages}
              projectNames={projectNames}
              onAssignProject={assignProject}
              editingDrive={editingDrive}
              setEditingDrive={setEditingDrive}
              savingDrive={savingDrive}
              onSaveDrive={saveDriveFolder}
              isActiveProject={false}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ── PageGroup Component ──────────────────────────────────────────────────

interface PageGroupProps {
  title: string;
  pages: RosterPage[];
  projectNames: string[];
  onAssignProject: (integrationId: string, project: string | null) => void;
  editingDrive: Record<string, string>;
  setEditingDrive: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  savingDrive: Record<string, boolean>;
  onSaveDrive: (integrationId: string) => void;
  isActiveProject: boolean;
}

function PageGroup({
  title,
  pages,
  projectNames,
  onAssignProject,
  editingDrive,
  setEditingDrive,
  savingDrive,
  onSaveDrive,
  isActiveProject,
}: PageGroupProps) {
  return (
    <div
      className={`rounded-[var(--border-radius)] border-2 shadow-[2px_2px_0_0_var(--border)] ${
        isActiveProject ? 'border-primary bg-primary/5' : 'border-border bg-card'
      }`}
    >
      <div className="flex items-center justify-between border-b-2 border-border px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-bold font-mono">{title}</span>
          <Badge variant="secondary">{pages.length} pages</Badge>
          {isActiveProject && (
            <Badge variant="default" className="text-[10px]">active project</Badge>
          )}
        </div>
      </div>

      <div className="divide-y divide-border">
        {pages.map((page) => (
          <PageRow
            key={page.integration_id}
            page={page}
            projectNames={projectNames}
            onAssignProject={onAssignProject}
            editingDrive={editingDrive}
            setEditingDrive={setEditingDrive}
            savingDrive={savingDrive}
            onSaveDrive={onSaveDrive}
          />
        ))}
      </div>
    </div>
  );
}

// ── PageRow Component ────────────────────────────────────────────────────

interface PageRowProps {
  page: RosterPage;
  projectNames: string[];
  onAssignProject: (integrationId: string, project: string | null) => void;
  editingDrive: Record<string, string>;
  setEditingDrive: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  savingDrive: Record<string, boolean>;
  onSaveDrive: (integrationId: string) => void;
}

function PageRow({
  page,
  projectNames,
  onAssignProject,
  editingDrive,
  setEditingDrive,
  savingDrive,
  onSaveDrive,
}: PageRowProps) {
  const isEditingDrive = page.integration_id in editingDrive;
  const driveValue = isEditingDrive
    ? editingDrive[page.integration_id]
    : page.drive_folder_url ?? '';

  return (
    <div className="px-4 py-3 space-y-2">
      {/* Row 1: Page info + project assignment */}
      <div className="flex items-center gap-3">
        {page.picture && (
          <img src={page.picture} alt="" className="h-8 w-8 rounded-full border border-border" />
        )}
        <div className="flex-1 min-w-0">
          <span className="text-sm font-bold block truncate">{page.name}</span>
          <div className="flex items-center gap-1">
            <Badge variant="secondary" className="text-[10px]">
              {page.provider}
            </Badge>
            <span className="text-[10px] text-muted-foreground font-mono">
              {page.integration_id.slice(0, 12)}...
            </span>
          </div>
        </div>

        {/* Project assignment dropdown */}
        <select
          className="rounded-[var(--border-radius)] border-2 border-border bg-card px-2 py-1 text-xs font-bold min-w-[140px]"
          value={page.project ?? ''}
          onChange={(e) => onAssignProject(page.integration_id, e.target.value || null)}
        >
          <option value="">Unassigned</option>
          {projectNames.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      {/* Row 2: Drive folder link */}
      <div className="flex items-center gap-2">
        <div className="flex-1">
          <Input
            type="text"
            placeholder="Paste Google Drive folder URL..."
            className="text-xs h-8"
            value={driveValue}
            onChange={(e) =>
              setEditingDrive((prev) => ({
                ...prev,
                [page.integration_id]: e.target.value,
              }))
            }
          />
        </div>
        {page.drive_folder_id && !isEditingDrive && (
          <Badge variant="success" className="text-[10px] shrink-0">
            Drive linked
          </Badge>
        )}
        {isEditingDrive && (
          <div className="flex gap-1 shrink-0">
            <Button
              size="sm"
              className="h-8 text-xs"
              onClick={() => onSaveDrive(page.integration_id)}
              disabled={savingDrive[page.integration_id]}
            >
              {savingDrive[page.integration_id] ? 'Saving...' : 'Save'}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-8 text-xs"
              onClick={() =>
                setEditingDrive((prev) => {
                  const next = { ...prev };
                  delete next[page.integration_id];
                  return next;
                })
              }
            >
              Cancel
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
