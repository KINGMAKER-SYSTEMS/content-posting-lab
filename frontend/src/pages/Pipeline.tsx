import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowsClockwiseIcon,
  CheckCircleIcon,
  ClockIcon,
  RocketLaunchIcon,
  WarningIcon,
  XCircleIcon,
  ArrowRightIcon,
  FolderIcon,
  EnvelopeSimpleIcon,
  HashIcon,
  PlusIcon,
} from '@phosphor-icons/react';
import { apiUrl } from '../lib/api';
import { useWorkflowStore } from '../stores/workflowStore';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type {
  PageStatus,
  PipelineStagesResponse,
  PipelineSetupResult,
  RosterPage,
} from '../types/api';

const STAGE_ORDER: PageStatus[] = [
  'New — Pending Setup',
  'In Production',
  'Delivered to Poster',
  'Live',
  'Complete',
];

const STAGE_META: Record<PageStatus, {
  short: string;
  tone: 'warn' | 'info' | 'active' | 'success' | 'muted';
  description: string;
}> = {
  'New — Pending Setup': {
    short: 'Pending Setup',
    tone: 'warn',
    description: 'Run setup chain: mint email + Drive + assign poster + create topic',
  },
  'In Production': {
    short: 'In Production',
    tone: 'info',
    description: 'Jay or Glitch producing content (~100 pieces) → R2 storage',
  },
  'Delivered to Poster': {
    short: 'Delivered',
    tone: 'active',
    description: 'Content dropped in poster\'s Telegram folder',
  },
  'Live': {
    short: 'Live',
    tone: 'success',
    description: 'Posting 3x/day for 30 days',
  },
  'Complete': {
    short: 'Complete',
    tone: 'muted',
    description: 'Day 30 done — archived',
  },
};

const TONE_CLASSES: Record<string, string> = {
  warn: 'border-warning/40 bg-warning/5 text-warning',
  info: 'border-primary/40 bg-primary/5 text-primary',
  active: 'border-info/40 bg-info/5',
  success: 'border-success/40 bg-success/5 text-success',
  muted: 'border-border bg-muted/30 text-muted-foreground',
};

export function PipelinePage() {
  const navigate = useNavigate();
  const { addNotification } = useWorkflowStore();
  const [data, setData] = useState<PipelineStagesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [setupResult, setSetupResult] = useState<PipelineSetupResult | null>(null);
  const [showIntake, setShowIntake] = useState(false);

  const fetchStages = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch(apiUrl('/api/pipeline/stages'));
      if (!resp.ok) throw new Error(`Failed (${resp.status})`);
      const payload = (await resp.json()) as PipelineStagesResponse;
      setData(payload);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Failed to load pipeline');
    } finally {
      setLoading(false);
    }
  }, [addNotification]);

  useEffect(() => {
    void fetchStages();
  }, [fetchStages]);

  const syncFromNotion = useCallback(async () => {
    setSyncing(true);
    try {
      const resp = await fetch(apiUrl('/api/roster/sync-notion'), { method: 'POST' });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Notion sync failed (${resp.status})`);
      }
      const result = await resp.json();
      addNotification(
        'success',
        `Synced: +${result.added} new, ${result.updated} updated (${result.total_in_notion} in DB)`,
      );
      await fetchStages();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Notion sync failed');
    } finally {
      setSyncing(false);
    }
  }, [addNotification, fetchStages]);

  const runSetup = useCallback(async (page: RosterPage) => {
    const isFlowStage = page.pipeline === 'Flow Stage';
    const stepList = isFlowStage
      ? `  • Mint Cloudflare email alias\n  • Write email back to Notion\n  • Flip status → In Production\n\nFlow Stage handles content + delivery externally (Jay's tooling).`
      : `  • Mint Cloudflare email alias\n  • Write email back to Notion\n  • Assign to poster (${page.poster_name || 'auto-detect'})\n  • Create Telegram topic\n  • Create R2 storage prefix\n  • Flip status → In Production`;
    if (!confirm(`Run setup for ${page.name} (${page.pipeline || 'King Maker'})?\n\n${stepList}`)) {
      return;
    }
    setBusyId(page.integration_id);
    setSetupResult(null);
    try {
      const resp = await fetch(apiUrl(`/api/pipeline/${encodeURIComponent(page.integration_id)}/setup`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const result = (await resp.json()) as PipelineSetupResult;
      setSetupResult(result);
      if (resp.ok && result.completed) {
        addNotification('success', `Setup complete for ${page.name}`);
      } else {
        const failed = Object.entries(result.steps || {}).find(([, v]) => !v.ok);
        const reason = failed ? `${failed[0]}: ${(failed[1] as { reason?: string }).reason || 'unknown'}` : 'partial';
        addNotification('error', `Setup ${result.completed ? 'partial' : 'failed'} — ${reason}`);
      }
      await fetchStages();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Setup failed');
    } finally {
      setBusyId(null);
    }
  }, [addNotification, fetchStages]);

  const transition = useCallback(async (page: RosterPage, status: PageStatus) => {
    if (!confirm(`Move ${page.name} → ${status}?`)) return;
    setBusyId(page.integration_id);
    try {
      const resp = await fetch(apiUrl(`/api/pipeline/${encodeURIComponent(page.integration_id)}/transition`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Transition failed (${resp.status})`);
      }
      addNotification('success', `${page.name} → ${status}`);
      await fetchStages();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Transition failed');
    } finally {
      setBusyId(null);
    }
  }, [addNotification, fetchStages]);

  const newCount = useMemo(
    () => data?.stages.find((s) => s.status === 'New — Pending Setup')?.count ?? 0,
    [data],
  );

  return (
    <div className="p-4 space-y-4">
      {/* ── Header ─────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-heading font-bold">Page Sale Pipeline</h1>
          <p className="text-sm text-muted-foreground">
            New sales only. Existing operational pages live on the{' '}
            <a href="/distribute" className="text-foreground font-bold hover:underline">Roster</a> tab.
            <br />
            Eric submits intake → card appears in <span className="font-bold">Pending Setup</span> →{' '}
            <span className="text-foreground font-bold">Run Setup</span>{' '}
            chains email + Drive + poster + Telegram.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {newCount > 0 && (
            <Badge variant="warning" className="text-xs">
              {newCount} pending setup
            </Badge>
          )}
          <Button
            size="sm"
            onClick={() => setShowIntake(true)}
          >
            <PlusIcon size={14} weight="bold" className="mr-1.5" />
            New Sale Intake
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={syncFromNotion}
            disabled={syncing}
          >
            <ArrowsClockwiseIcon
              size={14}
              weight="bold"
              className={`mr-1.5 ${syncing ? 'animate-spin' : ''}`}
            />
            {syncing ? 'Syncing...' : 'Sync from Notion'}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => void fetchStages()} disabled={loading}>
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Setup result drawer (last-run summary) ─────────── */}
      {setupResult && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
              Last setup run · {setupResult.integration_id}{' '}
              {setupResult.completed ? (
                <Badge variant="success" className="ml-2 text-[10px]">Complete</Badge>
              ) : (
                <Badge variant="warning" className="ml-2 text-[10px]">Partial</Badge>
              )}
            </div>
            <button
              type="button"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => setSetupResult(null)}
            >
              dismiss
            </button>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1.5">
            {Object.entries(setupResult.steps).map(([step, info]) => (
              <div
                key={step}
                className={`flex items-center gap-1.5 px-2 py-1 rounded text-xs ${info.ok ? 'bg-success/10' : 'bg-destructive/10'}`}
              >
                {info.ok ? (
                  <CheckCircleIcon size={12} weight="bold" className="text-success shrink-0" />
                ) : (
                  <XCircleIcon size={12} weight="bold" className="text-destructive shrink-0" />
                )}
                <span className="font-mono text-[11px]">{step}</span>
                {info.skipped && <Badge variant="secondary" className="text-[9px] ml-auto">skipped</Badge>}
                {!info.ok && info.reason && (
                  <span className="text-[10px] text-muted-foreground truncate ml-1" title={String(info.reason)}>
                    {String(info.reason)}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Kanban lanes ───────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-3">
        {STAGE_ORDER.map((status) => {
          const stage = data?.stages.find((s) => s.status === status);
          const meta = STAGE_META[status];
          const count = stage?.count ?? 0;
          return (
            <div
              key={status}
              className="rounded-md border border-border bg-card flex flex-col min-h-[200px]"
            >
              <div className={`px-3 py-2 border-b border-border ${TONE_CLASSES[meta.tone]}`}>
                <div className="flex items-center justify-between">
                  <div className="text-xs font-bold uppercase tracking-wide truncate" title={status}>
                    {meta.short}
                  </div>
                  <Badge variant="secondary" className="text-[10px] shrink-0">{count}</Badge>
                </div>
                <div className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  {meta.description}
                </div>
              </div>

              <div className="p-2 space-y-2 flex-1 overflow-y-auto max-h-[600px]">
                {count === 0 && (
                  <div className="text-[11px] text-muted-foreground/60 italic text-center py-4">
                    empty
                  </div>
                )}
                {stage?.pages.map((page) => (
                  <PipelineCard
                    key={page.integration_id}
                    page={page}
                    status={status}
                    isBusy={busyId === page.integration_id}
                    onRunSetup={() => runSetup(page)}
                    onTransition={(s) => transition(page, s)}
                    onOpenWorkspace={() => navigate(`/pipeline/${encodeURIComponent(page.integration_id)}`)}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Intake modal ───────────────────────────────────── */}
      {showIntake && (
        <IntakeModal
          onClose={() => setShowIntake(false)}
          onSubmitted={() => {
            setShowIntake(false);
            void fetchStages();
          }}
        />
      )}

      {/* ── Unassigned (unknown status) ────────────────────── */}
      {data && data.unassigned.length > 0 && (
        <div className="rounded-md border border-warning/40 bg-warning/5 p-3">
          <div className="flex items-center gap-2 mb-2">
            <WarningIcon size={14} weight="bold" className="text-warning" />
            <div className="text-xs font-bold">
              {data.unassigned.length} page{data.unassigned.length === 1 ? '' : 's'} with unknown Status — fix in Notion
            </div>
          </div>
          <div className="text-[11px] text-muted-foreground space-y-0.5">
            {data.unassigned.slice(0, 10).map((p) => (
              <div key={p.integration_id}>
                <span className="font-mono">{p.name}</span>: status was{' '}
                <span className="font-mono">{(p as { _unknown_status?: string })._unknown_status || 'empty'}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Card ──────────────────────────────────────────────────────────────────

function PipelineCard({
  page,
  status,
  isBusy,
  onRunSetup,
  onTransition,
  onOpenWorkspace,
}: {
  page: RosterPage;
  status: PageStatus;
  isBusy: boolean;
  onRunSetup: () => void;
  onTransition: (status: PageStatus) => void;
  onOpenWorkspace: () => void;
}) {
  const pageTypeColor: Record<string, 'default' | 'success' | 'info' | 'warning' | 'secondary'> = {
    'Lyric': 'info',
    'UGC': 'success',
    'Artist Burner': 'warning',
  };

  return (
    <div className="rounded border border-border bg-background p-2.5 space-y-2 hover:border-primary/40 transition-colors">
      {/* Header */}
      <div className="space-y-1">
        <div className="flex items-center gap-1.5">
          <span className="text-sm font-bold truncate flex-1" title={page.name}>
            {page.name}
          </span>
          {page.source === 'notion' && (
            <Badge variant="secondary" className="text-[9px] shrink-0">N</Badge>
          )}
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {page.page_type && (
            <Badge
              variant={pageTypeColor[page.page_type] || 'secondary'}
              className="text-[9px]"
            >
              {page.page_type}
            </Badge>
          )}
          {page.pipeline && (
            <Badge variant="secondary" className="text-[9px]">
              {page.pipeline === 'Flow Stage' ? 'FS' : page.pipeline === 'King Maker Tech' ? 'KMT' : page.pipeline}
            </Badge>
          )}
          {page.group && (
            <Badge variant="outline" className="text-[9px]">{page.group}</Badge>
          )}
        </div>
      </div>

      {/* Per-stage detail rows */}
      <div className="text-[10px] text-muted-foreground space-y-0.5 leading-tight">
        {page.poster_name && (
          <div className="flex items-center gap-1 truncate" title={`Poster: ${page.poster_name}`}>
            <span>Poster:</span>
            <span className="text-foreground">{page.poster_name}</span>
          </div>
        )}
        {page.email_alias && (
          <div className="flex items-center gap-1 truncate" title={page.email_alias}>
            <EnvelopeSimpleIcon size={9} weight="bold" />
            <span className="font-mono truncate">{page.email_alias}</span>
          </div>
        )}
        {page.r2_prefix && (
          <div className="flex items-center gap-1 truncate" title={`R2: ${page.r2_bucket}/${page.r2_prefix}`}>
            <FolderIcon size={9} weight="bold" />
            <span className="font-mono">r2 ✓</span>
          </div>
        )}
        {page.go_live_date && (
          <div className="flex items-center gap-1">
            <ClockIcon size={9} weight="bold" />
            <span>Go-live: {page.go_live_date}</span>
          </div>
        )}
      </div>

      {/* Action button matched to stage */}
      <div className="flex items-center gap-1">
        {status === 'New — Pending Setup' && (
          <Button
            size="xs"
            className="w-full text-[11px] h-7"
            onClick={onRunSetup}
            disabled={isBusy}
          >
            <RocketLaunchIcon size={11} weight="bold" className="mr-1" />
            {isBusy ? 'Running...' : 'Run Setup'}
          </Button>
        )}
        {status === 'In Production' && (
          <div className="flex flex-col gap-1 w-full">
            {page.pipeline !== 'Flow Stage' && (
              <Button
                size="xs"
                className="w-full text-[11px] h-7"
                onClick={onOpenWorkspace}
                disabled={isBusy}
              >
                <RocketLaunchIcon size={10} weight="bold" className="mr-1" />
                Open Workspace
              </Button>
            )}
            <Button
              size="xs"
              variant="outline"
              className="w-full text-[11px] h-7"
              onClick={() => onTransition('Delivered to Poster')}
              disabled={isBusy}
            >
              <ArrowRightIcon size={10} weight="bold" className="mr-1" />
              Mark Delivered
            </Button>
          </div>
        )}
        {status === 'Delivered to Poster' && (
          <Button
            size="xs"
            variant="outline"
            className="w-full text-[11px] h-7"
            onClick={() => onTransition('Live')}
            disabled={isBusy}
          >
            <ArrowRightIcon size={10} weight="bold" className="mr-1" />
            Mark Live
          </Button>
        )}
        {status === 'Live' && (
          <Button
            size="xs"
            variant="ghost"
            className="w-full text-[11px] h-7"
            onClick={() => onTransition('Complete')}
            disabled={isBusy}
          >
            Mark Complete
          </Button>
        )}
        {status === 'Complete' && page.notion_page_id && (
          <a
            href={`https://www.notion.so/${page.notion_page_id.replace(/-/g, '')}`}
            target="_blank"
            rel="noreferrer"
            className="w-full text-[11px] text-center text-muted-foreground hover:text-foreground transition-colors"
          >
            View in Notion <HashIcon size={9} weight="bold" className="inline" />
          </a>
        )}
      </div>
    </div>
  );
}

// ── Intake Modal ──────────────────────────────────────────────────────────

const PIPELINE_OPTIONS = ['Flow Stage', 'King Maker Tech'] as const;
const PAGE_TYPE_OPTIONS = ['Lyric page', 'UGC page', 'Artist burner page'] as const;
const GROUP_OPTIONS = ['ATLANTIC', 'WARNER', 'INTERNAL'] as const;
const GROUP_LABEL_OPTIONS = [
  'Sam Barber (Atlantic)',
  'Open account',
  'Internal Page',
  'Atlantic',
  'Warner Test UGC',
  'Jack Harlow (Atlantic)',
  'Warner UGC',
  'Mon Rovia',
  'Jake Marsh',
] as const;
const ACCOUNT_TYPE_OPTIONS = ['TRUCK', 'POV', 'Coffee', 'slideshow', 'silhouette', 'meme'] as const;

function IntakeModal({
  onClose,
  onSubmitted,
}: {
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const { addNotification } = useWorkflowStore();
  const [step, setStep] = useState<1 | 2>(1);
  const [minting, setMinting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [emailAlias, setEmailAlias] = useState<string | null>(null);
  const [fwdDestination, setFwdDestination] = useState<string | null>(null);
  const [emailLocal, setEmailLocal] = useState('');
  const [form, setForm] = useState({
    account_username: '',
    label_artist: '',
    pipeline_choice: '',
    page_type: '',
    sounds_reference: '',
    notes: '',
    poster: '',
    go_live_date: '',
    group: '',
    group_label: '',
    account_type: '',
  });

  const update = (field: keyof typeof form, value: string) =>
    setForm((prev) => ({ ...prev, [field]: value }));

  // Step 1: mint email + open TikTok
  const mintAndGo = async () => {
    if (!form.pipeline_choice) {
      addNotification('error', 'Pick Flow Stage or King Maker first — that decides who gets the verification emails');
      return;
    }
    setMinting(true);
    try {
      const resp = await fetch(apiUrl('/api/pipeline/mint-alias'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pipeline: form.pipeline_choice,
          desired_local: emailLocal.trim() || null,
        }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Request failed (${resp.status})`);
      }
      const result = await resp.json();
      const email = result.alias as string;
      setEmailAlias(email);
      setFwdDestination(result.destination as string);

      // Copy to clipboard + open TikTok in new tab
      try {
        await navigator.clipboard.writeText(email);
        addNotification('success', `📋 Email copied: ${email}`);
      } catch {
        addNotification('info', `Email: ${email} (copy manually — clipboard blocked)`);
      }
      window.open('https://www.tiktok.com/signup/phone-or-email/email', '_blank', 'noopener,noreferrer');

      // Move to step 2 — wait for user to come back with the handle
      setStep(2);
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Email mint failed');
    } finally {
      setMinting(false);
    }
  };

  const copyEmail = async () => {
    if (!emailAlias) return;
    try {
      await navigator.clipboard.writeText(emailAlias);
      addNotification('success', `📋 Copied: ${emailAlias}`);
    } catch {
      addNotification('info', emailAlias);
    }
  };

  // Step 2: submit the form w the handle they got
  const submit = async () => {
    if (!form.account_username.trim()) {
      addNotification('error', 'TikTok handle is required');
      return;
    }
    if (!emailAlias) {
      addNotification('error', 'Email alias missing — go back to step 1');
      return;
    }
    setSubmitting(true);
    try {
      const payload: Record<string, string | null> = {
        email_alias: emailAlias,
        fwd_destination: fwdDestination,
      };
      Object.entries(form).forEach(([k, v]) => {
        payload[k] = v.trim() || null;
      });
      const resp = await fetch(apiUrl('/api/pipeline/intake'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Request failed (${resp.status})`);
      }
      const result = await resp.json();
      addNotification('success', `${form.account_username} added to pipeline`);
      if (!result.synced) {
        addNotification('error', 'Row created but sync failed — click "Sync from Notion" to refresh');
      }
      onSubmitted();
    } catch (err) {
      addNotification('error', err instanceof Error ? err.message : 'Submit failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-md max-w-2xl w-full max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-border px-4 py-3 flex items-center justify-between sticky top-0 bg-card z-10">
          <div>
            <h2 className="text-lg font-heading font-bold">
              New Sale Intake {step === 1 ? '· Step 1: Mint Email' : '· Step 2: Page Details'}
            </h2>
            <p className="text-xs text-muted-foreground">
              {step === 1
                ? <>Step 1 mints a fresh email + opens TikTok. Sign up there w that email + password <span className="font-mono text-foreground">Risingtides123$</span>, pick whatever handle's available, then come back for step 2.</>
                : <>Use password <span className="font-mono text-foreground">Risingtides123$</span> on TikTok. Once you've got the account, fill in the actual handle below.</>
              }
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <XCircleIcon size={20} weight="bold" />
          </button>
        </div>

        {step === 1 && (() => {
          const sanitized = emailLocal.trim().toLowerCase().replace(/[^a-z0-9._-]/g, '-').replace(/^-+|-+$/g, '');
          const previewEmail = sanitized ? `${sanitized}@risingtidesviral.com` : 'acct-{random}@risingtidesviral.com';
          return (
            <div className="p-6 space-y-4">
              <Field label="Which pipeline owns this account?" required>
                <select
                  className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
                  value={form.pipeline_choice}
                  onChange={(e) => update('pipeline_choice', e.target.value)}
                >
                  <option value="">— pick one —</option>
                  {PIPELINE_OPTIONS.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
                <p className="text-[11px] text-muted-foreground mt-1">
                  Verification emails forward to: <span className="font-mono text-foreground">
                    {form.pipeline_choice === 'Flow Stage' ? 'jay@risingtidesent.com'
                      : form.pipeline_choice === 'King Maker Tech' ? 'glitch@risingtidesent.com'
                      : '— pick a pipeline —'}
                  </span>
                </p>
              </Field>

              <Field label="Email name (optional — leave blank for random)">
                <input
                  type="text"
                  className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm font-mono"
                  placeholder="e.g. samb-truck-04 or warner-april-3"
                  value={emailLocal}
                  onChange={(e) => setEmailLocal(e.target.value)}
                />
                <p className="text-[11px] text-muted-foreground mt-1">
                  Will become: <span className="font-mono text-foreground">{previewEmail}</span>
                  {sanitized && sanitized !== emailLocal.trim().toLowerCase() && (
                    <span className="ml-2 text-warning">(special chars get cleaned)</span>
                  )}
                </p>
              </Field>

              <div className="rounded-md border border-border bg-muted/30 p-4 space-y-3">
                <div className="text-sm">
                  <span className="font-bold">When you click "Mint Email + Open TikTok":</span>
                </div>
                <ol className="text-xs text-muted-foreground space-y-1 list-decimal list-inside">
                  <li>Backend creates the Cloudflare alias <span className="font-mono">{previewEmail}</span> forwarding to the right person</li>
                  <li>Email gets copied to your clipboard</li>
                  <li>TikTok signup opens in a new tab</li>
                  <li>You paste email + use password <span className="font-mono text-foreground">Risingtides123$</span></li>
                  <li>Pick any TikTok handle that's available</li>
                  <li>Come back here → fill out the rest of the form (step 2)</li>
                </ol>
              </div>

              <div className="flex justify-center">
                <Button size="lg" onClick={mintAndGo} disabled={minting || !form.pipeline_choice} className="w-full">
                  {minting ? 'Minting...' : '🔑 Mint Email + Open TikTok'}
                </Button>
              </div>
            </div>
          );
        })()}

        {step === 2 && (
        <div className="p-4 space-y-3">
          {/* Email banner — show what was minted in step 1 */}
          {emailAlias && (
            <div className="rounded-md border border-success/40 bg-success/5 px-3 py-2 flex items-center gap-2">
              <span className="text-xs text-muted-foreground shrink-0">Email minted:</span>
              <span className="text-xs font-mono flex-1 truncate">{emailAlias}</span>
              <Button size="xs" variant="ghost" onClick={copyEmail} className="text-[11px] h-6">
                Copy
              </Button>
            </div>
          )}

          {/* Account Username (required) */}
          <Field label="Actual TikTok Handle (the one you got)" required>
            <input
              type="text"
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              placeholder="e.g. gavin.wilder1"
              value={form.account_username}
              onChange={(e) => update('account_username', e.target.value)}
              autoFocus
            />
          </Field>

          {/* Label / Artist */}
          <Field label="Label / Artist">
            <input
              type="text"
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              placeholder="e.g. Sam Barber"
              value={form.label_artist}
              onChange={(e) => update('label_artist', e.target.value)}
            />
          </Field>

          {/* Pipeline (routing) */}
          <Field label="Pipeline (routes alert to Jay or Glitch)">
            <select
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              value={form.pipeline_choice}
              onChange={(e) => update('pipeline_choice', e.target.value)}
            >
              <option value="">— pick one —</option>
              {PIPELINE_OPTIONS.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </Field>

          {/* Page Type */}
          <Field label="Page Type">
            <select
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              value={form.page_type}
              onChange={(e) => update('page_type', e.target.value)}
            >
              <option value="">— pick one —</option>
              {PAGE_TYPE_OPTIONS.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </Field>

          {/* Sounds Reference */}
          <Field label="Sounds / Songs Reference Link (optional)">
            <input
              type="url"
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              placeholder="https://..."
              value={form.sounds_reference}
              onChange={(e) => update('sounds_reference', e.target.value)}
            />
          </Field>

          {/* Notes */}
          <Field label="Notes (sub-type details, references, directional dial-in)">
            <textarea
              className="w-full px-3 py-2 rounded-md border border-border bg-background text-sm min-h-[80px]"
              placeholder='e.g. "nightcore POV page, truck variant, see https://example.com"'
              value={form.notes}
              onChange={(e) => update('notes', e.target.value)}
            />
          </Field>

          {/* Poster (manual for Phase 1) */}
          <Field label="Assigned Poster (manual for now)">
            <input
              type="text"
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              placeholder="e.g. Jake Balik"
              value={form.poster}
              onChange={(e) => update('poster', e.target.value)}
            />
          </Field>

          {/* Go-Live Date */}
          <Field label="Go-Live Date">
            <input
              type="date"
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              value={form.go_live_date}
              onChange={(e) => update('go_live_date', e.target.value)}
            />
          </Field>

          {/* Group (label) */}
          <div className="grid grid-cols-2 gap-3">
            <Field label="Group (label)">
              <select
                className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
                value={form.group}
                onChange={(e) => update('group', e.target.value)}
              >
                <option value="">—</option>
                {GROUP_OPTIONS.map((g) => (
                  <option key={g} value={g}>{g}</option>
                ))}
              </select>
            </Field>

            {/* Account Type */}
            <Field label="Account Type">
              <select
                className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
                value={form.account_type}
                onChange={(e) => update('account_type', e.target.value)}
              >
                <option value="">—</option>
                {ACCOUNT_TYPE_OPTIONS.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
            </Field>
          </div>

          {/* Group sub-label */}
          <Field label="Group sub-label (artist / project bucket)">
            <select
              className="w-full h-9 px-3 rounded-md border border-border bg-background text-sm"
              value={form.group_label}
              onChange={(e) => update('group_label', e.target.value)}
            >
              <option value="">—</option>
              {GROUP_LABEL_OPTIONS.map((g) => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
          </Field>
        </div>
        )}

        <div className="border-t border-border px-4 py-3 flex items-center justify-end gap-2 sticky bottom-0 bg-card">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={submitting || minting}>
            {step === 1 ? 'Close' : 'Cancel'}
          </Button>
          {step === 2 && (
            <Button size="sm" onClick={submit} disabled={submitting || !form.account_username.trim()}>
              {submitting ? 'Submitting...' : 'Submit Intake'}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-xs font-bold text-muted-foreground uppercase tracking-wide mb-1">
        {label}
        {required && <span className="text-destructive ml-1">*</span>}
      </label>
      {children}
    </div>
  );
}
