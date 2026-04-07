import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useWorkflowStore } from '@/stores/workflowStore';
import { apiUrl } from '@/lib/api';
import { TabNav } from '@/components/TabNav';
import type {
  TelegramStatus,
  TelegramPoster,
  TelegramSound,
  TelegramBatchResult,
  NotionSyncResult,
  RosterPage,
  PostizStatusResponse,
  EmailStatusResponse,
  EmailDestination,
  AutoCreateEmailResponse,
  UploadJob,
  CookieStatus,
  DriveStatusResponse,
} from '@/types/api';

import { StatusBar } from './distribution/StatusBar';
import { RosterTab } from './distribution/RosterTab';
import { TelegramTab } from './distribution/TelegramTab';
import { SoundsTab } from './distribution/SoundsTab';
import { UploadsTab } from './distribution/UploadsTab';

// ---------------------------------------------------------------------------
// Helpers (shared across tabs)
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

// Normalize name for dedup: strip emoji, lowercase, collapse whitespace
const normalizeName = (n: string) =>
  n.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FAFF}\u{2600}-\u{26FF}\u{FE0F}\u{200D}]+/gu, '')
    .replace(/\s+/g, ' ').toLowerCase().trim();

type SortKey = 'name' | 'provider' | 'project';
type SortDir = 'asc' | 'desc';

// Sub-tab definitions
const SUB_TABS = [
  { id: 'roster', label: 'Roster' },
  { id: 'telegram', label: 'Telegram' },
  { id: 'sounds', label: 'Sounds' },
  { id: 'uploads', label: 'Uploads' },
] as const;

type SubTabId = typeof SUB_TABS[number]['id'];

function pathToSubTab(pathname: string): SubTabId {
  if (pathname.startsWith('/distribution/telegram')) return 'telegram';
  if (pathname.startsWith('/distribution/sounds')) return 'sounds';
  if (pathname.startsWith('/distribution/uploads')) return 'uploads';
  return 'roster';
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DistributionPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const isVisible = location.pathname.startsWith('/distribution');

  const {
    activeProjectName,
    projectStats,
    addNotification,
    rosterPages: storeRosterPages,
    setRosterPages: storeSetRosterPages,
    rosterLoading,
    setRosterLoading,
    uploadJobs,
    setUploadJobs,
    uploadStats,
    setUploadStats,
    addUploadJob,
    updateUploadJob,
  } = useWorkflowStore();

  // =========================================================================
  // TELEGRAM STATE
  // =========================================================================

  const [tgStatus, setTgStatus] = useState<TelegramStatus | null>(null);
  const [posters, setPosters] = useState<TelegramPoster[]>([]);
  const [sounds, setSounds] = useState<TelegramSound[]>([]);
  // tgRosterPages kept in sync with the store via refreshTelegram
  const [, setTgRosterPages] = useState<RosterPage[]>([]);
  const [tgLoading, setTgLoading] = useState(true);
  const [tgError, setTgError] = useState<string | null>(null);

  const [tokenInput, setTokenInput] = useState('');
  const [chatIdInput, setChatIdInput] = useState('');
  const [syncResult, setSyncResult] = useState<{ created: number; existing: number } | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanProgress, setScanProgress] = useState('');
  const [scanResult, setScanResult] = useState<{ scanned_topics: number; total_found: number } | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [discoverProgress, setDiscoverProgress] = useState('');

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

  const [notionSyncing, setNotionSyncing] = useState(false);
  const [notionResult, setNotionResult] = useState<NotionSyncResult | null>(null);

  const [sendPage, setSendPage] = useState('');
  const [sendFilePath, setSendFilePath] = useState('');
  const [sendCaption, setSendCaption] = useState('');
  const [sendResult, setSendResult] = useState<string | null>(null);

  // =========================================================================
  // PUBLISH STATE
  // =========================================================================

  const [postizStatus, setPostizStatus] = useState<PostizStatusResponse | null>(null);
  const [syncing, setSyncing] = useState(false);
  const hasSyncedRef = useRef(false);

  const [emailStatus, setEmailStatus] = useState<EmailStatusResponse | null>(null);
  const [destinations, setDestinations] = useState<EmailDestination[]>([]);
  const [creatingEmailFor, setCreatingEmailFor] = useState<string | null>(null);
  const [showDestModal, setShowDestModal] = useState(false);
  const [newDestEmail, setNewDestEmail] = useState('');
  const [addingDest, setAddingDest] = useState(false);

  const [cookieStatuses, setCookieStatuses] = useState<Record<string, string>>({});
  const [loggingIn, setLoggingIn] = useState<string | null>(null);

  const [driveStatus, setDriveStatus] = useState<DriveStatusResponse | null>(null);
  const [driveInventory, setDriveInventory] = useState<Record<string, number>>({});

  const [showUploadForm, setShowUploadForm] = useState(false);
  const [uploadTarget, setUploadTarget] = useState<RosterPage | null>(null);
  const [uploadDesc, setUploadDesc] = useState('');
  const [uploadHashtags, setUploadHashtags] = useState('');
  const [uploadSound, setUploadSound] = useState('');
  const [uploadSchedule, setUploadSchedule] = useState('');
  const [uploadStealth, setUploadStealth] = useState(true);
  const [submittingUpload, setSubmittingUpload] = useState(false);

  const [editingCell, setEditingCell] = useState<{ id: string; field: 'drive_folder_url' } | null>(null);
  const [editValue, setEditValue] = useState('');
  const [savingId, setSavingId] = useState<string | null>(null);

  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [filterProject, setFilterProject] = useState<string>('all');

  // =========================================================================
  // DERIVED DATA
  // =========================================================================

  const projectNames = useMemo(() => Object.keys(projectStats).sort(), [projectStats]);

  const verifiedDestinations = useMemo(
    () => destinations.filter((d) => d.verified),
    [destinations],
  );

  const queuedPerAccount = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const job of uploadJobs) {
      if (job.status === 'queued' || job.status === 'uploading') {
        counts[job.account_name] = (counts[job.account_name] || 0) + 1;
      }
    }
    return counts;
  }, [uploadJobs]);

  // Use the store roster pages as the single source of truth
  const rosterPages = storeRosterPages;

  const stagingGroup = tgStatus?.staging_group ?? null;
  const topics = stagingGroup?.topics ?? {};

  const topicEntries = useMemo(() => {
    const all = Object.entries(topics);
    const seen = new Set<string>();
    return all.filter(([integrationId, topic]) => {
      const page = rosterPages.find((p) => p.integration_id === integrationId);
      const name = normalizeName(page?.name || (topic as { topic_name?: string }).topic_name || integrationId);
      if (seen.has(name)) return false;
      seen.add(name);
      return true;
    });
  }, [topics, rosterPages]);

  const pagesWithTopics = useMemo(() => {
    const all = rosterPages.filter((p) => topics[p.integration_id]);
    const seen = new Set<string>();
    return all.filter((p) => {
      const key = normalizeName(p.name || p.integration_id);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [rosterPages, topics]);

  // Sub-tab state
  const activeSubTab = pathToSubTab(location.pathname);
  const [visitedSubTabs, setVisitedSubTabs] = useState<Set<string>>(new Set(['roster']));

  useEffect(() => {
    setVisitedSubTabs((prev) => {
      if (prev.has(activeSubTab)) return prev;
      return new Set([...prev, activeSubTab]);
    });
  }, [activeSubTab]);

  // =========================================================================
  // TELEGRAM DATA FETCHING
  // =========================================================================

  const refreshTelegram = useCallback(async () => {
    try {
      const [statusData, postersData, soundsData, rosterData] = await Promise.all([
        fetchJson<TelegramStatus>(apiUrl('/api/telegram/status')),
        fetchJson<TelegramPoster[]>(apiUrl('/api/telegram/posters')).catch(() => [] as TelegramPoster[]),
        fetchJson<TelegramSound[]>(apiUrl('/api/telegram/sounds?active_only=false')).catch(() => [] as TelegramSound[]),
        fetchJson<{ pages: RosterPage[] }>(apiUrl('/api/roster/')).catch(() => ({ pages: [] as RosterPage[] })),
      ]);
      setTgStatus(statusData);
      setPosters(postersData);
      setSounds(soundsData);
      setTgRosterPages(rosterData.pages);
      // Also update store roster pages so the Roster tab stays in sync
      storeSetRosterPages(rosterData.pages);

      setScheduleEnabled(statusData.schedule.enabled);
      setForwardTime(statusData.schedule.forward_time || '09:00');
      setTimezone(statusData.schedule.timezone || 'America/New_York');

      setTgError(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load Telegram status';
      setTgError(msg);
    }
  }, [storeSetRosterPages]);

  // =========================================================================
  // PUBLISH DATA FETCHING
  // =========================================================================

  const fetchPostizStatus = useCallback(async () => {
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

  const fetchRoster = useCallback(async () => {
    setRosterLoading(true);
    try {
      const resp = await fetch(apiUrl('/api/roster/'));
      if (!resp.ok) throw new Error(`Failed (${resp.status})`);
      const data = await resp.json();
      storeSetRosterPages(data.pages ?? []);
    } catch {
      storeSetRosterPages([]);
    } finally {
      setRosterLoading(false);
    }
  }, [storeSetRosterPages, setRosterLoading]);

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
    } catch { /* ignore */ }
  }, []);

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

  const fetchUploadJobs = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/upload/jobs'));
      if (!resp.ok) return;
      const data = await resp.json();
      setUploadJobs(data.jobs ?? []);
      setUploadStats(data.stats ?? null);
    } catch { /* ignore */ }
  }, [setUploadJobs, setUploadStats]);

  const syncFromPostiz = useCallback(async () => {
    setSyncing(true);
    try {
      const resp = await fetch(apiUrl('/api/roster/sync'), { method: 'POST' });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Sync failed (${resp.status})`);
      }
      const data = await resp.json();
      storeSetRosterPages(data.pages ?? []);
      if (data.added > 0 || data.removed > 0) {
        addNotification('success', `Synced: ${data.added} added, ${data.removed} removed`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Sync failed';
      addNotification('error', msg);
    } finally {
      setSyncing(false);
    }
  }, [storeSetRosterPages, addNotification]);

  // =========================================================================
  // INITIAL LOAD
  // =========================================================================

  useEffect(() => {
    // Telegram initial load
    setTgLoading(true);
    refreshTelegram().finally(() => {
      setTgLoading(false);
      fetch(apiUrl('/api/telegram/sounds/sync'), { method: 'POST' })
        .then(() => refreshTelegram())
        .catch(() => {});
    });
  }, [refreshTelegram]);

  // Publish auto-sync on tab activation
  useEffect(() => {
    if (!isVisible) {
      hasSyncedRef.current = false;
      return;
    }
    if (hasSyncedRef.current) return;
    hasSyncedRef.current = true;

    void (async () => {
      const status = await fetchPostizStatus();
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
  }, [isVisible, fetchPostizStatus, fetchEmailStatus, fetchCookies, fetchUploadJobs, fetchDriveStatus, fetchRoster, syncFromPostiz]);

  // Poll upload jobs while queue is active
  useEffect(() => {
    if (!isVisible || !uploadStats?.queue_running) return;
    const interval = setInterval(() => void fetchUploadJobs(), 5000);
    return () => clearInterval(interval);
  }, [isVisible, uploadStats?.queue_running, fetchUploadJobs]);

  // =========================================================================
  // TELEGRAM HANDLERS
  // =========================================================================

  const handleSaveToken = useCallback(async () => {
    if (!tokenInput.trim()) return;
    try {
      await fetchOk(apiUrl('/api/telegram/bot-token'), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: tokenInput.trim() }),
      });
      setTokenInput('');
      addNotification('success', 'Bot token saved');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to save token'); }
  }, [tokenInput, addNotification, refreshTelegram]);

  const handleClearToken = useCallback(async () => {
    try {
      await fetchOk(apiUrl('/api/telegram/bot-token'), { method: 'DELETE' });
      addNotification('success', 'Bot token cleared');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to clear token'); }
  }, [addNotification, refreshTelegram]);

  const handleSetGroup = useCallback(async () => {
    const id = parseInt(chatIdInput, 10);
    if (isNaN(id)) return;
    try {
      await fetchOk(apiUrl('/api/telegram/staging-group'), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: id }),
      });
      setChatIdInput('');
      addNotification('success', 'Storage group connected');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to connect storage group'); }
  }, [chatIdInput, addNotification, refreshTelegram]);

  const handleSyncTopics = useCallback(async () => {
    try {
      const result = await fetchJson<{ created: number; existing: number }>(
        apiUrl('/api/telegram/staging-group/sync-topics'), { method: 'POST' },
      );
      setSyncResult(result);
      addNotification('success', `Page folders: ${result.created} created, ${result.existing} existing`);
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to set up page folders'); }
  }, [addNotification, refreshTelegram]);

  const handleForwardPending = useCallback(async (integrationId: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/forward/${encodeURIComponent(integrationId)}`), { method: 'POST' });
      addNotification('success', 'Forwarded pending content');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to forward'); }
  }, [addNotification, refreshTelegram]);

  const handleAddPoster = useCallback(async () => {
    const name = newPosterName.trim();
    const chatId = parseInt(newPosterChatId, 10);
    if (!name || isNaN(chatId)) return;
    try {
      await fetchOk(apiUrl('/api/telegram/posters'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, chat_id: chatId }),
      });
      setNewPosterName(''); setNewPosterChatId('');
      addNotification('success', `Poster "${name}" added`);
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to add poster'); }
  }, [newPosterName, newPosterChatId, addNotification, refreshTelegram]);

  const handleRemovePoster = useCallback(async (posterId: string) => {
    if (!confirm(`Remove poster "${posterId}"? This cannot be undone.`)) return;
    try {
      await fetchOk(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}`), { method: 'DELETE' });
      addNotification('success', 'Poster removed');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to remove poster'); }
  }, [addNotification, refreshTelegram]);

  const handleSyncPosterTopics = useCallback(async (posterId: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}/sync-topics`), { method: 'POST' });
      addNotification('success', 'Page folders created');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to set up page folders'); }
  }, [addNotification, refreshTelegram]);

  const handleForwardSounds = useCallback(async (posterId: string) => {
    try {
      const result = await fetchJson<{ sent: number }>(
        apiUrl(`/api/telegram/sounds/forward/${encodeURIComponent(posterId)}`), { method: 'POST' },
      );
      addNotification('success', `Sent ${result.sent} sound${result.sent !== 1 ? 's' : ''} to poster`);
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to send sounds'); }
  }, [addNotification]);

  const handleForwardSoundsAll = useCallback(async () => {
    try {
      const result = await fetchJson<{ sent_to: number; sound_count: number }>(
        apiUrl('/api/telegram/sounds/forward-all'), { method: 'POST' },
      );
      addNotification('success', `Sent ${result.sound_count} sounds to ${result.sent_to} posters`);
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to send sounds to all'); }
  }, [addNotification]);

  const handleUnassignPage = useCallback(async (posterId: string, pageId: string, pageName: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}/pages/${encodeURIComponent(pageId)}`), { method: 'DELETE' });
      addNotification('success', `${pageName} removed`);
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to remove page'); }
  }, [addNotification, refreshTelegram]);

  const togglePageSelection = useCallback((posterId: string, pageId: string) => {
    setPosterSelectedPages((prev) => {
      const current = new Set(prev[posterId] ?? []);
      if (current.has(pageId)) current.delete(pageId);
      else current.add(pageId);
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
      const pageNames: Record<string, string> = {};
      for (const pid of pageIds) {
        const page = rosterPages.find((p) => p.integration_id === pid);
        if (page?.name) pageNames[pid] = page.name;
      }
      const res = await fetch(apiUrl(`/api/telegram/posters/${encodeURIComponent(posterId)}/pages`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page_ids: pageIds, page_names: pageNames }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || JSON.stringify(data) || `Request failed (${res.status})`);
      setPosterSelectedPages((prev) => ({ ...prev, [posterId]: new Set() }));
      setPosterAssignOpen((prev) => ({ ...prev, [posterId]: false }));
      addNotification('success', `${pageIds.length} page${pageIds.length > 1 ? 's' : ''} assigned — click "Set Up Folders" to create topics`);
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to assign pages'); }
    finally { setAssignLoading(null); }
  }, [posterSelectedPages, rosterPages, addNotification, refreshTelegram]);

  // Sound handlers
  const handleAddSound = useCallback(async () => {
    const url = newSoundUrl.trim(); const label = newSoundLabel.trim();
    if (!url || !label) return;
    try {
      await fetchOk(apiUrl('/api/telegram/sounds'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, label }),
      });
      setNewSoundUrl(''); setNewSoundLabel('');
      addNotification('success', 'Sound added');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to add sound'); }
  }, [newSoundUrl, newSoundLabel, addNotification, refreshTelegram]);

  const handleToggleSound = useCallback(async (soundId: string, active: boolean) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/sounds/${encodeURIComponent(soundId)}`), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active }),
      });
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to toggle sound'); }
  }, [addNotification, refreshTelegram]);

  const handleDeleteSound = useCallback(async (soundId: string) => {
    try {
      await fetchOk(apiUrl(`/api/telegram/sounds/${encodeURIComponent(soundId)}`), { method: 'DELETE' });
      addNotification('success', 'Sound removed');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to delete sound'); }
  }, [addNotification, refreshTelegram]);

  const handleNotionSync = useCallback(async () => {
    setNotionSyncing(true); setNotionResult(null);
    try {
      const result = await fetchJson<any>(apiUrl('/api/telegram/sounds/sync'), { method: 'POST' });
      setNotionResult(result);
      const added = result.sounds_added || 0;
      const deactivated = result.sounds_deactivated || 0;
      const unmatched = result.unmatched?.length || 0;
      if (added > 0 || deactivated > 0) {
        addNotification('success', `Sync: +${added} new, ${deactivated} deactivated${unmatched > 0 ? `, ${unmatched} unmatched` : ''}`);
      } else {
        addNotification('success', `Sync complete — ${result.active_campaigns || 0} active campaigns, no changes needed`);
      }
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Notion sync failed'); }
    finally { setNotionSyncing(false); }
  }, [addNotification, refreshTelegram]);

  // Schedule handlers
  const handleSaveSchedule = useCallback(async () => {
    try {
      await fetchOk(apiUrl('/api/telegram/schedule'), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: scheduleEnabled, forward_time: forwardTime, timezone }),
      });
      addNotification('success', 'Schedule updated');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to update schedule'); }
  }, [scheduleEnabled, forwardTime, timezone, addNotification, refreshTelegram]);

  const handleRunBatch = useCallback(async () => {
    setBatchRunning(true); setBatchResult(null);
    try {
      const result = await fetchJson<TelegramBatchResult>(apiUrl('/api/telegram/batch/run'), { method: 'POST' });
      setBatchResult(result);
      addNotification('success', `Batch complete: ${result.videos_forwarded} videos forwarded`);
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Batch run failed'); }
    finally { setBatchRunning(false); }
  }, [addNotification, refreshTelegram]);

  // Send content handler
  const handleSendToStaging = useCallback(async () => {
    if (!sendPage || !sendFilePath.trim()) return;
    try {
      await fetchOk(apiUrl('/api/telegram/send'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          integration_id: sendPage,
          file_path: sendFilePath.trim(),
          caption: sendCaption.trim() || undefined,
          project: activeProjectName || undefined,
        }),
      });
      setSendResult('Sent successfully');
      setSendFilePath(''); setSendCaption('');
      addNotification('success', 'Content sent to storage');
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to send'); }
  }, [sendPage, sendFilePath, sendCaption, activeProjectName, addNotification, refreshTelegram]);

  // Scan / Discover handlers (inline async, delegated from TelegramTab)
  const handleScanInventory = useCallback(async () => {
    setScanning(true); setScanResult(null); setScanProgress('Starting scan...');
    try {
      await fetch(apiUrl('/api/telegram/staging-group/scan-inventory'), { method: 'POST' });
      for (let i = 0; i < 300; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const s = await fetchJson<{ status: string; scanned_topics?: number; total_topics?: number; total_found?: number; current_topic?: string }>(
            apiUrl('/api/telegram/staging-group/scan-inventory'));
          if (s.status === 'done') {
            setScanResult({ scanned_topics: s.scanned_topics ?? 0, total_found: s.total_found ?? 0 });
            setScanProgress('');
            addNotification('success', `Scanned ${s.scanned_topics} topics, found ${s.total_found} media items`);
            await refreshTelegram(); break;
          } else if (s.status === 'running') {
            setScanProgress(`Scanning ${s.scanned_topics ?? 0}/${s.total_topics ?? '?'}: ${s.current_topic ?? '...'} (${s.total_found ?? 0} found)`);
          }
        } catch { /* retry */ }
      }
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Scan failed'); }
    finally { setScanning(false); setScanProgress(''); }
  }, [addNotification, refreshTelegram]);

  const handleDiscoverTopics = useCallback(async () => {
    setDiscovering(true); setDiscoverProgress('Starting discovery...');
    try {
      await fetch(apiUrl('/api/telegram/staging-group/discover-topics'), { method: 'POST' });
      for (let i = 0; i < 600; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const s = await fetchJson<{ status: string; probed?: number; ceiling?: number; topics_found?: number; matched?: number; unmatched?: number; error?: string }>(
            apiUrl('/api/telegram/staging-group/discover-topics'));
          if (s.status === 'done') {
            setDiscoverProgress('');
            addNotification('success', `Found ${s.topics_found} topics, matched ${s.matched} to pages, ${s.unmatched} unmatched`);
            await refreshTelegram(); break;
          } else if (s.status === 'error') { addNotification('error', s.error || 'Discovery failed'); break; }
          else if (s.status === 'running') { setDiscoverProgress(`Probing ${s.probed ?? 0}/${s.ceiling ?? '?'} — ${s.topics_found ?? 0} topics found`); }
        } catch { /* retry */ }
      }
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Discovery failed'); }
    finally { setDiscovering(false); setDiscoverProgress(''); }
  }, [addNotification, refreshTelegram]);

  const handleClearScanData = useCallback(async () => {
    if (!confirm('Remove all inventory data from scans? Items sent via API are kept.')) return;
    try {
      const res = await fetchJson<{ removed: number }>(apiUrl('/api/telegram/inventory/scan'), { method: 'DELETE' });
      addNotification('success', `Cleared ${res.removed} scan items`);
      await refreshTelegram();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to clear scan data'); }
  }, [addNotification, refreshTelegram]);

  const handleAuditDupes = useCallback(async () => {
    try {
      const res = await fetchJson<{
        total_pages: number; duplicate_names: number;
        duplicates: { name: string; count: number; entries: { integration_id: string; has_topic: boolean; inventory_total: number; inventory_pending: number; inventory_forwarded: number }[] }[];
      }>(apiUrl('/api/roster/duplicates'));
      if (res.duplicate_names === 0) {
        addNotification('info', `No duplicates found (${res.total_pages} pages)`);
      } else {
        const summary = res.duplicates.map((d) => {
          const parts = d.entries.map((e) => `  ${e.integration_id.slice(0, 8)}… topic=${e.has_topic ? 'yes' : 'no'} inv=${e.inventory_total}(${e.inventory_pending}p/${e.inventory_forwarded}f)`);
          return `${d.name} (x${d.count}):\n${parts.join('\n')}`;
        }).join('\n\n');
        setTgError(`${res.duplicate_names} duplicate names found:\n\n${summary}`);
      }
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Audit failed'); }
  }, [addNotification]);

  const handleCleanDupes = useCallback(async () => {
    if (!confirm('This will merge inventory from duplicate pages and remove extras. Continue?')) return;
    try {
      const res = await fetchJson<{ removed: number; inventory_merged: number; topics_cleaned: number; remaining: number }>(
        apiUrl('/api/roster/dedup'), { method: 'POST' });
      if (res.removed > 0) {
        addNotification('success', `Removed ${res.removed} dupes, merged ${res.inventory_merged} inventory items, cleaned ${res.topics_cleaned} topics. ${res.remaining} pages remaining.`);
        await refreshTelegram();
      } else { addNotification('info', 'No duplicates to clean'); }
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Dedup failed'); }
  }, [addNotification, refreshTelegram]);

  // =========================================================================
  // PUBLISH HANDLERS
  // =========================================================================

  const assignProject = useCallback(async (integrationId: string, project: string | null) => {
    try {
      const resp = await fetch(apiUrl(`/api/roster/${integrationId}`), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project }),
      });
      if (!resp.ok) throw new Error(`Failed (${resp.status})`);
      const data = await resp.json();
      storeSetRosterPages(rosterPages.map((p) => (p.integration_id === integrationId ? data.page : p)));
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Assignment failed'); }
  }, [rosterPages, storeSetRosterPages, addNotification]);

  const saveDriveFolder = useCallback(async (integrationId: string, url: string) => {
    setSavingId(integrationId);
    try {
      const resp = await fetch(apiUrl(`/api/roster/${integrationId}`), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ drive_folder_url: url }),
      });
      if (!resp.ok) { const text = await resp.text(); throw new Error(text || `Failed (${resp.status})`); }
      const data = await resp.json();
      storeSetRosterPages(rosterPages.map((p) => (p.integration_id === integrationId ? data.page : p)));
      setEditingCell(null);
      addNotification('success', 'Drive folder linked');
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to save'); }
    finally { setSavingId(null); }
  }, [rosterPages, storeSetRosterPages, addNotification]);

  const createEmailAlias = useCallback(async (page: RosterPage) => {
    if (verifiedDestinations.length === 0) {
      addNotification('error', 'No verified destination addresses. Add one first.');
      setShowDestModal(true); return;
    }
    setCreatingEmailFor(page.integration_id);
    try {
      const resp = await fetch(apiUrl('/api/email/auto-create'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ integration_id: page.integration_id, account_name: page.name, destination: verifiedDestinations[0].email }),
      });
      if (!resp.ok) { const text = await resp.text(); throw new Error(text || `Failed (${resp.status})`); }
      const data = (await resp.json()) as AutoCreateEmailResponse;
      storeSetRosterPages(rosterPages.map((p) => p.integration_id === page.integration_id ? data.page : p));
      addNotification('success', `Email alias created: ${data.alias}`);
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to create alias'); }
    finally { setCreatingEmailFor(null); }
  }, [verifiedDestinations, rosterPages, storeSetRosterPages, addNotification]);

  const deleteEmailAlias = useCallback(async (page: RosterPage) => {
    if (!page.email_rule_id) return;
    try {
      const resp = await fetch(apiUrl(`/api/email/rules/${page.email_rule_id}?integration_id=${page.integration_id}`), { method: 'DELETE' });
      if (!resp.ok) throw new Error('Failed to delete');
      storeSetRosterPages(rosterPages.map((p) =>
        p.integration_id === page.integration_id ? { ...p, email_alias: null, email_rule_id: null, fwd_destination: null } : p));
      addNotification('success', 'Email alias removed');
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to delete alias'); }
  }, [rosterPages, storeSetRosterPages, addNotification]);

  const addDestination = useCallback(async () => {
    if (!newDestEmail.trim()) return;
    setAddingDest(true);
    try {
      const resp = await fetch(apiUrl('/api/email/destinations'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: newDestEmail.trim() }),
      });
      if (!resp.ok) throw new Error('Failed');
      addNotification('success', `Verification email sent to ${newDestEmail.trim()}`);
      setNewDestEmail('');
      await fetchEmailStatus();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Failed to add destination'); }
    finally { setAddingDest(false); }
  }, [newDestEmail, addNotification, fetchEmailStatus]);

  const triggerLogin = useCallback(async (accountName: string) => {
    setLoggingIn(accountName);
    try {
      const resp = await fetch(apiUrl(`/api/upload/login/${accountName}`), { method: 'POST' });
      if (!resp.ok) throw new Error('Login failed');
      addNotification('info', `Browser opened for ${accountName}. Complete login manually.`);
      setTimeout(() => void fetchCookies(), 10000);
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Login failed'); }
    finally { setLoggingIn(null); }
  }, [addNotification, fetchCookies]);

  const submitUpload = useCallback(async (videoPath: string) => {
    if (!uploadTarget) return;
    setSubmittingUpload(true);
    try {
      const resp = await fetch(apiUrl('/api/upload/submit'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_path: videoPath, account_name: uploadTarget.name,
          description: uploadDesc, hashtags: uploadHashtags.split(',').map((h) => h.trim()).filter(Boolean),
          sound_name: uploadSound || null, stealth: uploadStealth, headless: true,
        }),
      });
      if (!resp.ok) { const text = await resp.text(); throw new Error(text || 'Submit failed'); }
      const data = await resp.json();
      addUploadJob(data.job as UploadJob);
      addNotification('success', `Upload queued for ${uploadTarget.name}`);
      setShowUploadForm(false);
      setUploadDesc(''); setUploadHashtags(''); setUploadSound(''); setUploadSchedule('');
      void fetchUploadJobs();
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Submit failed'); }
    finally { setSubmittingUpload(false); }
  }, [uploadTarget, uploadDesc, uploadHashtags, uploadSound, uploadStealth, addUploadJob, addNotification, fetchUploadJobs]);

  const cancelUploadJob = useCallback(async (jobId: string) => {
    try {
      const resp = await fetch(apiUrl(`/api/upload/jobs/${jobId}/cancel`), { method: 'POST' });
      if (!resp.ok) throw new Error('Cancel failed');
      const data = await resp.json();
      updateUploadJob(data.job as UploadJob);
      addNotification('success', 'Upload cancelled');
    } catch (err) { addNotification('error', err instanceof Error ? err.message : 'Cancel failed'); }
  }, [updateUploadJob, addNotification]);

  const toggleSort = useCallback((key: SortKey) => {
    setSortKey((prev) => {
      if (prev === key) { setSortDir((d) => (d === 'asc' ? 'desc' : 'asc')); return key; }
      setSortDir('asc'); return key;
    });
  }, []);

  const startEdit = useCallback((page: RosterPage) => {
    setEditingCell({ id: page.integration_id, field: 'drive_folder_url' });
    setEditValue(page.drive_folder_url ?? '');
  }, []);

  const cancelEdit = useCallback(() => { setEditingCell(null); setEditValue(''); }, []);
  const commitEdit = useCallback(() => {
    if (!editingCell) return;
    void saveDriveFolder(editingCell.id, editValue);
  }, [editingCell, editValue, saveDriveFolder]);

  // =========================================================================
  // RENDER
  // =========================================================================

  if (tgLoading) {
    return (
      <div className="flex justify-center py-12">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6 p-4">
      {/* Page Header */}
      <div>
        <h1 className="mb-1 text-2xl font-heading text-foreground">Distribution</h1>
        <p className="text-sm text-muted-foreground">
          Manage pages, stage content, and distribute to posters and platforms.
        </p>
      </div>

      {/* Status Bar — always visible */}
      <StatusBar
        status={tgStatus}
        tokenInput={tokenInput}
        onTokenInputChange={setTokenInput}
        onSaveToken={handleSaveToken}
        onClearToken={handleClearToken}
        postizStatus={postizStatus}
        emailStatus={emailStatus}
        driveStatus={driveStatus}
        scheduleEnabled={scheduleEnabled}
        onScheduleEnabledChange={setScheduleEnabled}
        forwardTime={forwardTime}
        onForwardTimeChange={setForwardTime}
        timezone={timezone}
        onTimezoneChange={setTimezone}
        onSaveSchedule={handleSaveSchedule}
        onRunBatch={handleRunBatch}
        batchRunning={batchRunning}
        batchResult={batchResult}
        lastRun={tgStatus?.schedule?.last_run ?? null}
        pageCount={rosterPages.length}
        sounds={sounds}
        uploadStats={uploadStats}
      />

      {/* Sub-tab navigation */}
      <TabNav
        tabs={SUB_TABS.map((t) => ({ ...t }))}
        activeTab={activeSubTab}
        onTabChange={(tabId) => {
          const path = tabId === 'roster' ? '/distribution' : `/distribution/${tabId}`;
          navigate(path);
        }}
      />

      {/* Sub-tab content — CSS display toggle for state preservation */}
      <div style={{ display: activeSubTab === 'roster' ? 'block' : 'none' }}>
        {visitedSubTabs.has('roster') && (
          <RosterTab
            rosterPages={rosterPages}
            rosterLoading={rosterLoading}
            syncing={syncing}
            postizConfigured={postizStatus?.configured ?? false}
            sortKey={sortKey}
            sortDir={sortDir}
            onToggleSort={toggleSort}
            filterProject={filterProject}
            onFilterProjectChange={setFilterProject}
            projectNames={projectNames}
            activeProjectName={activeProjectName}
            emailStatus={emailStatus}
            destinations={destinations}
            verifiedDestinations={verifiedDestinations}
            showDestModal={showDestModal}
            onShowDestModal={setShowDestModal}
            creatingEmailFor={creatingEmailFor}
            onCreateEmailAlias={createEmailAlias}
            onDeleteEmailAlias={deleteEmailAlias}
            newDestEmail={newDestEmail}
            onNewDestEmailChange={setNewDestEmail}
            addingDest={addingDest}
            onAddDestination={addDestination}
            onRefreshEmailStatus={fetchEmailStatus}
            cookieStatuses={cookieStatuses}
            loggingIn={loggingIn}
            onTriggerLogin={triggerLogin}
            driveStatus={driveStatus}
            driveInventory={driveInventory}
            editingCell={editingCell}
            editValue={editValue}
            savingId={savingId}
            onStartEdit={startEdit}
            onCancelEdit={cancelEdit}
            onCommitEdit={commitEdit}
            onEditValueChange={setEditValue}
            uploadStats={uploadStats}
            queuedPerAccount={queuedPerAccount}
            onOpenUploadForm={(page) => {
              setUploadTarget(page);
              setShowUploadForm(true);
              navigate('/distribution/uploads');
            }}
            onAssignProject={assignProject}
            onRefreshRoster={fetchRoster}
          />
        )}
      </div>

      <div style={{ display: activeSubTab === 'telegram' ? 'block' : 'none' }}>
        {visitedSubTabs.has('telegram') && (
          <TelegramTab
            status={tgStatus}
            posters={posters}
            sounds={sounds}
            rosterPages={rosterPages}
            error={tgError}
            setError={setTgError}
            chatIdInput={chatIdInput}
            onChatIdInputChange={setChatIdInput}
            onSetGroup={handleSetGroup}
            onSyncTopics={handleSyncTopics}
            onForwardPending={handleForwardPending}
            syncResult={syncResult}
            scanning={scanning}
            scanProgress={scanProgress}
            scanResult={scanResult}
            onScanInventory={handleScanInventory}
            discovering={discovering}
            discoverProgress={discoverProgress}
            onDiscoverTopics={handleDiscoverTopics}
            onClearScanData={handleClearScanData}
            onAuditDupes={handleAuditDupes}
            onCleanDupes={handleCleanDupes}
            newPosterName={newPosterName}
            onNewPosterNameChange={setNewPosterName}
            newPosterChatId={newPosterChatId}
            onNewPosterChatIdChange={setNewPosterChatId}
            onAddPoster={handleAddPoster}
            onRemovePoster={handleRemovePoster}
            onSyncPosterTopics={handleSyncPosterTopics}
            onForwardSounds={handleForwardSounds}
            onUnassignPage={handleUnassignPage}
            posterSelectedPages={posterSelectedPages}
            posterAssignOpen={posterAssignOpen}
            assignLoading={assignLoading}
            onTogglePageSelection={togglePageSelection}
            onSelectAllPages={selectAllPages}
            onClearPageSelection={clearPageSelection}
            onAssignPages={handleAssignPages}
            onOpenAssign={(posterId) => setPosterAssignOpen((prev) => ({ ...prev, [posterId]: true }))}
            onCloseAssign={(posterId) => setPosterAssignOpen((prev) => ({ ...prev, [posterId]: false }))}
            sendPage={sendPage}
            onSendPageChange={setSendPage}
            sendFilePath={sendFilePath}
            onSendFilePathChange={setSendFilePath}
            sendCaption={sendCaption}
            onSendCaptionChange={setSendCaption}
            sendResult={sendResult}
            onSendToStaging={handleSendToStaging}
            topicEntries={topicEntries}
            pagesWithTopics={pagesWithTopics}
          />
        )}
      </div>

      <div style={{ display: activeSubTab === 'sounds' ? 'block' : 'none' }}>
        {visitedSubTabs.has('sounds') && (
          <SoundsTab
            sounds={sounds}
            notionConfigured={tgStatus?.notion_configured ?? false}
            notionSyncing={notionSyncing}
            notionResult={notionResult}
            newSoundUrl={newSoundUrl}
            onNewSoundUrlChange={setNewSoundUrl}
            newSoundLabel={newSoundLabel}
            onNewSoundLabelChange={setNewSoundLabel}
            onAddSound={handleAddSound}
            onToggleSound={handleToggleSound}
            onDeleteSound={handleDeleteSound}
            onNotionSync={handleNotionSync}
            onForwardSoundsAll={handleForwardSoundsAll}
          />
        )}
      </div>

      <div style={{ display: activeSubTab === 'uploads' ? 'block' : 'none' }}>
        {visitedSubTabs.has('uploads') && (
          <UploadsTab
            uploadJobs={uploadJobs}
            onCancelUploadJob={cancelUploadJob}
            showUploadForm={showUploadForm}
            uploadTarget={uploadTarget}
            uploadDesc={uploadDesc}
            onUploadDescChange={setUploadDesc}
            uploadHashtags={uploadHashtags}
            onUploadHashtagsChange={setUploadHashtags}
            uploadSound={uploadSound}
            onUploadSoundChange={setUploadSound}
            uploadSchedule={uploadSchedule}
            onUploadScheduleChange={setUploadSchedule}
            uploadStealth={uploadStealth}
            onUploadStealthChange={setUploadStealth}
            submittingUpload={submittingUpload}
            onSubmitUpload={submitUpload}
            onCloseUploadForm={() => setShowUploadForm(false)}
          />
        )}
      </div>
    </div>
  );
}
