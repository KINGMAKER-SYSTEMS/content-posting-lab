import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { GeneratePage } from './Generate';
import { useWorkflowStore } from '../stores/workflowStore';

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
    text: async () => JSON.stringify(data),
  } as Response;
}

const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  const method = init?.method || 'GET';

  if (url.includes('/api/video/provider-schemas')) {
    return jsonResponse({
      'wan-t2v': {
        aspect_ratio: { type: 'select', options: ['16:9', '9:16'], default: '16:9', label: 'Aspect Ratio' },
        resolution: { type: 'select', options: ['480p', '720p'], default: '480p', label: 'Resolution' },
        num_frames: { type: 'range', min: 81, max: 121, default: 81, step: 4, label: 'Frames' },
      },
    });
  }

  if (url.includes('/api/video/providers')) {
    return jsonResponse([
      {
        id: 'wan-t2v',
        name: 'Text-to-Video (2.2 14B)',
        group: 'Wan',
        key_id: 'replicate',
        pricing: '~$0.06/sec',
        models: ['wan-video/wan-2.2-t2v-fast'],
      },
    ]);
  }

  if (url.includes('/api/video/prompts')) {
    return jsonResponse([]);
  }

  if (url.includes('/api/video/generate') && method === 'POST') {
    return jsonResponse({ job_id: 'job-1', count: 1 });
  }

  if (url.includes('/api/video/jobs/job-1')) {
    return jsonResponse({
      id: 'job-1',
      prompt: 'A test clip',
      provider: 'wan-t2v',
      count: 1,
      videos: [{ index: 0, status: 'done', file: 'provider/prompt/job_0.mp4' }],
    });
  }

  return jsonResponse({});
});

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockClear();
  // Radix Select calls scrollIntoView which jsdom doesn't implement
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  });
  useWorkflowStore.setState({
    activeProjectName: null,
    jobs: {},
    notifications: [],
    recentlyGeneratedVideos: [],
    recentlyScrapedCaptions: [],
    videoRunningCount: 0,
    captionJobActive: false,
    burnReadyCount: 0,
    burnSelection: { videoPaths: [], captionSource: null },
    generateJobs: [],
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('GeneratePage', () => {
  it('shows empty-state when no project is selected', () => {
    render(
      <MemoryRouter>
        <GeneratePage />
      </MemoryRouter>,
    );

    expect(screen.getByText('No Project Selected')).toBeTruthy();
  });

  it('loads provider list when project is active', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });

    render(
      <MemoryRouter>
        <GeneratePage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Generate Video')).toBeTruthy();
      expect(screen.getByText(/Text-to-Video/)).toBeTruthy();
    });
  });

  it('submits generation form and primes burn selection from completed job', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });

    render(
      <MemoryRouter>
        <GeneratePage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Generate Video')).toBeTruthy();
    });

    fireEvent.change(screen.getByPlaceholderText('Describe the video you want to generate...'), {
      target: { value: 'A test clip' },
    });

    // Radix Select can't be changed via fireEvent in jsdom, so click the trigger
    // to open and then select the first provider option.
    const providerTrigger = screen.getByRole('combobox', { name: /provider/i });
    fireEvent.click(providerTrigger);
    await waitFor(() => {
      const option = screen.getByRole('option', { name: /Text-to-Video/i });
      fireEvent.click(option);
    });

    const generateBtn = screen.getByRole('button', { name: 'Generate' });
    expect(generateBtn).toBeTruthy();
    expect((generateBtn as HTMLButtonElement).disabled).toBe(false);

    fireEvent.submit(generateBtn.closest('form')!);

    await waitFor(() => {
      const generateCall = fetchMock.mock.calls.find(
        ([url, init]) => String(url).includes('/api/video/generate') && (init?.method || 'GET') === 'POST',
      );
      expect(generateCall).toBeTruthy();
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Use in Burn/i })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: /Use in Burn/i }));
    const store = useWorkflowStore.getState();
    expect(store.burnSelection.videoPaths.length).toBe(1);
  });
});
