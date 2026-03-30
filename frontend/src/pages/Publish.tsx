import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useWorkflowStore } from '../stores/workflowStore';
import { apiUrl } from '../lib/api';
import type {
  RosterPage,
  PostizStatusResponse,
  EmailStatusResponse,
  EmailDestination,
  AutoCreateEmailResponse,
  UploadJob,
  CookieStatus,
  DriveStatusResponse,
} from '../types/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

// ── Sort helpers ────────────────────────────────────────────────────────────

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

// ── Component ───────────────────────────────────────────────────────────────

export function PublishPage() {
  const location = useLocation();
  const isVisible = location.pathname === '/publish';

  const {
    activeProjectName,
    projectStats,
    rosterPages,
    setRosterPages,
    rosterLoading,
    setRosterLoading,
    addNotification,
    uploadJobs,
    setUploadJobs,
    uploadStats,
    setUploadStats,
    addUploadJob,
    updateUploadJob,
  } = useWorkflowStore();

  const [postizStatus, setPostizStatus] = useState<PostizStatusResponse | null>(null);
  const [syncing, setSyncing] = useState(false);
  const hasSyncedRef = useRef(false);

  // Email routing state
  const [emailStatus, setEmailStatus] = useState<EmailStatusResponse | null>(null);
  const [destinations, setDestinations] = useState<EmailDestination[]>([]);
  const [creatingEmailFor, setCreatingEmailFor] = useState<string | null>(null);
  const [showDestModal, setShowDestModal] = useState(false);
  const [newDestEmail, setNewDestEmail] = useState('');
  const [addingDest, setAddingDest] = useState(false);

  // Cookie state
  const [cookieStatuses, setCookieStatuses] = useState<Record<string, string>>({});
  const [loggingIn, setLoggingIn] = useState<string | null>(null);

  // Drive state
  const [driveStatus, setDriveStatus] = useState<DriveStatusResponse | null>(null);
  const [driveInventory, setDriveInventory] = useState<Record<string, number>>({});

  // Upload form state
  const [showUploadForm, setShowUploadForm] = useState(false);
  const [uploadTarget, setUploadTarget] = useState<RosterPage | null>(null);
  const [uploadDesc, setUploadDesc] = useState('');
  const [uploadHashtags, setUploadHashtags] = useState('');
  const [uploadSound, setUploadSound] = useState('');
  const [uploadSchedule, setUploadSchedule] = useState('');
  const [uploadStealth, setUploadStealth] = useState(true);
  const [submittingUpload, setSubmittingUpload] = useState(false);

  // Inline editing state
  const [editingCell, setEditingCell] = useState<{ id: string; field: 'drive_folder_url' } | null>(null);
  const [editValue, setEditValue] = useState('');
  const [savingId, setSavingId] = useState<string | null>(null);

  // Sort and filter
  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [filterProject, setFilterProject] = useState<string>('all');

  const projectNames = useMemo(() => Object.keys(projectStats).sort(), [projectStats]);

  const verifiedDestinations = useMemo(
    () => destinations.filter((d) => d.verified),
    [destinations],
  );

  // Per-account queued job counts
  const queuedPerAccount = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const job of uploadJobs) {
      if (job.status === 'queued' || job.status === 'uploading') {
        counts[job.account_name] = (counts[job.account_name] || 0) + 1;
      }
    }
    return counts;
  }, [uploadJobs]);

  // ── Fetch Postiz status ─────────────────────────────────────────────────

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/postiz/status'));
      const ct = resp.headers.get('content-type') ?? '';
      if (!ct.includes('application/json')) throw new Error('Not JSON');
      const data = (await resp.json()) as PostizStatusResponse;
      setPostizStatus(data);
      return data;
    } catch {
      const fallback: PostizStatusResponse = { configured: false, reachable: false };
      setPostizStatus(fallback);
      return fallback;
    }
  }, []);

  // ── Fetch email status + destinations ─────────────────────────────────────

  const fetchEmailStatus = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/email/status'));
      if (!resp.ok) throw new Error('Failed');
      const data = (await resp.json()) as EmailStatusResponse;
      setEmailStatus(data);
      if (data.configured) {
        const destResp = await fetch(apiUrl('/api/email/destinations'));
        if (destResp.ok) {
          const destData = await destResp.json();
          setDestinations(destData.destinations ?? []);
        }
      }
    } catch {
      setEmailStatus({ configured: false, domain: null });
    }
  }, []);

  // ── Fetch roster ──────────────────────────────────────────────────────────

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

  // ── Fetch cookies ─────────────────────────────────────────────────────────

  const fetchCookies = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/upload/cookies'));
      if (!resp.ok) return;
      const data = await resp.json();
      const statuses: Record<string, string> = {};
      for (const c of (data.cookies ?? []) as CookieStatus[]) {
        statuses[c.account] = c.status;
      }
      setCookieStatuses(statuses);
    } catch {
      // ignore
    }
  }, []);

  // ── Fetch Drive status + inventory ─────────────────────────────────────

  const fetchDriveStatus = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/drive/status'));
      if (!resp.ok) return;
      const data = (await resp.json()) as DriveStatusResponse;
      setDriveStatus(data);
      if (data.configured) {
        const invResp = await fetch(apiUrl('/api/drive/inventory'));
        if (invResp.ok) {
          const invData = await invResp.json();
          setDriveInventory(invData.inventory ?? {});
        }
      }
    } catch {
      setDriveStatus({ configured: false });
    }
  }, []);

  // ── Fetch upload jobs ─────────────────────────────────────────────────────

  const fetchUploadJobs = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/upload/jobs'));
      if (!resp.ok) return;
      const data = await resp.json();
      setUploadJobs(data.jobs ?? []);
      setUploadStats(data.stats ?? null);
    } catch {
      // ignore
    }
  }, [setUploadJobs, setUploadStats]);

  // ── Sync from Postiz (background) ────────────────────────────────────────

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
      if (data.added > 0 || data.removed > 0) {
        addNotification('success', `Synced: ${data.added} added, ${data.removed} removed`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Sync failed';
      addNotification('error', msg);
    } finally {
      setSyncing(false);
    }
  }, [setRosterPages, addNotification]);

  // ── Auto-sync on tab activation ──────────────────────────────────────────

  useEffect(() => {
    if (!isVisible) {
      hasSyncedRef.current = false;
      return;
    }
    if (hasSyncedRef.current) return;
    hasSyncedRef.current = true;

    void (async () => {
      const status = await fetchStatus();
      void fetchEmailStatus();
      void fetchCookies();
      void fetchUploadJobs();
      void fetchDriveStatus();
      if (status.configured && status.reachable) {
        await syncFromPostiz();
      } else {
        await fetchRoster();
      }
    })();
  }, [isVisible, fetchStatus, fetchEmailStatus, fetchCookies, fetchUploadJobs, fetchDriveStatus, fetchRoster, syncFromPostiz]);

  // ── Poll upload jobs while queue is active ────────────────────────────────

  useEffect(() => {
    if (!isVisible || !uploadStats?.queue_running) return;
    const interval = setInterval(() => void fetchUploadJobs(), 5000);
    return () => clearInterval(interval);
  }, [isVisible, uploadStats?.queue_running, fetchUploadJobs]);

  // ── Assign page to project ────────────────────────────────────────────────

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

  // ── Save Drive folder URL (inline edit) ──────────────────────────────────

  const saveDriveFolder = useCallback(
    async (integrationId: string, url: string) => {
      setSavingId(integrationId);
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
        setEditingCell(null);
        addNotification('success', 'Drive folder linked');
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to save';
        addNotification('error', msg);
      } finally {
        setSavingId(null);
      }
    },
    [rosterPages, setRosterPages, addNotification],
  );

  // ── Auto-create email alias ──────────────────────────────────────────────

  const createEmailAlias = useCallback(
    async (page: RosterPage) => {
      if (verifiedDestinations.length === 0) {
        addNotification('error', 'No verified destination addresses. Add one first.');
        setShowDestModal(true);
        return;
      }

      setCreatingEmailFor(page.integration_id);
      try {
        const resp = await fetch(apiUrl('/api/email/auto-create'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            integration_id: page.integration_id,
            account_name: page.name,
            destination: verifiedDestinations[0].email,
          }),
        });
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `Failed (${resp.status})`);
        }
        const data = (await resp.json()) as AutoCreateEmailResponse;
        setRosterPages(
          rosterPages.map((p) =>
            p.integration_id === page.integration_id ? data.page : p,
          ),
        );
        addNotification('success', `Email alias created: ${data.alias}`);
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to create alias';
        addNotification('error', msg);
      } finally {
        setCreatingEmailFor(null);
      }
    },
    [verifiedDestinations, rosterPages, setRosterPages, addNotification],
  );

  // ── Delete email alias ───────────────────────────────────────────────────

  const deleteEmailAlias = useCallback(
    async (page: RosterPage) => {
      if (!page.email_rule_id) return;
      try {
        const resp = await fetch(
          apiUrl(`/api/email/rules/${page.email_rule_id}?integration_id=${page.integration_id}`),
          { method: 'DELETE' },
        );
        if (!resp.ok) throw new Error('Failed to delete');
        setRosterPages(
          rosterPages.map((p) =>
            p.integration_id === page.integration_id
              ? { ...p, email_alias: null, email_rule_id: null, fwd_destination: null }
              : p,
          ),
        );
        addNotification('success', 'Email alias removed');
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to delete alias';
        addNotification('error', msg);
      }
    },
    [rosterPages, setRosterPages, addNotification],
  );

  // ── Add destination address ──────────────────────────────────────────────

  const addDestination = useCallback(async () => {
    if (!newDestEmail.trim()) return;
    setAddingDest(true);
    try {
      const resp = await fetch(apiUrl('/api/email/destinations'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: newDestEmail.trim() }),
      });
      if (!resp.ok) throw new Error('Failed');
      addNotification('success', `Verification email sent to ${newDestEmail.trim()}`);
      setNewDestEmail('');
      await fetchEmailStatus();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to add destination';
      addNotification('error', msg);
    } finally {
      setAddingDest(false);
    }
  }, [newDestEmail, addNotification, fetchEmailStatus]);

  // ── Trigger TikTok login ─────────────────────────────────────────────────

  const triggerLogin = useCallback(
    async (accountName: string) => {
      setLoggingIn(accountName);
      try {
        const resp = await fetch(apiUrl(`/api/upload/login/${accountName}`), {
          method: 'POST',
        });
        if (!resp.ok) throw new Error('Login failed');
        addNotification('info', `Browser opened for ${accountName}. Complete login manually.`);
        // Refresh cookie status after a delay
        setTimeout(() => void fetchCookies(), 10000);
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Login failed';
        addNotification('error', msg);
      } finally {
        setLoggingIn(null);
      }
    },
    [addNotification, fetchCookies],
  );

  // ── Submit upload ────────────────────────────────────────────────────────

  const submitUpload = useCallback(
    async (videoPath: string) => {
      if (!uploadTarget) return;
      setSubmittingUpload(true);
      try {
        const resp = await fetch(apiUrl('/api/upload/submit'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            video_path: videoPath,
            account_name: uploadTarget.name,
            description: uploadDesc,
            hashtags: uploadHashtags
              .split(',')
              .map((h) => h.trim())
              .filter(Boolean),
            sound_name: uploadSound || null,
            stealth: uploadStealth,
            headless: true,
          }),
        });
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || 'Submit failed');
        }
        const data = await resp.json();
        addUploadJob(data.job as UploadJob);
        addNotification('success', `Upload queued for ${uploadTarget.name}`);
        setShowUploadForm(false);
        setUploadDesc('');
        setUploadHashtags('');
        setUploadSound('');
        setUploadSchedule('');
        void fetchUploadJobs();
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Submit failed';
        addNotification('error', msg);
      } finally {
        setSubmittingUpload(false);
      }
    },
    [uploadTarget, uploadDesc, uploadHashtags, uploadSound, uploadStealth, addUploadJob, addNotification, fetchUploadJobs],
  );

  // ── Cancel upload job ────────────────────────────────────────────────────

  const cancelUploadJob = useCallback(
    async (jobId: string) => {
      try {
        const resp = await fetch(apiUrl(`/api/upload/jobs/${jobId}/cancel`), {
          method: 'POST',
        });
        if (!resp.ok) throw new Error('Cancel failed');
        const data = await resp.json();
        updateUploadJob(data.job as UploadJob);
        addNotification('success', 'Upload cancelled');
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Cancel failed';
        addNotification('error', msg);
      }
    },
    [updateUploadJob, addNotification],
  );

  // ── Inline edit handlers ─────────────────────────────────────────────────

  const startEdit = useCallback((page: RosterPage) => {
    setEditingCell({ id: page.integration_id, field: 'drive_folder_url' });
    setEditValue(page.drive_folder_url ?? '');
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingCell(null);
    setEditValue('');
  }, []);

  const commitEdit = useCallback(() => {
    if (!editingCell) return;
    void saveDriveFolder(editingCell.id, editValue);
  }, [editingCell, editValue, saveDriveFolder]);

  // ── Sort toggle ──────────────────────────────────────────────────────────

  const toggleSort = useCallback((key: SortKey) => {
    setSortKey((prev) => {
      if (prev === key) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
        return key;
      }
      setSortDir('asc');
      return key;
    });
  }, []);

  // ── Filtered + sorted pages ──────────────────────────────────────────────

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

  // ── Sort indicator ───────────────────────────────────────────────────────

  const sortArrow = (key: SortKey) => {
    if (sortKey !== key) return <span className="text-muted-foreground/40 ml-1">↕</span>;
    return <span className="ml-1">{sortDir === 'asc' ? '↑' : '↓'}</span>;
  };

  // ── Active/recent jobs for display ───────────────────────────────────────

  const activeJobs = useMemo(
    () => uploadJobs.filter((j) => j.status === 'queued' || j.status === 'uploading'),
    [uploadJobs],
  );

  const recentJobs = useMemo(
    () =>
      uploadJobs
        .filter((j) => j.status === 'completed' || j.status === 'failed')
        .slice(0, 10),
    [uploadJobs],
  );

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-heading font-bold">Publish</h1>
          <p className="text-sm text-muted-foreground">
            Manage pages, link Drive folders, and distribute content
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
          {driveStatus?.configured && (
            <Badge variant="success">Drive</Badge>
          )}
          {emailStatus?.configured && (
            <Badge variant="info">{emailStatus.domain}</Badge>
          )}
          {uploadStats?.queue_running && (
            <Badge variant="active">Queue Running</Badge>
          )}
          {syncing && <Badge variant="info">Syncing...</Badge>}
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

      {postizStatus?.configured && (
        <div className="space-y-4">
          {/* Toolbar: stats + filter + destinations */}
          <div className="flex items-center justify-between rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-3">
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
            </div>
            <div className="flex items-center gap-2">
              {emailStatus?.configured && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowDestModal(true)}
                >
                  Destinations ({verifiedDestinations.length})
                </Button>
              )}
              <label className="text-xs font-bold text-muted-foreground">Filter:</label>
              <select
                className="rounded-[var(--border-radius)] border-2 border-border bg-card px-2 py-1 text-xs font-bold"
                value={filterProject}
                onChange={(e) => setFilterProject(e.target.value)}
              >
                <option value="all">All Pages</option>
                <option value="unassigned">Unassigned</option>
                {projectNames.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Empty state */}
          {rosterPages.length === 0 && !rosterLoading && !syncing && (
            <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-8 text-center text-muted-foreground">
              No pages found. Make sure Postiz is reachable — pages sync automatically.
            </div>
          )}

          {/* Data table */}
          {displayPages.length > 0 && (
            <div className="rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[2px_2px_0_0_var(--border)] overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b-2 border-border bg-muted text-left">
                    <th className="px-3 py-2 w-10"></th>
                    <th
                      className="px-3 py-2 font-bold cursor-pointer select-none hover:text-primary transition-colors"
                      onClick={() => toggleSort('name')}
                    >
                      Name {sortArrow('name')}
                    </th>
                    <th
                      className="px-3 py-2 font-bold cursor-pointer select-none hover:text-primary transition-colors"
                      onClick={() => toggleSort('provider')}
                    >
                      Provider {sortArrow('provider')}
                    </th>
                    <th
                      className="px-3 py-2 font-bold cursor-pointer select-none hover:text-primary transition-colors"
                      onClick={() => toggleSort('project')}
                    >
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
                        {/* Avatar */}
                        <td className="px-3 py-2">
                          {page.picture ? (
                            <img
                              src={page.picture}
                              alt=""
                              className="h-7 w-7 rounded-full border border-border"
                            />
                          ) : (
                            <div className="h-7 w-7 rounded-full border border-border bg-muted" />
                          )}
                        </td>

                        {/* Name */}
                        <td className="px-3 py-2">
                          <span className="font-bold block truncate max-w-[180px]">{page.name}</span>
                        </td>

                        {/* Provider */}
                        <td className="px-3 py-2">
                          <Badge variant="secondary" className="text-[10px]">
                            {page.provider}
                          </Badge>
                        </td>

                        {/* Project (dropdown) */}
                        <td className="px-3 py-2">
                          <select
                            className="rounded-[var(--border-radius)] border-2 border-border bg-card px-2 py-1 text-xs font-bold w-full min-w-[120px]"
                            value={page.project ?? ''}
                            onChange={(e) =>
                              assignProject(page.integration_id, e.target.value || null)
                            }
                          >
                            <option value="">Unassigned</option>
                            {projectNames.map((name) => (
                              <option key={name} value={name}>{name}</option>
                            ))}
                          </select>
                        </td>

                        {/* Drive Folder (inline edit) */}
                        <td className="px-3 py-2 min-w-[200px]">
                          {isEditing ? (
                            <div className="flex items-center gap-1">
                              <Input
                                type="text"
                                className="text-xs h-7 flex-1"
                                placeholder="Paste Drive folder URL..."
                                value={editValue}
                                onChange={(e) => setEditValue(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') commitEdit();
                                  if (e.key === 'Escape') cancelEdit();
                                }}
                                disabled={isSaving}
                                autoFocus
                              />
                              <Button size="xs" onClick={commitEdit} disabled={isSaving}>
                                {isSaving ? '...' : '✓'}
                              </Button>
                              <Button size="xs" variant="outline" onClick={cancelEdit} disabled={isSaving}>
                                ✕
                              </Button>
                            </div>
                          ) : (
                            <div
                              className="flex items-center gap-1.5 cursor-pointer group"
                              onClick={() => startEdit(page)}
                            >
                              {page.drive_folder_id ? (
                                <>
                                  <Badge variant="success" className="text-[10px] shrink-0">
                                    Linked
                                  </Badge>
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

                        {/* Email */}
                        <td className="px-3 py-2 min-w-[180px]">
                          {page.email_alias ? (
                            <div className="flex items-center gap-1.5">
                              <span className="text-xs font-mono truncate max-w-[140px]">
                                {page.email_alias}
                              </span>
                              <button
                                type="button"
                                className="text-[10px] text-muted-foreground hover:text-destructive transition-colors shrink-0"
                                onClick={() => deleteEmailAlias(page)}
                                title="Remove alias"
                              >
                                ✕
                              </button>
                            </div>
                          ) : emailStatus?.configured ? (
                            <Button
                              size="xs"
                              variant="outline"
                              className="text-[10px] h-6"
                              onClick={() => createEmailAlias(page)}
                              disabled={isCreatingEmail}
                            >
                              {isCreatingEmail ? '...' : 'Create'}
                            </Button>
                          ) : (
                            <span className="text-xs text-muted-foreground/40">—</span>
                          )}
                        </td>

                        {/* Cookie Status */}
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-1.5">
                            <Badge variant={cookieBadge.variant} className="text-[10px]">
                              {cookieBadge.label}
                            </Badge>
                            {(cookieStatus === 'missing' || cookieStatus === 'expired') && (
                              <Button
                                size="xs"
                                variant="outline"
                                className="text-[10px] h-5 px-1.5"
                                onClick={() => triggerLogin(page.name)}
                                disabled={isLoggingIn}
                              >
                                {isLoggingIn ? '...' : 'Login'}
                              </Button>
                            )}
                          </div>
                        </td>

                        {/* Queue (Drive files + upload queue) */}
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
                              <Badge variant="active" className="text-[10px]" title="Uploading to TikTok">
                                {queueCount}
                              </Badge>
                            )}
                            {cookieStatus === 'valid' && (
                              <Button
                                size="xs"
                                variant="outline"
                                className="text-[10px] h-5 px-1.5"
                                onClick={() => {
                                  setUploadTarget(page);
                                  setShowUploadForm(true);
                                }}
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

          {/* Upload Queue Panel */}
          {(activeJobs.length > 0 || recentJobs.length > 0) && (
            <div className="rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[2px_2px_0_0_var(--border)] p-4 space-y-3">
              <h2 className="text-sm font-heading font-bold">Upload Queue</h2>

              {activeJobs.length > 0 && (
                <div className="space-y-2">
                  {activeJobs.map((job) => (
                    <div
                      key={job.job_id}
                      className="flex items-center justify-between rounded-[var(--border-radius)] border-2 border-border px-3 py-2"
                    >
                      <div className="flex items-center gap-2">
                        <Badge
                          variant={job.status === 'uploading' ? 'active' : 'secondary'}
                          className="text-[10px]"
                        >
                          {job.status}
                        </Badge>
                        <span className="text-xs font-bold">{job.account_name}</span>
                        <span className="text-[10px] text-muted-foreground truncate max-w-[200px]">
                          {job.video_path.split('/').pop()}
                        </span>
                      </div>
                      {job.status === 'queued' && (
                        <Button
                          size="xs"
                          variant="outline"
                          className="text-[10px] h-5"
                          onClick={() => cancelUploadJob(job.job_id)}
                        >
                          Cancel
                        </Button>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {recentJobs.length > 0 && (
                <div className="space-y-1">
                  <h3 className="text-[10px] font-bold text-muted-foreground uppercase">Recent</h3>
                  {recentJobs.map((job) => (
                    <div
                      key={job.job_id}
                      className="flex items-center gap-2 text-[11px] text-muted-foreground"
                    >
                      <Badge
                        variant={job.status === 'completed' ? 'success' : 'error'}
                        className="text-[9px]"
                      >
                        {job.status}
                      </Badge>
                      <span className="font-bold">{job.account_name}</span>
                      <span className="truncate max-w-[200px]">
                        {job.video_path.split('/').pop()}
                      </span>
                      {job.error && (
                        <span className="text-destructive truncate max-w-[200px]" title={job.error}>
                          {job.error}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Upload Form Modal */}
      {showUploadForm && uploadTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-lg rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[4px_4px_0_0_var(--border)] p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-heading font-bold">
                Upload to {uploadTarget.name}
              </h2>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground text-lg"
                onClick={() => setShowUploadForm(false)}
              >
                ✕
              </button>
            </div>

            <div className="space-y-3">
              <div>
                <label className="text-xs font-bold text-muted-foreground block mb-1">Video Path</label>
                <Input
                  type="text"
                  id="upload-video-path"
                  placeholder="projects/myproject/burned/batch_001/video.mp4"
                  className="text-sm"
                />
              </div>

              <div>
                <label className="text-xs font-bold text-muted-foreground block mb-1">Description</label>
                <textarea
                  className="w-full rounded-[var(--border-radius)] border-2 border-border bg-transparent px-3 py-2 text-sm resize-none h-20"
                  placeholder="Caption text..."
                  value={uploadDesc}
                  onChange={(e) => setUploadDesc(e.target.value)}
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-bold text-muted-foreground block mb-1">Hashtags (comma-separated)</label>
                  <Input
                    type="text"
                    placeholder="#fyp, #viral"
                    className="text-sm"
                    value={uploadHashtags}
                    onChange={(e) => setUploadHashtags(e.target.value)}
                  />
                </div>
                <div>
                  <label className="text-xs font-bold text-muted-foreground block mb-1">Sound Name (optional)</label>
                  <Input
                    type="text"
                    placeholder="trending_sound"
                    className="text-sm"
                    value={uploadSound}
                    onChange={(e) => setUploadSound(e.target.value)}
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-bold text-muted-foreground block mb-1">Schedule (HH:MM, optional)</label>
                  <Input
                    type="text"
                    placeholder="15:00"
                    className="text-sm"
                    value={uploadSchedule}
                    onChange={(e) => setUploadSchedule(e.target.value)}
                  />
                </div>
                <div className="flex items-end pb-1">
                  <label className="flex items-center gap-2 text-sm cursor-pointer">
                    <input
                      type="checkbox"
                      checked={uploadStealth}
                      onChange={(e) => setUploadStealth(e.target.checked)}
                      className="rounded border-2 border-border"
                    />
                    <span className="text-xs font-bold">Stealth Mode</span>
                  </label>
                </div>
              </div>
            </div>

            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setShowUploadForm(false)}>
                Cancel
              </Button>
              <Button
                size="sm"
                disabled={submittingUpload}
                onClick={() => {
                  const videoInput = document.getElementById('upload-video-path') as HTMLInputElement;
                  if (videoInput?.value) {
                    void submitUpload(videoInput.value);
                  }
                }}
              >
                {submittingUpload ? 'Submitting...' : 'Queue Upload'}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Destination Address Modal */}
      {showDestModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-md rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[4px_4px_0_0_var(--border)] p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-heading font-bold">Destination Addresses</h2>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground text-lg"
                onClick={() => setShowDestModal(false)}
              >
                ✕
              </button>
            </div>

            <p className="text-sm text-muted-foreground">
              Emails forwarded to these verified addresses. CF sends a verification link when you add one.
            </p>

            {/* Existing destinations */}
            <div className="space-y-2">
              {destinations.length === 0 ? (
                <div className="text-sm text-muted-foreground text-center py-3">
                  No destinations yet.
                </div>
              ) : (
                destinations.map((dest) => (
                  <div
                    key={dest.id}
                    className="flex items-center justify-between rounded-[var(--border-radius)] border-2 border-border px-3 py-2"
                  >
                    <span className="text-sm font-mono">{dest.email}</span>
                    <Badge variant={dest.verified ? 'success' : 'warning'} className="text-[10px]">
                      {dest.verified ? 'Verified' : 'Pending'}
                    </Badge>
                  </div>
                ))
              )}
            </div>

            {/* Add new destination */}
            <div className="flex items-center gap-2">
              <Input
                type="email"
                placeholder="Add destination email..."
                className="text-sm h-9 flex-1"
                value={newDestEmail}
                onChange={(e) => setNewDestEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void addDestination();
                }}
                disabled={addingDest}
              />
              <Button size="sm" onClick={() => void addDestination()} disabled={addingDest || !newDestEmail.trim()}>
                {addingDest ? '...' : 'Add'}
              </Button>
            </div>

            <div className="flex justify-end">
              <Button variant="outline" size="sm" onClick={() => { void fetchEmailStatus(); }}>
                Refresh
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
