import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { HomePage } from './Home';
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

  if (url.includes('/api/projects') && method === 'GET') {
    return jsonResponse({
      projects: [
        {
          name: 'quick-test',
          path: '/tmp/projects/quick-test',
          video_count: 2,
          caption_count: 1,
          burned_count: 1,
        },
      ],
    });
  }

  if (url.includes('/api/projects') && method === 'POST') {
    return jsonResponse(
      {
        project: {
          name: 'new-campaign',
          path: '/tmp/projects/new-campaign',
          video_count: 0,
          caption_count: 0,
          burned_count: 0,
        },
      },
      201,
    );
  }

  if (url.includes('/api/projects') && method === 'DELETE') {
    return jsonResponse({ deleted: true, name: 'quick-test' });
  }

  return jsonResponse({});
});

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockClear();
  window.localStorage.clear();
  useWorkflowStore.setState({
    activeProjectName: null,
    jobs: {},
    notifications: [],
    recentlyGeneratedVideos: [],
    recentlyScrapedCaptions: [],
    videoRunningCount: 0,
    captionJobActive: false,
    recreateJobActive: false,
    burnReadyCount: 0,
    burnSelection: { videoPaths: [], captionSource: null },
    generateJobs: [],
    uploadJobs: [],
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('HomePage', () => {
  it('renders project summary and quick launch cards', async () => {
    render(
      <BrowserRouter>
        <HomePage />
      </BrowserRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('quick-test')).toBeTruthy();
    });
    expect(screen.getByText('Generate Video')).toBeTruthy();
    expect(screen.getByText('Clip Video')).toBeTruthy();
    expect(screen.getByText('Scrape Captions')).toBeTruthy();
    expect(screen.getByText('Burn Captions')).toBeTruthy();
  });

  it('shows empty state when no projects exist', async () => {
    fetchMock.mockImplementationOnce(async () => jsonResponse({ projects: [] }));

    render(
      <BrowserRouter>
        <HomePage />
      </BrowserRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText('No projects yet')).toBeTruthy();
    });
  });

  it('shows pipeline status when jobs are active', async () => {
    useWorkflowStore.setState({ videoRunningCount: 3 });

    render(
      <BrowserRouter>
        <HomePage />
      </BrowserRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText(/3 videos generating/)).toBeTruthy();
    });
  });
});
