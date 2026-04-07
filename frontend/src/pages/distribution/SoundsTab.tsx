import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { TelegramSound, NotionSyncResult } from '@/types/api';

export interface SoundsTabProps {
  sounds: TelegramSound[];
  notionConfigured: boolean;
  notionSyncing: boolean;
  notionResult: NotionSyncResult | null;
  newSoundUrl: string;
  onNewSoundUrlChange: (v: string) => void;
  newSoundLabel: string;
  onNewSoundLabelChange: (v: string) => void;
  onAddSound: () => void;
  onToggleSound: (soundId: string, active: boolean) => void;
  onDeleteSound: (soundId: string) => void;
  onNotionSync: () => void;
  onForwardSoundsAll: () => void;
}

export function SoundsTab({
  sounds,
  notionConfigured,
  notionSyncing,
  notionResult,
  newSoundUrl,
  onNewSoundUrlChange,
  newSoundLabel,
  onNewSoundLabelChange,
  onAddSound,
  onToggleSound,
  onDeleteSound,
  onNotionSync,
  onForwardSoundsAll,
}: SoundsTabProps) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span>Campaign Sounds</span>
              {sounds.length > 0 && (
                <div className="flex items-center gap-2">
                  <Badge variant="success">{sounds.filter((s) => s.active).length} active</Badge>
                  {sounds.filter((s) => !s.active).length > 0 && (
                    <Badge variant="secondary">{sounds.filter((s) => !s.active).length} completed</Badge>
                  )}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              {notionResult && (
                <span className="text-xs font-normal text-muted-foreground">
                  {(notionResult as any).sounds_added > 0 || (notionResult as any).sounds_deactivated > 0
                    ? `+${(notionResult as any).sounds_added || 0} / -${(notionResult as any).sounds_deactivated || 0}`
                    : `${(notionResult as any).active_campaigns || 0} active`}
                </span>
              )}
              <Button
                variant="outline"
                size="xs"
                onClick={onNotionSync}
                disabled={notionSyncing}
              >
                {notionSyncing ? 'Syncing...' : 'Sync Campaigns'}
              </Button>
              <Button
                variant="outline"
                size="xs"
                onClick={onForwardSoundsAll}
                disabled={sounds.filter((s) => s.active).length === 0}
              >
                Send to All Posters
              </Button>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {sounds.length > 0 ? (
            <div className="space-y-2">
              {[...sounds].sort((a, b) => (a.active === b.active ? 0 : a.active ? -1 : 1)).map((sound) => (
                <div
                  key={sound.id}
                  className={`flex items-center gap-3 rounded-[var(--border-radius)] border-2 p-3 ${
                    sound.active
                      ? 'border-border'
                      : 'border-border/50 bg-muted/30 opacity-60'
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className={`text-sm font-medium ${sound.active ? 'text-foreground' : 'text-muted-foreground line-through'}`}>
                        {sound.label}
                      </p>
                      {!sound.active && (
                        <Badge variant="secondary" className="text-[10px]">Completed</Badge>
                      )}
                    </div>
                    <a
                      href={sound.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="block truncate text-xs text-primary underline-offset-2 hover:underline"
                    >
                      {sound.url}
                    </a>
                  </div>

                  <button
                    type="button"
                    role="switch"
                    aria-checked={sound.active}
                    onClick={() => onToggleSound(sound.id, !sound.active)}
                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-border transition-colors ${
                      sound.active ? 'bg-primary' : 'bg-muted'
                    }`}
                  >
                    <span
                      className={`pointer-events-none inline-block h-4 w-4 translate-y-[1px] rounded-full bg-white shadow-sm ring-0 transition-transform ${
                        sound.active ? 'translate-x-5' : 'translate-x-0.5'
                      }`}
                    />
                  </button>

                  <Button
                    variant="destructive"
                    size="xs"
                    onClick={() => onDeleteSound(sound.id)}
                  >
                    Delete
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              No sounds yet. {notionConfigured ? 'Click "Sync Campaigns" to pull campaign sounds.' : 'Add sounds manually or configure Notion integration.'}
            </p>
          )}

          <div className="border-t border-border pt-4">
            <p className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">Add Sound</p>
            <div className="flex items-center gap-2">
              <Input
                value={newSoundUrl}
                onChange={(e) => onNewSoundUrlChange(e.target.value)}
                placeholder="Sound URL"
                className="flex-1"
              />
              <Input
                value={newSoundLabel}
                onChange={(e) => onNewSoundLabelChange(e.target.value)}
                placeholder="Label"
                className="w-40"
              />
              <Button
                onClick={onAddSound}
                disabled={!newSoundUrl.trim() || !newSoundLabel.trim()}
              >
                Add
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
