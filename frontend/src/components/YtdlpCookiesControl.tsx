import { useCallback, useEffect, useRef, useState } from 'react';
import { apiUrl } from '../lib/api';
import { Button } from '@/components/ui/button';

interface CookiesStatus {
  configured: boolean;
  source: string | null;
}

interface Props {
  onNotify?: (kind: 'success' | 'error', msg: string) => void;
  className?: string;
}

export function YtdlpCookiesControl({ onNotify, className }: Props) {
  const [status, setStatus] = useState<CookiesStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => {
    fetch(apiUrl('/api/clipper/cookies/status'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => { if (data) setStatus({ configured: !!data.configured, source: data.source ?? null }); })
      .catch(() => {});
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleUpload = async (file: File) => {
    setUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const resp = await fetch(apiUrl('/api/clipper/cookies'), { method: 'POST', body: form });
      if (!resp.ok) throw new Error((await resp.text()) || `Upload failed (${resp.status})`);
      onNotify?.('success', 'Cookies uploaded — yt-dlp will use them on next download');
      refresh();
      setOpen(false);
    } catch (e) {
      onNotify?.('error', e instanceof Error ? e.message : 'Cookies upload failed');
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const handleDelete = async () => {
    try {
      const resp = await fetch(apiUrl('/api/clipper/cookies'), { method: 'DELETE' });
      if (!resp.ok) throw new Error((await resp.text()) || `Delete failed (${resp.status})`);
      onNotify?.('success', 'Cookies removed');
      refresh();
    } catch (e) {
      onNotify?.('error', e instanceof Error ? e.message : 'Cookies delete failed');
    }
  };

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
          status?.configured
            ? 'text-emerald-600 hover:bg-emerald-50 dark:text-emerald-400 dark:hover:bg-emerald-950'
            : 'text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950'
        }`}
        title={status?.source ? `Source: ${status.source}` : 'No cookies configured'}
      >
        {status?.configured ? 'Cookies: set' : 'Cookies: none'}
      </button>

      {open && (
        <div className="mt-2 p-2.5 rounded border border-border bg-muted/30 text-xs space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="font-medium">yt-dlp cookies</span>
            <button type="button" onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground">&times;</button>
          </div>
          <p className="text-[11px] text-muted-foreground leading-snug">
            TikTok and YouTube increasingly require a logged-in cookies.txt to download.
            Export one with the &quot;Get cookies.txt LOCALLY&quot; browser extension and upload it here.
            Stored on the Railway volume — survives deploys.
          </p>
          {status && (
            <div className="text-[11px] text-muted-foreground">
              Status: {status.configured
                ? <span className="text-emerald-600 dark:text-emerald-400">configured ({status.source})</span>
                : <span className="text-amber-600 dark:text-amber-400">not configured</span>}
            </div>
          )}
          <input
            ref={fileRef}
            type="file"
            accept=".txt,text/plain"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleUpload(f);
            }}
          />
          <div className="flex gap-2 pt-0.5">
            <Button
              size="sm"
              variant="outline"
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? 'Uploading…' : 'Upload cookies.txt'}
            </Button>
            {status?.configured && status.source === 'volume' && (
              <Button size="sm" variant="ghost" onClick={() => void handleDelete()}>
                Remove
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
