import { useMemo, useState } from 'react';
import {
  ArrowsClockwiseIcon,
  CheckCircleIcon,
  EnvelopeSimpleIcon,
  PlusIcon,
  TrashIcon,
  WarningIcon,
} from '@phosphor-icons/react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type {
  EmailDestination,
  EmailStatusResponse,
  RosterPage,
  TelegramPoster,
} from '@/types/api';

export interface EmailTabProps {
  rosterPages: RosterPage[];
  posters: TelegramPoster[];

  emailStatus: EmailStatusResponse | null;
  destinations: EmailDestination[];
  verifiedDestinations: EmailDestination[];

  newDestEmail: string;
  onNewDestEmailChange: (v: string) => void;
  addingDest: boolean;
  onAddDestination: () => void;

  creatingEmailFor: string | null;
  onCreateEmailAlias: (page: RosterPage) => void;
  onDeleteEmailAlias: (page: RosterPage) => void;

  assignChainLoading: string | null;
  onAssignPosterAndSync: (page: RosterPage, posterId: string) => void;

  // Notion sync
  notionSyncing: boolean;
  onSyncFromNotion: () => void;
  notionSyncSummary: { added: number; updated: number; total: number } | null;
}

function findAssignedPoster(
  page: RosterPage,
  posters: TelegramPoster[],
): TelegramPoster | null {
  return posters.find((p) => p.page_ids.includes(page.integration_id)) ?? null;
}

export function EmailTab({
  rosterPages,
  posters,
  emailStatus,
  destinations,
  verifiedDestinations,
  newDestEmail,
  onNewDestEmailChange,
  addingDest,
  onAddDestination,
  creatingEmailFor,
  onCreateEmailAlias,
  onDeleteEmailAlias,
  assignChainLoading,
  onAssignPosterAndSync,
  notionSyncing,
  onSyncFromNotion,
  notionSyncSummary,
}: EmailTabProps) {
  const [search, setSearch] = useState('');
  const [filterMode, setFilterMode] = useState<'all' | 'notion' | 'with_alias' | 'no_email'>('all');

  const configured = !!emailStatus?.configured;
  const domain = emailStatus?.domain ?? null;

  const filteredPages = useMemo(() => {
    const term = search.trim().toLowerCase();
    return rosterPages
      .filter((p) => {
        if (!term) return true;
        return (
          p.name.toLowerCase().includes(term) ||
          (p.signup_email ?? '').toLowerCase().includes(term) ||
          (p.poster_name ?? '').toLowerCase().includes(term)
        );
      })
      .filter((p) => {
        if (filterMode === 'all') return true;
        if (filterMode === 'notion') return p.source === 'notion';
        if (filterMode === 'with_alias') return !!p.email_alias;
        if (filterMode === 'no_email') return !p.signup_email && !p.email_alias;
        return true;
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [rosterPages, search, filterMode]);

  const verifiedCount = verifiedDestinations.length;
  const pendingCount = destinations.length - verifiedCount;

  const notionCount = useMemo(
    () => rosterPages.filter((p) => p.source === 'notion').length,
    [rosterPages],
  );

  return (
    <div className="space-y-4 p-4">
      {/* ── Notion sync banner ──────────────────────────────────── */}
      <div className="rounded-md border border-border bg-card px-4 py-3 flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-bold">
            Notion Master Pages
            <Badge variant="secondary" className="ml-2 text-[10px]">
              {notionCount} synced
            </Badge>
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            Notion is the source of truth for usernames, emails, passwords, and poster assignments.
            {notionSyncSummary && (
              <span className="ml-2 text-success">
                Last sync: +{notionSyncSummary.added} new, {notionSyncSummary.updated} updated
              </span>
            )}
          </div>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={onSyncFromNotion}
          disabled={notionSyncing}
          className="shrink-0"
        >
          <ArrowsClockwiseIcon
            size={14}
            weight="bold"
            className={`mr-1.5 ${notionSyncing ? 'animate-spin' : ''}`}
          />
          {notionSyncing ? 'Syncing...' : 'Sync from Notion'}
        </Button>
      </div>

      {/* ── CF status banner ────────────────────────────────────── */}
      <div
        className={`rounded-md border px-4 py-3 flex items-center gap-3 ${
          configured
            ? 'border-success/40 bg-success/5'
            : 'border-destructive/40 bg-destructive/5'
        }`}
      >
        <EnvelopeSimpleIcon
          size={20}
          weight="bold"
          className={configured ? 'text-success' : 'text-destructive'}
        />
        <div className="flex-1 min-w-0">
          {configured ? (
            <>
              <div className="text-sm font-bold">
                Cloudflare Email Routing configured
              </div>
              <div className="text-xs text-muted-foreground">
                Domain <span className="font-mono">{domain}</span> · {verifiedCount}{' '}
                verified destination{verifiedCount === 1 ? '' : 's'}
                {pendingCount > 0 ? ` · ${pendingCount} pending verification` : ''}
              </div>
            </>
          ) : (
            <>
              <div className="text-sm font-bold text-destructive">
                Cloudflare Email Routing not configured
              </div>
              <div className="text-xs text-muted-foreground">
                Set <span className="font-mono">CF_API_TOKEN</span>,{' '}
                <span className="font-mono">CF_ACCOUNT_ID</span>,{' '}
                <span className="font-mono">CF_ZONE_ID</span>, and{' '}
                <span className="font-mono">CF_EMAIL_DOMAIN</span> in <code>.env</code>{' '}
                and restart the server. Aliases here are only for new accounts going forward;
                existing accounts use the email recorded in Notion.
              </div>
            </>
          )}
        </div>
      </div>

      {/* ── Destinations strip ──────────────────────────────────── */}
      {configured && (
        <div className="rounded-md border border-border bg-card p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
              Forward destinations
            </div>
          </div>

          {destinations.length === 0 ? (
            <div className="text-xs text-muted-foreground py-1">
              No destinations yet — add a Gmail to forward emails to.
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {destinations.map((d) => (
                <Badge
                  key={d.id}
                  variant={d.verified ? 'success' : 'warning'}
                  className="text-[11px] font-mono"
                  title={d.verified ? 'Verified' : 'Pending verification — check inbox'}
                >
                  {d.verified ? (
                    <CheckCircleIcon size={10} weight="bold" className="mr-1" />
                  ) : (
                    <WarningIcon size={10} weight="bold" className="mr-1" />
                  )}
                  {d.email}
                </Badge>
              ))}
            </div>
          )}

          <div className="flex items-center gap-2 pt-1">
            <Input
              type="email"
              placeholder="add-destination@gmail.com"
              className="h-8 text-xs flex-1"
              value={newDestEmail}
              onChange={(e) => onNewDestEmailChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newDestEmail.trim() && !addingDest) {
                  onAddDestination();
                }
              }}
              disabled={addingDest}
            />
            <Button
              size="sm"
              onClick={onAddDestination}
              disabled={!newDestEmail.trim() || addingDest}
            >
              {addingDest ? '...' : <><PlusIcon size={12} weight="bold" className="mr-1" />Add</>}
            </Button>
          </div>
        </div>
      )}

      {/* ── Filters ─────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 flex-wrap">
        <Input
          type="text"
          placeholder="Search by username, email, poster..."
          className="h-8 text-xs flex-1 max-w-md"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="h-8 text-xs rounded-md border border-border bg-background px-2"
          value={filterMode}
          onChange={(e) => setFilterMode(e.target.value as typeof filterMode)}
        >
          <option value="all">All accounts</option>
          <option value="notion">From Notion only</option>
          <option value="with_alias">With CF alias</option>
          <option value="no_email">No email recorded</option>
        </select>
        <div className="ml-auto text-xs text-muted-foreground">
          {filteredPages.length} of {rosterPages.length} accounts
        </div>
      </div>

      {/* ── Account workflow table ──────────────────────────────── */}
      <div className="rounded-md border border-border bg-card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="text-left px-3 py-2 font-bold">Account</th>
              <th className="text-left px-3 py-2 font-bold">Email (Notion)</th>
              <th className="text-left px-3 py-2 font-bold">CF alias</th>
              <th className="text-left px-3 py-2 font-bold">Poster</th>
              <th className="text-left px-3 py-2 font-bold">Topic</th>
              <th className="text-right px-3 py-2 font-bold w-10"></th>
            </tr>
          </thead>
          <tbody>
            {filteredPages.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-xs text-muted-foreground">
                  {rosterPages.length === 0
                    ? 'No roster pages yet — click "Sync from Notion" above.'
                    : 'No matches.'}
                </td>
              </tr>
            )}

            {filteredPages.map((page) => {
              const assignedPoster = findAssignedPoster(page, posters);
              const topicInfo = assignedPoster?.topics?.[page.integration_id];
              const isCreatingEmail = creatingEmailFor === page.integration_id;
              const isAssigning = assignChainLoading === page.integration_id;
              const expectedPoster = page.poster_name?.trim();
              const assignedMatchesExpected =
                expectedPoster && assignedPoster?.name?.toLowerCase() === expectedPoster.toLowerCase();

              return (
                <tr key={page.integration_id} className="border-t border-border hover:bg-muted/20 align-top">
                  {/* Account */}
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className="flex flex-col min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm font-bold truncate max-w-[180px]" title={page.name}>
                            {page.name || page.integration_id}
                          </span>
                          {page.source === 'notion' && (
                            <Badge variant="secondary" className="text-[9px] shrink-0">N</Badge>
                          )}
                        </div>
                        <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                          {page.account_type && <span>{page.account_type}</span>}
                          {page.account_type && page.group && <span>·</span>}
                          {page.group && <span className="uppercase">{page.group}</span>}
                          {!page.account_type && !page.group && <span>{page.provider || '—'}</span>}
                        </div>
                      </div>
                    </div>
                  </td>

                  {/* Email (from Notion — read-only) */}
                  <td className="px-3 py-2 min-w-[220px]">
                    {page.signup_email ? (
                      <div className="flex flex-col gap-0.5">
                        <span
                          className="text-xs font-mono truncate max-w-[220px]"
                          title={`Signup email: ${page.signup_email}`}
                        >
                          {page.signup_email}
                        </span>
                        {page.fwd_address && (
                          <span
                            className="text-[10px] text-muted-foreground truncate max-w-[220px]"
                            title={`Forwards to: ${page.fwd_address}`}
                          >
                            → {page.fwd_address}
                          </span>
                        )}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground/50">—</span>
                    )}
                  </td>

                  {/* CF alias (only meaningful for new accounts) */}
                  <td className="px-3 py-2 min-w-[200px]">
                    {page.email_alias ? (
                      <div className="flex items-center gap-1.5">
                        <Badge variant="success" className="text-[9px] shrink-0">CF</Badge>
                        <span
                          className="text-xs font-mono truncate max-w-[180px]"
                          title={page.email_alias}
                        >
                          {page.email_alias}
                        </span>
                      </div>
                    ) : configured && verifiedDestinations.length > 0 ? (
                      <Button
                        size="xs"
                        variant="outline"
                        className="text-[11px] h-6 px-2"
                        onClick={() => onCreateEmailAlias(page)}
                        disabled={isCreatingEmail}
                        title="Generate Cloudflare alias for this account (use for new accounts only)"
                      >
                        {isCreatingEmail ? '...' : (
                          <><PlusIcon size={10} weight="bold" className="mr-1" />Mint</>
                        )}
                      </Button>
                    ) : (
                      <span className="text-xs text-muted-foreground/40">—</span>
                    )}
                  </td>

                  {/* Poster */}
                  <td className="px-3 py-2 min-w-[200px]">
                    <div className="flex flex-col gap-0.5">
                      <select
                        className="w-full max-w-[200px] h-7 text-xs rounded-md border border-border bg-background px-2"
                        value={assignedPoster?.poster_id ?? ''}
                        disabled={isAssigning || posters.length === 0}
                        onChange={(e) => {
                          const next = e.target.value;
                          if (!next) return;
                          if (assignedPoster?.poster_id === next) return;
                          onAssignPosterAndSync(page, next);
                        }}
                      >
                        <option value="">
                          {posters.length === 0 ? 'No posters configured' : '— pick a poster —'}
                        </option>
                        {posters.map((p) => (
                          <option key={p.poster_id} value={p.poster_id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                      {expectedPoster && !assignedMatchesExpected && (
                        <span
                          className="text-[10px] text-muted-foreground"
                          title="Notion lists this poster — pick them above to assign"
                        >
                          Notion: <span className="text-foreground">{expectedPoster}</span>
                        </span>
                      )}
                      {isAssigning && (
                        <div className="text-[10px] text-muted-foreground">
                          Assigning + creating topic...
                        </div>
                      )}
                    </div>
                  </td>

                  {/* Topic */}
                  <td className="px-3 py-2 min-w-[160px]">
                    {!assignedPoster ? (
                      <span className="text-xs text-muted-foreground/50">—</span>
                    ) : topicInfo ? (
                      <div className="flex items-center gap-1.5">
                        <Badge variant="success" className="text-[10px] shrink-0">
                          <CheckCircleIcon size={10} weight="bold" className="mr-1" />
                          Live
                        </Badge>
                        <span className="text-xs truncate max-w-[120px]" title={topicInfo.topic_name}>
                          {topicInfo.topic_name}
                        </span>
                      </div>
                    ) : (
                      <Badge variant="warning" className="text-[10px]">Pending sync</Badge>
                    )}
                  </td>

                  {/* Delete CF alias */}
                  <td className="px-3 py-2 text-right">
                    {page.email_alias && (
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-destructive transition-colors"
                        onClick={() => onDeleteEmailAlias(page)}
                        title="Remove CF alias"
                      >
                        <TrashIcon size={14} weight="bold" />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
