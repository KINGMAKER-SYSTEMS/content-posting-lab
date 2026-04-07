import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';
import type {
  TelegramStatus,
  TelegramSound,
  TelegramBatchResult,
  PostizStatusResponse,
  EmailStatusResponse,
  DriveStatusResponse,
  UploadQueueStats,
} from '@/types/api';

const US_TIMEZONES = [
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
] as const;

export interface StatusBarProps {
  // Bot config
  status: TelegramStatus | null;
  tokenInput: string;
  onTokenInputChange: (v: string) => void;
  onSaveToken: () => void;
  onClearToken: () => void;

  // Services
  postizStatus: PostizStatusResponse | null;
  emailStatus: EmailStatusResponse | null;
  driveStatus: DriveStatusResponse | null;

  // Schedule
  scheduleEnabled: boolean;
  onScheduleEnabledChange: (v: boolean) => void;
  forwardTime: string;
  onForwardTimeChange: (v: string) => void;
  timezone: string;
  onTimezoneChange: (v: string) => void;
  onSaveSchedule: () => void;
  onRunBatch: () => void;
  batchRunning: boolean;
  batchResult: TelegramBatchResult | null;
  lastRun: string | null;

  // Quick stats
  pageCount: number;
  sounds: TelegramSound[];
  uploadStats: UploadQueueStats | null;
}

export function StatusBar({
  status,
  tokenInput,
  onTokenInputChange,
  onSaveToken,
  onClearToken,
  postizStatus,
  emailStatus,
  driveStatus,
  scheduleEnabled,
  onScheduleEnabledChange,
  forwardTime,
  onForwardTimeChange,
  timezone,
  onTimezoneChange,
  onSaveSchedule,
  onRunBatch,
  batchRunning,
  batchResult,
  lastRun,
  pageCount,
  sounds,
  uploadStats,
}: StatusBarProps) {
  const activeSounds = sounds.filter((s) => s.active).length;

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
      {/* Card 1: Bot Status */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center justify-between">
            <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">Bot</p>
            {status?.bot_configured ? (
              status.bot_running ? (
                <Badge variant="success">Connected</Badge>
              ) : (
                <Badge variant="error">Error</Badge>
              )
            ) : (
              <Badge variant="secondary">Not Configured</Badge>
            )}
          </div>
          {status?.bot_username && (
            <p className="text-sm text-muted-foreground">
              <span className="font-mono font-bold text-foreground">@{status.bot_username}</span>
            </p>
          )}
          <div className="flex items-center gap-2">
            <Input
              type="password"
              value={tokenInput}
              onChange={(e) => onTokenInputChange(e.target.value)}
              placeholder="Bot token..."
              className="flex-1 h-8 text-xs"
            />
            <Button size="xs" onClick={onSaveToken} disabled={!tokenInput.trim()}>
              Save
            </Button>
            {status?.bot_configured && (
              <Button variant="destructive" size="xs" onClick={onClearToken}>
                Clear
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Card 2: Services */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">Services</p>
          <div className="flex flex-wrap items-center gap-2">
            {postizStatus ? (
              <Badge variant={postizStatus.configured && postizStatus.reachable ? 'success' : 'destructive'}>
                {postizStatus.configured
                  ? postizStatus.reachable
                    ? 'Postiz'
                    : 'Postiz Down'
                  : 'Postiz Off'}
              </Badge>
            ) : (
              <Badge variant="secondary">Postiz...</Badge>
            )}
            {driveStatus?.configured && (
              <Badge variant="success">Drive</Badge>
            )}
            {emailStatus?.configured && (
              <Badge variant="info">{emailStatus.domain}</Badge>
            )}
            {status?.notion_configured && (
              <Badge variant="info">Notion</Badge>
            )}
          </div>
          {status?.staging_group?.chat_id && (
            <div className="flex items-center gap-2">
              <Badge variant="success">Storage</Badge>
              <span className="truncate text-xs text-muted-foreground">
                {status.staging_group.name ?? `Chat ${status.staging_group.chat_id}`}
              </span>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Card 3: Schedule & Batch */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center justify-between">
            <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">Schedule</p>
            <button
              type="button"
              role="switch"
              aria-checked={scheduleEnabled}
              onClick={() => onScheduleEnabledChange(!scheduleEnabled)}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-border transition-colors ${
                scheduleEnabled ? 'bg-primary' : 'bg-muted'
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-3 w-3 translate-y-[1px] rounded-full bg-white shadow-sm transition-transform ${
                  scheduleEnabled ? 'translate-x-4' : 'translate-x-0.5'
                }`}
              />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <Input
              type="time"
              value={forwardTime}
              onChange={(e) => onForwardTimeChange(e.target.value)}
              className="h-7 w-24 text-xs"
            />
            <select
              value={timezone}
              onChange={(e) => onTimezoneChange(e.target.value)}
              className="h-7 rounded-md border border-border bg-card px-2 text-xs text-foreground"
            >
              {US_TIMEZONES.map((tz) => (
                <option key={tz} value={tz}>
                  {tz.replace('America/', '').replace('_', ' ')}
                </option>
              ))}
            </select>
            <Button variant="outline" size="xs" onClick={onSaveSchedule}>
              Save
            </Button>
          </div>
          <div className="flex items-center gap-2">
            <Button size="xs" onClick={onRunBatch} disabled={batchRunning}>
              {batchRunning ? 'Running...' : 'Run Batch'}
            </Button>
            {batchResult && (
              <span className="text-[10px] text-muted-foreground">
                {batchResult.videos_forwarded}v / {batchResult.sounds_sent}s / {batchResult.posters_notified}p
              </span>
            )}
            {!batchResult && lastRun && (
              <span className="text-[10px] text-muted-foreground">
                Last: {new Date(lastRun).toLocaleString()}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Card 4: Quick Stats */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">Quick Stats</p>
          <div className="grid grid-cols-3 gap-3 text-center">
            <div>
              <p className="text-2xl font-heading font-bold tabular-nums text-foreground">{pageCount}</p>
              <p className="text-[10px] text-muted-foreground">Pages</p>
            </div>
            <div>
              <p className="text-2xl font-heading font-bold tabular-nums text-foreground">{activeSounds}</p>
              <p className="text-[10px] text-muted-foreground">
                Sound{activeSounds !== 1 ? 's' : ''}{sounds.length > activeSounds ? ` / ${sounds.length}` : ''}
              </p>
            </div>
            <div>
              <p className="text-2xl font-heading font-bold tabular-nums text-foreground">
                {(uploadStats?.queued ?? 0) + (uploadStats?.uploading ?? 0)}
              </p>
              <p className="text-[10px] text-muted-foreground">Queue</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
