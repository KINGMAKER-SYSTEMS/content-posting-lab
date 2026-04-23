import { useMemo } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { XIcon } from '@phosphor-icons/react';
import type { UploadJob, RosterPage } from '@/types/api';

export interface UploadsTabProps {
  uploadJobs: UploadJob[];
  onCancelUploadJob: (jobId: string) => void;

  // Upload form (inline, not modal)
  showUploadForm: boolean;
  uploadTarget: RosterPage | null;
  uploadDesc: string;
  onUploadDescChange: (v: string) => void;
  uploadHashtags: string;
  onUploadHashtagsChange: (v: string) => void;
  uploadSound: string;
  onUploadSoundChange: (v: string) => void;
  uploadSchedule: string;
  onUploadScheduleChange: (v: string) => void;
  uploadStealth: boolean;
  onUploadStealthChange: (v: boolean) => void;
  submittingUpload: boolean;
  onSubmitUpload: (videoPath: string) => void;
  onCloseUploadForm: () => void;
}

export function UploadsTab({
  uploadJobs,
  onCancelUploadJob,
  showUploadForm,
  uploadTarget,
  uploadDesc,
  onUploadDescChange,
  uploadHashtags,
  onUploadHashtagsChange,
  uploadSound,
  onUploadSoundChange,
  uploadSchedule,
  onUploadScheduleChange,
  uploadStealth,
  onUploadStealthChange,
  submittingUpload,
  onSubmitUpload,
  onCloseUploadForm,
}: UploadsTabProps) {
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

  return (
    <div className="space-y-4">
      {/* Upload Form (inline when target selected) */}
      {showUploadForm && uploadTarget && (
        <div className="rounded-[var(--border-radius)] border-2 border-border bg-card shadow-[2px_2px_0_0_var(--border)] p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-heading font-bold">
              Upload to {uploadTarget.name}
            </h2>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground"
              onClick={onCloseUploadForm}
            >
              <XIcon size={18} weight="bold" />
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
                onChange={(e) => onUploadDescChange(e.target.value)}
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
                  onChange={(e) => onUploadHashtagsChange(e.target.value)}
                />
              </div>
              <div>
                <label className="text-xs font-bold text-muted-foreground block mb-1">Sound Name (optional)</label>
                <Input
                  type="text"
                  placeholder="trending_sound"
                  className="text-sm"
                  value={uploadSound}
                  onChange={(e) => onUploadSoundChange(e.target.value)}
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
                  onChange={(e) => onUploadScheduleChange(e.target.value)}
                />
              </div>
              <div className="flex items-end pb-1">
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="checkbox"
                    checked={uploadStealth}
                    onChange={(e) => onUploadStealthChange(e.target.checked)}
                    className="rounded border-2 border-border"
                  />
                  <span className="text-xs font-bold">Stealth Mode</span>
                </label>
              </div>
            </div>
          </div>

          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onCloseUploadForm}>
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={submittingUpload}
              onClick={() => {
                const videoInput = document.getElementById('upload-video-path') as HTMLInputElement;
                if (videoInput?.value) {
                  onSubmitUpload(videoInput.value);
                }
              }}
            >
              {submittingUpload ? 'Submitting...' : 'Queue Upload'}
            </Button>
          </div>
        </div>
      )}

      {/* Upload Queue */}
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
                      onClick={() => onCancelUploadJob(job.job_id)}
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
                  <span className="truncate max-w-[200px]">{job.video_path.split('/').pop()}</span>
                  {job.error && (
                    <span className="text-destructive truncate max-w-[200px]" title={job.error}>{job.error}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Empty state when no form and no jobs */}
      {!showUploadForm && activeJobs.length === 0 && recentJobs.length === 0 && (
        <div className="rounded-[var(--border-radius)] border-2 border-border bg-muted px-4 py-8 text-center text-muted-foreground">
          No uploads in progress. Click "Upload" on a page in the Roster tab to queue a video.
        </div>
      )}
    </div>
  );
}
