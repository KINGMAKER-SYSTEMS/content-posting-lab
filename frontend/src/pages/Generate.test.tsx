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

  if (url.includes('/api/video/providers')) {
    return jsonResponse([
      {
        id: 'fal-wan',
        name: 'FAL Wan',
        key_id: 'fal',
        pricing: 'low',
        models: ['wan-v2.2-a14b'],
      },
    ]);
  }

  if (url.includes('/api/video/generate') && method === 'POST') {
    return jsonResponse({ job_id: 'job-1', count: 1 });
  }

  if (url.includes('/api/video/jobs/job-1')) {
    return jsonResponse({
      id: 'job-1',
      prompt: 'A test clip',
      provider: 'fal-wan',
      count: 1,
      videos: [{ index: 0, status: 'done', file: 'provider/prompt/job_0.mp4' }],
    });
  }

  return jsonResponse({});
});

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockClear();
  useWorkflowStore.setState({
    activeProject: null,
    jobs: {},
    notifications: [],
    recentlyGeneratedVideos: [],
    recentlyScrapedCaptions: [],
    videoRunningCount: 0,
    captionJobActive: false,
    burnReadyCount: 0,
    burnSelection: { videoPaths: [], captionSource: null },
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
      activeProject: {
        name: 'quick-test',
        path: '/tmp/projects/quick-test',
        video_count: 0,
        caption_count: 0,
        burned_count: 0,
      },
    });

    render(
      <MemoryRouter>
        <GeneratePage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('Generate Video')).toBeTruthy();
      expect(screen.getByText(/FAL Wan/)).toBeTruthy();
    });
  });

  it('submits generation form and primes burn selection from completed job', async () => {
    useWorkflowStore.setState({
      activeProject: {
        name: 'quick-test',
        path: '/tmp/projects/quick-test',
        video_count: 0,
        caption_count: 0,
        burned_count: 0,
      },
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

    fireEvent.click(screen.getByRole('button', { name: 'Generate Videos' }));

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
