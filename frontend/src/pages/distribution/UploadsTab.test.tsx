import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import type { UploadJob } from '@/types/api';
import { UploadsTab, type UploadsTabProps } from './UploadsTab';

const reason = 'Duplicate stored job ID; reconciliation required before execution or resubmission';

function job(status: UploadJob['status'], id = status): UploadJob {
  return {
    job_id: id, status, video_path: `/tmp/${id}.mp4`, account_name: 'linked',
    description: '', hashtags: [], sound_name: null, sound_aud_vol: 'mix',
    schedule_time: null, schedule_day: null, copyrightcheck: false, headless: true,
    stealth: true, proxy: null, created_at: '2026-10-16T00:00:00',
    started_at: null, completed_at: null, error: status === 'held' ? reason : null,
  };
}

function renderJobs(uploadJobs: UploadJob[]) {
  const props: UploadsTabProps = {
    uploadJobs, onCancelUploadJob: vi.fn(), showUploadForm: false, uploadTarget: null,
    uploadDesc: '', onUploadDescChange: vi.fn(), uploadHashtags: '', onUploadHashtagsChange: vi.fn(),
    uploadSound: '', onUploadSoundChange: vi.fn(), uploadSchedule: '', onUploadScheduleChange: vi.fn(),
    uploadScheduleDay: '', onUploadScheduleDayChange: vi.fn(), uploadStealth: true,
    onUploadStealthChange: vi.fn(), submittingUpload: false, onSubmitUpload: vi.fn(),
    onCloseUploadForm: vi.fn(),
  };
  return render(<UploadsTab {...props} />);
}

afterEach(cleanup);

describe('held upload visibility', () => {
  it('shows held-only jobs and their reason without action buttons or an empty-state invitation', () => {
    renderJobs([job('held')]);
    const held = screen.queryByRole('region', { name: 'Held uploads' });
    expect(held).not.toBeNull();
    expect(within(held!).getByText('held')).toBeTruthy();
    expect(within(held!).getByText('linked')).toBeTruthy();
    expect(within(held!).getByText('held.mp4')).toBeTruthy();
    expect(within(held!).getByText(reason)).toBeTruthy();
    expect(within(held!).queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryByText(/No uploads in progress/)).toBeNull();
    expect(screen.queryByText(/Click "Upload"/)).toBeNull();
  });

  it('keeps held jobs separate from queued and completed jobs', () => {
    renderJobs([job('queued'), job('held'), job('completed')]);
    const held = screen.queryByRole('region', { name: 'Held uploads' });
    expect(held).not.toBeNull();
    expect(within(held!).queryByText('queued.mp4')).toBeNull();
    expect(within(held!).queryByText('completed.mp4')).toBeNull();
    expect(within(held!).queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(screen.getByText('completed.mp4')).toBeTruthy();
  });

  it('shows the hold even when the rejected record has damaged display fields', () => {
    renderJobs([{ ...job('held'), video_path: null, account_name: null, error: null } as unknown as UploadJob]);
    const held = screen.getByRole('region', { name: 'Held uploads' });
    expect(within(held).getByText('Unknown account')).toBeTruthy();
    expect(within(held).getByText('Unknown video')).toBeTruthy();
    expect(within(held).getByText(/requires reconciliation/)).toBeTruthy();
    expect(within(held).queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryByText(/No uploads in progress/)).toBeNull();
  });

  it('retains the invitation when there are no jobs', () => {
    renderJobs([]);
    expect(screen.queryByRole('region', { name: 'Held uploads' })).toBeNull();
    expect(screen.getByText(/No uploads in progress/)).toBeTruthy();
  });
});
